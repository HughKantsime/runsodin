"""Read-only SQLite integrity check, shared by the bootstrap command and the database guardian.

The connection is mode=ro, so nothing here can write to or repair the database. In WAL mode a
reader doesn't block writers. A running check does stop the WAL from resetting until it ends.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

OK = "ok"
DAMAGED = "damaged"
UNAVAILABLE = "unavailable"

RUNBOOK = 'docs/BACKUP_RESTORE_RUNBOOK.md, section "Database failed its integrity check"'

_BUSY_SECONDS = 5.0
_MAX_PROBLEMS_KEPT = 5
_MAX_PROBLEM_CHARS = 200
_MAX_FOREIGN_KEY_ROWS = 1000
_DAMAGE_CODES = frozenset({sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB})


@dataclass(frozen=True)
class IntegrityReport:
    """``problems`` holds SQLite's own lines: table, page and rowid references, never row contents."""

    state: str
    seconds: float
    integrity_problems: int = 0
    foreign_key_violations: int = 0
    problems: tuple[str, ...] = ()


def _is_damage(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    return code is not None and (code & 0xFF) in _DAMAGE_CODES


def check_database(path: Path | str) -> IntegrityReport:
    """Run integrity_check and foreign_key_check, each in its own read snapshot.

    DAMAGED means SQLite reported damage. UNAVAILABLE means the check couldn't finish (locked,
    unreadable, missing), which says nothing about the data either way.
    """
    started = time.monotonic()
    try:
        connection = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=_BUSY_SECONDS)
        try:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchmany(_MAX_FOREIGN_KEY_ROWS)
        finally:
            connection.close()
    except sqlite3.Error as error:
        reason = (getattr(error, "sqlite_errorname", None) or type(error).__name__,)
        state = DAMAGED if _is_damage(error) else UNAVAILABLE
        return IntegrityReport(state, time.monotonic() - started,
                               integrity_problems=int(state == DAMAGED), problems=reason)
    seconds = time.monotonic() - started
    integrity_problems = [] if integrity == ["ok"] else integrity
    if not integrity_problems and not foreign_keys:
        return IntegrityReport(OK, seconds)
    lines = integrity_problems + [
        f"foreign key violation in {table} rowid {rowid}, parent {parent}"
        for table, rowid, parent, _ in foreign_keys
    ]
    return IntegrityReport(
        DAMAGED, seconds, len(integrity_problems), len(foreign_keys),
        tuple(line[:_MAX_PROBLEM_CHARS] for line in lines[:_MAX_PROBLEMS_KEPT]),
    )


def boot_refusal(path: Path | str, report: IntegrityReport) -> str:
    """What an operator reads in the container log when the database stops ODIN from starting."""
    return "\n".join([
        "ODIN will not start: the database failed its integrity check.",
        f"  Database: {path}",
        f"  integrity_check problems: {report.integrity_problems}; "
        f"foreign_key_check violations: {report.foreign_key_violations}",
        *(f"  {line}" for line in report.problems),
        "  This is damage in the database file, so rolling back the release won't help.",
        "  Don't repair or replace the live file. Copy it first, test any repair on the copy with",
        "  scripts.bootstrap_database, and get the owner's yes before changing live data.",
        f"  Runbook: {RUNBOOK}",
    ])
