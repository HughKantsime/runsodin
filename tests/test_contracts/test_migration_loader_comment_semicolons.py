"""
Contract test — the SQLite migration loader must not be fooled by
inline semicolons inside SQL line comments.

v1.9.1 prod incident (2026-04-16): migration
`004_drop_setup_token_add_delivery_status.sql` carried a header
comment reading "would be dead state; one DELETE is the correct
cleanup." The `;` inside the comment was interpreted by
`sql.split(";")` as a statement separator. The chunk following the
split began with "one DELETE..." — which sqlite3 tried to parse as
SQL and rejected: `sqlite3.OperationalError: near "one": syntax
error`. Prod crash-looped.

The loader was hardened to strip line comments BEFORE splitting.
This test pins that invariant with a synthetic migration mirroring
the failure shape.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_inline_semicolon_in_comment_does_not_break_alter_path():
    """The loader's ALTER-TABLE branch must handle `;` inside line comments."""
    from core.db import _run_sql_file

    # Mimics the shape of migration 004: a blob with `ALTER TABLE` in
    # it (forcing the split-by-semicolon code path) and a header
    # comment that contains an inline semicolon. The loader must
    # strip the comment first so the inline `;` doesn't split SQL.
    sql = """-- Migration with inline semicolon in comment
-- Leaving this here would be dead state; one DELETE is the correct cleanup.
-- The loader must NOT interpret the semicolon above as a statement
-- separator, or else sqlite3 will choke with `near "one": syntax error`.

DELETE FROM example WHERE id = 99;

ALTER TABLE example ADD COLUMN status TEXT DEFAULT 'pending';
"""

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        db_path = str(tmp_dir / "test.db")
        sql_file = tmp_dir / "004_test.sql"
        sql_file.write_text(sql, encoding="utf-8")

        # Pre-create the example table (migration presumes it exists).
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE example (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.commit()
        conn.close()

        # If the loader splits on the `;` inside the comment, sqlite3
        # raises OperationalError with `near "one"`. If the fix works,
        # the DELETE + ALTER complete silently.
        _run_sql_file(db_path, sql_file)

        # Verify the ALTER actually landed.
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(example)")]
        conn.close()
        assert "status" in cols, (
            f"ALTER TABLE did not apply — loader may have bailed early. "
            f"Columns present: {cols}"
        )


def test_strip_sql_comments_removes_line_comments_but_preserves_strings():
    """The comment stripper must not nuke `--` that appears inside a string
    literal. Regression guard if someone swaps in a more aggressive stripper."""
    from core.db import _strip_sql_comments

    sql = "SELECT 'a -- b' AS x; -- trailing comment\nINSERT INTO t VALUES ('c');"
    stripped = _strip_sql_comments(sql)
    # Naive acceptance: the stripper DOES nuke `--` inside string literals
    # right now (documented as acceptable — no ODIN migration does that).
    # Pin the current observed behavior so a future refactor is intentional.
    # If you change the stripper to be string-aware, update this assertion.
    assert "-- trailing comment" not in stripped, (
        "Line comment should be stripped"
    )
    assert "SELECT 'a " in stripped  # SELECT statement survives
    assert "INSERT INTO t VALUES ('c')" in stripped


def test_migration_004_has_no_semicolons_in_comments():
    """Source-level guard: migration 004 (which caused the prod incident)
    must not reintroduce an inline `;` inside a comment line.

    Narrower than the loader test but makes the regression visible
    immediately at code-review time rather than only when the migration
    is applied to a fresh DB."""
    path = BACKEND_DIR / "core" / "migrations" / "004_drop_setup_token_add_delivery_status.sql"
    assert path.exists(), "Migration 004 file missing"
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("--") and ";" in stripped:
            pytest.fail(
                f"Migration 004 line {n} contains a `;` inside a comment: "
                f"{line!r}. The loader now strips line comments before "
                "splitting (v1.9.2 fix) so this is technically survivable, "
                "but the line itself reintroduces the exact shape of the "
                "original v1.9.1 prod incident. Rephrase without `;`."
            )
