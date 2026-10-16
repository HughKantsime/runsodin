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

All keys share the ``quota_attempt:`` prefix, which the diagnostics dump excludes.
``run`` is a ``run(sql, params)`` callable (see sqlalchemy_runner/qmark_runner).
"""
from __future__ import annotations

import json
import re

from sqlalchemy import text

PREFIX = "quota_attempt:"


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
    from core.db_compat import as_utc_datetime
    seq = attempt_seq(run, job_id) if seq is None else seq
    if seq <= 0:
        return None
    row = run("SELECT value FROM system_config WHERE key = :k", {"k": _started_key(job_id, seq)}).fetchone()
    if not row:
        return None
    value = row[0]
    try:
        value = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
        return as_utc_datetime(value)
    except (TypeError, ValueError):
        return None


def filament_marker(run, job_id) -> str:
    """This attempt's filament-ledger claim key (written only by core.filament_ledger)."""
    return f"{PREFIX}filament:job:{int(job_id)}:{attempt_seq(run, job_id)}"


def clear_job_attempts(run, job_id) -> None:
    """Drop the counter and every per-attempt marker of a deleted job (SQLite id reuse is safe)."""
    jid = int(job_id)
    run("DELETE FROM system_config WHERE key = :seq OR key LIKE :job OR key LIKE :filament OR key LIKE :started",
        {"seq": _seq_key(jid), "job": f"{PREFIX}job:{jid}:%", "filament": f"{PREFIX}filament:job:{jid}:%",
         "started": f"{PREFIX}started:{jid}:%"})
