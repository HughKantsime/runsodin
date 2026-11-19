"""Finish legacy evidence adoption before database setup admits any worker."""
import json
import logging
import re

from sqlalchemy import bindparam, text
from core import filament_ledger as ledger
from core.schema.migrator import MigrationError
from core.print_attempts import sqlalchemy_runner, filament_marker_for

CURSOR = 'print_filament_evidence_backfill_cursor'
MISSING_EVIDENCE_SQL = (
    "NOT EXISTS (SELECT 1 FROM print_filament_evidence e WHERE e.print_job_id=p.id "
    "AND e.print_birth_nonce=COALESCE(p.economic_birth_nonce,''))"
)
log = logging.getLogger(__name__)
# Configured application handlers retain the cause; the standalone CLI prints
# its plain refusal instead of logging's fallback traceback on stderr.
log.addHandler(logging.NullHandler())


class FilamentEvidenceBackfillError(MigrationError):
    """Startup cannot admit workers until evidence adoption finishes."""


def _announce(done, total):
    log.info('Checking past filament use: %s of %s prints', done, total)


# Called after each batch when the work spans more than one. The start-up script
# replaces it with a line on stderr: no logging is configured there, and a long
# first start after an upgrade must not look like a hang.
announce = _announce


def _decoded(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return None


def _markers(connection):
    direct, indirect, job_only = set(), {}, set()
    for key, value in connection.execute(text("SELECT key,value FROM system_config WHERE key LIKE 'quota_attempt:filament:%'")):
        marker = _decoded(value)
        match = re.fullmatch(r'quota_attempt:filament:(?:job:\d+:)?print:(\d+)', key)
        reconcile = re.fullmatch(r'quota_attempt:filament:(?:job:\d+:)?reconcile:print:(\d+)', key)
        if match:
            direct.add(int(match[1]))
        elif reconcile:
            indirect[int(reconcile[1])] = marker.get('reason', 'marker') if isinstance(marker, dict) else 'marker'
        elif re.fullmatch(r'quota_attempt:filament:job:\d+:\d+', key):
            pid = marker.get('print_job_id') if isinstance(marker, dict) else None
            if type(pid) is int and pid > 0:
                direct.add(pid)
            else:
                job_only.add(key)
    return direct, indirect, job_only


def _print(run, pid, markers):
    if ledger.print_evidence(run, [pid])[pid][1]:
        return 'existing'
    direct, indirect, job_only = markers
    usage = [r[0] for r in run('SELECT id FROM spool_usage WHERE print_job_id=:pid ORDER BY id', {'pid': pid}).fetchall()]
    if usage:
        ledger.retain_print_evidence(run, pid, usage_ids=usage)
        return 'usage'
    archives = run('SELECT accounting_state,accounting_reason FROM print_archives WHERE print_job_id=:pid', {'pid': pid}).fetchall()
    if any(state in ('accounted', 'reconciliation_required') for state, _ in archives) or pid in direct:
        ledger.retain_print_evidence(run, pid, reason='marker' if pid in direct else 'archive_accounted')
        return 'closed'
    job = run('SELECT scheduled_job_id FROM print_jobs WHERE id=:pid', {'pid': pid}).fetchone()[0]
    valid = ledger.job_link_is_live(run, pid, job)
    if valid:
        ledger.carry_job_candidates(run, pid, job, source='backfill')
        kinds = {row['kind'] for row in ledger.print_evidence(run, [pid])[pid][1]}
        if kinds:
            return tuple(kind for kind in ('unattributed', 'manual') if kind in kinds)
    reasons = {'attempt_usage_without_archive', 'job_usage_unattributed', 'legacy_usage_unattributed'}
    reason = next((reason for state, reason in archives if state == 'unresolved' and reason in reasons), indirect.get(pid))
    if reason is None and valid and filament_marker_for(job, ledger.print_attempt(run, job, pid).seq) in job_only:
        reason = 'job_usage_unattributed'
    rule = 'unknown'
    if reason is None and job is not None and not valid and any(state == 'unresolved' for state, _ in archives):
        reason = 'job_deleted_before_upgrade'
        rule = 'job_deleted'
    if reason:
        ledger.note_candidates(run, pid, [], kind='unattributed', source='backfill', reason=reason)
        return rule
    return 'none'


def _cursor(connection):
    value = connection.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': CURSOR}).scalar()
    try:
        return int(str(value).strip('"')) if value is not None else 0
    except ValueError:
        return 0


def _heal_ids(connection, direct):
    # Read only qualifying ids; the evidence anti-joins use the print id index.
    # Archives have no print-id index, so scan that source once, not per print.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — only MISSING_EVIDENCE_SQL is appended; print ids are bound.
    ids = set(connection.execute(text(
        'SELECT DISTINCT p.id FROM spool_usage u JOIN print_jobs p ON p.id=u.print_job_id '
        'WHERE u.print_job_id IS NOT NULL AND ' + MISSING_EVIDENCE_SQL)).scalars())
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — only MISSING_EVIDENCE_SQL is appended; print ids are bound.
    ids.update(connection.execute(text(
        'SELECT DISTINCT p.id FROM print_archives a JOIN print_jobs p ON p.id=a.print_job_id '
        "WHERE a.accounting_state IN ('accounted','reconciliation_required') AND " + MISSING_EVIDENCE_SQL)).scalars())
    # Filter stale or already adopted markers in batches, without per-print reads.
    direct = sorted(direct - ids)
    for start in range(0, len(direct), 500):
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — only MISSING_EVIDENCE_SQL is appended; print ids are bound.
        ids.update(connection.execute(text(
            'SELECT p.id FROM print_jobs p WHERE p.id IN :ids AND ' + MISSING_EVIDENCE_SQL
        ).bindparams(bindparam('ids', expanding=True)), {'ids': direct[start:start + 500]}).scalars())
    return sorted(ids)


def _adopt(connection, pid, markers, counts):
    run = sqlalchemy_runner(connection)
    try:
        with connection.begin_nested():
            rule = _print(run, pid, markers)
    except Exception:
        # The savepoint restores partial claims; failure to write this
        # conservative record propagates to the startup refusal below.
        ledger.note_candidates(run, pid, [], kind='unattributed', source='backfill', reason='backfill_error')
        rule = 'error'
    for matched in (rule,) if isinstance(rule, str) else rule:
        counts[matched] = counts.get(matched, 0) + 1


def backfill(engine, *, batch_size=500):
    """Commit progress per batch; unreadable prints ask, unreadable storage stops boot."""
    counts = {}
    try:
        with engine.connect() as connection:
            markers = _markers(connection)
            total = connection.execute(text(
                'SELECT COUNT(*) FROM print_jobs WHERE id>:cursor'), {'cursor': _cursor(connection)}).scalar_one()
            heal_ids = _heal_ids(connection, markers[0])
        for start in range(0, len(heal_ids), batch_size):
            with engine.begin() as connection:
                for pid in heal_ids[start:start + batch_size]:
                    _adopt(connection, pid, markers, counts)
            if len(heal_ids) > batch_size:
                announce(min(start + batch_size, len(heal_ids)), len(heal_ids))
        done = 0
        while True:
            with engine.begin() as connection:
                cursor = _cursor(connection)
                ids = connection.execute(text('SELECT id FROM print_jobs WHERE id>:cursor ORDER BY id LIMIT :limit'),
                                         {'cursor': cursor, 'limit': batch_size}).scalars().all()
                if not ids:
                    break
                for pid in ids:
                    _adopt(connection, pid, markers, counts)
                connection.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) '
                                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                                   {'key': CURSOR, 'value': str(ids[-1])})
            done += len(ids)
            if total > batch_size:
                announce(done, max(total, done))
    except Exception as exc:
        log.exception('Filament evidence backfill could not finish')
        raise FilamentEvidenceBackfillError(
            'ODIN could not finish checking past filament use, so it did not start. '
            'No filament balance was changed. Start ODIN again; if this repeats, restore your latest backup.'
        ) from exc
    log.info('Filament evidence backfill: %s', counts)
    return counts
