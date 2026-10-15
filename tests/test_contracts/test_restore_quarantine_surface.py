"""A quarantined PostgreSQL restore is visible and actionable for the superadmin.

Backups list (with reason and action), re-stage and delete from the list, diagnostics,
a count-only readiness notice, a persistent in-app alert, and retention.
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
    alerts = []
    monkeypatch.setattr(b, "_notify_superadmins_of_quarantine", lambda url, pw, record: alerts.append(record))
    record = b.quarantine_failed_pending_postgres_restore("postgresql://o@pg/odin", error=b.ArchiveRefused("Archive schema differs"))
    monkeypatch.setattr(routes_backup, "sql", __import__("types").SimpleNamespace(is_postgres=True))
    return paths, record, alerts


def test_backups_list_shows_the_quarantined_archive_with_reason(quarantined):
    paths, record, alerts = quarantined
    listed = routes_backup.list_backups(current_user={"id": 1})
    assert listed[0]["filename"] == record["archive"] and listed[0]["quarantined"] is True
    assert listed[0]["reason"] == "Archive schema differs" and "re-stage" in listed[0]["action"].lower()
    assert alerts == [record]


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
    for path in paths.backups.glob("failed_restore_*"):
        os.utime(path, (now - 31 * 86400, now - 31 * 86400))
    removed = restore_artifacts.sweep_stale(paths.backups, "unused", None, drop=lambda names: [])
    assert sorted(removed["files"]) == sorted(p for p in (record["archive"], Path(record["archive"]).with_suffix(".json").name))
    assert b.quarantined_restores() == []


def test_superadmins_receive_a_persistent_in_app_alert(tmp_path, monkeypatch):
    from core.schema import bootstrap_database
    db = tmp_path / "alerts.db"
    engine = create_engine(f"sqlite:///{db}")
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'root','x','admin',1,NULL),"
                       "(2,'orgadmin','x','admin',1,5),(3,'viewer','x','viewer',1,NULL),(4,'gone','x','admin',0,NULL)"))
    engine.dispose()
    record = {"reason": "Archive schema differs", "action": "Re-stage it.", "archive": "failed_restore_20261015_000000_000000.dump",
              "candidate_sha256": "a" * 64, "failed_at": "2026-10-15T00:00:00+00:00"}
    b._notify_superadmins_of_quarantine(f"sqlite:///{db}", None, record)
    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as c:
        rows = c.execute(text("SELECT user_id,severity,title,is_read,metadata_json FROM alerts")).all()
    engine.dispose()
    assert [(r[0], r[1], r[2], r[3]) for r in rows] == [(1, "critical", "Database restore was not applied", 0)]
    assert json.loads(rows[0][4])["kind"] == "restore_quarantined"
