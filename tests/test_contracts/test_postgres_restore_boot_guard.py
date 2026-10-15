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


def _run(monkeypatch, base, error):
    from modules.system import postgres_backup_service as b
    from modules.system import restore_coordinator
    def apply(*args, **kwargs):
        raise error
    monkeypatch.setattr(restore_coordinator, "apply_pending_postgres_restore", apply)
    monkeypatch.setenv("ODIN_DATA_DIR", str(base))
    monkeypatch.setattr(restore_coordinator.restore_artifacts, "sweep_stale", lambda *a, **k: {"databases": [], "files": []})
    monkeypatch.setattr(b, "_insert_postgres_restore_audit", lambda *a, **k: None)
    monkeypatch.setenv("DATABASE_MAINTENANCE_URL", "postgresql://m@pg/postgres")
    monkeypatch.setattr(sys, "argv", ["restore_coordinator", "--database-url", "postgresql://o@pg/odin"])
    return restore_coordinator.main()


def test_staged_restore_that_fails_is_quarantined_and_the_next_boot_starts(tmp_path, monkeypatch, capsys):
    from modules.system.backup_service import BackupValidationError
    paths = _stage(tmp_path)
    assert _run(monkeypatch, tmp_path, BackupValidationError("Preserved029 credential is unsafe")) == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["status"] == "restore_failed_target_untouched" and "credential is unsafe" in out["reason"]
    assert not paths.pending.exists() and not paths.manifest.exists()
    kept = sorted(paths.backups.glob("failed_restore_*"))
    assert [p.suffix for p in kept] == [".dump", ".json"]
    record = json.loads(kept[1].read_text())
    assert record["candidate_sha256"] == "a" * 64 and "re-stage" in record["action"].lower()
    # Next boot: nothing pending, so the coordinator is a no-op instead of failing again.
    from modules.system import postgres_backup_service as b
    assert b.apply_pending_postgres_restore("postgresql://o@pg/odin", "postgresql://m@pg/postgres", acknowledgement=None,
                                            password_file="/a", maintenance_password_file="/b", base_dir=tmp_path) is None


def test_failure_after_the_target_was_touched_still_fails_loudly(tmp_path, monkeypatch):
    from modules.system.backup_service import BackupValidationError
    paths = _stage(tmp_path, state="restore_in_progress")
    with pytest.raises(BackupValidationError, match="recovery incomplete"):
        _run(monkeypatch, tmp_path, BackupValidationError("PostgreSQL recovery incomplete; offline barrier retained in this process"))
    assert paths.pending.exists() and paths.manifest.exists()


def test_entrypoint_runs_the_postgres_coordinator_as_the_app_user():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    branch = entrypoint[entrypoint.index("postgresql://*|postgres://*)\n            "):]
    branch = branch[:branch.index(";;")]
    assert re.search(r"/usr/sbin/runuser -u odin -- python3 -m modules\.system\.restore_coordinator", branch), branch
