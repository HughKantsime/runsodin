import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from modules.printers import moonraker_controls as controls
from modules.printers.adapters import moonraker
from modules.printers.models import Printer
from modules.printers.schemas import PrinterResponse
from tests.test_contracts.test_printer_control_behavior import control_client, review_db, token_client
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine
from tests.test_contracts.test_temperature_alerts import farm
from tests.test_contracts.test_restart_safety_vision import guarded_vision, vision


REAL_ADAPTER = moonraker.MoonrakerPrinter


def wire(monkeypatch, transport, state):
    clock = [0.]
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setattr(moonraker, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    posts = []
    def post(adapter, action):
        posts.append(action)
        return moonraker.ControlReply(transport, 409 if transport == "rejected" else None, "Printer busy")
    monkeypatch.setattr(moonraker.MoonrakerPrinter, "post_control", post)
    def get(adapter, path, timeout):
        assert "print_stats" in path and "pause_resume" not in path
        assert 0 < timeout <= 5
        return {"result": {"status": {"print_stats": {"state": state}}}}
    monkeypatch.setattr(moonraker.MoonrakerPrinter, "_get", get)
    return posts, clock


@pytest.mark.parametrize("action,expected", [("pause", "paused"), ("resume", "printing"), ("stop", "cancelled")])
@pytest.mark.parametrize("transport,outcome", [
    ("accepted","confirmed"), ("no_reply","confirmed"), ("accepted","pending"), ("no_reply","pending"),
    ("rejected","failed"), ("unreachable","failed")])
def test_route(control_client, review_db, monkeypatch, action, expected, transport, outcome):
    client, issue, _ = control_client
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='fake.test',model='Kobra S1',"
                           "gcode_state='PAUSE',print_stage='Original stage' WHERE id=299"))
    review_db.commit()
    posts, clock = wire(monkeypatch, transport, expected if outcome == "confirmed" else "unknown")
    response = client.post(f"/printers/299/{action}", headers=issue(["agent:write", "write:printers"]))
    assert posts == [action]
    assert review_db.execute(text("SELECT gcode_state,print_stage FROM printers WHERE id=299")).one() == ("PAUSE","Original stage")
    record = controls._read(review_db, 299)
    if outcome == "failed":
        assert response.status_code == 503
        assert transport in response.json()["detail"]
        assert clock == [0.] and record is None
        return
    assert response.status_code == 200
    data = response.json()
    assert data["success"] and data["outcome"] == outcome
    assert data["message"] == (controls.PENDING if outcome == "pending" else controls.CONFIRMED)[action]
    if action == "pause":
        assert [a["tool"] for a in data["next_actions"]] == ["get_printer", "resume_printer"]
    if action != "resume":
        assert data["unproven"]
    if outcome == "pending":
        assert clock == [8 if action == "pause" else 3]
        assert record["actor_token_id"] and record["action"] == action
        assert "state" not in data
    else:
        assert record is None


def pending(db, monkeypatch, action):
    wire(monkeypatch, "no_reply", "unknown")
    printer = db.get(Printer, 9)
    receipt = controls.send_control(db, printer, action, {"id": 1})
    assert receipt["outcome"] == "pending"
    return datetime.fromisoformat(receipt["pending_control"]["sent_at"])


def install_printer(db):
    db.add(Printer(id=9, name="Test printer", model="Kobra S1", machine_type="Synthetic",
                   api_type="moonraker", api_host="fake.test"))
    db.commit()


def observe(db, state, when, active=None):
    status = moonraker.MoonrakerStatus(raw_print_state=state, filename="sample.gcode",
                                      raw_data={"virtual_sdcard":{"is_active":active}})
    controls.observe_control(db, 9, "Test printer", status, now=when)


@pytest.mark.parametrize("action", ["pause", "resume", "stop"])
def test_monitor_replay_from_attended_capture(farm, monkeypatch, action):
    # Raw Nov 20 capture, timestamps relative to each POST; no device address or original filename.
    sequence = json.loads((Path(__file__).parents[1] / "fixtures/moonraker-control-replay.json").read_text())[action]
    with farm.factory() as db:
        install_printer(db)
        start = pending(db, monkeypatch, action)
        from modules.printers.monitors import moonraker_monitor
        monkeypatch.setattr(moonraker_monitor, "engine", farm.engine)
        monitor = moonraker_monitor.MoonrakerMonitor(9, "Test printer", "fake.test", model="Kobra S1")
        for sample in sequence:
            assert controls._read(db, 9)["action"] == action
            when = start + timedelta(seconds=sample["seconds"])
            monkeypatch.setattr(controls, "datetime", type("Clock", (datetime,), {"now": staticmethod(lambda tz: when)}))
            objects = {"print_stats": {"state": sample["state"], "filename": sample.get("filename", "")},
                       "virtual_sdcard": {"is_active": sample.get("is_active")}}
            monkeypatch.setattr(monitor.printer, "_get", lambda _: {"result": {"status": objects}})
            monitor._observe_control(monitor.printer.get_status(), observed_at=when)
        assert controls._read(db, 9) is None
        details = db.execute(text("SELECT details FROM audit_logs WHERE action=:a"), {"a":f"printer.{action}_confirmed"}).scalar_one()
        assert json.loads(details)["elapsed_seconds"] == pytest.approx(sequence[-1]["seconds"], abs=.006)
        assert farm.delivered == []


def test_replacement_and_offline_timeout_alert_once(farm, monkeypatch):
    with farm.factory() as db:
        install_printer(db)
        pending(db, monkeypatch, "pause")
        start = pending(db, monkeypatch, "resume")
        observe(db, "paused", start + timedelta(seconds=270))
        assert controls._read(db, 9)["action"] == "resume"
        assert controls.expired_printer_ids(db, start + timedelta(seconds=299)) == []
        assert controls.expired_printer_ids(db, start + timedelta(seconds=300)) == [9]
        for seconds in (300, 330):
            controls.observe_control(db, 9, "Test printer", now=start + timedelta(seconds=seconds))
        assert controls._read(db, 9) is None
        assert len(farm.delivered) == 1
        rows = db.execute(text("SELECT user_id,message FROM alerts ORDER BY user_id")).all()
        assert rows == [(i, "Test printer: ODIN sent Resume but the printer did not confirm it within 5 minutes. Check the printer.") for i in (1,2)]
        assert db.execute(text("SELECT action FROM audit_logs WHERE action LIKE 'printer.%' ORDER BY id")).scalars().all() == [
            "printer.pause_requested","printer.pause_replaced","printer.resume_requested","printer.resume_unconfirmed"]


def store_round_trip(db, monkeypatch):
    install_printer(db)
    start = pending(db, monkeypatch, "pause")
    printer = db.get(Printer, 9)
    controls.enrich_controls(db, [printer])
    payload = PrinterResponse.model_validate(printer).model_dump()
    assert payload["unproven_controls"] == ["pause", "stop"] and payload["unavailable_controls"] == []
    assert set(payload["pending_control"]) == {"action","sent_at","deadline_at"}
    observe(db, "paused", start + timedelta(seconds=1))
    assert controls._read(db, 9) is None


def test_sqlite_store_and_payload(farm, monkeypatch):
    with farm.factory() as db:
        store_round_trip(db, monkeypatch)


@pytest.mark.postgresql
def test_postgresql_store_and_payload(pg_engine, monkeypatch):
    with Session(pg_engine) as db:
        store_round_trip(db, monkeypatch)


@pytest.mark.parametrize("state", ["paused", "printing"])
def test_automatic_no_reply(guarded_vision, review_db, monkeypatch, state):
    thread, _, clock, report, _, _, alerts = guarded_vision
    # guarded_vision replaces the class; restore the actual adapter for this test.
    monkeypatch.setattr(moonraker, "MoonrakerPrinter", REAL_ADAPTER)
    sends, _ = wire(monkeypatch, "no_reply", state)
    thread._analyzed_frames = 3
    thread._first_analysis_at = clock[0] - 60
    report()
    thread._on_detection("spaghetti", {"confidence":.99, "bbox":[]}, None)
    assert sends == ["pause"]
    record = controls._read(review_db, 9)
    if state == "printing":
        assert "pause sent, not yet confirmed" in alerts[-1]["message"]
        assert record["automatic"] and record["print_job_id"] == 23
        thread._auto_pause()
        assert sends == ["pause"]
    else:
        assert record is None
        assert "not yet confirmed" not in alerts[-1]["message"]
    assert review_db.execute(text("SELECT gcode_state FROM printers WHERE id=9")).scalar_one() == "RUNNING"



def test_failed_proof_refuses_manual_and_automatic_pause(farm, monkeypatch):
    from fastapi import HTTPException
    from modules.printers.support_levels import CONTROL_PROOF
    monkeypatch.setitem(CONTROL_PROOF, ("kobra_s1", "pause"), "not_working")
    with farm.factory() as db:
        install_printer(db)
        printer = db.get(Printer, 9)
        assert PrinterResponse.model_validate(printer).unavailable_controls == ["pause"]
        with pytest.raises(HTTPException) as exc:
            controls.send_control(db, printer, "pause", {})
        assert exc.value.status_code == 409 and exc.value.detail["code"] == "control_not_available"
        assert controls.send_control(db, printer, "pause", {}, live_check=lambda: True)["outcome"] == "unavailable"
        assert controls._read(db, 9) is None


def test_older_request_finishing_after_stop_cannot_restore_its_pending_pause(farm, monkeypatch):
    def run(adapter, action):
        if action == "pause":
            with farm.factory() as other:
                controls.send_control(other, other.get(Printer, 9), "stop", {"id": 2})
        adapter.last_control = (moonraker.ControlReply("no_reply"), False)
        return False
    monkeypatch.setattr(REAL_ADAPTER, "run_control", run)
    with farm.factory() as db:
        install_printer(db)
        controls.send_control(db, db.get(Printer, 9), "pause", {"id": 1})
        record = controls._read(db, 9)
        assert (record["action"], record["actor_user_id"]) == ("stop", 2)


@pytest.mark.parametrize("state,active,filename,confirmed", [
    ("cancelled", False, "previous.gcode", True), ("standby", False, "", True),
    ("complete", False, "previous.gcode", True), ("error", False, "previous.gcode", True),
    ("error", True, "active.gcode", False), ("error", None, "active.gcode", False),
    ("printing", False, "", False),
])
def test_stop_uses_terminal_state_and_file_activity(state, active, filename, confirmed):
    status = moonraker.MoonrakerStatus(raw_print_state=state, filename=filename,
                                      raw_data={"virtual_sdcard": {"is_active": active}})
    assert moonraker.control_confirmed("stop", status) is confirmed
