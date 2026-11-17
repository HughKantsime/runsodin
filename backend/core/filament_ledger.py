"""The only writer of filament consumption and durable physical-print evidence.

Print claims survive job, spool and archive deletion. Job-only completions keep
an attempt marker until a physical print is linked and carries usage candidates.
The caller owns the transaction; no ledger function commits.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional

from core.print_attempts import (PREFIX, attempt_seq, attempt_starts, bound_print_attempt, moment,
                                 filament_marker_for)

log = logging.getLogger("odin.filament_ledger")

AUTO_PREFIX = "auto:"
MANUAL_PREFIX = "manual:"
NOTES_MAX = 255  # spool_usage.notes is VARCHAR(255)
MANUAL_NOTES_MAX = NOTES_MAX - len(MANUAL_PREFIX) - 1  # what an operator may write after "manual: "
SOURCES = frozenset({"operator_complete", "moonraker", "archive", "settlement"})
# Notes written by the automatic deducers before the ledger existed.
LEGACY_AUTO_NOTES = ("Auto-deducted on job #", "Moonraker observed extrusion", "Moonraker sliced estimate",
                     "Physical attempt ")

# prior-usage reasons
MARKER = "marker"                  # this print's (or, job-scoped, this attempt's) claim exists
PRINT_USAGE = "print_usage"        # a marker-less usage row exists for this physical print
JOB_USAGE = "job_usage"            # print-scoped: the attempt was deducted job-level (operator)
LEGACY_JOB_USAGE = "legacy_job_usage"  # a marker-less automatic job row in this attempt


@dataclass
class LedgerResult:
    state: str                      # 'deducted' | 'already_deducted' | 'nothing_deducted'
    reason: Optional[str] = None    # for already_deducted: MARKER / PRINT_USAGE / LEGACY_JOB_USAGE
    deductions: list = field(default_factory=list)

    @property
    def deducted(self) -> bool:
        return self.state == "deducted"

    @property
    def unattributed(self) -> bool:
        """A print-scoped deduction refused because its attempt holds usage that cannot
        be attributed to this print (job-level or marker-less rows): reconcile it."""
        return self.state == "already_deducted" and self.reason in (PRINT_USAGE, JOB_USAGE, LEGACY_JOB_USAGE)


def _now():
    # Naive UTC, microsecond precision (portable for SQLite text and PostgreSQL timestamp).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc(value):
    return moment(value)


LEGACY_MANUAL_NOTES = ("Manual weigh adjustment",)


def row_kind(notes) -> str:
    """'ledger' (written here; always covered by a claim marker), 'auto' (a legacy
    automatic deduction), 'manual', or 'unknown' (a legacy row whose writer cannot be told)."""
    text_value = notes or ""
    if text_value.startswith(AUTO_PREFIX):
        return "ledger"
    if text_value.startswith(LEGACY_AUTO_NOTES):
        return "auto"
    if text_value.startswith(MANUAL_PREFIX) or text_value.startswith(LEGACY_MANUAL_NOTES):
        return "manual"
    return "unknown"


def _audit_resets(run, job_id):
    return [moment for moment in (_utc(row[0]) for row in run(
        "SELECT timestamp FROM audit_logs WHERE action = 'job.reset' AND entity_type = 'job' AND entity_id = :jid",
        {"jid": int(job_id)}).fetchall()) if moment is not None]


@dataclass(frozen=True)
class AttemptView:
    """One attempt of a job: its marker seq and its time window [start, end).

    ``floor`` additionally bounds job-level rows of the *current* attempt by the
    job's actual_start (for resets that left no trace).
    """
    seq: int
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    floor: Optional[datetime] = None

    def holds(self, moment) -> bool:
        if moment is None:
            return self.start is None and self.floor is None
        lower = max([b for b in (self.start, self.floor) if b is not None], default=None)
        return (lower is None or moment >= lower) and (self.end is None or moment < self.end)

    def same_attempt(self, other) -> bool:
        # A counted attempt is its seq. Only attempt 0 can hold several windows
        # (resets from before the counter existed), told apart by their start.
        return self.seq == other.seq and (self.seq > 0 or self.start == other.start)


def current_attempt(run, job_id) -> AttemptView:
    seq = attempt_seq(run, job_id)
    starts = attempt_starts(run, job_id)
    bounds = sorted(set(starts.values()) | set(_audit_resets(run, job_id)))
    if seq > 0 and seq in starts:
        start = starts[seq]
    else:
        if seq > 0:
            log.warning("Job %s attempt %s has no reset anchor; using its latest audited reset", job_id, seq)
        start = bounds[-1] if bounds else None
    row = run("SELECT actual_start FROM jobs WHERE id = :jid", {"jid": int(job_id)}).fetchone()
    return AttemptView(seq, start, None, _utc(row[0]) if row else None)


def _window_around(bounds, started, ceiling=None):
    before = [b for b in bounds if b <= started]
    after = [b for b in bounds if b > started and (ceiling is None or b < ceiling)]
    return before[-1] if before else None, after[0] if after else ceiling


def print_attempt(run, job_id, print_job_id) -> AttemptView:
    """The attempt a physical print belongs to -- never simply the job's current one.

    A bound print (core.print_attempts.bind_print_attempt, at create/link time)
    belongs to its bound attempt and spans that attempt's own window:
    [reset that began it, next reset). Attempt 0 ends at the first counted reset;
    within it, resets from before the counter (audit only) are told apart by the
    print's started_at. An unbound (older) print belongs to the attempt whose
    reset window holds its started_at. A missing anchor falls back explicitly to
    the started_at window, with a warning.
    """
    row = run("SELECT started_at FROM print_jobs WHERE id = :p", {"p": int(print_job_id)}).fetchone()
    started = _utc(row[0]) if row else None
    bound = bound_print_attempt(run, print_job_id, job_id)
    starts = attempt_starts(run, job_id)
    audits = sorted(set(_audit_resets(run, job_id)))
    if bound is not None:
        later = sorted(moment_ for seq, moment_ in starts.items() if seq > bound)
        end = later[0] if later else None
        if bound > 0 and bound in starts:
            return AttemptView(bound, starts[bound], end)
        if bound == 0:
            if started is None:
                return AttemptView(0, None, end)
            start, sub_end = _window_around(audits, started, end)
            return AttemptView(0, start, sub_end)
        log.warning("Print %s is bound to attempt %s of job %s, whose anchor is missing; "
                    "using its started_at window", print_job_id, bound, job_id)
    if started is None:
        if bound is None:
            return current_attempt(run, job_id)
        return AttemptView(bound, None, None)
    start, end = _window_around(sorted(set(starts.values()) | set(audits)), started)
    seq = bound if bound is not None else sum(1 for moment_ in starts.values() if moment_ <= started)
    return AttemptView(seq, start, end)


def attempt_anchor(run, job_id):
    """Earliest time a usage row of the current attempt can carry (None: no bound)."""
    view = current_attempt(run, job_id)
    known = [moment for moment in (view.start, view.floor) if moment is not None]
    return max(known) if known else None


def _attempt_rows(run, job_id, view, kinds):
    """print_job_id of every usage row of ``kinds`` that belongs to attempt ``view``.

    A row naming a physical print belongs to that print's own attempt (a late
    archive written after a reset still belongs to the earlier attempt); a
    job-level row belongs to the attempt whose window holds its used_at.
    """
    prints = {}
    rows = run("SELECT print_job_id, used_at, notes FROM spool_usage WHERE job_id = :jid", {"jid": int(job_id)}).fetchall()
    for print_job, used_at, notes in rows:
        if row_kind(notes) not in kinds:
            continue
        if print_job is not None:
            if print_job not in prints:
                prints[print_job] = print_attempt(run, job_id, print_job)
            if prints[print_job].same_attempt(view):
                yield print_job
        elif view.holds(_utc(used_at)):
            yield None


def print_key(run, print_job_id):
    row = run("SELECT id,COALESCE(economic_birth_nonce,'') FROM print_jobs WHERE id=:id",
              {'id': print_job_id}).fetchone()
    return tuple(row) if row else None


def print_evidence(run, print_job_ids):
    """One indexed batch, matched to each physical print's immutable birth."""
    ids = sorted(set(p for p in print_job_ids if p is not None))
    result = {pid: ('none', []) for pid in ids}
    if not ids:
        return result
    params = {f'p{i}': pid for i, pid in enumerate(ids)}
    rows = run("SELECT e.print_job_id,e.kind,e.usage_id,e.spool_id,e.grams,e.used_at,e.reason,e.source,u.notes,u.id IS NOT NULL "
               "FROM print_filament_evidence e JOIN print_jobs p ON p.id=e.print_job_id "
               "AND COALESCE(p.economic_birth_nonce,'')=e.print_birth_nonce "
               "LEFT JOIN spool_usage u ON u.id=e.usage_id AND u.spool_id=e.spool_id AND u.used_at=e.used_at "
               "WHERE NOT (e.kind IN ('manual','unattributed') AND EXISTS ("
               "SELECT 1 FROM print_filament_evidence claimed WHERE claimed.kind='usage' "
               "AND claimed.usage_id=e.usage_id AND claimed.spool_id=e.spool_id AND claimed.used_at=e.used_at "
               "AND (claimed.print_job_id<>e.print_job_id OR claimed.print_birth_nonce<>e.print_birth_nonce))) "
               "AND e.print_job_id IN ("
               + ','.join(':'+k for k in params) + ') ORDER BY e.id', params).fetchall()
    for pid, kind, uid, spool, grams, at, reason, source, notes, live in rows:
        state, evidence = result[pid]
        evidence.append(dict(kind=kind, usage_id=uid, spool_id=spool, grams=grams,
                             used_at=at, reason=reason, source=source, notes=notes or '', live=bool(live)))
        result[pid] = ('final' if kind in ('usage', 'closed') or state == 'final' else 'ask', evidence)
    return result


def job_link_is_live(run, print_job_id, job_id):
    """Distinguish explicit/legacy live links from a deleted job's reused id."""
    row = run('SELECT p.started_at,p.ended_at,j.created_at,j.actual_start FROM print_jobs p '
              'JOIN jobs j ON j.id=p.scheduled_job_id WHERE p.id=:pid AND j.id=:job',
              {'pid': print_job_id, 'job': job_id}).fetchone()
    if row is None:
        return False
    if bound_print_attempt(run, print_job_id, job_id) is not None:
        return True
    started, ended, created, actual = map(moment, row)
    return bool((started is not None and created is not None and started >= created)
                or (actual is not None and (ended or started) is not None and actual <= (ended or started)))


def _current_prints(run, job_id):
    current = current_attempt(run, job_id)
    rows = run('SELECT p.id,p.started_at FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id '
               'WHERE j.id=:job ORDER BY p.id', {'job': job_id}).fetchall()
    return [pid for pid, started in rows if job_link_is_live(run, pid, job_id) and print_attempt(run, job_id, pid).same_attempt(current)
            and (bound_print_attempt(run, pid, job_id) is not None or current.holds(moment(started)))]


def prior_attempt_usage(run, job_id, print_job_id=None) -> Optional[str]:
    if print_job_id is not None:
        state, rows = print_evidence(run, [print_job_id])[print_job_id]
        if state == 'final':
            return MARKER
        if any(r['kind'] == 'unattributed' for r in rows):
            return JOB_USAGE
        return None  # Manual use never prevents automatic deduction.
    if job_id is None:
        return None
    view = current_attempt(run, job_id)
    if run('SELECT 1 FROM system_config WHERE key=:key', {'key': filament_marker_for(job_id, view.seq)}).fetchone():
        return MARKER
    if any(state == 'final' or any(r['kind'] == 'unattributed' for r in rows)
           for state, rows in print_evidence(run, _current_prints(run, job_id)).values()):
        return PRINT_USAGE
    if any(True for _ in _attempt_rows(run, job_id, view, ('auto',))):
        return LEGACY_JOB_USAGE
    return None


def _append_evidence(run, key, *, kind, source, claim=None, row=None, reason=None):
    row = row or {}
    return run('INSERT INTO print_filament_evidence '
               '(print_job_id,print_birth_nonce,kind,claim,source,usage_id,spool_id,grams,used_at,reason) '
               'VALUES(:pid,:nonce,:kind,:claim,:source,:uid,:spool,:grams,:at,:reason) ON CONFLICT DO NOTHING',
               dict(pid=key[0], nonce=key[1], kind=kind, claim=claim, source=source,
                    uid=row.get('usage_id'), spool=row.get('spool_id'), grams=row.get('grams'),
                    at=row.get('used_at'), reason=reason)).rowcount == 1


def _claimed_elsewhere(run, key, row):
    return run("SELECT 1 FROM print_filament_evidence WHERE kind='usage' AND usage_id=:uid "
               'AND spool_id=:spool AND used_at=:at AND (print_job_id<>:pid OR print_birth_nonce<>:nonce) LIMIT 1',
               dict(uid=row['usage_id'], spool=row['spool_id'], at=row['used_at'], pid=key[0], nonce=key[1])).fetchone()


def note_candidates(run, print_job_id, usage_ids, *, kind, source, reason=None):
    if kind not in ('manual', 'unattributed'):
        raise ValueError('A candidate must be manual or unattributed')
    key = print_key(run, print_job_id)
    if key is None:
        return
    rows = _usage_rows(run, usage_ids)
    _lock_spools(run, [r['spool_id'] for r in rows])
    existing = print_evidence(run, [print_job_id])[print_job_id][1]
    identities = {(r['usage_id'], r['spool_id'], str(r['used_at']), r['kind']) for r in existing}
    for row in rows or ([{}] if not usage_ids else []):
        if row and (_claimed_elsewhere(run, key, row) or
                    (row['usage_id'], row['spool_id'], str(row['used_at']), kind) in identities):
            continue
        if not row and any(r['kind'] == kind and r['reason'] == reason and r['usage_id'] is None for r in existing):
            continue
        _append_evidence(run, key, kind=kind, source=source, row=row, reason=reason)


def carry_job_candidates(run, print_job_id, job_id, *, source='link'):
    if not job_link_is_live(run, print_job_id, job_id):
        return
    view = print_attempt(run, job_id, print_job_id)
    rows = run('SELECT id,used_at,notes FROM spool_usage WHERE job_id=:job AND print_job_id IS NULL',
               {'job': job_id}).fetchall()
    for kind in ('manual', 'unattributed'):
        ids = [uid for uid, at, notes in rows if view.holds(moment(at))
               and (row_kind(notes) == 'manual') == (kind == 'manual')]
        if ids:
            note_candidates(run, print_job_id, ids, kind=kind, source=source)


def _claim(run, key, value) -> bool:
    result = run("INSERT INTO system_config (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING",
                 {"k": key, "v": json.dumps(value)})
    return getattr(result, "rowcount", 0) == 1


def _clean(grams_by_spool: Mapping) -> list:
    items = []
    for spool_id, grams in (grams_by_spool or {}).items():
        try:
            value = float(grams)
        except (TypeError, ValueError):
            continue
        if spool_id is not None and math.isfinite(value) and value > 0:
            items.append((int(spool_id), value))
    return sorted(items)


def _lock_spools(run, spool_ids) -> set:
    """Row-lock spools in id order (portable: SQLite writer lock / PostgreSQL row lock).

    Lock order on every ledger path: spool rows in ascending id first, then the
    claim keys. Archive accounting locks the same spools in the same order before
    calling the ledger, so archive and operator completion never wait on each
    other in opposite orders.
    """
    return {spool_id for spool_id in sorted(set(spool_ids))
            if getattr(run("UPDATE spools SET id = id WHERE id = :id", {"id": spool_id}), "rowcount", 0) == 1}


def _deduct(run, spool_id, grams, *, job_id, print_job_id, notes, allow_unknown_balance=False):
    """Read and decrement one locked spool. Confirmed usage may retain an unknown balance."""
    row = run("SELECT remaining_weight_g, spoolman_spool_id FROM spools WHERE id = :id", {"id": spool_id}).fetchone()
    before = row[0] if row else None
    try:
        invalid_balance = before is not None and not math.isfinite(float(before))
    except (ValueError, TypeError):
        invalid_balance = True
    if row is None or (before is None and not allow_unknown_balance) or invalid_balance:
        return None
    before = float(before) if before is not None else None
    run("UPDATE spools SET remaining_weight_g = CASE WHEN remaining_weight_g > :g THEN remaining_weight_g - :g "
        "ELSE 0 END WHERE id = :id AND remaining_weight_g IS NOT NULL", {"g": grams, "id": spool_id})
    at = _now()
    uid = run("INSERT INTO spool_usage (spool_id, job_id, print_job_id, weight_used_g, used_at, notes) "
        "VALUES (:spool, :job, :pj, :g, :at, :notes) RETURNING id",
        {"spool": spool_id, "job": job_id, "pj": print_job_id, "g": grams, "at": at, "notes": notes}).fetchone()[0]
    return {"_usage_id": uid, "_used_at": at, "spool_id": spool_id, "grams": grams, "remaining_g": max(0.0, before - grams) if before is not None else None,
            "shortfall_g": max(0.0, grams - before) if before is not None else 0, "spoolman_spool_id": row[1]}


def job_accounting_skip_reason(run, job_id) -> Optional[str]:
    """Resolve current-attempt physical ownership without changing spool attribution.

    A reset excludes earlier physical prints. Completion without a physical print
    retains the assigned-printer workflow. A reassignment is not evidence that the
    new printer's loaded spools supplied an existing physical print.
    """
    from core.config import settings
    from core.interfaces.filament_ownership import uses_external_filament_accounting
    if not settings.external_filament_accounting_printers:
        return None
    job = run("SELECT printer_id FROM jobs WHERE id = :id", {"id": int(job_id)}).fetchone()
    if not job:
        return "missing_job"
    current = current_attempt(run, job_id)
    prints = []
    for pid, printer, started in run(
            "SELECT p.id,p.printer_id,p.started_at FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id "
            "WHERE j.id=:id",
            {"id": int(job_id)}).fetchall():
        # An unreadable legacy start cannot rule out an external physical owner.
        # Keep that refusal conservative without treating the link as usage evidence.
        if not job_link_is_live(run, pid, job_id) and moment(started) is not None:
            continue
        if not print_attempt(run, job_id, pid).same_attempt(current):
            continue
        # Explicit bindings survive clock skew. Legacy unbound prints must also
        # fall within actual_start's floor when a pre-counter reset left no audit.
        started_at = moment(started)
        if bound_print_attempt(run, pid, job_id) is None and started_at is not None and not current.holds(started_at):
            continue
        prints.append((pid, printer))
    if len(prints) > 1:
        if uses_external_filament_accounting(job[0]) or any(uses_external_filament_accounting(printer) for _, printer in prints):
            return "ambiguous_physical_print"
        return None
    printer = prints[0][1] if prints else job[0]
    if uses_external_filament_accounting(printer):
        return "external_accounting"
    if prints and printer != job[0] and uses_external_filament_accounting(job[0]):
        return "printer_reassigned"
    return None


@dataclass(frozen=True)
class _JobAccountingDecision:
    job_id: int
    run: object
    skip_reason: Optional[str]


def job_accounting_decision(run, job_id) -> _JobAccountingDecision:
    """Evaluate once for a job-only deduction in the caller's current transaction.

    Reuse requires the exact same runner and job. Do not retain across commits.
    """
    return _JobAccountingDecision(int(job_id), run, job_accounting_skip_reason(run, job_id))


def record_job_filament(run, job_id, *, source: str, grams_by_spool: Mapping, print_job_id=None,
                        notes: str = "", allow_unknown_balance=False, _job_accounting=None) -> LedgerResult:
    """Deduct once per physical print (``print_job_id``) or per job attempt, claiming atomically."""
    if source not in SOURCES:
        raise ValueError(f"Unknown filament ledger source {source!r}")
    job_id = int(job_id) if job_id is not None else None
    print_job_id = int(print_job_id) if print_job_id is not None else None
    if job_id is None and print_job_id is None:
        raise ValueError("A filament deduction names a job or a physical print")
    from core.interfaces.filament_ownership import uses_external_filament_accounting
    from core.config import settings
    if _job_accounting is not None and (
            not isinstance(_job_accounting, _JobAccountingDecision)
            or _job_accounting.job_id != job_id or _job_accounting.run is not run
            or print_job_id is not None):
        raise ValueError("Accounting decision must name this job and transaction runner")
    # Physical history remains authoritative if the scheduled job is reassigned.
    if print_job_id is not None:
        if settings.external_filament_accounting_printers:
            printer = run("SELECT printer_id FROM print_jobs WHERE id = :id", {"id": print_job_id}).fetchone()
            if not printer:
                return LedgerResult("nothing_deducted", "missing_physical_print")
            if uses_external_filament_accounting(printer[0]):
                return LedgerResult("nothing_deducted", "external_accounting")
    else:
        decision = _job_accounting or job_accounting_decision(run, job_id)
        if decision.skip_reason:
            return LedgerResult("nothing_deducted", decision.skip_reason)
    if print_job_id is None:
        prior = prior_attempt_usage(run, job_id)
        if prior:
            return LedgerResult('already_deducted', prior)
        prints = _current_prints(run, job_id)
        if prints:
            print_job_id = max(prints)
    if print_job_id is not None:
        return record_print_usage(run, print_job_id, source=source, grams_by_spool=grams_by_spool,
                                  allow_unknown_balance=allow_unknown_balance, notes=notes)
    items = _clean(grams_by_spool)
    locked = _lock_spools(run, [spool for spool, _ in items])
    key = filament_marker_for(job_id, current_attempt(run, job_id).seq)
    if not items:
        return LedgerResult('nothing_deducted')
    if not _claim(run, key, {'source': source, 'print_job_id': None}):
        return LedgerResult('already_deducted', MARKER)
    deductions = []
    for spool, grams in items:
        if spool not in locked:
            continue
        row = _deduct(run, spool, grams, job_id=job_id, print_job_id=None,
                      notes=f'auto:{source}: {notes}'[:NOTES_MAX], allow_unknown_balance=allow_unknown_balance)
        if row:
            row.pop('_usage_id'); row.pop('_used_at')
            deductions.append(row)
    if not deductions:
        run('DELETE FROM system_config WHERE key=:key', {'key': key})
    return LedgerResult('deducted' if deductions else 'nothing_deducted', None, deductions)


def _usage_rows(run, ids):
    ids = sorted(set(ids))
    if not ids:
        return []
    if len(ids) > 500:
        return [row for offset in range(0, len(ids), 500) for row in _usage_rows(run, ids[offset:offset + 500])]
    params = {f'u{i}': uid for i, uid in enumerate(ids)}
    return [dict(zip(('usage_id', 'spool_id', 'grams', 'used_at', 'notes'), r)) for r in run(
        'SELECT id,spool_id,weight_used_g,used_at,notes FROM spool_usage WHERE id IN ('
        + ','.join(':'+k for k in params) + ') ORDER BY id', params).fetchall()]


def manual_rows_for_print(run, job_id, print_job_id):
    key = print_key(run, print_job_id)
    rows = print_evidence(run, [print_job_id]).get(print_job_id, ('none', []))[1]
    counted = {(r['usage_id'], r['spool_id'], str(r['used_at'])) for r in rows if r['kind'] == 'usage'}
    candidates = _distinct_usage([r for r in rows if r['kind'] == 'manual'
            and (r['usage_id'], r['spool_id'], str(r['used_at'])) not in counted
            and not _claimed_elsewhere(run, key, r)])
    return [r for r in candidates if r['live']]


def existing_rows_for_print(run, job_id, print_job_id):
    key = print_key(run, print_job_id)
    state, rows = print_evidence(run, [print_job_id]).get(print_job_id, ('none', []))
    kinds = ('usage',) if state == 'final' else ('unattributed',)
    result = {}
    for row in rows:
        if row['kind'] in kinds and row['usage_id'] is not None and not _claimed_elsewhere(run, key, row):
            result[(row['usage_id'], row['spool_id'], str(row['used_at']))] = row
    return [r for r in _distinct_usage(list(result.values())) if r['live']]


def _distinct_usage(rows):
    selected = {}
    for row in rows:
        previous = selected.get(row["usage_id"])
        if previous and (previous["spool_id"], str(previous["used_at"])) != (row["spool_id"], str(row["used_at"])):
            raise InvalidSettlementEvidence("This print has different filament records with the same number. An administrator must review them before its filament can be settled.")
        selected[row["usage_id"]] = row
    return list(selected.values())


class InvalidSettlementEvidence(ValueError):
    pass


class InvalidSettlementBalance(ValueError):
    pass


def close_print(run, print_job_id, *, source, reason, counted_usage_ids=(), accepted_usage_ids=()):
    key = print_key(run, print_job_id)
    if key is None:
        raise ValueError('A settlement requires a physical print')
    rows = print_evidence(run, [print_job_id])[print_job_id][1]
    selected = set(counted_usage_ids) | set(accepted_usage_ids)
    candidates = {r['usage_id']: r for r in _distinct_usage([
        r for r in rows if r['usage_id'] in selected and not _claimed_elsewhere(run, key, r)])}
    if selected != set(candidates):
        raise InvalidSettlementEvidence('A selected filament record is no longer available. Reload the print and review its filament use.')
    _lock_spools(run, [r['spool_id'] for r in candidates.values()])
    if any(_claimed_elsewhere(run, key, r) for r in candidates.values()):
        raise InvalidSettlementEvidence('This filament use already belongs to another print. Reload the print and review its filament use.')
    if not _append_evidence(run, key, kind='closed', claim=1, source=source, reason=reason):
        return LedgerResult('already_deducted', MARKER)
    for uid, row in candidates.items():
        _append_evidence(run, key, kind='usage', source='settlement_counted' if uid in counted_usage_ids else 'settlement_accepted', row=row)
    return LedgerResult('nothing_deducted')


def record_print_usage(run, print_job_id, *, source, grams_by_spool, allow_unknown_balance=False, notes=''):
    """Claim and deduct atomically, including when rival writers name different spools."""
    if source not in SOURCES:
        raise ValueError('Unknown filament ledger source')
    from core.interfaces.filament_ownership import uses_external_filament_accounting
    physical = run('SELECT scheduled_job_id,printer_id FROM print_jobs WHERE id=:id', {'id': print_job_id}).fetchone()
    if physical is None:
        return LedgerResult('nothing_deducted', 'missing_physical_print')
    if uses_external_filament_accounting(physical[1]):
        return LedgerResult('nothing_deducted', 'external_accounting')
    prior = prior_attempt_usage(run, None, print_job_id)
    if prior:
        return LedgerResult('already_deducted', prior)
    key = print_key(run, print_job_id)
    items = _clean(grams_by_spool)
    locked = _lock_spools(run, [spool for spool, _ in items])
    prior = prior_attempt_usage(run, None, print_job_id)
    if prior:
        return LedgerResult('already_deducted', prior)
    # A deleted job link is retained for history, but cannot be used as a usage FK.
    job_id = physical[0] if job_link_is_live(run, print_job_id, physical[0]) else None
    run('SAVEPOINT filament_deduction', {})
    try:
        # Claim before deducting, as a settlement does: a rival writer for this print
        # waits on the claim (PostgreSQL) and is refused, instead of deducting from a
        # different spool and being rolled back. Nothing deducted keeps no claim.
        if not _append_evidence(run, key, kind='closed', claim=1, source=source, reason='deducted'):
            return LedgerResult('already_deducted', MARKER)
        deductions = []
        for spool, grams in items:
            if spool not in locked:
                continue
            entry = _deduct(run, spool, grams, job_id=job_id, print_job_id=print_job_id,
                            notes=f'auto:{source}: {notes}'[:NOTES_MAX], allow_unknown_balance=allow_unknown_balance)
            if entry is None:
                continue
            row = dict(usage_id=entry.pop('_usage_id'), used_at=entry.pop('_used_at'), spool_id=spool, grams=grams)
            _append_evidence(run, key, kind='usage', source=source, row=row)
            deductions.append(entry)
        if not deductions:
            run('ROLLBACK TO SAVEPOINT filament_deduction', {})
            return LedgerResult('nothing_deducted')
        return LedgerResult('deducted', None, deductions)
    except Exception:
        run('ROLLBACK TO SAVEPOINT filament_deduction', {})
        raise
    finally:
        run('RELEASE SAVEPOINT filament_deduction', {})


def record_settlement(run, job_id, print_job_id, *, grams_by_spool, counted_usage_ids,
                      accept_existing=False, archive_id=None, reason=None):
    items = _clean(grams_by_spool)
    if len(items) != len(grams_by_spool):
        raise ValueError('Settlement deductions must be positive finite quantities')
    prior = prior_attempt_usage(run, job_id, print_job_id)
    if prior and not accept_existing:
        return LedgerResult('already_deducted', prior)
    if accept_existing:
        if not prior or items:
            raise ValueError('Accept existing requires a prior deduction and no new deduction')
        state, rows = print_evidence(run, [print_job_id])[print_job_id]
        if state == 'final':
            key = print_key(run, print_job_id)
            selected = {r['usage_id']: r for r in _distinct_usage([
                r for r in rows if r['kind'] == 'manual' and r['usage_id'] in counted_usage_ids
                and not _claimed_elsewhere(run, key, r)])}
            if set(selected) != set(counted_usage_ids):
                raise InvalidSettlementEvidence('A selected filament record is no longer available. Reload the print and review its filament use.')
            _lock_spools(run, [r['spool_id'] for r in selected.values()])
            for row in selected.values():
                if _claimed_elsewhere(run, key, row):
                    raise InvalidSettlementEvidence('This filament use already belongs to another print. Reload the print and review its filament use.')
                if not any(r['kind'] == 'usage' and r['usage_id'] == row['usage_id'] and r['spool_id'] == row['spool_id'] and str(r['used_at']) == str(row['used_at']) for r in rows):
                    _append_evidence(run, key, kind='usage', source='settlement_counted', row=row)
            return LedgerResult('nothing_deducted')
    if _lock_spools(run, [spool for spool, _ in items]) != {spool for spool, _ in items}:
        raise InvalidSettlementBalance('A selected spool is no longer available. Reload the print and choose a spool.')
    selected = list(counted_usage_ids)
    accepted = [r['usage_id'] for r in existing_rows_for_print(run, job_id, print_job_id)] if accept_existing else []
    # Close first, then add usage under that claim. Caller rolls the transaction
    # back on any refused balance, outbox or audit write.
    outcome = close_print(run, print_job_id, source='settlement', reason=reason or 'operator_settled',
                          counted_usage_ids=selected, accepted_usage_ids=accepted)
    if outcome.state == 'already_deducted':
        return outcome
    key = print_key(run, print_job_id)
    job_id = job_id if job_link_is_live(run, print_job_id, job_id) else None
    deductions = []
    for spool, grams in items:
        entry = _deduct(run, spool, grams, job_id=job_id, print_job_id=print_job_id,
                        notes=f'auto:settlement: archive {archive_id}', allow_unknown_balance=True)
        if entry is None:
            raise InvalidSettlementBalance(f'Spool {spool} has an invalid balance. Correct or weigh the spool, then try again.')
        row = dict(usage_id=entry.pop('_usage_id'), used_at=entry.pop('_used_at'), spool_id=spool, grams=grams)
        _append_evidence(run, key, kind='usage', source='settlement', row=row)
        deductions.append(entry)
    return LedgerResult('deducted' if deductions else 'nothing_deducted', None, deductions)


def record_manual_usage(run, spool_id, grams, *, job_id=None, notes=None):
    items = _clean({spool_id: grams})
    if not items:
        return None
    if notes is not None and len(notes) > MANUAL_NOTES_MAX:
        raise ValueError(f'Manual usage notes are limited to {MANUAL_NOTES_MAX} characters')
    if not _lock_spools(run, [items[0][0]]):
        return None
    entry = _deduct(run, items[0][0], items[0][1], job_id=job_id, print_job_id=None,
                    notes=f'manual: {notes}' if notes else 'manual:')
    if entry:
        uid = entry.pop('_usage_id'); entry.pop('_used_at')
        if job_id is not None:
            for pid in _current_prints(run, job_id):
                note_candidates(run, pid, [uid], kind='manual', source='manual_use')
    return entry


def record_weighed_spool(run, spool_id, net_weight_g) -> Optional[dict]:
    """A scale reading: set the net weight; a decrease is recorded as a manual adjustment."""
    value = float(net_weight_g)
    if not math.isfinite(value):
        raise ValueError("Weight must be finite")
    value = max(0.0, value)
    if getattr(run("UPDATE spools SET id = id WHERE id = :id", {"id": int(spool_id)}), "rowcount", 0) != 1:
        return None
    row = run("SELECT remaining_weight_g FROM spools WHERE id = :id", {"id": int(spool_id)}).fetchone()
    before = float(row[0]) if row and row[0] is not None else None
    run("UPDATE spools SET remaining_weight_g = :w WHERE id = :id", {"w": value, "id": int(spool_id)})
    if before is not None and value < before:
        run("INSERT INTO spool_usage (spool_id, job_id, print_job_id, weight_used_g, used_at, notes) "
            "VALUES (:spool, NULL, NULL, :g, :at, :notes)",
            {"spool": int(spool_id), "g": before - value, "at": _now(), "notes": f"{MANUAL_PREFIX} weigh adjustment"})
    record_balance_set(run, spool_id)
    return {"spool_id": int(spool_id), "old_weight_g": before, "remaining_g": value}


def record_balance_set(run, spool_id):
    """Retain an absolute balance correction, including increases and NULL -> known."""
    run("INSERT INTO system_config(key,value) VALUES(:key,:at) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        {"key": f"filament_balance_weighed:{int(spool_id)}", "at": json.dumps(_now().isoformat())})


def create_rfid_spool(run, *, filament_id, qr_code, tag, tray_uuid, color_hex, color_name, org_id):
    """Open an RFID balance as unknown until a measuring printer reports it."""
    return run("""INSERT INTO spools(filament_id,qr_code,rfid_tag,tray_uuid,color_hex,color_name,
        initial_weight_g,remaining_weight_g,spool_weight_g,low_stock_threshold_g,org_id,status)
        VALUES(:filament,:qr,:tag,:uuid,:hex,:color,1000,NULL,250,50,:org,'active') RETURNING id""",
        dict(filament=filament_id, qr=qr_code, tag=tag, uuid=tray_uuid,
             hex=color_hex, color=color_name, org=org_id)).fetchone()[0]


def record_ams_measurement(run, spool_id, weight, printer_id):
    """Replace remaining and its measured basis, never invent per-job usage.

    The caller has validated the measuring printer and locked the spool graph.
    This shares its transaction and does not acquire a different lock order.
    """
    value = float(weight)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Measured remaining must be finite and nonnegative")
    run("""UPDATE spools SET remaining_weight_g=:weight,remaining_measured_g=:weight,
        remaining_measured_at=:at,remaining_measured_printer_id=:printer,updated_at=CURRENT_TIMESTAMP WHERE id=:id""",
        dict(weight=value, at=_now(), printer=printer_id, id=spool_id))


def transfer_rfid_balance(run, source_id, target_id):
    """Carry a measured opened roll's balance onto its locked sealed record.

    The merge caller verifies that the source has no usage/history references.
    An unmeasured source must leave the target's existing balance untouched.
    """
    row = run("""SELECT remaining_weight_g,remaining_measured_g,remaining_measured_at,
        remaining_measured_printer_id FROM spools WHERE id=:id""", {"id": source_id}).fetchone()
    if row is not None and row[2] is not None:
        run("""UPDATE spools SET remaining_weight_g=:weight,remaining_measured_g=:measured,
            remaining_measured_at=:at,remaining_measured_printer_id=:printer,updated_at=CURRENT_TIMESTAMP WHERE id=:id""",
            dict(weight=row[0], measured=row[1], at=row[2], printer=row[3], id=target_id))


def flag_reconciliation(run, job_id, print_job_id, *, reason, source):
    note_candidates(run, print_job_id, [], kind='unattributed', source=source, reason=reason)


def retain_print_evidence(run, print_job_id, *, usage_ids=(), reason=None):
    """Startup-only adoption of direct legacy evidence, without changing stock."""
    key = print_key(run, print_job_id)
    if key is None:
        raise ValueError('Physical print is missing')
    rows = _usage_rows(run, usage_ids)
    _lock_spools(run, [r['spool_id'] for r in rows])
    if not rows:
        _append_evidence(run, key, kind='closed', source='backfill', claim=1, reason=reason)
        return
    for index, row in enumerate(rows):
        if _claimed_elsewhere(run, key, row):
            raise InvalidSettlementEvidence('Filament use belongs to another print')
        if not _append_evidence(run, key, kind='usage', source='backfill', row=row, claim=1 if index == 0 else None):
            return
