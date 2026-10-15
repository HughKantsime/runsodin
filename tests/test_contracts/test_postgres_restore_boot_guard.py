"""A staged PostgreSQL restore that cannot apply must not restart the container forever.

The container entrypoint runs restore_coordinator on every start; set -e turns a raised
error into an exit and the orchestrator restarts it. A failure while the restore is still
'staged' has not touched the target, so it is moved aside with an actionable reason and
startup continues on the untouched target. A failure after the target was touched keeps
failing loudly (the offline barrier/rollback rules are unchanged).
"""
import json
from pathlib import Path
import re
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _stage(base: Path, state="staged"):
    from modules.system import postgres_backup_service as b
    paths = b.postgres_paths(base); paths.backups.mkdir(parents=True)
    paths.pending.write_bytes(b"x" * 200)
    manifest = {"schema_version": 1, "dialect": "postgresql", "state": state, "sha256": "a" * 64, "size_bytes": 200,
                "actor_id": 1, "staged_at": "2026-10-15T00:00:00+00:00"}
    paths.manifest.write_text(json.dumps(manifest))
    return paths


def _run(monkeypatch, base, *errors, result=None):
    """Coordinator main() with apply raising ``errors`` in turn, then returning ``result``."""
    from modules.system import postgres_backup_service as b
    from modules.system import restore_coordinator
    calls = []
    def apply(*args, **kwargs):
        calls.append(1)
        if len(calls) <= len(errors):
            raise errors[len(calls) - 1]
        return result
    monkeypatch.setattr(restore_coordinator, "apply_pending_postgres_restore", apply)
    monkeypatch.setattr(restore_coordinator.restore_artifacts, "sweep_stale", lambda *a, **k: {"databases": [], "files": []})
    monkeypatch.setattr(restore_coordinator.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(b, "_insert_postgres_restore_audit", lambda *a, **k: None)
    monkeypatch.setattr(b, "_notify_superadmins_of_quarantine", lambda *a, **k: None)
    monkeypatch.setenv("ODIN_DATA_DIR", str(base))
    monkeypatch.setenv("DATABASE_MAINTENANCE_URL", "postgresql://m@pg/postgres")
    monkeypatch.setattr(sys, "argv", ["restore_coordinator", "--database-url", "postgresql://o@pg/odin"])
    return restore_coordinator.main(), calls


def test_deterministic_archive_refusal_is_quarantined_and_the_next_boot_starts(tmp_path, monkeypatch, capsys):
    from modules.system.postgres_backup_service import ArchiveRefused
    paths = _stage(tmp_path)
    code, calls = _run(monkeypatch, tmp_path, ArchiveRefused("Unsupported PostgreSQL archive schema version or partial table inventory"))
    assert code == 0 and len(calls) == 1
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "restore_failed_target_untouched" and "Unsupported" in out["reason"]
    assert not paths.pending.exists() and not paths.manifest.exists()
    kept = sorted(paths.backups.glob("failed_restore_*"))
    assert [p.suffix for p in kept] == [".dump", ".json"]
    record = json.loads(kept[1].read_text())
    assert record["candidate_sha256"] == "a" * 64 and "re-stage" in record["action"].lower()
    from modules.system import postgres_backup_service as b
    assert b.apply_pending_postgres_restore("postgresql://o@pg/odin", "postgresql://m@pg/postgres", acknowledgement=None,
                                            password_file="/a", maintenance_password_file="/b", base_dir=tmp_path) is None
    assert b.quarantined_restores(tmp_path)[0]["archive"] == kept[0].name


@pytest.mark.parametrize("make", [
    lambda: __import__("modules.system.postgres_backup_service", fromlist=["x"]).RestoreAcknowledgementRequired("PostgreSQL restore acknowledgement must exactly match the staged SHA-256"),
    lambda: __import__("modules.system.postgres_backup_service", fromlist=["x"]).RestoreEnvironmentError("PostgreSQL password file is not readable"),
    lambda: __import__("core.postgres_restore_gate", fromlist=["x"]).RestoreConsumerBlocked("PostgreSQL restore state is unavailable or requires recovery"),
    lambda: __import__("modules.system.backup_service", fromlist=["x"]).BackupValidationError("PostgreSQL recovery incomplete; offline barrier retained in this process"),
])
def test_operator_and_environment_errors_keep_the_staged_restore_and_fail(tmp_path, monkeypatch, make):
    paths = _stage(tmp_path)
    error = make()
    with pytest.raises(type(error)):
        _run(monkeypatch, tmp_path, error)
    assert paths.pending.exists() and paths.manifest.exists() and not list(paths.backups.glob("failed_restore_*"))


def test_transient_errors_are_retried_with_bounded_backoff_then_apply(tmp_path, monkeypatch, capsys):
    from sqlalchemy.exc import OperationalError
    from modules.system.postgres_backup_service import PostgreSQLClientFailed
    paths = _stage(tmp_path)
    transient = [OperationalError("SELECT 1", {}, Exception("could not translate host name")),
                 PostgreSQLClientFailed("PostgreSQL client operation failed: pg_dump", connects=True)]
    code, calls = _run(monkeypatch, tmp_path, *transient, result={"status": "restored", "candidate_sha256": "a" * 64})
    assert code == 0 and len(calls) == 3
    assert "retrying" in capsys.readouterr().err


def test_transient_errors_that_persist_keep_the_staged_restore_and_fail(tmp_path, monkeypatch):
    from sqlalchemy.exc import OperationalError
    from modules.system import restore_coordinator
    paths = _stage(tmp_path)
    errors = [OperationalError("SELECT 1", {}, Exception("server not ready"))] * (len(restore_coordinator.RETRY_DELAYS) + 1)
    with pytest.raises(OperationalError):
        _run(monkeypatch, tmp_path, *errors)
    assert paths.pending.exists() and paths.manifest.exists()


def test_failure_after_the_target_was_touched_is_never_quarantined(tmp_path, monkeypatch):
    from modules.system.postgres_backup_service import ArchiveRefused
    paths = _stage(tmp_path, state="restore_in_progress")
    with pytest.raises(ArchiveRefused):
        _run(monkeypatch, tmp_path, ArchiveRefused("late"))
    assert paths.pending.exists() and paths.manifest.exists()


def test_archive_phase_classifies_only_archive_judgements():
    from core.schema.migrator import MigrationError
    from modules.system import postgres_backup_service as b
    assert isinstance(b._as_archive_refusal(b.BackupValidationError("Archive schema differs")), b.ArchiveRefused)
    assert isinstance(b._as_archive_refusal(MigrationError("Migration history mismatch: x")), b.ArchiveRefused)
    assert isinstance(b._as_archive_refusal(b.PostgreSQLClientFailed("pg_restore", connects=False)), b.ArchiveRefused)
    for kept in (b.PostgreSQLClientFailed("pg_dump", connects=True), b.RestoreEnvironmentError("x"),
                 b.RestoreAcknowledgementRequired("x"), ConnectionError("x")):
        assert b._as_archive_refusal(kept) is kept


def test_entrypoint_runs_the_postgres_coordinator_as_the_app_user():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    branch = entrypoint[entrypoint.index("postgresql://*|postgres://*)\n            "):]
    branch = branch[:branch.index(";;")]
    assert re.search(r"/usr/sbin/runuser -u odin -- python3 -m modules\.system\.restore_coordinator", branch), branch
