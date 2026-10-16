"""Per-job physical print attempts, shared by quota charging and filament deduction.

A job's attempt counter starts at 0 and is bumped only by a reset (a genuine
new physical print; a reprint creates a new job and therefore starts its own
counter). A status correction (complete -> fail -> complete) stays on the same
attempt, so anything charged "once per attempt" is charged once.

Storage is system_config, in the same scheme as the quota attempt markers
(cert-fix-F, core/quota.py):

    quota_attempt:seq:<job_id>                    the attempt counter
    quota_attempt:job:<job_id>:<seq>              quota charged for that attempt
    quota_attempt:filament:job:<job_id>:<seq>     filament deducted for that attempt (core.filament_ledger)
    quota_attempt:started:<job_id>:<seq>          when the reset that began attempt <seq> happened
    quota_attempt:print_seq:<print_job_id>        the job attempt a physical print belongs to
                                                  ({"job_id": j, "seq": n}, bound when it is created/linked)

All keys share the ``quota_attempt:`` prefix, which the diagnostics dump excludes.
``run`` is a ``run(sql, params)`` callable (see sqlalchemy_runner/qmark_runner).
"""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text

PREFIX = "quota_attempt:"
log = logging.getLogger("odin.print_attempts")


def sqlalchemy_runner(db):
    """Adapt a SQLAlchemy session/connection to ``run(sql, params)``."""
    return lambda statement, params: db.execute(text(statement), params)


def qmark_runner(cursor):
    """Adapt a qmark DB-API cursor (core.db_utils.get_db) to ``run(sql, params)``."""
    def run(statement, params):
        values = []

        def bind(match):
            values.append(params[match.group(1)])
            return "?"
        return cursor.execute(re.sub(r"(?<!:):([A-Za-z_]\w*)", bind, statement), tuple(values))
    return run


def _seq_key(job_id) -> str:
    return f"{PREFIX}seq:{int(job_id)}"


def attempt_seq(run, job_id) -> int:
    """The job's current print-attempt counter (0 until a reset bumps it)."""
    row = run("SELECT value FROM system_config WHERE key = :k", {"k": _seq_key(job_id)}).fetchone()
    if not row:
        return 0
    value = row[0]
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    try:
        return int(str(value).strip().strip('"'))
    except (TypeError, ValueError):
        return 0


def _started_key(job_id, seq) -> str:
    return f"{PREFIX}started:{int(job_id)}:{int(seq)}"


def bump_print_attempt(run, job_id) -> int:
    """A reset starts a new physical attempt; returns the new counter.

    The reset time is kept as the attempt's anchor (``started:<job>:<seq>``) so the
    filament ledger can tell this attempt's usage rows from earlier ones.
    """
    from datetime import datetime, timezone
    seq = attempt_seq(run, job_id) + 1
    run("DELETE FROM system_config WHERE key = :k", {"k": _seq_key(job_id)})
    run("INSERT INTO system_config (key, value) VALUES (:k, :v)", {"k": _seq_key(job_id), "v": str(seq)})
    run("INSERT INTO system_config (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING",
        {"k": _started_key(job_id, seq), "v": json.dumps(datetime.now(timezone.utc).isoformat())})
    return seq


def attempt_reset_at(run, job_id, seq=None):
    """When the current attempt was started by a reset (None for attempt 0 or unknown)."""
    seq = attempt_seq(run, job_id) if seq is None else seq
    return attempt_starts(run, job_id).get(seq) if seq > 0 else None


def _print_seq_key(print_job_id) -> str:
    return f"{PREFIX}print_seq:{int(print_job_id)}"


def bind_print_attempt(run, print_job_id, job_id=None, *, seq=None) -> None:
    """Record which attempt of its job a physical print belongs to (first binding wins).

    Called wherever a print is created for, or linked to, a job, so a late end or
    archive of that print is accounted to its own attempt, never to a later one.
    """
    if print_job_id is None:
        return
    if job_id is None:
        row = run("SELECT scheduled_job_id FROM print_jobs WHERE id = :p", {"p": int(print_job_id)}).fetchone()
        job_id = row[0] if row else None
    if job_id is None:
        return
    run("INSERT INTO system_config (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING",
        {"k": _print_seq_key(print_job_id),
         "v": json.dumps({"job_id": int(job_id), "seq": attempt_seq(run, job_id) if seq is None else int(seq)})})


def bound_print_attempt(run, print_job_id, job_id):
    """The bound attempt seq of a print for this job, or None (unbound or bound elsewhere)."""
    row = run("SELECT value FROM system_config WHERE key = :k", {"k": _print_seq_key(print_job_id)}).fetchone()
    if not row:
        return None
    value = row[0]
    try:
        value = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("job_id") == int(job_id) and isinstance(value.get("seq"), int):
        return value["seq"]
    return None


def moment(value):
    """A stored time as the ledger compares it: UTC, truncated to whole seconds.

    Every attempt boundary and every row time goes through this one function, so
    times written at different precisions (SQLite CURRENT_TIMESTAMP seconds,
    Python microseconds, PostgreSQL timestamps) fall in exactly one window
    (windows are [start, end): inclusive below, exclusive above). Naive values are
    UTC: PostgreSQL sessions are pinned to TimeZone=UTC (core.database_config), so
    server-default now() columns are UTC too.
    """
    if value is None:
        return None
    from core.db_compat import as_utc_datetime
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        return as_utc_datetime(value).replace(microsecond=0)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None


def attempt_starts(run, job_id) -> dict:
    """{seq: reset time} for the job's counted attempts (seq >= 1).

    An unparseable anchor is reported (logged) and left out; callers that need
    it fall back explicitly to the job's audited resets and print times.
    """
    found = {}
    for key, value in run("SELECT key, value FROM system_config WHERE key LIKE :p",
                          {"p": f"{PREFIX}started:{int(job_id)}:%"}).fetchall():
        try:
            parsed = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
            seq = int(str(key).rsplit(":", 1)[1])
        except (TypeError, ValueError):
            parsed, seq = None, None
        when = moment(parsed)
        if seq is None or when is None:
            log.warning("Unparseable attempt anchor %s for job %s; falling back to audited resets", key, job_id)
            continue
        found[seq] = when
    return found


def filament_marker_for(job_id, seq) -> str:
    return f"{PREFIX}filament:job:{int(job_id)}:{int(seq)}"


def filament_marker(run, job_id) -> str:
    """This attempt's filament-ledger claim key (written only by core.filament_ledger)."""
    return filament_marker_for(job_id, attempt_seq(run, job_id))


def clear_job_attempts(run, job_id) -> None:
    """Drop the counter and every per-attempt marker of a deleted job (SQLite id reuse is safe)."""
    jid = int(job_id)
    for (print_job,) in run("SELECT id FROM print_jobs WHERE scheduled_job_id = :jid", {"jid": jid}).fetchall():
        run("DELETE FROM system_config WHERE key = :k", {"k": _print_seq_key(print_job)})
    run("DELETE FROM system_config WHERE key = :seq OR key LIKE :job OR key LIKE :filament OR key LIKE :started",
        {"seq": _seq_key(jid), "job": f"{PREFIX}job:{jid}:%", "filament": f"{PREFIX}filament:job:{jid}:%",
         "started": f"{PREFIX}started:{jid}:%"})
