"""A quarantined PostgreSQL restore is visible and actionable for the superadmin.

Backups list (with reason and action), re-stage and delete from the list, diagnostics,
a count-only readiness notice, the superadmin system-notices endpoint (which clears
when the archive is re-staged or deleted), and retention. No in-app alert is written:
alert types are routed by the notification rules.
"""
import json
import os
from pathlib import Path
import time

import pytest
from sqlalchemy import create_engine, text

from modules.system import postgres_backup_service as b
from modules.system import restore_artifacts
from modules.system import routes_backup


@pytest.fixture
def quarantined(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    paths = b.postgres_paths(tmp_path); paths.backups.mkdir(parents=True)
    paths.pending.write_bytes(b"x" * 200)
    paths.manifest.write_text(json.dumps({"schema_version": 1, "dialect": "postgresql", "state": "staged", "sha256": "a" * 64,
                                          "size_bytes": 200, "actor_id": 1, "staged_at": "2026-10-15T00:00:00+00:00"}))
    monkeypatch.setattr(b, "_insert_postgres_restore_audit", lambda *a, **k: None)
    record = b.quarantine_failed_pending_postgres_restore("postgresql://o@pg/odin", error=b.ArchiveRefused("Archive schema differs"))
    monkeypatch.setattr(routes_backup, "sql", __import__("types").SimpleNamespace(is_postgres=True))
    return paths, record, []


def test_backups_list_shows_the_quarantined_archive_with_reason(quarantined):
    paths, record, alerts = quarantined
    listed = routes_backup.list_backups(current_user={"id": 1})
    assert listed[0]["filename"] == record["archive"] and listed[0]["quarantined"] is True
    assert listed[0]["reason"] == "Archive schema differs" and "re-stage" in listed[0]["action"].lower()


def test_quarantined_archive_can_be_staged_again_from_the_list(quarantined, monkeypatch):
    paths, record, _ = quarantined
    staged = []
    monkeypatch.setattr(routes_backup, "_postgres_maintenance_config", lambda: ("postgresql://m@pg/postgres", "/a", "/b"))
    monkeypatch.setattr(routes_backup, "stage_postgres_restore", lambda path, *a, **k: staged.append(path) or {"candidate_sha256": "a" * 64})
    monkeypatch.setattr(routes_backup, "log_audit", lambda *a, **k: None)
    class DB:
        def commit(self): pass
    result = routes_backup.stage_existing_backup(record["archive"], current_user={"id": 1}, db=DB())
    assert result["status"] == "staged" and staged == [paths.backups / record["archive"]]
    assert not list(paths.backups.glob("failed_restore_*"))


def test_quarantined_archive_delete_removes_its_record(quarantined, monkeypatch):
    paths, record, _ = quarantined
    monkeypatch.setattr(routes_backup, "log_audit", lambda *a, **k: None)
    class DB:
        def commit(self): pass
    routes_backup.delete_backup(record["archive"], current_user={"id": 1}, db=DB())
    assert not list(paths.backups.glob("failed_restore_*"))


def test_list_paths_refuse_other_names(quarantined):
    from fastapi import HTTPException
    for name in ("../odin.db", "failed_restore_x.json", "restore-pending.dump"):
        with pytest.raises(HTTPException):
            routes_backup._safe_backup_path(name)


def test_diagnostics_and_readiness_surface_it(quarantined):
    paths, record, _ = quarantined
    entries = b.quarantined_restores()
    assert entries[0]["archive"] == record["archive"] and entries[0]["reason"] == "Archive schema differs"


def test_quarantined_restores_are_retained_30_days(quarantined):
    paths, record, _ = quarantined
    now = time.time()
    record_path = paths.backups / Path(record["archive"]).with_suffix(".json").name
    data = json.loads(record_path.read_text())
    from datetime import datetime, timezone
    data["failed_at"] = datetime.fromtimestamp(now - 31 * 86400, tz=timezone.utc).isoformat()
    record_path.write_text(json.dumps(data))
    removed = restore_artifacts.sweep_stale(paths.backups, "unused", None, drop=lambda names: [])
    assert sorted(removed["files"]) == sorted(p for p in (record["archive"], Path(record["archive"]).with_suffix(".json").name))
    assert b.quarantined_restores() == []


def test_system_notice_is_active_until_the_archive_is_restaged(quarantined, monkeypatch):
    paths, record, _ = quarantined
    notices = routes_backup.system_notices(current_user={"id": 1})["notices"]
    assert [(n["kind"], n["archive"], n["link"]) for n in notices] == [("restore_quarantined", record["archive"], "/settings?tab=system#backups")]
    assert "Archive schema differs" in notices[0]["message"] and notices[0]["severity"] == "critical"
    monkeypatch.setattr(routes_backup, "_postgres_maintenance_config", lambda: ("postgresql://m@pg/postgres", "/a", "/b"))
    monkeypatch.setattr(routes_backup, "stage_postgres_restore", lambda path, *a, **k: {"candidate_sha256": "a" * 64})
    monkeypatch.setattr(routes_backup, "log_audit", lambda *a, **k: None)
    class DB:
        def commit(self): pass
    routes_backup.stage_existing_backup(record["archive"], current_user={"id": 1}, db=DB())
    assert routes_backup.system_notices(current_user={"id": 1}) == {"notices": []}


def test_system_notice_clears_when_the_archive_is_deleted(quarantined, monkeypatch):
    paths, record, _ = quarantined
    monkeypatch.setattr(routes_backup, "log_audit", lambda *a, **k: None)
    class DB:
        def commit(self): pass
    assert routes_backup.system_notices(current_user={"id": 1})["notices"]
    routes_backup.delete_backup(record["archive"], current_user={"id": 1}, db=DB())
    assert routes_backup.system_notices(current_user={"id": 1}) == {"notices": []}


def test_system_notices_are_empty_on_sqlite_and_write_no_alerts(quarantined, monkeypatch):
    monkeypatch.setattr(routes_backup, "sql", __import__("types").SimpleNamespace(is_postgres=False))
    assert routes_backup.active_system_notices() == []
    import inspect
    assert "INSERT INTO alerts" not in inspect.getsource(b)


def test_system_notices_endpoint_requires_a_superadmin():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.dependencies import get_current_user
    app = FastAPI(); app.include_router(routes_backup.router)
    with TestClient(app) as client:
        assert client.get("/system/notices").status_code in (401, 403)
        for principal in ({"id": 2, "role": "admin", "group_id": 5}, {"id": 3, "role": "viewer", "group_id": None}):
            app.dependency_overrides[get_current_user] = lambda principal=principal: principal
            assert client.get("/system/notices").status_code == 403, principal
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "role": "admin", "group_id": None}
        response = client.get("/system/notices")
        assert response.status_code == 200 and "notices" in response.json()


def test_retention_ages_from_the_quarantine_not_the_staging_time(tmp_path, monkeypatch):
    """A restore staged long ago but quarantined today is kept the full 30 days."""
    monkeypatch.setenv("ODIN_DATA_DIR", str(tmp_path))
    paths = b.postgres_paths(tmp_path); paths.backups.mkdir(parents=True)
    paths.pending.write_bytes(b"x" * 200)
    old = time.time() - 40 * 86400
    os.utime(paths.pending, (old, old))
    paths.manifest.write_text(json.dumps({"schema_version": 1, "dialect": "postgresql", "state": "staged", "sha256": "a" * 64,
                                          "size_bytes": 200, "actor_id": 1, "staged_at": "2026-09-01T00:00:00+00:00"}))
    monkeypatch.setattr(b, "_insert_postgres_restore_audit", lambda *a, **k: None)
    record = b.quarantine_failed_pending_postgres_restore("postgresql://o@pg/odin", error=b.ArchiveRefused("x"))
    assert restore_artifacts.sweep_stale(paths.backups, "unused", None, drop=lambda names: [])["files"] == []
    assert b.quarantined_restores()[0]["archive"] == record["archive"]
    # ...and its record's failed_at governs even if the files' times are touched.
    for path in paths.backups.glob("failed_restore_*"):
        os.utime(path, (old, old))
    assert restore_artifacts.sweep_stale(paths.backups, "unused", None, drop=lambda names: [])["files"] == []
    assert b.quarantined_restores()[0]["archive"] == record["archive"]


def test_orphan_quarantine_records_are_swept_and_never_counted(quarantined, monkeypatch):
    paths, record, _ = quarantined
    (paths.backups / record["archive"]).unlink()  # the archive is gone, only the JSON record is left
    assert b.quarantined_restores()[0]["archive"] is None
    assert routes_backup.active_system_notices() == []
    assert b.quarantined_restore_count("postgresql") == 0
    removed = restore_artifacts.sweep_stale(paths.backups, "unused", None, drop=lambda names: [])
    assert removed["files"] == [Path(record["archive"]).with_suffix(".json").name]
    assert b.quarantined_restores() == []


def test_restore_staging_routes_are_marked_for_the_interactive_session_guard():
    """require_interactive_session lives on cert-fix-F; the merge must add it to these routes."""
    marked = routes_backup.BACKUP_ROUTES_REQUIRING_INTERACTIVE
    assert ("POST", "/backups/{filename}/stage") in marked and ("POST", "/backups/restore") in marked
    registered = {(method, route.path) for route in routes_backup.router.routes for method in getattr(route, "methods", ())}
    assert set(marked) <= registered


def test_sweep_removes_stale_rollback_dumps_but_keeps_the_newest_and_any_in_use(tmp_path):
    backups = tmp_path / "backups"; backups.mkdir()
    now = time.time()
    names = [f"rollback_20261001_00000{i}_000000.dump" for i in range(4)]
    for age, name in zip((40, 30, 20, 10), names):
        path = backups / name; path.write_bytes(b"x" * 200); os.utime(path, (now - age * 86400, now - age * 86400))
    (tmp_path / "restore-pending-postgres.json").write_text(json.dumps({"state": "restore_in_progress", "rollback_backup": names[1]}))
    removed = restore_artifacts.sweep_stale(backups, "unused", None, drop=lambda n: [])
    assert sorted(removed["files"]) == sorted([names[0], names[2]])  # names[1] is in use, names[3] is the newest
    assert sorted(p.name for p in backups.glob("rollback_*")) == sorted([names[1], names[3]])
    (tmp_path / "restore-pending-postgres.json").write_text("{not json")
    for name in names[1:]:
        if (backups / name).exists():
            os.utime(backups / name, (now - 50 * 86400, now - 50 * 86400))
    assert restore_artifacts.sweep_stale(backups, "unused", None, drop=lambda n: [])["files"] == []
