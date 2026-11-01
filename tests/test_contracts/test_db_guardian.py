"""The supervised guardian: schedule, alerts, /health, and that it never repairs anything."""
import hashlib
import json
import os
import re
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
from modules.system import backup_failure_alerts, db_guardian, db_guardian_status, routes_health
from modules.system.backup_service import SourceDatabaseError
from modules.system.db_guardian import missed_backups, record_integrity, tick
from modules.system.db_guardian_status import (
    ALERT_TITLE,
    FAILED,
    HEALTHY,
    PENDING,
    STALE,
    alert_if_stopped,
    backup_view,
    integrity_view,
    offsite_path,
    parse_time,
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


@pytest.fixture(autouse=True)
def saved_backup_alerts(monkeypatch):
    seen = []
    monkeypatch.setattr(backup_failure_alerts, "publish_backup_failure", seen.append)
    return seen


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
    assert status["integrity"]["state"] == OK and status["integrity"]["alerted"] == {}
    record_integrity(status, DAMAGE, NOW + timedelta(days=2, minutes=30), publish=publish)
    assert calls == [DAMAGED, DAMAGED, DAMAGED]


def test_an_alert_that_cannot_be_saved_keeps_health_failed_and_retries():
    status = {}
    def broken(_):
        raise sqlite3.DatabaseError("database disk image is malformed")
    record_integrity(status, DAMAGE, NOW, publish=broken)
    assert status["integrity"]["state"] == DAMAGED and status["integrity"]["alerted"] == {}
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


def test_a_may_be_fine_alert_never_holds_back_real_damage():
    """The review's reproduction: three checks that can't finish, then damage within the same day."""
    status, (calls, publish) = {}, _publisher()
    for minutes in (0, 5, 10):
        record_integrity(status, CANNOT, NOW + timedelta(minutes=minutes), publish=publish)
    assert calls == [UNAVAILABLE]
    record_integrity(status, DAMAGE, NOW + timedelta(minutes=15), publish=publish)
    assert calls == [UNAVAILABLE, DAMAGED]
    for minutes in (20, 60, 600, 1430):
        record_integrity(status, DAMAGE, NOW + timedelta(minutes=minutes), publish=publish)
    assert calls == [UNAVAILABLE, DAMAGED], "each state keeps its own daily limit"
    record_integrity(status, DAMAGE, NOW + timedelta(minutes=15, days=1), publish=publish)
    assert calls == [UNAVAILABLE, DAMAGED, DAMAGED]


def test_damage_does_not_reset_the_other_states_daily_limit():
    status, (calls, publish) = {}, _publisher()
    for minutes in (0, 5, 10):
        record_integrity(status, CANNOT, NOW + timedelta(minutes=minutes), publish=publish)
    record_integrity(status, DAMAGE, NOW + timedelta(minutes=15), publish=publish)
    assert sorted(status["integrity"]["alerted"]) == [DAMAGED, UNAVAILABLE]


def test_with_damage_known_a_check_that_cannot_finish_waits_for_the_interval():
    """It used to retry every tick, because checked_at stays on the last real result."""
    status, checks = {}, []
    record_integrity(status, DAMAGE, NOW, publish=lambda _: None)
    def check(_):
        checks.append(1)
        return CANNOT
    status["backup"] = {"last_success_at": NOW.isoformat(), "last_attempt_at": NOW.isoformat()}
    _tick(status, NOW + timedelta(seconds=1800), check=check)
    assert checks == [1] and status["integrity"]["checked_at"] == NOW.isoformat()
    assert _tick(status, NOW + timedelta(seconds=1815), check=check) is False
    assert checks == [1]
    assert _tick(status, NOW + timedelta(seconds=3600), check=check) is True
    assert checks == [1, 1]


def test_the_attempt_is_saved_before_the_backup_starts():
    """A run killed halfway must not start again straight after the restart."""
    status, saved = {}, []
    def create(*_, **__):
        assert saved, "nothing was saved before the backup started"
        return _result()
    tick(status, NOW, Path("/nonexistent/odin.db"), "sqlite:////nonexistent/odin.db", interval=300, check=lambda _: FINE,
         create=create, publish=lambda _: None, save=lambda current: saved.append(json.loads(json.dumps(current))))
    assert saved[0]["backup"]["last_attempt_at"] == NOW.isoformat()
    assert "last_success_at" not in saved[0]["backup"]


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


def test_a_failed_status_write_keeps_the_old_file_and_leaves_no_temporary(tmp_path, monkeypatch):
    path = status_path(tmp_path / "odin.db")
    write_status(path, {"integrity": {"state": OK}})
    def full(*_):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(db_guardian_status.os, "replace", full)
    with pytest.raises(OSError):
        write_status(path, {"integrity": {"state": DAMAGED}})
    assert read_status(path) == {"integrity": {"state": OK}}
    assert not path.with_name(path.name + ".tmp").exists()


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
    ({**_status(UNAVAILABLE), "integrity": {**_status(UNAVAILABLE)["integrity"], "consecutive_unavailable": 1}}, 0, "retrying"),
    ({**_status(UNAVAILABLE), "integrity": {**_status(UNAVAILABLE)["integrity"], "consecutive_unavailable": 2}}, 0, "retrying"),
    ({**_status(UNAVAILABLE), "integrity": {**_status(UNAVAILABLE)["integrity"], "consecutive_unavailable": 3}}, 0, "unavailable"),
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


@pytest.mark.parametrize("misses,overall", [(1, "ok"), (2, "ok"), (3, "degraded")])
def test_one_check_that_cannot_finish_does_not_fail_a_deploys_health_window(monkeypatch, tmp_path, misses, overall):
    """The promote workflow requires status == "ok". A passing lock must not fail it."""
    status = _status(UNAVAILABLE, checked_at=datetime.now(timezone.utc))
    status["integrity"]["consecutive_unavailable"] = misses
    assert _health(monkeypatch, tmp_path, status)["status"] == overall


def test_health_omits_the_field_on_postgresql(monkeypatch, tmp_path):
    body = _health(monkeypatch, tmp_path, url="postgresql://odin@postgres:5432/odin")
    assert body["status"] == "ok" and body["database_integrity"] is None


def test_measured_interval_and_priority_are_pinned():
    assert db_guardian.INTEGRITY_INTERVAL_SECONDS == 300
    assert db_guardian.NICE_INCREMENT == 10


def test_daemon_is_supervised_in_the_single_container_only():
    conf = (ROOT / "docker/supervisord.conf").read_text(encoding="utf-8")
    assert "[program:db_guardian]\ncommand=python3 -m modules.system.db_guardian\n" in conf
    block = conf.split("[program:db_guardian]")[1]
    assert "autorestart=true" in block and 'ODIN_DB_ROLE="backup"' in block
    # Any startup crash backs off, instead of burning CPU on a host that is already in trouble.
    assert "startsecs=10" in block and "startretries=30" in block
    for name in ("supervisord.api.conf", "supervisord.monitors.conf", "supervisord.reports.conf", "supervisord.vision.conf"):
        assert "db_guardian" not in (ROOT / "docker" / name).read_text(encoding="utf-8")


def test_entrypoint_makes_the_status_directory_the_app_user_owns():
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    assert "/data/run" in entrypoint and "chown -R odin:odin /data" in entrypoint


def test_entrypoint_writes_a_first_status_so_a_guardian_that_never_starts_goes_stale(tmp_path):
    """Without it, a guardian that dies before its first write looks "pending" forever."""
    entrypoint = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    block = entrypoint[entrypoint.index('guardian_status="'):entrypoint.index("exec \"$@\"")]
    assert 'if [ ! -e "${guardian_status}" ]' in block and "/usr/sbin/runuser -u odin" in block
    assert f'\\"interval_seconds\\": {db_guardian.INTEGRITY_INTERVAL_SECONDS}' in block
    # Runs the line as written, on a temporary directory.
    command = re.search(r"runuser -u odin -- /bin/sh -c '(.*?)' sh ", block).group(1)
    path = status_path(tmp_path / "odin.db")
    subprocess.run(["/bin/sh", "-c", command, "sh", str(path)], check=True, timeout=30)
    status = read_status(path)
    assert status["interval_seconds"] == 300 and oct(path.stat().st_mode & 0o777) == "0o600"
    started = parse_time(status["started_at"])
    assert integrity_view(status, now=started + timedelta(seconds=899))["status"] == PENDING
    assert integrity_view(status, now=started + timedelta(seconds=901))["status"] == STALE


def test_guardian_that_never_wrote_goes_stale_and_the_watcher_reports_it(alert_db, monkeypatch):
    from modules.notifications import alert_dispatcher
    engine, factory = alert_db
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: None)
    write_status(status_path(Path(engine.url.database)), {
        "started_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "interval_seconds": 300})
    assert alert_if_stopped(str(engine.url), session_factory=factory) is True
    assert [row[0] for row in _alert_rows(engine)] == [1, 2]


def test_publish_checks_the_guardian_and_the_library_on_both_architectures():
    workflow = (ROOT / ".github/workflows/publish-image.yml").read_text(encoding="utf-8")
    loop = workflow[workflow.index("for arch in amd64 arm64; do"):]
    loop = loop[:loop.index("            docker rm -f \"$name\" >/dev/null\n          done")]
    assert "python3 -I -S /usr/local/libexec/odin-sqlite-image-check.py" in loop
    assert '["database_integrity"]["status"]' in loop and 'test "$guardian" = ok' in loop


def test_a_recent_damage_alert_does_not_stand_in_for_a_stopped_guardian(alert_db, monkeypatch):
    from modules.notifications import alert_dispatcher
    engine, factory = alert_db
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: None)
    db_guardian.publish_integrity_alert(DAMAGED, session_factory=factory)
    _stale_status(Path(engine.url.database))
    assert alert_if_stopped(str(engine.url), session_factory=factory) is True
    states = [json.loads(row[4])["state"] if isinstance(row[4], str) else row[4]["state"] for row in _alert_rows(engine)]
    assert states.count(DAMAGED) == 2 and states.count(UNAVAILABLE) == 2


# --- The guardian keeps working when it can't write its status.

class _Stop(BaseException):
    pass


def _run_main_loop(monkeypatch, tmp_path, ticks=2):
    database = tmp_path / "odin.db"
    monkeypatch.setattr(db_guardian, "DATABASE_URL", f"sqlite:///{database}")
    niced = []
    monkeypatch.setattr(db_guardian.os, "nice", niced.append)
    seen = []
    def fake_tick(status, now, *a, **k):
        seen.append(1)
        status["integrity"] = {"state": OK, "checked_at": now.isoformat()}
        if len(seen) >= ticks:
            raise _Stop
        return True
    monkeypatch.setattr(db_guardian, "tick", fake_tick)
    monkeypatch.setattr(db_guardian.time, "sleep", lambda _: None)
    with pytest.raises(_Stop):
        db_guardian.main_loop()
    return seen, niced


def test_the_loop_runs_at_low_priority(monkeypatch, tmp_path):
    _, niced = _run_main_loop(monkeypatch, tmp_path)
    assert niced == [10]


def test_an_unwritable_status_directory_does_not_stop_the_loop(monkeypatch, tmp_path):
    (tmp_path / "run").mkdir()
    (tmp_path / "run").chmod(0o500)
    try:
        seen, _ = _run_main_loop(monkeypatch, tmp_path)
    finally:
        (tmp_path / "run").chmod(0o700)
    assert len(seen) == 2 and list((tmp_path / "run").iterdir()) == []


def test_a_full_disk_does_not_stop_the_loop_or_leave_a_temporary(monkeypatch, tmp_path):
    original = Path.write_text
    def full(self, *a, **k):
        if self.name.endswith(".tmp"):
            original(self, "half")
            raise OSError(28, "No space left on device")
        return original(self, *a, **k)
    monkeypatch.setattr(Path, "write_text", full)
    seen, _ = _run_main_loop(monkeypatch, tmp_path)
    assert len(seen) == 2 and list((tmp_path / "run").glob("*.tmp")) == []


def test_leftovers_that_cannot_be_cleared_do_not_stop_the_guardian(monkeypatch, tmp_path):
    def refuse(_):
        raise OSError(13, "Permission denied")
    monkeypatch.setattr(db_guardian, "clear_leftovers", refuse)
    assert len(_run_main_loop(monkeypatch, tmp_path)[0]) == 2


def test_a_real_guardian_stays_up_with_a_read_only_status_directory(tmp_path):
    """The review's reproduction was 46 restarts in 60 s: the process died on its first write."""
    from core.schema import bootstrap_database
    database = tmp_path / "odin.db"
    engine = create_engine(f"sqlite:///{database}")
    bootstrap_database(engine)
    engine.dispose()
    run = tmp_path / "run"
    run.mkdir()
    run.chmod(0o500)
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{database}", LICENSE_DIR=str(tmp_path), API_KEY="",
               JWT_SECRET_KEY="db-safety-test-only", ODIN_ITAR_MODE="0", ODIN_TELEMETRY_V2="0", COOKIE_SECURE="false",
               ODIN_DB_ROLE="backup", PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}", PYTHONUNBUFFERED="1")
    process = subprocess.Popen([sys.executable, "-m", "modules.system.db_guardian"], cwd=ROOT / "backend", env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.5)
            if time.monotonic() > deadline - 30:
                break
        assert process.poll() is None, process.stdout.read()[-2000:]
    finally:
        process.terminate()
        output = process.communicate(timeout=20)[0]
        run.chmod(0o700)
    assert "Status could not be saved (PermissionError)" in output


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


def _reasons(errors):
    return [error.reason for error in errors]


def test_no_backup_for_26_hours_counts_as_missed(tmp_path):
    database = tmp_path / "odin.db"
    status = {"backup": {"last_success_at": NOW.isoformat()}}
    assert missed_backups(status, NOW + timedelta(hours=25, minutes=59), database) == []
    assert _reasons(missed_backups(status, NOW + timedelta(hours=26), database)) == ["missed"]


def test_a_backup_that_never_succeeded_is_missed_from_the_first_start(tmp_path):
    database = tmp_path / "odin.db"
    assert missed_backups({}, NOW, database) == []
    status = {"started_at": NOW.isoformat(), "backup": {"state": "failed", "last_attempt_at": NOW.isoformat()}}
    assert missed_backups(status, NOW + timedelta(hours=25), database) == []
    assert _reasons(missed_backups(status, NOW + timedelta(hours=26), database)) == ["missed"]


def test_a_job_that_registered_and_never_confirmed_a_copy_is_noticed(tmp_path):
    """A first install has no confirmed copy, so the clock starts when the job registers."""
    database = tmp_path / "odin.db"
    status = {"backup": {"last_success_at": NOW.isoformat()}}
    write_status(offsite_path(database), {"expected_since": NOW.isoformat()})
    assert missed_backups(status, NOW + timedelta(hours=25), database) == []
    late = NOW + timedelta(hours=36)
    status["backup"]["last_success_at"] = late.isoformat()
    assert _reasons(missed_backups(status, late, database)) == ["offsite_missed"]


def test_no_off_server_alert_straight_after_an_outage(tmp_path):
    """The review's reproduction: 37 hours since the last confirm, 13 of them with the server off."""
    database = tmp_path / "odin.db"
    write_status(offsite_path(database), {"confirmed_at": NOW.isoformat()})
    moment = NOW + timedelta(hours=37)
    status = {"backup": {"last_success_at": moment.isoformat()}}
    status["booted_at"] = (moment - timedelta(hours=1)).isoformat()
    assert missed_backups(status, moment, database) == []
    status["booted_at"] = (moment - timedelta(hours=4)).isoformat()
    assert _reasons(missed_backups(status, moment, database)) == ["offsite_missed"]


def test_off_server_copies_only_count_once_one_was_confirmed(tmp_path):
    database = tmp_path / "odin.db"
    status = {"backup": {"last_success_at": NOW.isoformat()}}
    assert missed_backups(status, NOW + timedelta(days=1), database) == []
    write_status(offsite_path(database), {"confirmed_at": NOW.isoformat()})
    assert missed_backups(status, NOW + timedelta(hours=25), database) == []
    late = NOW + timedelta(hours=36)
    status["backup"]["last_success_at"] = late.isoformat()
    assert _reasons(missed_backups(status, late, database)) == ["offsite_missed"]


def test_tick_raises_the_missed_alert_when_every_run_keeps_failing(saved_backup_alerts, tmp_path):
    status = {"started_at": NOW.isoformat()}
    def bad(*_, **__):
        raise OSError(5, "synthetic")
    later = NOW + timedelta(hours=26)
    _tick(status, later, create=bad, database=tmp_path / "odin.db")
    assert _reasons(saved_backup_alerts) == ["missed"]
    assert backup_failure_alerts.backup_failure_reason(saved_backup_alerts[0])[1].startswith("Backup missed")


def test_a_good_run_at_the_same_tick_clears_the_missed_alert(saved_backup_alerts, tmp_path):
    status = {"started_at": NOW.isoformat()}
    _tick(status, NOW + timedelta(hours=30), database=tmp_path / "odin.db")
    assert saved_backup_alerts == []


def test_a_missed_alert_that_cannot_be_saved_does_not_stop_the_guardian(monkeypatch, tmp_path):
    def broken(_):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(backup_failure_alerts, "publish_backup_failure", broken)
    status = {"started_at": NOW.isoformat(), "backup": {"last_attempt_at": (NOW + timedelta(hours=26)).isoformat()}}
    assert _tick(status, NOW + timedelta(hours=26), database=tmp_path / "odin.db") is True


def test_missed_messages_are_plain_and_have_no_paths():
    from modules.system.backup_service import BackupMissedError
    for reason in ("missed", "offsite_missed"):
        found, message = backup_failure_alerts.backup_failure_reason(BackupMissedError(reason))
        assert found == reason and message.startswith("Backup missed") and "/" not in message


def _stale_status(database):
    old = datetime.now(timezone.utc) - timedelta(days=1)
    write_status(status_path(database), _status(OK, checked_at=old, interval=300))


def test_report_runner_alerts_when_the_guardian_has_stopped_once_a_day(alert_db, tmp_path, monkeypatch):
    from modules.notifications import alert_dispatcher
    engine, factory = alert_db
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: None)
    url = str(engine.url)
    database = Path(engine.url.database)
    _stale_status(database)
    assert alert_if_stopped(url, session_factory=factory) is True
    rows = _alert_rows(engine)
    assert [row[0] for row in rows] == [1, 2] and all(row[2] == ALERT_TITLE for row in rows)
    assert "not been able to finish" in rows[0][3]
    assert alert_if_stopped(url, session_factory=factory) is False
    assert len(_alert_rows(engine)) == 2
    assert alert_if_stopped(url, session_factory=factory, now=datetime.now(timezone.utc) + timedelta(days=1, seconds=5)) is True


def test_report_runner_stays_quiet_when_the_guardian_is_healthy_or_absent(alert_db):
    engine, factory = alert_db
    database = Path(engine.url.database)
    url = str(engine.url)
    assert alert_if_stopped(url, session_factory=factory) is False
    write_status(status_path(database), _status(OK, checked_at=datetime.now(timezone.utc), interval=300))
    assert alert_if_stopped(url, session_factory=factory) is False
    assert alert_if_stopped("postgresql://odin@postgres:5432/odin") is False
    assert _alert_rows(engine) == []


def test_the_watcher_gives_a_booting_guardian_time_before_calling_it_stopped(monkeypatch):
    """After a long outage the status file looks old until the guardian's first check of this boot."""
    import modules.reporting.report_runner as runner
    calls = []
    monkeypatch.setattr(db_guardian_status, "alert_if_stopped", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(runner.time, "monotonic", lambda: runner._STARTED_AT + runner.GUARDIAN_WATCH_DELAY_SECONDS - 1)
    runner._check_database_guardian()
    assert calls == []
    monkeypatch.setattr(runner.time, "monotonic", lambda: runner._STARTED_AT + runner.GUARDIAN_WATCH_DELAY_SECONDS)
    runner._check_database_guardian()
    assert calls == [1]


@pytest.mark.parametrize("last_check,waits", [(timedelta(minutes=1), True), (timedelta(days=2), False)], ids=["recent", "after-an-outage"])
def test_first_check_of_a_boot_waits_only_when_the_last_result_is_recent(monkeypatch, tmp_path, last_check, waits):
    database = tmp_path / "odin.db"
    write_status(status_path(database), _status(OK, checked_at=datetime.now(timezone.utc) - last_check, interval=300))
    monkeypatch.setattr(db_guardian, "DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setattr(db_guardian.os, "nice", lambda _: 0)
    slept = []
    def sleep(seconds):
        slept.append(seconds)
        if seconds == db_guardian.TICK_SECONDS:
            raise _Stop
    monkeypatch.setattr(db_guardian.time, "sleep", sleep)
    monkeypatch.setattr(db_guardian, "tick", lambda *a, **k: False)
    with pytest.raises(_Stop):
        db_guardian.main_loop()
    assert (db_guardian.FIRST_CHECK_DELAY_SECONDS in slept) is waits


def test_a_failed_watcher_check_never_stops_the_report_loop(monkeypatch):
    import modules.reporting.report_runner as runner
    monkeypatch.setattr(runner, "GUARDIAN_WATCH_DELAY_SECONDS", 0)
    monkeypatch.setattr(db_guardian_status, "alert_if_stopped", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("synthetic")))
    runner._check_database_guardian()
    source = Path(runner.__file__).read_text(encoding="utf-8")
    assert "_check_database_guardian()\n        time.sleep(POLL_INTERVAL)" in source


# --- The off-server hold has a limit: every restart restarts it.

@pytest.mark.parametrize("confirm_age,expected", [
    (timedelta(hours=35), []),                       # not missed yet
    (timedelta(hours=36), []),                       # missed, held: just restarted
    (timedelta(days=3) - timedelta(minutes=1), []),  # still held
    (timedelta(days=3), ["offsite_missed"]),         # the hold no longer applies
    (timedelta(days=30), ["offsite_missed"]),
])
def test_the_hold_after_a_restart_ends_once_the_last_confirm_is_three_days_old(tmp_path, confirm_age, expected):
    database = tmp_path / "odin.db"
    write_status(offsite_path(database), {"confirmed_at": (NOW - confirm_age).isoformat()})
    status = {"backup": {"last_success_at": NOW.isoformat()}, "booted_at": (NOW - timedelta(minutes=30)).isoformat()}
    assert _reasons(missed_backups(status, NOW, database)) == expected


def test_a_restart_loop_cannot_hold_back_the_alert_with_a_30_day_old_confirm(monkeypatch, tmp_path, saved_backup_alerts):
    """The real start path, restarted over and over. Each restart resets booted_at, so only the limit can help."""
    database = tmp_path / "odin.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE t(id INTEGER PRIMARY KEY)")
    connection.close()
    write_status(offsite_path(database), {"confirmed_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()})
    monkeypatch.setattr(db_guardian, "DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setattr(db_guardian.os, "nice", lambda _: 0)
    def stop_at_first_tick(seconds):
        if seconds == db_guardian.TICK_SECONDS:
            raise _Stop
    monkeypatch.setattr(db_guardian.time, "sleep", stop_at_first_tick)
    boots = []
    for restart in range(1, 6):
        # An hour after the last result, so a check is due; backups are fine, so none is.
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        current = read_status(status_path(database))
        write_status(status_path(database), {
            **current, "integrity": {"state": OK, "checked_at": hour_ago, "attempted_at": hour_ago},
            "backup": {"state": "ok", "last_success_at": hour_ago, "last_attempt_at": hour_ago}})
        with pytest.raises(_Stop):
            db_guardian.main_loop()
        boots.append(read_status(status_path(database))["booted_at"])
        assert _reasons(saved_backup_alerts) == ["offsite_missed"] * restart
    assert len(set(boots)) == 5, "each restart really did reset the grace"


# --- Backups on /health: public, so a state and a time; and never the top-level status.

def _backup_status(state="ok", age=timedelta(hours=1), **extra):
    now = NOW
    return {"interval_seconds": 300, "started_at": (now - timedelta(days=40)).isoformat(),
            "integrity": {"state": OK, "checked_at": now.isoformat(), "attempted_at": now.isoformat()},
            "backup": {"state": state, "reason": "disk_space", "filename": "odin_backup_x.db", "size_bytes": 123456789,
                       "last_attempt_at": now.isoformat(), "last_success_at": (now - age).isoformat(), **extra}}


@pytest.mark.parametrize("status,expected", [
    ({}, "pending"),
    ({"interval_seconds": 300, "started_at": NOW.isoformat(), "integrity": {"state": OK, "checked_at": NOW.isoformat()}}, "pending"),
    (_backup_status("ok"), "ok"),
    (_backup_status("failed"), "failed"),
    (_backup_status("refused"), "failed"),
    (_backup_status("ok", timedelta(hours=26) - timedelta(seconds=1)), "ok"),
    (_backup_status("ok", timedelta(hours=26)), "overdue"),
    (_backup_status("failed", timedelta(hours=26)), "overdue"),
    (_backup_status("failed", last_success_at=None), "overdue"),  # never succeeded, started long ago
])
def test_backup_view(status, expected):
    view = backup_view(status, now=NOW)
    assert view["status"] == expected
    assert set(view) == {"status", "last_success_at"}


def test_with_no_room_to_save_anything_the_backup_field_still_moves_on_time():
    """The file can't change any more, so this reads the same file later: this is what /health shows then."""
    saved = _backup_status("ok", timedelta(hours=1))
    assert backup_view(saved, now=NOW + timedelta(minutes=14))["status"] == "ok"
    assert backup_view(saved, now=NOW + timedelta(minutes=16))["status"] == "stale"
    assert backup_view(saved, now=NOW + timedelta(hours=25))["status"] == "overdue"
    assert backup_view(saved, now=NOW + timedelta(hours=25))["last_success_at"] == saved["backup"]["last_success_at"]


def _fresh(status):
    """The same status, as of now, for the route, which reads the real clock."""
    now = datetime.now(timezone.utc)
    shifted = json.loads(json.dumps(status))
    delta = now - NOW
    for section in ("integrity", "backup"):
        for key, value in list(shifted[section].items()):
            if key.endswith("_at") and value:
                shifted[section][key] = (datetime.fromisoformat(value) + delta).isoformat()
    shifted["started_at"] = (datetime.fromisoformat(shifted["started_at"]) + delta).isoformat()
    return shifted


@pytest.mark.parametrize("state,age,field", [("failed", timedelta(hours=1), "failed"), ("failed", timedelta(hours=30), "overdue")])
def test_a_backup_problem_shows_on_health_but_never_changes_the_status(monkeypatch, tmp_path, state, age, field):
    """It mustn't block the promote that might fix it. Public, so no name, size or reason."""
    body = _health(monkeypatch, tmp_path, _fresh(_backup_status(state, age)))
    assert body["status"] == "ok" and body["database_integrity"]["status"] == "ok"
    assert body["database_backup"]["status"] == field and body["database_backup"]["last_success_at"]
    assert set(body["database_backup"]) == {"status", "last_success_at"}
    text = json.dumps(body)
    assert "odin_backup" not in text and "123456789" not in text and "disk_space" not in text


def test_the_backup_field_is_absent_on_postgresql(monkeypatch, tmp_path):
    assert _health(monkeypatch, tmp_path, url="postgresql://odin@postgres:5432/odin")["database_backup"] is None


# --- The watcher doesn't depend on what it watches.

def test_a_guardian_module_that_will_not_import_still_produces_an_alert(alert_db, monkeypatch):
    """The review's reproduction: 0 alert rows after 22 minutes, because the watcher imported it too."""
    import modules.reporting.report_runner as runner
    from modules.notifications import alert_dispatcher
    engine, factory = alert_db
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: None)
    monkeypatch.setattr(runner, "DATABASE_URL", str(engine.url))
    monkeypatch.setattr(runner, "SessionLocal", factory)
    monkeypatch.setattr(runner, "GUARDIAN_WATCH_DELAY_SECONDS", 0)
    write_status(status_path(Path(engine.url.database)), {
        "started_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), "interval_seconds": 300})
    monkeypatch.setitem(sys.modules, "modules.system.db_guardian", None)
    with pytest.raises(ImportError):
        import modules.system.db_guardian  # noqa: F401
    runner._check_database_guardian()
    assert [row[0] for row in _alert_rows(engine)] == [1, 2]


def test_neither_the_watcher_nor_the_status_code_imports_the_guardian():
    pattern = re.compile(r"modules\.system\.db_guardian\s+import|modules\.system import .*\bdb_guardian\b(?!_)")
    for name in ("modules/reporting/report_runner.py", "modules/system/db_guardian_status.py", "modules/system/routes_health.py"):
        assert not pattern.search((ROOT / "backend" / name).read_text(encoding="utf-8")), name


# --- Leftovers go at guardian start, not at the next backup, which may be an hour away.

def test_guardian_start_clears_a_killed_runs_leftovers(monkeypatch, tmp_path):
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "partial_scheduled_20261101_000000_000000.db").write_bytes(b"half written")
    (backups / "partial_scheduled_abc").mkdir()
    (backups / "partial_scheduled_abc" / "odin.db").write_bytes(b"a restore test's copy")
    (backups / "odin-pre-repair.db").write_bytes(b"not ours")
    _run_main_loop(monkeypatch, tmp_path)
    assert [path.name for path in backups.iterdir()] == ["odin-pre-repair.db"]
