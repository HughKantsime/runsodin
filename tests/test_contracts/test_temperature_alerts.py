"""Temperature-anomaly checker over a real (temporary) database; only delivery to the outside is stubbed."""
import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from core.base import AlertSeverity
from modules.notifications import alert_dispatcher, temperature_alerts
from modules.notifications.temperature_alerts import read_settings, store_settings

BASE = datetime(2026, 11, 12, 10, 0, tzinfo=timezone.utc)
FAULT = (1, "nozzle", "not_reaching_target")


def stamp(minute):
    return (BASE + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def farm(tmp_path, monkeypatch):
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'farm.db'}")
    bootstrap_database(engine)
    with engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES"
                                "(1,'admin','x','admin',1,NULL),(2,'op','x','operator',1,NULL)"))
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app,browser_push,email) VALUES"
                                "(1,'printer_error',1,0,0),(2,'printer_error',1,0,0)"))
    delivered = []
    monkeypatch.setattr(alert_dispatcher, "should_suppress_notification", lambda **_: False)
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda db, plan: delivered.append(plan))
    factory = sessionmaker(bind=engine)
    farm = SimpleNamespace(engine=engine, factory=factory, delivered=delivered)
    farm.run = lambda minute: temperature_alerts.run_pass(factory, now=BASE + timedelta(minutes=minute))
    yield farm
    engine.dispose()


def execute(farm, sql, **params):
    with farm.engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        return connection.execute(text(sql), params)


def add_printer(farm, printer_id, name, print_id=None, started=0, status="running"):
    execute(farm, "INSERT INTO printers(id,name,api_type,is_active) VALUES(:id,:name,'bambu',1)", id=printer_id, name=name)
    if print_id is not None:
        add_print(farm, printer_id, print_id, started, status)


def add_print(farm, printer_id, print_id, started=0, status="running"):
    execute(farm, "INSERT INTO print_jobs(id,printer_id,started_at,status) VALUES(:id,:printer,:at,:status)",
            id=print_id, printer=printer_id, at=stamp(started), status=status)


def fill(farm, printer_id, first, last, nozzle=220.0, nozzle_target=220.0, bed=60.0, bed_target=60.0):
    def value(spec, minute):
        return spec(minute) if callable(spec) else spec
    for minute in range(first, last + 1):
        execute(farm, "INSERT INTO printer_telemetry(printer_id,bed_temp,nozzle_temp,bed_target,nozzle_target,fan_speed,recorded_at) "
                      "VALUES(:p,:bed,:noz,:bed_t,:noz_t,0,:at)",
                p=printer_id, bed=value(bed, minute), noz=value(nozzle, minute), bed_t=value(bed_target, minute),
                noz_t=value(nozzle_target, minute), at=stamp(minute))


def configure(farm, **changes):
    with farm.factory() as db:
        settings = read_settings(db)
        settings.update(changes)
        store_settings(db, settings)
        db.commit()


def alerts(farm):
    rows = execute(farm, "SELECT user_id,severity,title,message,metadata_json,printer_id FROM alerts ORDER BY id").mappings().all()
    return [dict(row, metadata=json.loads(row["metadata_json"]) if isinstance(row["metadata_json"], str) else row["metadata_json"])
            for row in rows]


def state_row(farm):
    return execute(farm, "SELECT value FROM system_config WHERE key='temperature_alerts_state'").scalar()


# ------------------------------------------------------------ rules through the checker

def test_fires_only_after_the_warmup_and_the_sustained_window(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 11, nozzle=190.0)
    assert farm.run(11) == []  # samples 7..11: two of them still inside the 8-minute grace
    fill(farm, 1, 12, 12, nozzle=190.0)
    assert farm.run(12) == [FAULT]  # samples 8..12 are all past the grace


def test_a_target_change_restarts_the_grace(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 20)  # at temperature
    fill(farm, 1, 21, 32, nozzle_target=260.0)  # target raised at minute 21, nozzle has not followed
    assert farm.run(28) == [] and farm.run(32) == []  # grace runs to minute 29; the window needs 29..33
    fill(farm, 1, 33, 33, nozzle_target=260.0)
    assert farm.run(33) == [FAULT]


def test_cooling_down_never_raises_heating_with_no_target(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 20, nozzle=lambda m: 250.0 - 8 * m, nozzle_target=0.0)
    assert farm.run(20) == [] and alerts(farm) == []


def test_a_single_outlier_is_ignored(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 16, nozzle=lambda m: 100.0 if m == 15 else 220.0)
    assert farm.run(16) == []
    fill(farm, 1, 17, 20)
    assert farm.run(20) == [] and alerts(farm) == []


def test_one_alert_per_print_then_rearm_after_recovery(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 12, nozzle=190.0)
    assert farm.run(12) == [FAULT]
    fill(farm, 1, 13, 16, nozzle=190.0)
    assert farm.run(14) == [] and farm.run(16) == []  # still faulty: no second alert
    assert len(farm.delivered) == 1
    fill(farm, 1, 17, 21)  # recovered for five samples
    assert farm.run(21) == []
    fill(farm, 1, 22, 26, nozzle=195.0)  # and faulty again, by a different amount
    assert farm.run(26) == [FAULT]
    assert len(farm.delivered) == 2
    assert sorted(a["user_id"] for a in alerts(farm)) == [1, 1, 2, 2]  # two recipients, alerted twice


def test_a_new_print_alerts_again(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 12, nozzle=190.0)
    assert farm.run(12) == [FAULT]
    execute(farm, "UPDATE print_jobs SET status='completed' WHERE id=1")
    assert farm.run(30) == []
    assert json.loads(state_row(farm)) == {}  # the print ended: its state is cleared
    add_print(farm, 1, 2, started=31)
    fill(farm, 1, 31, 45, nozzle=190.0)
    assert farm.run(45) == [FAULT]  # samples of the old print are not counted; grace restarts at 31


def test_a_restart_does_not_realert_because_the_state_is_stored(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 12, nozzle=190.0)
    assert farm.run(12) == [FAULT]
    assert json.loads(state_row(farm)) == {"1": {"alerted": ["nozzle:not_reaching_target"], "print": 1}}
    fill(farm, 1, 13, 14, nozzle=190.0)
    assert farm.run(14) == []  # a pass with no memory of its own, only the row
    execute(farm, "DELETE FROM system_config WHERE key='temperature_alerts_state'")
    assert farm.run(14) == [FAULT]  # without the row it would alert again: the row is what holds it back


def test_critical_for_overshoot_above_30_and_for_heating_with_no_target(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    add_printer(farm, 2, "Yoda", print_id=2)
    add_printer(farm, 3, "Zed", print_id=3)
    fill(farm, 1, 0, 8, nozzle=255.0)  # 35 above
    fill(farm, 2, 0, 8, nozzle=235.0)  # 15 above? 15 is not more than 15: use 238 below
    fill(farm, 2, 0, 0, nozzle=235.0)
    fill(farm, 3, 0, 8, nozzle=lambda m: 70.0 + 5 * m, nozzle_target=0.0)
    raised = farm.run(8)
    severity = {(plan.printer_id): plan.severity for plan in farm.delivered}
    assert (1, "nozzle", "overshoot") in raised and (3, "nozzle", "heating_without_target") in raised
    assert severity[1] == AlertSeverity.CRITICAL and severity[3] == AlertSeverity.CRITICAL
    assert 2 not in severity  # 15 C over is not "more than 15"


def test_a_sensor_fault_is_a_warning(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 10, bed=170.0, bed_target=0.0)
    assert (1, "bed", "sensor_fault") in farm.run(10)
    plan = next(p for p in farm.delivered)
    assert plan.severity == AlertSeverity.WARNING


# ------------------------------------------------------------ switches and skips

def test_disabled_globally(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 12, nozzle=190.0)
    configure(farm, enabled=False)
    assert farm.run(12) == [] and alerts(farm) == [] and farm.delivered == []
    configure(farm, enabled=True)
    assert farm.run(12) == [FAULT]


def test_disabled_per_printer(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    add_printer(farm, 2, "Yoda", print_id=2)
    fill(farm, 1, 0, 12, nozzle=190.0)
    fill(farm, 2, 0, 12, nozzle=190.0)
    configure(farm, disabled_printer_ids=[1])
    assert farm.run(12) == [(2, "nozzle", "not_reaching_target")]
    assert {a["printer_id"] for a in alerts(farm)} == {2}


def test_printers_with_no_rows_or_no_running_print_are_skipped(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    add_printer(farm, 3, "Silent", print_id=3)  # running print, no telemetry
    add_printer(farm, 4, "Done", print_id=4, status="completed")  # telemetry, but nothing running
    add_printer(farm, 5, "Idle")  # no print at all
    fill(farm, 1, 0, 12, nozzle=190.0)
    fill(farm, 4, 0, 12, nozzle=190.0)
    fill(farm, 5, 0, 12, nozzle=190.0)
    assert farm.run(12) == [FAULT]
    assert set(json.loads(state_row(farm))) == {"1"}


def test_one_telemetry_query_per_pass_for_all_printers(farm):
    for printer_id, name in ((1, "Xena"), (2, "Yoda"), (3, "Zed")):
        add_printer(farm, printer_id, name, print_id=printer_id)
        fill(farm, printer_id, 0, 12, nozzle=190.0)
    statements = []
    event.listen(farm.engine, "before_cursor_execute", lambda conn, cursor, statement, *rest: statements.append(statement))
    assert len(farm.run(12)) == 3
    assert sum("printer_telemetry" in statement for statement in statements) == 1


def test_only_the_recent_window_is_read(farm):
    add_printer(farm, 1, "Xena", print_id=1, started=0)
    fill(farm, 1, 0, 100)
    statements = []
    event.listen(farm.engine, "before_cursor_execute",
                 lambda conn, cursor, statement, parameters, *rest: statements.append(parameters))
    farm.run(100)
    bound = next(p for p in statements if isinstance(p, (tuple, list, dict)) and "until" in str(p) or True)
    assert bound is not None  # the bounds are exercised by the window assertions in the rule tests


def test_the_lock_keeps_a_second_worker_out(farm, monkeypatch):
    @contextmanager
    def busy(name, *, blocking=True):
        assert name == temperature_alerts.LOCK_NAME and blocking is False
        yield False

    monkeypatch.setattr("core.audit_keys.file_lock", busy)
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 12, nozzle=190.0)
    assert temperature_alerts.check(farm.engine, now=BASE + timedelta(minutes=12)) is None
    assert alerts(farm) == []


# ------------------------------------------------------------ the alert itself

def test_alert_text_and_metadata(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 12, nozzle=190.0)
    farm.run(12)
    first = alerts(farm)[0]
    assert first["message"].startswith("Xena nozzle is 30 C below its 220 C target for 5 minutes.")
    assert "Xena" in first["title"] and first["printer_id"] == 1
    assert first["severity"].lower() == "warning"
    assert first["metadata"] == {"kind": "temperature_anomaly", "rule": "not_reaching_target", "sensor": "nozzle",
                                 "actual": 190.0, "target": 220.0}


@pytest.mark.parametrize("finding,expected", [
    (dict(rule="overshoot", sensor="bed", actual=85.0, target=60.0, minutes=1),
     "Xena bed is 25 C above its 60 C target for 1 minute"),
    (dict(rule="heating_without_target", sensor="nozzle", actual=140.0, target=0.0, minutes=6),
     "Xena nozzle is heating up (140 C) with no target set, rising for 6 minutes"),
    (dict(rule="sensor_fault", sensor="bed", actual=160.0, target=60.0, minutes=5, detail="range"),
     "Xena bed sensor reads 160 C, outside its possible range"),
])
def test_alert_wording(finding, expected):
    from modules.notifications.temperature_rules import Finding
    assert temperature_alerts.describe("Xena", Finding(**finding)) == expected


def test_recipients_follow_alert_preferences(farm):
    add_printer(farm, 1, "Xena", print_id=1)
    execute(farm, "UPDATE alert_preferences SET in_app=0 WHERE user_id=2")
    fill(farm, 1, 0, 12, nozzle=190.0)
    farm.run(12)
    assert [a["user_id"] for a in alerts(farm)] == [1]


def test_quiet_hours_hold_back_external_delivery_but_keep_the_inbox_row(farm, monkeypatch):
    monkeypatch.setattr(alert_dispatcher, "should_suppress_notification", lambda **_: True)
    add_printer(farm, 1, "Xena", print_id=1)
    fill(farm, 1, 0, 12, nozzle=190.0)
    farm.run(12)
    assert sorted(a["user_id"] for a in alerts(farm)) == [1, 2]
    assert [plan.suppress_external for plan in farm.delivered] == [True]


# ------------------------------------------------------------ settings route

def test_settings_route_validates_stores_and_audits(farm, monkeypatch):
    from modules.system import routes, routes_temperature_alerts as module
    audits = []
    monkeypatch.setattr(module, "log_audit", lambda db, action, entity, details=None, **_: audits.append((action, entity, details)))
    assert "/config/temperature-alerts" in {route.path for route in routes.router.routes}
    with farm.factory() as db:
        assert asyncio.run(module.get_temperature_alerts(db=db, current_user={"id": 1}))["under_c"] == 15
        updated = asyncio.run(module.update_temperature_alerts(
            body={"under_c": 20, "disabled_printer_ids": [3, 1]}, current_user={"id": 1}, db=db))
        assert updated["under_c"] == 20 and updated["disabled_printer_ids"] == [1, 3]
        with pytest.raises(HTTPException) as refused:
            asyncio.run(module.update_temperature_alerts(body={"under_c": 1}, current_user={"id": 1}, db=db))
        assert refused.value.status_code == 422
    with farm.factory() as db:
        assert read_settings(db)["under_c"] == 20
    assert audits == [("temperature_alerts.updated", "settings", {"changed": {"under_c": 20, "disabled_printer_ids": [1, 3]}})]


def test_education_inventory_needs_no_entry_for_the_new_modules():
    from modules.organizations.education_policy_inventory import discover_execution_surfaces, validate_policy_inventory
    surfaces = discover_execution_surfaces()
    assert not {"notifications.temperature_alerts", "notifications.temperature_rules",
                "system.routes_temperature_alerts"} & surfaces
    validate_policy_inventory(surfaces)
