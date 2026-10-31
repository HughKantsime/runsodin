"""The supervised guardian: schedule, alerts, /health, and that it never repairs anything."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.db_integrity import DAMAGED, OK, UNAVAILABLE, IntegrityReport
from modules.system import db_guardian, db_guardian_status, routes_health
from modules.system.backup_service import SourceDatabaseError
from modules.system.db_guardian import ALERT_TITLE, record_integrity, tick
from modules.system.db_guardian_status import (
    FAILED,
    HEALTHY,
    PENDING,
    STALE,
    integrity_view,
    read_status,
    status_path,
    write_status,
)
from tests.test_contracts.db_damage_support import overwrite_rightmost_leaf

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
DAMAGE = IntegrityReport(DAMAGED, 1.0, 2, 0, ("Tree 4 page 9: wrong page",))
FINE = IntegrityReport(OK, 1.0)
CANNOT = IntegrityReport(UNAVAILABLE, 5.0, problems=("SQLITE_BUSY",))


def _publisher():
    calls = []
    return calls, calls.append


def test_passing_check_is_recorded_without_an_alert():
    status, (calls, publish) = {}, _publisher()
    record_integrity(status, FINE, NOW, publish=publish)
    assert status["integrity"]["state"] == OK and status["integrity"]["checked_at"] == NOW.isoformat()
    assert calls == []


def test_damage_alerts_once_a_day_and_clears_only_on_a_pass():
    status, (calls, publish) = {}, _publisher()
    record_integrity(status, DAMAGE, NOW, publish=publish)
    record_integrity(status, DAMAGE, NOW + timedelta(minutes=30), publish=publish)
    assert calls == [DAMAGED] and status["integrity"]["state"] == DAMAGED
    record_integrity(status, DAMAGE, NOW + timedelta(days=1, seconds=1), publish=publish)
    assert calls == [DAMAGED, DAMAGED]
    record_integrity(status, FINE, NOW + timedelta(days=2), publish=publish)
    assert status["integrity"]["state"] == OK and status["integrity"]["alerted_at"] is None
    record_integrity(status, DAMAGE, NOW + timedelta(days=2, minutes=30), publish=publish)
    assert calls == [DAMAGED, DAMAGED, DAMAGED]


def test_an_alert_that_cannot_be_saved_keeps_health_failed_and_retries():
    status = {}
    def broken(_):
        raise sqlite3.DatabaseError("database disk image is malformed")
    record_integrity(status, DAMAGE, NOW, publish=broken)
    assert status["integrity"]["state"] == DAMAGED and status["integrity"]["alerted_at"] is None
    assert integrity_view(status, now=NOW)["status"] == FAILED
    calls, publish = _publisher()
    record_integrity(status, DAMAGE, NOW + timedelta(minutes=30), publish=publish)
    assert calls == [DAMAGED]


def test_one_check_that_cannot_finish_is_quiet_and_three_alert():
    status, (calls, publish) = {}, _publisher()
    for minutes in (0, 30):
        record_integrity(status, CANNOT, NOW + timedelta(minutes=minutes), publish=publish)
    assert calls == [] and status["integrity"]["consecutive_unavailable"] == 2
    record_integrity(status, CANNOT, NOW + timedelta(minutes=60), publish=publish)
    assert calls == [UNAVAILABLE]
    record_integrity(status, FINE, NOW + timedelta(minutes=90), publish=publish)
    assert status["integrity"]["consecutive_unavailable"] == 0


def test_a_check_that_cannot_finish_does_not_erase_known_damage():
    status, (calls, publish) = {}, _publisher()
    record_integrity(status, DAMAGE, NOW, publish=publish)
    record_integrity(status, CANNOT, NOW + timedelta(minutes=30), publish=publish)
    assert status["integrity"]["state"] == DAMAGED and status["integrity"]["checked_at"] == NOW.isoformat()
    assert integrity_view(status, now=NOW + timedelta(days=30))["status"] == FAILED


def _result(report=FINE):
    return {"filename": "odin_backup_x.db", "size_bytes": 10, "sha256": "a", "pruned": [], "source_report": report}


def _tick(status, now, *, check=None, create=None, publish=None, database=Path("/nonexistent/odin.db")):
    return tick(status, now, database, "sqlite:////nonexistent/odin.db", interval=1800,
                check=check or (lambda _: FINE), create=create or (lambda *a, **k: _result()),
                publish=publish or (lambda _: None))


def test_first_tick_backs_up_and_reuses_that_runs_check():
    status, checks = {}, []
    assert _tick(status, NOW, check=lambda _: checks.append(1) or FINE) is True
    assert checks == [] and status["backup"]["state"] == "ok"
    assert status["integrity"]["state"] == OK and status["interval_seconds"] == 1800


def test_nothing_runs_between_intervals_and_the_check_repeats_on_schedule():
    status, checks, backups = {}, [], []
    check = lambda _: checks.append(1) or FINE
    def create(*_, **__):
        backups.append(1)
        return _result()
    _tick(status, NOW, check=check, create=create)
    assert _tick(status, NOW + timedelta(seconds=1799), check=check, create=create) is False
    assert checks == [] and len(backups) == 1
    assert _tick(status, NOW + timedelta(seconds=1800), check=check, create=create) is True
    assert checks == [1] and len(backups) == 1


def test_backup_repeats_daily_and_a_failure_retries_hourly_not_every_tick():
    status, backups = {}, []
    def good(*_, **__):
        backups.append("good")
        return _result()
    def bad(*_, **__):
        backups.append("bad")
        raise OSError(28, "synthetic")
    _tick(status, NOW, create=good)
    moment = NOW + timedelta(days=1)
    _tick(status, moment, create=bad)
    assert backups == ["good", "bad"] and status["backup"]["state"] == "failed" and status["backup"]["reason"] == "disk_space"
    assert status["backup"]["last_success_at"] == NOW.isoformat()
    # The failed run recorded no integrity result, so the check runs on the next tick instead.
    _tick(status, moment + timedelta(seconds=15), create=bad)
    assert backups == ["good", "bad"] and status["integrity"]["checked_at"] == (moment + timedelta(seconds=15)).isoformat()
    _tick(status, moment + timedelta(minutes=30, seconds=15), create=bad)
    assert backups == ["good", "bad"]
    _tick(status, moment + timedelta(hours=1, seconds=15), create=good)
    assert backups == ["good", "bad", "good"] and status["backup"]["state"] == "ok"


def test_a_backup_refused_for_damage_records_the_damage_and_alerts():
    status, (calls, publish) = {}, _publisher()
    def refused(*_, **__):
        raise SourceDatabaseError(DAMAGE)
    _tick(status, NOW, create=refused, publish=publish)
    assert status["backup"]["state"] == "refused" and status["backup"]["reason"] == "source_integrity"
    assert status["integrity"]["state"] == DAMAGED and calls == [DAMAGED]
    assert "last_success_at" not in status["backup"]


def test_a_real_cycle_on_a_damaged_database_changes_nothing_on_disk(tmp_path):
    database = tmp_path / "odin.db"
    connection = sqlite3.connect(database)
    connection.executescript("CREATE TABLE printers(id INTEGER PRIMARY KEY, name TEXT);"
                             "CREATE TABLE audit_logs(id INTEGER PRIMARY KEY, action TEXT);")
    connection.executemany("INSERT INTO printers(name) VALUES(?)", [(f"p{n}",) for n in range(7)])
    connection.executemany("INSERT INTO audit_logs(action) VALUES(?)", [(f"a{n}",) for n in range(3000)])
    connection.commit()
    connection.close()
    overwrite_rightmost_leaf(database, "audit_logs", "printers")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    calls, publish = _publisher()
    status = {"backup": {"last_success_at": NOW.isoformat(), "last_attempt_at": NOW.isoformat()}}
    assert tick(status, NOW + timedelta(hours=1), database, f"sqlite:///{database}", publish=publish) is True
    assert status["integrity"]["state"] == DAMAGED and calls == [DAMAGED]
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before


def test_guardian_source_has_no_repair_path():
    source = (ROOT / "backend/modules/system/db_guardian.py").read_text(encoding="utf-8")
    for forbidden in ("REINDEX", "recover", "VACUUM;", "writable_schema", "DROP ", "DELETE FROM"):
        assert forbidden not in source


@pytest.fixture
def alert_db(tmp_path):
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    bootstrap_database(engine)
    with engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES"
                                "(1,'admin','x','admin',1,NULL),(2,'muted','x','admin',1,NULL),"
                                "(3,'viewer','x','viewer',1,NULL),(4,'off','x','admin',0,NULL)"))
    yield engine, sessionmaker(bind=engine)
    engine.dispose()


def _alert_rows(engine):
    with engine.connect() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        return connection.execute(text("SELECT user_id, severity, title, message, metadata_json FROM alerts ORDER BY user_id")).all()


def test_critical_alert_reaches_active_admins_through_the_normal_path(alert_db, monkeypatch):
    from modules.notifications import alert_dispatcher
    engine, factory = alert_db
    delivered = []
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: delivered.append(plan))
    db_guardian.publish_integrity_alert(DAMAGED, session_factory=factory)
    rows = _alert_rows(engine)
    assert [row[0] for row in rows] == [1, 2]
    assert all(str(row[1]).lower() == "critical" and row[2] == ALERT_TITLE for row in rows)
    assert "BACKUP_RESTORE_RUNBOOK" in rows[0][3] and "/data" not in rows[0][3]
    assert delivered and delivered[0] is not None and delivered[0].alerts_created == 2


def test_a_muted_preference_does_not_hide_this_alert(alert_db, monkeypatch):
    from modules.notifications import alert_dispatcher
    engine, factory = alert_db
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: None)
    with factory() as db:
        alert_dispatcher.seed_alert_preferences(db, 2)
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        db.execute(text("UPDATE alert_preferences SET in_app=0 WHERE user_id=2 AND lower(alert_type)='maintenance_overdue'"))
        db.commit()
    db_guardian.publish_integrity_alert(DAMAGED, session_factory=factory)
    assert [row[0] for row in _alert_rows(engine)] == [1, 2]


def test_status_file_round_trip_is_private_and_atomic(tmp_path):
    path = status_path(tmp_path / "odin.db")
    assert path == tmp_path / "run" / "db-guardian.json"
    assert read_status(path) == {}
    write_status(path, {"integrity": {"state": OK}})
    assert read_status(path) == {"integrity": {"state": OK}}
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert not path.with_name(path.name + ".tmp").exists()


@pytest.mark.parametrize("content", [b"{not json", b"[1,2]", b"\xff\xfe", b""])
def test_unreadable_status_counts_as_none_yet(tmp_path, content):
    path = status_path(tmp_path / "odin.db")
    path.parent.mkdir()
    path.write_bytes(content)
    assert read_status(path) == {}
    assert integrity_view(read_status(path), now=NOW)["status"] == PENDING


def _status(state, checked_at=NOW, interval=1800):
    return {"interval_seconds": interval, "started_at": NOW.isoformat(),
            "integrity": {"state": state, "checked_at": checked_at.isoformat()}}


@pytest.mark.parametrize("status,seconds,expected", [
    ({}, 0, PENDING),
    ({"interval_seconds": 1800, "started_at": NOW.isoformat()}, 3900, PENDING),
    ({"interval_seconds": 1800, "started_at": NOW.isoformat()}, 3901, STALE),
    (_status(OK), 0, HEALTHY),
    (_status(OK), 3900, HEALTHY),
    (_status(OK), 3901, STALE),
    (_status(UNAVAILABLE), 0, "unavailable"),
    (_status(UNAVAILABLE), 3901, STALE),
    (_status(DAMAGED), 0, FAILED),
    (_status(DAMAGED), 86400 * 365, FAILED),
    (_status("surprise"), 0, STALE),
    ({"integrity": {"state": OK, "checked_at": NOW.isoformat()}}, 0, STALE),
])
def test_health_view(status, seconds, expected):
    assert integrity_view(status, now=NOW + timedelta(seconds=seconds))["status"] == expected


def _health(monkeypatch, tmp_path, status=None, url=None):
    from core.config import settings
    monkeypatch.setattr(routes_health, "get_spoolman_url", lambda: "")
    database = tmp_path / "odin.db"
    monkeypatch.setattr(settings, "database_url", url or f"sqlite:///{database}")
    if status is not None:
        write_status(status_path(database), status)
    app = FastAPI()
    app.include_router(routes_health.router, prefix="/api")
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    return response.json()


def test_health_reports_a_passing_check_as_ok(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    body = _health(monkeypatch, tmp_path, _status(OK, checked_at=now))
    assert body["status"] == "ok"
    assert body["database_integrity"] == {"status": "ok", "checked_at": now.isoformat()}


def test_health_shows_damage_and_nothing_about_what_is_damaged(monkeypatch, tmp_path):
    now = datetime.now(timezone.utc)
    status = _status(DAMAGED, checked_at=now)
    status["integrity"]["problems"] = ["Tree 4 page 9"]
    body = _health(monkeypatch, tmp_path, status)
    assert body["status"] == "degraded" and body["database_integrity"]["status"] == "failed"
    assert set(body["database_integrity"]) == {"status", "checked_at"} and "Tree" not in json.dumps(body)


def test_health_is_ok_before_the_first_check_and_degraded_when_checks_stop(monkeypatch, tmp_path):
    assert _health(monkeypatch, tmp_path)["status"] == "ok"
    old = datetime.now(timezone.utc) - timedelta(days=2)
    body = _health(monkeypatch, tmp_path, _status(OK, checked_at=old))
    assert body["status"] == "degraded" and body["database_integrity"]["status"] == "stale"


def test_health_omits_the_field_on_postgresql(monkeypatch, tmp_path):
    body = _health(monkeypatch, tmp_path, url="postgresql://odin@postgres:5432/odin")
    assert body["status"] == "ok" and body["database_integrity"] is None


def test_daemon_is_supervised_in_the_single_container_only():
    conf = (ROOT / "docker/supervisord.conf").read_text(encoding="utf-8")
    assert "[program:db_guardian]\ncommand=python3 -m modules.system.db_guardian\n" in conf
    block = conf.split("[program:db_guardian]")[1]
    assert "autorestart=true" in block and 'ODIN_DB_ROLE="backup"' in block
    for name in ("supervisord.api.conf", "supervisord.monitors.conf", "supervisord.reports.conf", "supervisord.vision.conf"):
        assert "db_guardian" not in (ROOT / "docker" / name).read_text(encoding="utf-8")


def test_entrypoint_makes_the_status_directory_the_app_user_owns():
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    assert "/data/run" in entrypoint and "chown -R odin:odin /data" in entrypoint


def test_real_daemon_process_checks_backs_up_and_survives(tmp_path):
    """The supervised command, on a real bootstrapped database, with its delays shortened."""
    from core.schema import bootstrap_database
    database = tmp_path / "odin.db"
    engine = create_engine(f"sqlite:///{database}")
    bootstrap_database(engine)
    engine.dispose()
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{database}", LICENSE_DIR=str(tmp_path), API_KEY="",
               JWT_SECRET_KEY="db-safety-test-only", ODIN_ITAR_MODE="0", ODIN_TELEMETRY_V2="0", COOKIE_SECURE="false",
               ODIN_DB_ROLE="backup", PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}", PYTHONUNBUFFERED="1")
    # Same module, same main; only the two waits are shortened, then its own __main__ block runs.
    script = ("import runpy, time\n"
              "real = time.sleep\n"
              "time.sleep = lambda s: real(min(s, 0.2))\n"
              "runpy.run_module('modules.system.db_guardian', run_name='__main__')\n")
    process = subprocess.Popen([sys.executable, "-c", script], cwd=ROOT / "backend", env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    path = status_path(database)
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and read_status(path).get("backup", {}).get("state") != "ok":
            assert process.poll() is None, process.stdout.read()[-3000:]
            time.sleep(0.25)
        status = read_status(path)
        assert status["integrity"]["state"] == OK and status["backup"]["state"] == "ok", status
        assert integrity_view(status)["status"] == HEALTHY
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=20)
    assert (database.parent / "backups" / status["backup"]["filename"]).is_file()
