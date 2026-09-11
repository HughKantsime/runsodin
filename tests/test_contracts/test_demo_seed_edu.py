"""Contract tests for idempotent EDU sandbox persona seeding."""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

from core.auth import verify_password
from scripts.demo_seed_edu import Persona, upsert_personas


ROOT = Path(__file__).resolve().parents[2]


def _make_users_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'viewer',
            is_active INTEGER DEFAULT 1,
            mfa_enabled INTEGER DEFAULT 0,
            mfa_secret TEXT
        );
        CREATE TABLE printers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            is_active INTEGER DEFAULT 1
        );
        """
    )
    conn.close()


def test_persona_seed_is_idempotent_and_updates_passwords(tmp_path, capsys):
    db_path = tmp_path / "odin.db"
    _make_users_db(db_path)
    first = [
        Persona("admin@school.test", "first-admin", "admin", no_mfa=True),
        Persona("teacher@school.test", "first-teacher", "operator", no_mfa=True),
        Persona("student@school.test", "first-student", "viewer", no_mfa=True),
        Persona("appreview@demo.subsystem.app", "first-review", "viewer", no_mfa=True),
    ]
    upsert_personas(str(db_path), first)

    second = [
        Persona(person.email, f"second-{person.role}", person.role, no_mfa=True)
        for person in first
    ]
    upsert_personas(str(db_path), second)

    output = capsys.readouterr()
    for person in first + second:
        assert person.password not in output.out
        assert person.password not in output.err

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT username, email, password_hash, role, is_active, mfa_enabled, mfa_secret "
        "FROM users ORDER BY username"
    ).fetchall()
    conn.close()
    assert len(rows) == 4
    by_email = {row[0]: row for row in rows}
    expected_roles = {
        "admin@school.test": "admin",
        "teacher@school.test": "operator",
        "student@school.test": "viewer",
        "appreview@demo.subsystem.app": "viewer",
    }
    for email, role in expected_roles.items():
        row = by_email[email]
        assert row[1] == email
        assert row[3:] == (role, 1, 0, None)
        assert verify_password(f"second-{role}", row[2])


def test_persona_seed_rejects_unknown_role_before_writing(tmp_path):
    db_path = tmp_path / "odin.db"
    _make_users_db(db_path)
    try:
        upsert_personas(str(db_path), [Persona("bad@school.test", "secret", "teacher")])
    except ValueError as exc:
        assert "role" in str(exc).lower()
    else:
        raise AssertionError("unknown sandbox role was accepted")

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    conn.close()


def test_copied_reviewer_wrapper_accepts_password_over_stdin(tmp_path):
    db_path = tmp_path / "odin.db"
    _make_users_db(db_path)
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    for name in ("demo_seed_reviewer.py", "demo_seed_edu.py"):
        shutil.copy2(ROOT / "backend/scripts" / name, script_dir / name)

    password = "reviewer-test-password"
    stale_environment_password = "stale-environment-password"
    result = subprocess.run(
        [
            sys.executable,
            str(script_dir / "demo_seed_reviewer.py"),
            "--email", "reviewer@example.test",
            "--password-stdin",
            "--no-mfa",
            "--db-path", str(db_path),
        ],
        env={**os.environ, "ODIN_DEMO_REVIEWER_PASSWORD": stale_environment_password},
        input=password,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert password not in result.stdout
    assert password not in result.stderr
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT role, password_hash FROM users WHERE username = ?",
        ("reviewer@example.test",),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "viewer"
    assert verify_password(password, row[1])
    assert not verify_password(stale_environment_password, row[1])
