"""Per-job physical print attempts, shared by quota charging and filament deduction.

A job's attempt counter starts at 0 and is bumped only by a reset (a genuine
new physical print; a reprint creates a new job and therefore starts its own
counter). A status correction (complete -> fail -> complete) stays on the same
attempt, so anything charged "once per attempt" is charged once.

Storage is system_config, in the same scheme as the quota attempt markers
(cert-fix-F, core/quota.py):

    quota_attempt:seq:<job_id>                    the attempt counter
    quota_attempt:job:<job_id>:<seq>              quota charged for that attempt
    quota_attempt:filament:job:<job_id>:<seq>     filament deducted for that attempt

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


def bump_print_attempt(run, job_id) -> int:
    """A reset starts a new physical attempt; returns the new counter."""
    seq = attempt_seq(run, job_id) + 1
    run("DELETE FROM system_config WHERE key = :k", {"k": _seq_key(job_id)})
    run("INSERT INTO system_config (key, value) VALUES (:k, :v)", {"k": _seq_key(job_id), "v": str(seq)})
    return seq


def filament_marker(run, job_id) -> str:
    return f"{PREFIX}filament:job:{int(job_id)}:{attempt_seq(run, job_id)}"


def filament_already_deducted(run, job_id) -> bool:
    """True when this attempt's filament was already deducted.

    A marker settles it. Attempt 0 also counts any spool_usage row for the job
    as deducted: pre-upgrade completions and Moonraker deductions made before
    markers existed wrote usage rows but no marker.
    """
    if run("SELECT 1 FROM system_config WHERE key = :k", {"k": filament_marker(run, job_id)}).fetchone():
        return True
    return attempt_seq(run, job_id) == 0 and run(
        "SELECT 1 FROM spool_usage WHERE job_id = :jid LIMIT 1", {"jid": int(job_id)}).fetchone() is not None


def mark_filament_deducted(run, job_id) -> bool:
    """Atomically claim this attempt's filament deduction; False when already done.

    INSERT ... ON CONFLICT (key) DO NOTHING is atomic on SQLite (>= 3.24) and
    PostgreSQL; the rowcount says whether this caller won the claim.
    """
    key = filament_marker(run, job_id)
    if filament_already_deducted(run, job_id):
        # Record legacy (marker-less) attempt-0 deductions so later checks are cheap.
        run("INSERT INTO system_config (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING",
            {"k": key, "v": json.dumps("legacy")})
        return False
    result = run("INSERT INTO system_config (key, value) VALUES (:k, :v) ON CONFLICT (key) DO NOTHING",
                 {"k": key, "v": "true"})
    return getattr(result, "rowcount", 0) == 1


def clear_job_attempts(run, job_id) -> None:
    """Drop the counter and every per-attempt marker of a deleted job (SQLite id reuse is safe)."""
    jid = int(job_id)
    run("DELETE FROM system_config WHERE key = :seq OR key LIKE :job OR key LIKE :filament",
        {"seq": _seq_key(jid), "job": f"{PREFIX}job:{jid}:%", "filament": f"{PREFIX}filament:job:{jid}:%"})
