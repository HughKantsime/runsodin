"""The read-only integrity check, and the plain refusal a damaged database gets at boot."""
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from core import db_integrity
from core.db_integrity import DAMAGED, OK, UNAVAILABLE, boot_refusal, check_database
from tests.test_contracts.db_damage_support import overwrite_rightmost_leaf

ROOT = Path(__file__).resolve().parents[2]


def _healthy(path: Path, *, wal: bool = False) -> Path:
    connection = sqlite3.connect(path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.executescript(
        "CREATE TABLE printers(id INTEGER PRIMARY KEY, name TEXT);"
        "CREATE TABLE audit_logs(id INTEGER PRIMARY KEY, action TEXT);"
        "CREATE INDEX ix_audit_action ON audit_logs(action);"
    )
    connection.executemany("INSERT INTO printers(name) VALUES(?)", [(f"printer {n}",) for n in range(7)])
    connection.executemany("INSERT INTO audit_logs(action) VALUES(?)", [(f"action {n}",) for n in range(3000)])
    connection.commit()
    connection.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_healthy_database_passes(tmp_path):
    report = check_database(_healthy(tmp_path / "ok.db"))
    assert report.state == OK and report.integrity_problems == 0 and report.foreign_key_violations == 0
    assert report.problems == () and report.seconds >= 0


def test_incident_shaped_damage_is_reported_and_writes_still_succeed(tmp_path):
    """The shape that stayed silent for 21 hours: a live leaf overwritten by another table's page."""
    database = _healthy(tmp_path / "damaged.db")
    overwrite_rightmost_leaf(database, "audit_logs", "printers")
    connection = sqlite3.connect(database)
    connection.execute("INSERT INTO audit_logs(action) VALUES('written on the damage')")
    connection.commit()
    connection.close()
    report = check_database(database)
    assert report.state == DAMAGED and report.integrity_problems > 0 and report.problems


def test_foreign_key_violation_alone_is_damage(tmp_path):
    database = tmp_path / "fk.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        "CREATE TABLE parents(id INTEGER PRIMARY KEY);"
        "CREATE TABLE children(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parents(id));"
        "INSERT INTO children(parent_id) VALUES(42);"
    )
    connection.close()
    report = check_database(database)
    assert report.state == DAMAGED and report.integrity_problems == 0 and report.foreign_key_violations == 1
    assert "children" in report.problems[0] and "42" not in report.problems[0].split("parent")[1]


def test_unreadable_header_is_damage(tmp_path):
    database = tmp_path / "garbage.db"
    database.write_bytes(b"this is not a database at all" * 200)
    assert check_database(database).state == DAMAGED


def test_missing_file_is_unavailable_not_damage(tmp_path):
    missing = tmp_path / "missing.db"
    report = check_database(missing)
    assert report.state == UNAVAILABLE and report.integrity_problems == 0
    assert not missing.exists()


def test_locked_database_is_unavailable_not_damage(tmp_path, monkeypatch):
    monkeypatch.setattr(db_integrity, "_BUSY_SECONDS", 0.05)
    database = _healthy(tmp_path / "locked.db")
    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert check_database(database).state == UNAVAILABLE
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_problem_lines_are_capped(tmp_path, monkeypatch):
    class Cursor(list):
        def fetchmany(self, _):
            return self
    class Connection:
        def execute(self, sql):
            if "integrity_check" in sql:
                return Cursor([("x" * 900,)] * 50)
            return Cursor([])
        def close(self):
            pass
    monkeypatch.setattr(db_integrity.sqlite3, "connect", lambda *a, **k: Connection())
    report = check_database(tmp_path / "any.db")
    assert report.integrity_problems == 50 and len(report.problems) == 5
    assert all(len(line) == 200 for line in report.problems)


def test_check_only_ever_opens_read_only_and_changes_nothing(tmp_path, monkeypatch):
    database = _healthy(tmp_path / "damaged.db")
    overwrite_rightmost_leaf(database, "audit_logs", "printers")
    before = _digest(database)
    seen = []
    original = sqlite3.connect
    monkeypatch.setattr(db_integrity.sqlite3, "connect",
                        lambda target, **kwargs: seen.append(target) or original(target, **kwargs))
    assert check_database(database).state == DAMAGED
    assert len(seen) == 1 and seen[0].endswith("?mode=ro")
    assert _digest(database) == before


def test_committed_wal_content_is_seen_after_a_writer_dies(tmp_path):
    """A boot after a crash finds a hot WAL; the read-only check has to read through it."""
    database = _healthy(tmp_path / "hot.db", wal=True)
    script = (
        "import os, sqlite3, sys\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute('PRAGMA wal_autocheckpoint=0')\n"
        "c.execute(\"INSERT INTO audit_logs(action) VALUES('survives the crash')\")\n"
        "c.commit()\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", script, str(database)], check=True, timeout=60)
    assert Path(f"{database}-wal").stat().st_size > 0
    assert check_database(database).state == OK
    reader = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    assert reader.execute("SELECT count(*) FROM audit_logs WHERE action='survives the crash'").fetchone() == (1,)
    reader.close()


def test_refusal_says_what_happened_and_where_to_go():
    report = db_integrity.IntegrityReport(DAMAGED, 1.0, 3, 1, ("Tree 4 page 37916: btreeInitPage() returns error code 11",))
    message = boot_refusal("/data/odin.db", report)
    assert message.startswith("ODIN will not start: the database failed its integrity check.")
    assert "/data/odin.db" in message and "Tree 4 page 37916" in message
    assert "integrity_check problems: 3" in message and "foreign_key_check violations: 1" in message
    assert "BACKUP_RESTORE_RUNBOOK.md" in message and "scripts.bootstrap_database" in message
    assert "owner's yes" in message and "Media ownership" not in message


def _bootstrap(database: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{database}", LICENSE_DIR=str(tmp_path), API_KEY="",
               JWT_SECRET_KEY="db-safety-test-only", ODIN_ITAR_MODE="0", ODIN_TELEMETRY_V2="0", COOKIE_SECURE="false",
               ODIN_DB_ROLE="bootstrap", PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}")
    return subprocess.run([sys.executable, "-m", "scripts.bootstrap_database"], cwd=ROOT / "backend", env=env,
                          capture_output=True, text=True, timeout=300)


@pytest.fixture(scope="module")
def booted_database(tmp_path_factory):
    directory = tmp_path_factory.mktemp("boot")
    database = directory / "odin.db"
    result = _bootstrap(database, directory)
    assert result.returncode == 0, result.stderr[-2000:]
    return database


def test_real_boot_passes_a_healthy_database_and_a_missing_one(booted_database, tmp_path):
    copied = tmp_path / "odin.db"
    copied.write_bytes(booted_database.read_bytes())
    result = _bootstrap(copied, tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "integrity" not in result.stderr


def test_real_boot_refuses_damage_plainly_before_changing_anything(booted_database, tmp_path):
    damaged = tmp_path / "odin.db"
    damaged.write_bytes(booted_database.read_bytes())
    connection = sqlite3.connect(damaged)
    connection.execute("INSERT INTO groups(id, name, is_org) VALUES(1, 'org', 1)")
    connection.execute("INSERT INTO printers(id, name, org_id) VALUES(1, 'one', 1)")
    connection.executemany("INSERT INTO audit_logs(action, entity_type) VALUES(?, 'x')", [(f"a{n}",) for n in range(3000)])
    connection.commit()
    connection.close()
    overwrite_rightmost_leaf(damaged, "audit_logs", "printers")
    before = _digest(damaged)
    result = _bootstrap(damaged, tmp_path)
    assert result.returncode == 1
    assert "ODIN will not start: the database failed its integrity check." in result.stderr
    assert "BACKUP_RESTORE_RUNBOOK.md" in result.stderr
    assert "Media ownership" not in result.stderr and "Traceback" not in result.stderr
    assert result.stdout.strip() == ""
    assert _digest(damaged) == before


def test_a_check_that_cannot_finish_leaves_the_decision_to_the_migrations(monkeypatch, tmp_path, capsys):
    """Boot's fail-closed set doesn't grow: only reported damage refuses here."""
    from scripts import bootstrap_database as script
    database = _healthy(tmp_path / "odin.db")
    monkeypatch.setattr(script, "check_database", lambda _: db_integrity.IntegrityReport(UNAVAILABLE, 0.0))
    assert script._refuse_damaged_sqlite(f"sqlite:///{database}") is False
    monkeypatch.setattr(script, "check_database", lambda _: pytest.fail("no file to check"))
    assert script._refuse_damaged_sqlite(f"sqlite:///{tmp_path / 'absent.db'}") is False
    assert script._refuse_damaged_sqlite("postgresql://odin@postgres:5432/odin") is False
    assert capsys.readouterr().err == ""


def test_migration_016_keeps_its_ledger_checksum():
    """Rewording 016 would change its checksum and stop every existing database at boot
    ("Migration history mismatch"). The plain message belongs in the bootstrap command."""
    source = (ROOT / "backend/core/schema/migrations/016_timelapse_ownership.py").read_text(encoding="utf-8")
    assert "Media ownership database integrity differs" in source
