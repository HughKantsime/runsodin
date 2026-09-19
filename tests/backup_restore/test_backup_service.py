import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import modules.system.backup_service as backup_service
import modules.system.backup_verifier as backup_verifier

from modules.system.backup_service import (
    BackupValidationError,
    apply_pending_restore,
    create_online_backup,
    finalize_pending_restore,
    paths_from_database_url,
    rollback_pending_restore,
    stage_restore,
    validate_database,
)
from modules.system.backup_verifier import verify_backup


def _database(path: Path, marker: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY, username TEXT NOT NULL, email TEXT,
            password_hash TEXT NOT NULL, role TEXT NOT NULL, is_active INTEGER,
            group_id INTEGER
        );
        CREATE TABLE api_tokens (
            id INTEGER PRIMARY KEY, user_id INTEGER, token_hash TEXT,
            token_prefix TEXT, scopes TEXT, expires_at DATETIME
        );
        CREATE TABLE active_sessions (
            id INTEGER PRIMARY KEY, user_id INTEGER, token_jti TEXT,
            created_at DATETIME, last_seen_at DATETIME
        );
        CREATE TABLE token_blacklist (jti TEXT PRIMARY KEY, expires_at DATETIME);
        CREATE TABLE login_attempts (
            id INTEGER PRIMARY KEY, ip TEXT, username TEXT,
            attempted_at REAL, success INTEGER
        );
        CREATE TABLE audit_logs (
            id INTEGER PRIMARY KEY, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            action TEXT NOT NULL, entity_type TEXT, entity_id INTEGER, details TEXT
        );
        CREATE TABLE system_config (
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DATETIME
        );
        CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL, is_org INTEGER);
        CREATE TABLE models (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, build_time_hours REAL,
            color_requirements TEXT, org_id INTEGER
        );
        CREATE TABLE printers (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, api_type TEXT,
            is_active INTEGER, org_id INTEGER, shared INTEGER
        );
        CREATE TABLE jobs (
            id INTEGER PRIMARY KEY, item_name TEXT NOT NULL, quantity INTEGER,
            status TEXT, submitted_by INTEGER, created_at DATETIME
        );
        """
    )
    connection.execute(
        "INSERT INTO users VALUES (1, ?, ?, 'hash', 'admin', 1, NULL)",
        (f"{marker}@school.test", f"{marker}@school.test"),
    )
    connection.execute("INSERT INTO groups VALUES (1, ?, 1)", (f"{marker}-group",))
    connection.execute("INSERT INTO models VALUES (1, ?, 1.0, '{}', 1)", (f"{marker}-model",))
    connection.execute("INSERT INTO printers VALUES (1, ?, 'fixture', 1, 1, 0)", (f"{marker}-printer",))
    connection.execute(
        "INSERT INTO jobs VALUES (1, ?, 1, 'submitted', 1, CURRENT_TIMESTAMP)",
        (f"{marker}-job",),
    )
    connection.execute("INSERT INTO system_config VALUES ('marker', ?, CURRENT_TIMESTAMP)", (marker,))
    connection.commit()
    connection.close()


def _marker(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT value FROM system_config WHERE key='marker'"
        ).fetchone()[0]
    finally:
        connection.close()


def test_backup_and_offline_restore_round_trip(tmp_path: Path):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"

    backup, metadata = create_online_backup(url)
    assert metadata["size_bytes"] > 0
    assert backup.stat().st_mode & 0o777 == 0o600
    assert _marker(backup) == "before"
    staged = stage_restore(candidate, url)
    assert staged["restart_required"] is True
    applied = apply_pending_restore(url, pid_file=None)
    assert applied and applied["status"] == "applied_pending_validation"
    assert paths_from_database_url(url).manifest.exists()
    result = finalize_pending_restore(url, pid_file=None)

    assert result and result["status"] == "restored"
    assert _marker(live) == "after"
    connection = sqlite3.connect(live)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='restore_completed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_backup_fails_before_copy_when_disk_capacity_is_insufficient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live = tmp_path / "odin.db"
    _database(live, "before")
    url = f"sqlite:///{live}"
    usage = shutil._ntuple_diskusage(total=1024, used=1024, free=0)
    monkeypatch.setattr(backup_service.shutil, "disk_usage", lambda _path: usage)

    with pytest.raises(BackupValidationError, match="Insufficient free disk space"):
        create_online_backup(url)

    assert _marker(live) == "before"
    assert not list(paths_from_database_url(url).backups.glob("odin_backup_*.db"))
    assert not paths_from_database_url(url).pending.exists()


@pytest.mark.parametrize("content", [b"not a database", b"SQLite format 3\x00truncated"])
def test_corrupt_restore_leaves_original_usable(tmp_path: Path, content: bytes):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    candidate.write_bytes(content)
    with pytest.raises(BackupValidationError):
        stage_restore(candidate, f"sqlite:///{live}")
    assert _marker(live) == "before"


def test_wrong_schema_and_active_content_are_rejected(tmp_path: Path):
    wrong = tmp_path / "wrong.db"
    connection = sqlite3.connect(wrong)
    connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(BackupValidationError, match="missing required ODIN tables"):
        validate_database(wrong)

    malicious = tmp_path / "malicious.db"
    _database(malicious, "candidate")
    connection = sqlite3.connect(malicious)
    connection.execute(
        "CREATE TRIGGER unexpected AFTER INSERT ON users BEGIN DELETE FROM users; END"
    )
    connection.commit()
    connection.close()
    with pytest.raises(BackupValidationError, match="unexpected triggers or views"):
        validate_database(malicious)

    spoofed = tmp_path / "spoofed.db"
    _database(spoofed, "candidate")
    connection = sqlite3.connect(spoofed)
    connection.execute(
        "CREATE TRIGGER trg_education_audit_no_update AFTER INSERT ON users "
        "BEGIN DELETE FROM users; END"
    )
    connection.commit()
    connection.close()
    with pytest.raises(BackupValidationError, match="unexpected triggers or views"):
        validate_database(spoofed)


def test_required_columns_are_validated(tmp_path: Path):
    wrong = tmp_path / "wrong-columns.db"
    _database(wrong, "candidate")
    connection = sqlite3.connect(wrong)
    connection.execute("ALTER TABLE audit_logs RENAME TO old_audit_logs")
    connection.execute("CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, action TEXT NOT NULL)")
    connection.commit()
    connection.close()

    with pytest.raises(BackupValidationError, match="missing required columns"):
        validate_database(wrong)


def test_non_destructive_verifier_reports_latest_backup_without_modifying_it(tmp_path: Path):
    live = tmp_path / "odin.db"
    _database(live, "before")
    url = f"sqlite:///{live}"
    backup, expected = create_online_backup(url)
    original_bytes = backup.read_bytes()

    result = verify_backup(url)

    assert result["status"] == "verified"
    assert result["filename"] == backup.name
    assert result["sha256"] == expected["sha256"]
    assert result["table_count"] >= len(backup_service.REQUIRED_TABLES)
    assert backup.read_bytes() == original_bytes


def test_non_destructive_verifier_rejects_noncanonical_backup_name(tmp_path: Path):
    live = tmp_path / "odin.db"
    _database(live, "before")
    with pytest.raises(BackupValidationError, match="name is invalid"):
        verify_backup(f"sqlite:///{live}", "../odin_backup_escape.db")


def test_non_destructive_verifier_dispatches_postgres_dump_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    archive = backup_dir / "odin_backup_20260912_130000_123456.dump"
    archive.write_bytes(b"synthetic archive")
    calls: list[tuple[object, ...]] = []

    def validate(*args, **kwargs):
        calls.append((*args, kwargs))
        return {"size_bytes": archive.stat().st_size, "sha256": "f" * 64}

    monkeypatch.setattr(backup_verifier, "validate_postgres_restore", validate)
    result = verify_backup(
        "postgresql://odin@postgres:5432/odin",
        maintenance_url="postgresql://odin_maintenance@postgres:5432/postgres",
        password_file="/run/secrets/app",
        maintenance_password_file="/run/secrets/maintenance",
    )

    assert result["filename"] == archive.name
    assert result["sha256"] == "f" * 64
    assert calls[0][0] == archive
    assert calls[0][1] == "postgresql://odin@postgres:5432/odin"
    assert calls[0][2] == "postgresql://odin_maintenance@postgres:5432/postgres"


def test_relationally_invalid_backup_is_rejected(tmp_path: Path):
    candidate = tmp_path / "foreign-key-invalid.db"
    _database(candidate, "candidate")
    connection = sqlite3.connect(candidate)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("CREATE TABLE restore_relation (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id))")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("INSERT INTO restore_relation VALUES (1, 999)")
    connection.commit()
    connection.close()

    with pytest.raises(BackupValidationError, match="relational integrity"):
        validate_database(candidate)


def test_manifest_tamper_is_rejected_before_replacement(tmp_path: Path):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"
    stage_restore(candidate, url)
    paths = paths_from_database_url(url)
    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    manifest["sha256"] = "0" * 64
    paths.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BackupValidationError, match="does not match"):
        apply_pending_restore(url, pid_file=None)
    assert _marker(live) == "before"
    assert not paths.pending.exists()
    assert not paths.manifest.exists()


def test_live_pid_marker_blocks_offline_restore(tmp_path: Path):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"
    stage_restore(candidate, url)
    pid_file = tmp_path / "odin.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    with pytest.raises(RuntimeError, match="process is running"):
        apply_pending_restore(url, pid_file=pid_file)
    assert _marker(live) == "before"


def test_failed_post_replace_validation_cleans_pending_pair_and_restores_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"
    stage_restore(candidate, url, actor_id=1)
    paths = paths_from_database_url(url)
    original_insert = backup_service._insert_restore_audit

    def fail_completed(database: Path, action: str, details: dict[str, object]):
        if action == "restore_completed":
            raise RuntimeError("simulated migration/startup validation failure")
        return original_insert(database, action, details)

    monkeypatch.setattr(backup_service, "_insert_restore_audit", fail_completed)
    applied = apply_pending_restore(url, pid_file=None)
    assert applied and applied["status"] == "applied_pending_validation"
    with pytest.raises(RuntimeError, match="simulated"):
        finalize_pending_restore(url, pid_file=None)

    assert _marker(live) == "before"
    assert not paths.pending.exists()
    assert not paths.manifest.exists()
    connection = sqlite3.connect(live)
    try:
        actions = {row[0] for row in connection.execute("SELECT action FROM audit_logs")}
    finally:
        connection.close()
    assert {"restore_staged", "restore_failed"} <= actions


def test_startup_migration_failure_rolls_back_applied_restore(tmp_path: Path):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"
    stage_restore(candidate, url, actor_id=1)

    applied = apply_pending_restore(url, pid_file=None)
    assert applied and applied["status"] == "applied_pending_validation"
    assert _marker(live) == "after"

    # This is the entrypoint ERR-trap path after any schema migration fails.
    result = rollback_pending_restore(url, pid_file=None)

    assert result and result["status"] == "rolled_back"
    assert _marker(live) == "before"
    paths = paths_from_database_url(url)
    assert not paths.pending.exists()
    assert not paths.manifest.exists()
    connection = sqlite3.connect(live)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='restore_failed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_unclean_startup_rolls_back_before_retrying_migrations(tmp_path: Path):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"
    stage_restore(candidate, url)
    apply_pending_restore(url, pid_file=None)

    result = apply_pending_restore(url, pid_file=None)

    assert result and result["status"] == "rolled_back"
    assert result["reason"] == "unclean_startup_before_finalize"
    assert _marker(live) == "before"


def test_interruption_after_live_rename_recovers_from_pre_swap_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"
    stage_restore(candidate, url)
    paths = paths_from_database_url(url)
    original_secure = backup_service._secure

    def interrupt_after_live_rename(path: Path):
        if path.name.startswith("rollback_"):
            raise KeyboardInterrupt("simulated process interruption")
        return original_secure(path)

    monkeypatch.setattr(backup_service, "_secure", interrupt_after_live_rename)
    with pytest.raises(KeyboardInterrupt, match="simulated"):
        apply_pending_restore(url, pid_file=None)

    assert not live.exists()
    journal = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert journal["state"] == "prepared_swap"
    assert (paths.backups / journal["rollback_backup"]).exists()

    monkeypatch.setattr(backup_service, "_secure", original_secure)
    recovered = apply_pending_restore(url, pid_file=None)
    assert recovered and recovered["status"] == "rolled_back"
    assert recovered["reason"] == "unclean_startup_during_swap"
    assert _marker(live) == "before"
    assert not paths.pending.exists()
    assert not paths.manifest.exists()


@pytest.mark.parametrize("survivor", ["pending", "manifest"])
def test_incomplete_pending_pair_is_audited_and_cleaned(tmp_path: Path, survivor: str):
    live = tmp_path / "odin.db"
    candidate = tmp_path / "candidate.db"
    _database(live, "before")
    _database(candidate, "after")
    url = f"sqlite:///{live}"
    stage_restore(candidate, url)
    paths = paths_from_database_url(url)
    (paths.manifest if survivor == "pending" else paths.pending).unlink()

    with pytest.raises(BackupValidationError, match="must both exist"):
        apply_pending_restore(url, pid_file=None)

    assert not paths.pending.exists()
    assert not paths.manifest.exists()
    connection = sqlite3.connect(live)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action='restore_failed'"
        ).fetchone()[0] == 1
    finally:
        connection.close()
