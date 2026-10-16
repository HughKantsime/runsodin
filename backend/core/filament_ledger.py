"""The one filament consumption ledger.

Every write that consumes filament -- decrementing ``spools.remaining_weight_g``
or inserting a ``spool_usage`` row -- goes through this module (a census test,
``test_cert_D_filament_ledger.py``, fails on any other writer):

    record_job_filament   automatic deduction for a job's physical print attempt:
                          operator /complete, the Moonraker monitor, and archive
                          accounting (Bambu/MQTT and every archived attempt)
    record_manual_usage   an operator's manual "use N grams" (optionally naming a job)
    record_weighed_spool  a scale reading that sets the net weight

Two claim scopes, both INSERT ... ON CONFLICT DO NOTHING in the caller's
transaction, kept only when something was actually deducted (with no matching
spool or an unknown/NULL balance every claim is withdrawn, so a later correction
can still deduct):

    physical print   ``quota_attempt:filament:job:<job>:print:<print_job>`` (or
                     ``quota_attempt:filament:print:<print_job>`` without a job):
                     each physical print deducts once (archive accounting, and
                     the Moonraker monitor when it knows the print)
    job attempt      ``quota_attempt:filament:job:<job>:<seq>`` (core.print_attempts):
                     the job's current attempt has been deducted. A physical
                     print also records itself here (value ``{"print_job_id": n}``),
                     so an operator /complete correction never deducts again; a
                     job-level deduction (operator /complete, value
                     ``{"print_job_id": null}``) cannot be attributed to a physical
                     print, so a later print-scoped deduction for that attempt
                     is refused as ``job_usage_unattributed`` instead of
                     deducting twice.

Attempts are resolved per deduction, never assumed to be the job's current one:
a physical print belongs to the attempt it was bound to when created/linked
(core.print_attempts.bind_print_attempt), else to the attempt whose reset window
holds its started_at -- so a late archive (or startup recovery) after a reset
claims and checks the print's own attempt. Usage rows naming a print belong to
that print's attempt; job-level rows to the attempt whose window holds used_at.

A marker-less deduction (written before markers existed, or by a pre-ledger
build) still counts when its spool_usage row belongs to the same attempt:
a row for the given physical print, or an automatic job row written at or after
the attempt's anchor -- the reset that began it (the counter's ``started`` key,
or for pre-counter data the latest ``job.reset`` audit entry) or, later, the
job's actual_start. With no anchor at all (never reset, never started) every
automatic row counts. Manual adjustments never count as the job's deduction.
A legacy row whose writer cannot be told from its notes ("unknown") is not the
job's deduction either, but print-scoped accounting refuses to deduct over it
(reconciliation: ``legacy_usage_unattributed``).

Row kinds are recorded in ``spool_usage.notes`` (no schema change): automatic rows
start with ``auto:<source>:``, manual ones with ``manual:``. Older rows are
classified by the notes the previous writers used (LEGACY_AUTO_NOTES).

``run`` is a ``run(sql, params)`` callable (core.print_attempts.sqlalchemy_runner /
qmark_runner); the caller owns the transaction.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Mapping, Optional

from core.print_attempts import (PREFIX, attempt_seq, attempt_starts, bound_print_attempt,
                                 filament_marker_for)

AUTO_PREFIX = "auto:"
MANUAL_PREFIX = "manual:"
NOTES_MAX = 255  # spool_usage.notes is VARCHAR(255)
MANUAL_NOTES_MAX = NOTES_MAX - len(MANUAL_PREFIX) - 1  # what an operator may write after "manual: "
SOURCES = frozenset({"operator_complete", "moonraker", "archive"})
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


def _now():
    # Naive UTC, microsecond precision (portable for SQLite text and PostgreSQL timestamp).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc(value):
    if value is None:
        return None
    from core.db_compat import as_utc_datetime
    try:
        return as_utc_datetime(value)
    except (TypeError, ValueError):
        return None


LEGACY_MANUAL_NOTES = ("Manual weigh adjustment",)


def row_kind(notes) -> str:
    """'auto', 'manual' or 'unknown' (a legacy row whose writer cannot be told)."""
    text_value = notes or ""
    if text_value.startswith(AUTO_PREFIX) or text_value.startswith(LEGACY_AUTO_NOTES):
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
        return self.seq == other.seq and self.start == other.start


def _boundaries(run, job_id):
    """Every known reset of the job: counted ones (started anchors) and audited ones."""
    return sorted(set(attempt_starts(run, job_id).values()) | set(_audit_resets(run, job_id)))


def current_attempt(run, job_id) -> AttemptView:
    bounds = _boundaries(run, job_id)
    row = run("SELECT actual_start FROM jobs WHERE id = :jid", {"jid": int(job_id)}).fetchone()
    return AttemptView(attempt_seq(run, job_id), bounds[-1] if bounds else None, None,
                       _utc(row[0]) if row else None)


def print_attempt(run, job_id, print_job_id) -> AttemptView:
    """The attempt a physical print belongs to -- never simply the job's current one.

    The seq is the binding recorded when the print was created/linked
    (core.print_attempts.bind_print_attempt); for an older print it is the number
    of counted resets at or before the print's started_at. The window is the span
    between the resets around started_at.
    """
    row = run("SELECT started_at FROM print_jobs WHERE id = :p", {"p": int(print_job_id)}).fetchone()
    started = _utc(row[0]) if row else None
    bound = bound_print_attempt(run, print_job_id, job_id)
    starts = attempt_starts(run, job_id)
    if started is None:
        if bound is None:
            return current_attempt(run, job_id)
        later = [moment for seq, moment in starts.items() if seq > bound]
        return AttemptView(bound, starts.get(bound), min(later) if later else None)
    bounds = _boundaries(run, job_id)
    before = [b for b in bounds if b <= started]
    after = [b for b in bounds if b > started]
    seq = bound if bound is not None else sum(1 for moment in starts.values() if moment <= started)
    return AttemptView(seq, before[-1] if before else None, after[0] if after else None)


def attempt_anchor(run, job_id):
    """Earliest time a usage row of the current attempt can carry (None: no bound)."""
    view = current_attempt(run, job_id)
    known = [moment for moment in (view.start, view.floor) if moment is not None]
    return max(known) if known else None


def print_marker(job_id, print_job_id) -> str:
    if job_id is None:
        return f"{PREFIX}filament:print:{int(print_job_id)}"
    return f"{PREFIX}filament:job:{int(job_id)}:print:{int(print_job_id)}"


def _marker_print(value):
    """The print_job_id a job-attempt marker names (None: a job-level deduction)."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value.get("print_job_id") if isinstance(value, dict) else None


def _attempt_rows(run, job_id, view, kinds):
    """print_job_id of every usage row of ``kinds`` that belongs to attempt ``view``.

    A row naming a physical print belongs to that print's own attempt (a late
    archive written after a reset still belongs to the earlier attempt); a
    job-level row belongs to the attempt whose window holds its used_at.
    """
    prints = {}
    for print_job, used_at, notes in run("SELECT print_job_id, used_at, notes FROM spool_usage WHERE job_id = :jid",
                                         {"jid": int(job_id)}).fetchall():
        if row_kind(notes) not in kinds:
            continue
        if print_job is not None:
            if print_job not in prints:
                prints[print_job] = print_attempt(run, job_id, print_job)
            if prints[print_job].same_attempt(view):
                yield print_job
        elif view.holds(_utc(used_at)):
            yield None


def prior_attempt_usage(run, job_id, print_job_id=None) -> Optional[str]:
    """Why this deduction is already accounted for (None when it is not).

    With ``print_job_id`` the question is about that physical print and the
    attempt *it* belongs to; without it, about the job's current attempt.
    """
    if print_job_id is not None:
        if run("SELECT 1 FROM system_config WHERE key = :k", {"k": print_marker(job_id, print_job_id)}).fetchone():
            return MARKER
        if run("SELECT 1 FROM spool_usage WHERE print_job_id = :pj LIMIT 1", {"pj": int(print_job_id)}).fetchone():
            return PRINT_USAGE
        if job_id is None:
            return None
        view = print_attempt(run, job_id, print_job_id)
        row = run("SELECT value FROM system_config WHERE key = :k",
                  {"k": filament_marker_for(job_id, view.seq)}).fetchone()
        if row and _marker_print(row[0]) is None:
            return JOB_USAGE
        # A marker-less job row of this attempt (automatic, or a legacy row whose
        # writer is unknown) cannot be attributed to this print: reconcile.
        if any(print_job is None for print_job in _attempt_rows(run, job_id, view, ("auto", "unknown"))):
            return LEGACY_JOB_USAGE
        return None
    if job_id is None:
        return None
    view = current_attempt(run, job_id)
    if run("SELECT 1 FROM system_config WHERE key = :k", {"k": filament_marker_for(job_id, view.seq)}).fetchone():
        return MARKER
    for print_job in _attempt_rows(run, job_id, view, ("auto",)):
        return PRINT_USAGE if print_job is not None else LEGACY_JOB_USAGE
    return None


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


def _deduct(run, spool_id, grams, *, job_id, print_job_id, notes):
    """Read and decrement one (already locked) spool; insert its usage row. None when its balance is unknown."""
    row = run("SELECT remaining_weight_g, spoolman_spool_id FROM spools WHERE id = :id", {"id": spool_id}).fetchone()
    before = row[0] if row else None
    if before is None or not math.isfinite(float(before)):
        return None
    before = float(before)
    run("UPDATE spools SET remaining_weight_g = CASE WHEN remaining_weight_g > :g THEN remaining_weight_g - :g "
        "ELSE 0 END WHERE id = :id AND remaining_weight_g IS NOT NULL", {"g": grams, "id": spool_id})
    run("INSERT INTO spool_usage (spool_id, job_id, print_job_id, weight_used_g, used_at, notes) "
        "VALUES (:spool, :job, :pj, :g, :at, :notes)",
        {"spool": spool_id, "job": job_id, "pj": print_job_id, "g": grams, "at": _now(), "notes": notes})
    return {"spool_id": spool_id, "grams": grams, "remaining_g": max(0.0, before - grams),
            "shortfall_g": max(0.0, grams - before), "spoolman_spool_id": row[1]}


def record_job_filament(run, job_id, *, source: str, grams_by_spool: Mapping, print_job_id=None,
                        notes: str = "") -> LedgerResult:
    """Deduct once per physical print (``print_job_id``) or per job attempt, claiming atomically."""
    if source not in SOURCES:
        raise ValueError(f"Unknown filament ledger source {source!r}")
    job_id = int(job_id) if job_id is not None else None
    print_job_id = int(print_job_id) if print_job_id is not None else None
    if job_id is None and print_job_id is None:
        raise ValueError("A filament deduction names a job or a physical print")
    prior = prior_attempt_usage(run, job_id, print_job_id)
    if prior:
        return LedgerResult("already_deducted", prior)
    items = _clean(grams_by_spool)
    if not items:
        return LedgerResult("nothing_deducted")
    locked = _lock_spools(run, [spool_id for spool_id, _ in items])
    items = [(spool_id, grams) for spool_id, grams in items if spool_id in locked]
    claimed = []
    if job_id is None:
        job_key = None
    else:  # the attempt this deduction belongs to: the print's own, or the job's current one
        view = print_attempt(run, job_id, print_job_id) if print_job_id is not None else current_attempt(run, job_id)
        job_key = filament_marker_for(job_id, view.seq)
    if print_job_id is not None:
        key = print_marker(job_id, print_job_id)
        if not _claim(run, key, {"source": source}):
            return LedgerResult("already_deducted", MARKER)
        claimed.append(key)
        if job_key is not None:
            if _claim(run, job_key, {"source": source, "print_job_id": print_job_id}):
                claimed.append(job_key)
            else:
                row = run("SELECT value FROM system_config WHERE key = :k", {"k": job_key}).fetchone()
                if row and _marker_print(row[0]) is None:  # a job-level deduction won the attempt
                    run("DELETE FROM system_config WHERE key = :k", {"k": key})
                    return LedgerResult("already_deducted", JOB_USAGE)
    else:
        if not _claim(run, job_key, {"source": source, "print_job_id": None}):
            return LedgerResult("already_deducted", MARKER)
        claimed.append(job_key)
    label = (f"{AUTO_PREFIX}{source}: {notes}" if notes else f"{AUTO_PREFIX}{source}")[:NOTES_MAX]
    deductions = [entry for entry in (
        _deduct(run, spool_id, grams, job_id=job_id, print_job_id=print_job_id, notes=label)
        for spool_id, grams in items) if entry is not None]
    if not deductions:
        for key in claimed:
            # Nothing was deducted: withdraw the claims so a later correction can still deduct.
            run("DELETE FROM system_config WHERE key = :k", {"k": key})
        return LedgerResult("nothing_deducted")
    return LedgerResult("deducted", None, deductions)


def record_manual_usage(run, spool_id, grams, *, job_id=None, notes=None) -> Optional[dict]:
    """An operator's manual consumption. Never counts as a job's automatic deduction."""
    items = _clean({spool_id: grams})
    if not items:
        return None
    if notes is not None and len(notes) > MANUAL_NOTES_MAX:
        raise ValueError(f"Manual usage notes are limited to {MANUAL_NOTES_MAX} characters")
    label = f"{MANUAL_PREFIX} {notes}" if notes else MANUAL_PREFIX
    if not _lock_spools(run, [items[0][0]]):
        return None
    return _deduct(run, items[0][0], items[0][1], job_id=int(job_id) if job_id is not None else None,
                   print_job_id=None, notes=label)


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
    return {"spool_id": int(spool_id), "old_weight_g": before, "remaining_g": value}
