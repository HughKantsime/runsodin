"""Finish legacy evidence adoption before database setup admits any worker."""
import json
import logging
import re

from sqlalchemy import text
from core import filament_ledger as ledger
from core.print_attempts import sqlalchemy_runner, filament_marker_for

CURSOR = 'print_filament_evidence_backfill_cursor'
log = logging.getLogger(__name__)


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
    physical = run('SELECT p.scheduled_job_id,j.id,p.started_at,j.created_at FROM print_jobs p '
                   'LEFT JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:pid', {'pid': pid}).fetchone()
    job, existing, started, created = physical
    valid = existing is not None and ledger.moment(started) is not None and ledger.moment(created) is not None and ledger.moment(started) >= ledger.moment(created)
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
    if reason is None and job is not None and existing is None and any(state == 'unresolved' for state, _ in archives):
        reason = 'job_deleted_before_upgrade'
        rule = 'job_deleted'
    if reason:
        ledger.note_candidates(run, pid, [], kind='unattributed', source='backfill', reason=reason)
        return rule
    return 'none'


def backfill(engine, *, batch_size=500):
    """Commit progress per batch; unreadable prints ask, unreadable storage stops boot."""
    counts = {}
    try:
        with engine.connect() as connection:
            markers = _markers(connection)
        while True:
            with engine.begin() as connection:
                cursor = connection.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': CURSOR}).scalar()
                try:
                    cursor = int(str(cursor).strip('"')) if cursor is not None else 0
                except ValueError:
                    cursor = 0
                ids = connection.execute(text('SELECT id FROM print_jobs WHERE id>:cursor ORDER BY id LIMIT :limit'),
                                         {'cursor': cursor, 'limit': batch_size}).scalars().all()
                if not ids:
                    break
                run = sqlalchemy_runner(connection)
                for pid in ids:
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
                connection.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value) '
                                        'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
                                   {'key': CURSOR, 'value': str(ids[-1])})
    except Exception as exc:
        raise RuntimeError('ODIN could not finish checking past filament use. Database setup must finish before ODIN can start.') from exc
    log.info('Filament evidence backfill: %s', counts)
    return counts
