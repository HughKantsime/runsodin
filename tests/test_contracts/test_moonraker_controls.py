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
    if action == "resume" and outcome == "confirmed":
        review_db.execute(text("INSERT INTO print_jobs(printer_id,status,started_at) VALUES(299,'running',CURRENT_TIMESTAMP)"))
    review_db.commit()
    posts, clock = wire(monkeypatch, transport, expected if outcome == "confirmed" else "unknown")
    response = client.post(f"/printers/299/{action}", headers=issue(["write:printers"]))
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
            monkeypatch.setattr(moonraker_monitor, "datetime", controls.datetime)
            monkeypatch.setattr(monitor, "_process_status", lambda status: None)
            monkeypatch.setattr(moonraker_monitor, "time", SimpleNamespace(sleep=lambda _: setattr(monitor, "_running", False)))
            monitor._running = True
            monitor._poll_loop()
        assert controls._read(db, 9) is None
        details = db.execute(text("SELECT details FROM audit_logs WHERE action=:a"), {"a":f"printer.{action}_confirmed"}).scalar_one()
        assert json.loads(details)["elapsed_seconds"] == pytest.approx(sequence[-1]["seconds"], abs=.006)
        assert farm.delivered == []


def test_replacement_and_offline_timeout_alert_once(farm, monkeypatch):
    with farm.factory() as db:
        install_printer(db)
        assert db.execute(text("SELECT in_app,browser_push,email FROM alert_preferences "
                               "WHERE user_id=1 AND alert_type='printer_error'")).one() == (True, True, False)
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
        plan, = farm.delivered
        assert set(plan.push_users) == {1, 2} and not plan.email_users
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
        assert alerts[-1]["message"] == "Confidence: 99%"
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

@pytest.mark.parametrize("action,automatic", [("stop", False), ("pause", False), ("pause", True)])
def test_bookkeeping_failure_never_prevents_send(farm, monkeypatch, action, automatic):
    from sqlalchemy.exc import OperationalError
    sends, _ = wire(monkeypatch, "no_reply", "unknown")
    def busy(*args, **kwargs):
        assert sends == [action]
        raise OperationalError("synthetic busy", None, None)
    monkeypatch.setattr(controls, "_begin", busy)
    with farm.factory() as db:
        install_printer(db)
        result = controls.send_control(db, db.get(Printer, 9), action, {},
                                       live_check=(lambda: True) if automatic else None)
        assert sends == [action] and result["outcome"] == "pending"


@pytest.mark.parametrize("reader", ["enrich", "read", "scan"])
@pytest.mark.parametrize("bad", ["{", '{"action":"pause"}'])
def test_bad_record_consumed_and_audited_once(farm, reader, bad):
    from datetime import timezone
    with farm.factory() as db:
        install_printer(db)
        controls.write_config_text(db, controls._key(9), bad)
        db.commit()
        for _ in range(2):
            if reader == "enrich":
                assert controls.enrich_controls(db, [db.get(Printer, 9)])[0].pending_control is None
            elif reader == "read":
                assert controls._read(db, 9) is None
            else:
                assert controls.expired_printer_ids(db, datetime.now(timezone.utc)) == []
    with farm.factory() as db:
        assert db.execute(text("SELECT count(*) FROM system_config WHERE key='pending_control:9'")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='printer.invalid_pending_control'")).scalar_one() == 1


@pytest.mark.parametrize("previous,expired,count", [("stop", False, 1), ("pause", True, 1), ("pause", False, 0)])
def test_auto_pause_only_suppressed_by_live_pause(farm, monkeypatch, previous, expired, count):
    with farm.factory() as db:
        install_printer(db)
        sent = pending(db, monkeypatch, previous)
        if expired:
            now = sent + timedelta(seconds=controls.DEADLINES[previous])
            monkeypatch.setattr(controls, "datetime", type("Clock", (datetime,), {"now": staticmethod(lambda tz: now)}))
            assert controls.enrich_controls(db, [db.get(Printer, 9)])[0].pending_control is None
        sends, _ = wire(monkeypatch, "no_reply", "paused")
        result = controls.send_control(db, db.get(Printer, 9), "pause", {}, live_check=lambda: True)
        assert sends == ["pause"] * count
        assert result["outcome"] == ("confirmed" if count else "skipped")


@pytest.mark.parametrize("awaiting", [True, False])
def test_late_fresh_observation_confirms(farm, monkeypatch, awaiting):
    with farm.factory() as db:
        install_printer(db)
        sent = pending(db, monkeypatch, "pause")
        record = controls._read(db, 9)
        controls.write_config_text(db, controls._key(9), json.dumps({**record, "awaiting_reply": awaiting}))
        db.commit()
        observe(db, "paused", sent + timedelta(minutes=2))
        assert db.execute(text("SELECT action FROM audit_logs ORDER BY id DESC LIMIT 1")).scalar_one() == "printer.pause_confirmed"
        assert not farm.delivered


@pytest.mark.parametrize("replacement", [False, True])
def test_resume_requires_original_active_attempt(farm, monkeypatch, replacement):
    from tests.test_contracts.test_temperature_alerts import add_print
    with farm.factory() as db:
        install_printer(db)
        add_print(farm, 9, 41)
        sent = pending(db, monkeypatch, "resume")
        db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=41"))
        db.commit()
        if replacement:
            add_print(farm, 9, 42)
        observe(db, "printing", sent + timedelta(seconds=5))
        assert controls._read(db, 9)["print_job_id"] == 41
        observe(db, "printing", sent + timedelta(minutes=5))
        assert len(farm.delivered) == 1


def test_overlapping_requests_keep_newer_completed_stop(farm, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    started, release = Event(), Event()
    def run(adapter, action):
        if action == "pause":
            started.set()
            assert release.wait(5)
        adapter.last_control = (moonraker.ControlReply("accepted"), action == "stop")
        return True
    def send(action):
        with farm.factory() as db:
            return controls.send_control(db, db.get(Printer, 9), action, {})
    monkeypatch.setattr(REAL_ADAPTER, "run_control", run)
    with farm.factory() as db:
        install_printer(db)
    with ThreadPoolExecutor(2) as pool:
        older = pool.submit(send, "pause")
        try:
            assert started.wait(5)
            assert pool.submit(send, "stop").result(5)["outcome"] == "confirmed"
        finally:
            release.set()
        assert older.result(5)["outcome"] == "pending"
    with farm.factory() as db:
        assert controls._read(db, 9) is None
        actions = db.execute(text("SELECT action FROM audit_logs WHERE entity_id=9")).scalars().all()
        assert sorted(actions) == sorted(["printer.pause_requested", "printer.stop_requested",
                                          "printer.stop_confirmed", "printer.pause_replaced"])


def test_confirmation_poll_rate(monkeypatch):
    _, clock = wire(monkeypatch, "no_reply", "printing")
    polls = []
    monkeypatch.setattr(REAL_ADAPTER, "get_status", lambda self, **kw: polls.append(clock[0]) or moonraker.MoonrakerStatus(raw_print_state="printing"))
    REAL_ADAPTER("fake.test").run_control("pause")
    from modules.printers.monitors.moonraker_monitor import POLL_INTERVAL
    assert len(polls) >= 2
    assert all(b - a >= max(1, POLL_INTERVAL) for a, b in zip(polls, polls[1:]))


def test_worker_owns_control_session(control_client, review_db, monkeypatch):
    from sqlalchemy.orm import object_session
    from modules.printers import routes_controls
    client, issue, _ = control_client
    review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=299"))
    review_db.commit()
    client.app.dependency_overrides[routes_controls.get_db] = lambda: review_db
    seen = []
    def inspect(db, printer, action, actor):
        seen.append((db is not review_db, object_session(printer) is not review_db))
        return {"outcome": "pending"}
    monkeypatch.setattr(routes_controls, "send_control", inspect)
    assert client.post('/printers/299/stop', headers=issue(["write:printers"])).status_code == 200
    assert seen == [(True, True)]


@pytest.mark.parametrize("reply,expected", [("no_reply", "pending"), ("accepted", "confirmed"), ("rejected", "failed")])
def test_moonraker_dismissal_sends_without_open_transaction(review_db, monkeypatch, reply, expected):
    from tests.test_contracts.test_monitor_command_channel import setup_resume
    from modules.vision.routes.detections import dismiss_and_resume
    from core.errors import OdinError
    setup_resume(review_db, "PAUSE")
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='fake.test',api_key='fixture' WHERE id=9"))
    review_db.commit()
    sends = []
    def run(adapter, action):
        sends.append((action, adapter.api_key, review_db.in_transaction()))
        adapter.last_control = moonraker.ControlReply(reply, 409), expected == "confirmed"
        return expected == "confirmed"
    monkeypatch.setattr(REAL_ADAPTER, "run_control", run)
    from core import crypto
    monkeypatch.setattr(crypto, "decrypt", lambda _: "test-only")
    args = dict(current_user={"id":3, "role":"admin", "group_id":None}, db=review_db)
    if expected == "failed":
        with pytest.raises(OdinError) as error:
            dismiss_and_resume(701, **args)
        assert error.value.status == 503
    else:
        result = dismiss_and_resume(701, **args)
        assert result["outcome"] == expected and result["resume_requested"]
        assert result["message"] == (controls.PENDING if expected == "pending" else controls.CONFIRMED)["resume"]
    assert sends == [("resume", "test-only", False)]
    assert review_db.execute(text("SELECT status FROM vision_detections WHERE id=701")).scalar_one() == (
        "confirmed" if expected == "failed" else "dismissed")
    assert bool(controls._read(review_db, 9)) == (expected == "pending")


def test_detection_alert_and_push_precede_send(guarded_vision, monkeypatch):
    from modules.vision import detection_thread
    from modules.notifications import event_dispatcher
    thread, _, _, _, _, _, _ = guarded_vision
    calls = []
    monkeypatch.setattr(event_dispatcher, "dispatch_alert", lambda **kw: calls.append(kw["message"]))
    monkeypatch.setattr(detection_thread, "ws_push", lambda *args: calls.append("push"))
    monkeypatch.setattr(thread, "_auto_pause", lambda *args: calls.append("pause"))
    thread._on_detection("spaghetti", {"confidence": .99, "bbox": []}, None)
    assert calls == ["Confidence: 99%", "push", "pause"]


def test_confirmed_automatic_pause_marks_only_triggering_detection(guarded_vision, review_db, monkeypatch):
    thread, _, clock, report, _, _, _ = guarded_vision
    monkeypatch.setattr(moonraker, "MoonrakerPrinter", REAL_ADAPTER)
    wire(monkeypatch, "no_reply", "printing")
    thread._analyzed_frames = 3
    thread._first_analysis_at = clock[0] - 60
    from modules.vision.detection_thread import PrinterVisionThread
    monkeypatch.setattr(thread, "_insert_detection", PrinterVisionThread._insert_detection.__get__(thread))
    report()
    thread._on_detection("spaghetti", {"confidence": .99, "bbox":[]}, None)
    first = review_db.execute(text("SELECT max(id) FROM vision_detections WHERE printer_id=9")).scalar_one()
    second = thread._insert_detection("spaghetti", .9, "another.jpg", [])
    record = controls._read(review_db, 9)
    observe(review_db, "paused", datetime.fromisoformat(record["sent_at"]) + timedelta(seconds=15))
    assert review_db.execute(text("SELECT status FROM vision_detections WHERE id IN (:a,:b) ORDER BY id"),
                             {"a":first, "b":second}).scalars().all() == ["auto_paused","pending"]


@pytest.mark.parametrize("action", ["pause", "resume", "stop", "deleted"])
def test_deadline_pass_rechecks_capture_state_or_cleans_deleted_printer(farm, monkeypatch, action):
    from modules.printers.monitors import moonraker_monitor as monitor
    replay = json.loads((Path(__file__).parents[1] / "fixtures/moonraker-control-replay.json").read_text())
    with farm.factory() as db:
        install_printer(db)
        command = "pause" if action == "deleted" else action
        sent = pending(db, monkeypatch, command)
        if action == "resume":
            paused = replay["resume"][-2]
            observe(db, paused["state"], sent + timedelta(seconds=paused["seconds"]))
            assert controls._read(db, 9)["action"] == "resume"
        when = sent + timedelta(seconds=1 if action == "deleted" else controls.DEADLINES[command])
        sample = replay[command][-1]
        if action == "deleted":
            db.execute(text("DELETE FROM printers WHERE id=9"))
            db.commit()
        reads = []
        def status(adapter, **kwargs):
            reads.append(1)
            return moonraker.MoonrakerStatus(raw_print_state=sample["state"], filename=sample.get("filename", ""),
                                              raw_data={"virtual_sdcard":{"is_active": sample.get("is_active")}})
        monkeypatch.setattr(REAL_ADAPTER, "get_status", status)
        clock = type("Clock", (datetime,), {"now": staticmethod(lambda tz: when)})
        monkeypatch.setattr(monitor, "datetime", clock)
        monkeypatch.setattr(controls, "datetime", clock)
        monkeypatch.setattr(monitor, "engine", farm.engine)
        passes = iter([False, False, True])
        monitor._check_control_deadlines(SimpleNamespace(wait=lambda _: next(passes)))
        assert controls._read(db, 9) is None
        assert reads == ([] if action == "deleted" else [1])
        assert not farm.delivered


def test_imported_monitor_launcher_starts_one_deadline_worker(monkeypatch):
    from modules.printers.monitors import moonraker_monitor as monitor
    calls = []
    class Thread:
        def __init__(self, target, **kw):
            self.target = target
        def start(self):
            calls.append(self.target)
        def is_alive(self):
            return True
    monkeypatch.setattr(monitor, "_rows", lambda: [])
    monkeypatch.setattr(monitor, "_deadline_thread", None, raising=False)
    monkeypatch.setattr(monitor.threading, "Thread", Thread)
    monitor.start_moonraker_monitors()
    monitor.start_moonraker_monitors()
    assert calls == [monitor._check_control_deadlines]


@pytest.mark.parametrize("use_live_engine", [False, True])
def test_existing_pause_explained_on_matching_detection_alert(farm, monkeypatch, use_live_engine):
    from core.base import AlertType, AlertSeverity
    from modules.notifications.models import Alert
    from modules.printers.services import automatic_moonraker_pause
    from core import db as core_db
    if use_live_engine:
        monkeypatch.setattr(core_db, "engine", farm.engine)
    engine = core_db.live_engine if use_live_engine else farm.engine
    with farm.factory() as db:
        install_printer(db)
        pending(db, monkeypatch, "pause")
        for detection_id in (17, 18):
            db.add(Alert(user_id=1, printer_id=9, alert_type=AlertType.SPAGHETTI_DETECTED,
                         severity=AlertSeverity.CRITICAL, title="Detection", message="Confidence: 99%",
                         metadata_json={"detection_id": detection_id}))
        db.commit()
        sends, _ = wire(monkeypatch, "no_reply", "printing")
        automatic_moonraker_pause(engine, 9, lambda: True, 18)
        messages = db.query(Alert.message).order_by(Alert.id).all()
        assert messages == [("Confidence: 99%",), ("Confidence: 99%. Automatic pause skipped: a pause is already pending.",)]
        assert sends == []


def test_stale_poll_cannot_expire_newer_record(farm, monkeypatch):
    with farm.factory() as db:
        install_printer(db)
        sent = pending(db, monkeypatch, "pause")
        status = moonraker.MoonrakerStatus(raw_print_state="printing")
        controls.observe_control(db, 9, "Test printer", status, observed_at=sent-timedelta(seconds=1),
                                 now=sent+timedelta(seconds=30))
        assert controls._read(db, 9) and not farm.delivered


@pytest.mark.parametrize("case", ["inactive", "no_status", "monitoring_locked"])
def test_deadline_worker_alerts_when_fresh_status_unavailable(farm, monkeypatch, case):
    from core import license_lock
    from modules.printers.monitors import moonraker_monitor as monitor
    with farm.factory() as db:
        install_printer(db)
        sent = pending(db, monkeypatch, "pause")
        if case == "inactive":
            db.execute(text("UPDATE printers SET is_active=0 WHERE id=9"))
            db.commit()
        if case == "monitoring_locked":
            monkeypatch.setattr(license_lock, "enabled", lambda: True)
            monkeypatch.setattr(license_lock, "snapshot", lambda db: object())
            monkeypatch.setattr(license_lock, "printer_allowed", lambda *args: False)
        reads = []
        def offline(*args, **kwargs):
            reads.append(1)
            raise OSError("no status")
        monkeypatch.setattr(REAL_ADAPTER, "get_status", offline)
        clock = type("Clock", (datetime,), {"now": staticmethod(lambda tz: sent + timedelta(seconds=31))})
        monkeypatch.setattr(monitor, "datetime", clock)
        monkeypatch.setattr(controls, "datetime", clock)
        monkeypatch.setattr(monitor, "engine", farm.engine)
        passes = iter([False, False, True])
        monitor._check_control_deadlines(SimpleNamespace(wait=lambda _: next(passes)))
        assert reads == ([1] if case == "no_status" else [])
        assert controls._read(db, 9) is None and len(farm.delivered) == 1


def test_failed_rollback_cannot_mask_a_sent_stop(farm, monkeypatch):
    from sqlalchemy.exc import OperationalError
    sends, _ = wire(monkeypatch, "no_reply", "unknown")
    def disconnected(*args, **kwargs):
        raise OperationalError("connection lost", None, None)
    with farm.factory() as db:
        install_printer(db)
        monkeypatch.setattr(controls, "_begin", disconnected)
        monkeypatch.setattr(db, "rollback", disconnected)
        result = controls.send_control(db, db.get(Printer, 9), "stop", {})
        assert result["outcome"] == "pending" and sends == ["stop"]


def test_resume_short_window_cannot_confirm_after_original_print_ended(farm, monkeypatch):
    from datetime import timezone
    from tests.test_contracts.test_temperature_alerts import add_print
    with farm.factory() as db:
        install_printer(db)
        add_print(farm, 9, 41)
        def run(adapter, action):
            with farm.engine.begin() as conn:
                conn.execute(text("UPDATE print_jobs SET status='completed', ended_at=:ended WHERE id=41"),
                             {"ended": datetime.now(timezone.utc)})
            adapter.last_control = (moonraker.ControlReply("no_reply"), True)
            return True
        monkeypatch.setattr(REAL_ADAPTER, "run_control", run)
        receipt = controls.send_control(db, db.get(Printer, 9), "resume", {})
        assert receipt["outcome"] == "pending" and receipt["message"] == controls.PENDING["resume"]
        assert controls._read(db, 9)["print_job_id"] == 41


@pytest.mark.parametrize("transport", ["unreachable", "rejected"])
def test_failed_pause_preserves_pending_stop(farm, monkeypatch, transport):
    from core.errors import OdinError
    with farm.factory() as db:
        install_printer(db)
        sent = pending(db, monkeypatch, "stop")
        before = controls._read(db, 9)
        wire(monkeypatch, transport, "unknown")
        with pytest.raises(OdinError):
            controls.send_control(db, db.get(Printer, 9), "pause", {"id": 2})
        assert controls._read(db, 9) == before
        observe(db, "cancelled", sent + timedelta(seconds=10))
        assert db.execute(text("SELECT action FROM audit_logs ORDER BY id")).scalars().all() == [
            "printer.stop_requested", "printer.pause_failed", "printer.stop_confirmed"]


@pytest.mark.parametrize("persistent", [False, True])
def test_sent_stop_retries_only_bookkeeping(farm, monkeypatch, caplog, persistent):
    begin = controls._begin
    transactions = []
    def fail_write(db, printer_id, record, outcome):
        transactions.append(db.connection().get_transaction())
        if persistent or len(transactions) == 1:
            raise RuntimeError("synthetic write failure")
        return begin(db, printer_id, record, outcome)

    monkeypatch.setattr(controls, "_begin", fail_write)
    sends, _ = wire(monkeypatch, "accepted", "unknown")
    with farm.factory() as db:
        install_printer(db)
        receipt = controls.send_control(db, db.get(Printer, 9), "stop", {"id": 2, "_token_id": 77})
        assert receipt["outcome"] == "pending" and sends == ["stop"]
        assert len(transactions) == 2 and transactions[0] is not transactions[1]
        if persistent:
            errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
            assert len(errors) == 1
            assert all(field in errors[0] for field in ("stop", "printer 9", "user_id=2", "token_id=77", "sent_at="))
            assert datetime.fromisoformat(errors[0].split("sent_at=")[1].split()[0]).tzinfo
        else:
            assert controls._read(db, 9)["actor_token_id"] == 77
            assert db.execute(text("SELECT action FROM audit_logs")).scalars().all() == ["printer.stop_requested"]


def test_concurrent_moonraker_dismissals_send_once(review_db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from threading import Event
    from fastapi import HTTPException
    from modules.vision.routes.detections import dismiss_and_resume
    from tests.test_contracts.test_monitor_command_channel import setup_resume

    setup_resume(review_db, "PAUSE")
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='fake.test' WHERE id=9"))
    review_db.commit()
    engine = review_db.get_bind()
    entered, release = Event(), Event()
    sends = []
    def post(request, **kwargs):
        sends.append((request.method, request.selector))
        if len(sends) == 1:
            entered.set()
            assert release.wait(10)
        return nullcontext(SimpleNamespace(status=200))
    monkeypatch.setattr(moonraker, "urlopen", post)
    monkeypatch.setattr(REAL_ADAPTER, "get_status", lambda *a, **kw: moonraker.MoonrakerStatus(raw_print_state="paused"))

    def dismiss():
        with Session(engine) as db:
            return dismiss_and_resume(701, {"id": 3, "role": "admin"}, db)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(dismiss)
        try:
            assert entered.wait(5)
            with pytest.raises(HTTPException) as error:
                pool.submit(dismiss).result(5)
            assert error.value.status_code == 409
        finally:
            release.set()
        assert first.result(5)["resume_requested"]
    assert sends == [("POST", "/printer/print/resume")]
    audit = review_db.execute(text("SELECT details FROM audit_logs WHERE action='printer.resume_requested'")).scalar_one()
    for details in (json.loads(audit), controls._read(review_db, 9)):
        assert (details["via"], details["detection_id"]) == ("vision_dismissal", 701)


@pytest.mark.parametrize("newer", [False, True])
def test_unbound_resume_needs_transition_and_no_new_start(farm, monkeypatch, newer):
    with farm.factory() as db:
        install_printer(db)
        # A cached PAUSE can predate both the command and the current print.
        printer = db.get(Printer, 9)
        printer.gcode_state = "PAUSE"
        db.commit()
        wire(monkeypatch, "accepted", "printing")
        receipt = controls.send_control(db, printer, "resume", {})
        assert receipt["outcome"] == "pending"
        sent = datetime.fromisoformat(receipt["pending_control"]["sent_at"])
        observe(db, "paused", sent - timedelta(seconds=1))
        observe(db, "printing", sent + timedelta(seconds=1))
        assert controls._read(db, 9)["print_job_id"] is None
        observe(db, "paused", sent + timedelta(seconds=2))
        if newer:
            db.execute(text("INSERT INTO print_jobs(printer_id,started_at,ended_at,status) "
                            "VALUES(9,:started,:ended,'completed')"),
                       {"started": sent + timedelta(seconds=3), "ended": sent + timedelta(seconds=4)})
            db.commit()
        observe(db, "printing", sent + timedelta(seconds=5))
        assert bool(controls._read(db, 9)) == newer


def test_same_monitor_poll_records_new_start_before_confirming_resume(farm, monkeypatch):
    from modules.printers.monitors import moonraker_monitor as monitor
    with farm.factory() as db:
        install_printer(db)
        sent = pending(db, monkeypatch, "resume")
        observe(db, "paused", sent + timedelta(seconds=1))
        when = sent + timedelta(seconds=3)
        worker = monitor.MoonrakerMonitor(9, "Test", "fake.test")
        monkeypatch.setattr(monitor, "engine", farm.engine)
        monkeypatch.setattr(monitor, "datetime", type("Clock", (datetime,), {"now": staticmethod(lambda tz: when)}))
        status = moonraker.MoonrakerStatus(state=moonraker.MoonrakerState.PRINTING, raw_print_state="printing")
        monkeypatch.setattr(worker.printer, "get_status", lambda: status)
        def process(_):
            with farm.engine.begin() as conn:
                conn.execute(text("INSERT INTO print_jobs(printer_id,started_at,status) VALUES (9,:at,'running')"),
                             {"at": when})
            worker._running = False
        monkeypatch.setattr(worker, "_process_status", process)
        monkeypatch.setattr(monitor, "time", SimpleNamespace(sleep=lambda _: None))
        worker._running = True
        worker._poll_loop()
        assert controls._read(db, 9)["action"] == "resume"


def test_short_resume_window_cannot_adopt_a_newer_start(farm, monkeypatch):
    from datetime import timezone
    sent = datetime.now(timezone.utc).replace(microsecond=100000)
    monkeypatch.setattr(controls, "datetime", type("Clock", (datetime,), {"now": staticmethod(lambda tz: sent)}))
    with farm.factory() as db:
        install_printer(db)
        printer = db.get(Printer, 9)
        printer.gcode_state = "PAUSE"
        db.commit()
        def run(adapter, action):
            adapter.resume_paused_at = sent + timedelta(microseconds=2)
            with farm.engine.begin() as conn:
                conn.execute(text("INSERT INTO print_jobs(printer_id,started_at,status) VALUES(9,:at,'running')"),
                             {"at": sent + timedelta(microseconds=1)})
            adapter.last_control = moonraker.ControlReply("accepted"), True
            return True
        monkeypatch.setattr(REAL_ADAPTER, "run_control", run)
        receipt = controls.send_control(db, printer, "resume", {})
        assert receipt["outcome"] == "pending"
        assert controls._read(db, 9)["print_job_id"] is None
        observe(db, "printing", sent + timedelta(seconds=1))
        assert controls._read(db, 9)["action"] == "resume"


@pytest.mark.parametrize("state", ["unknown", "cancelled"])
def test_stop_retry_after_commit_ack_loss_keeps_one_audit(farm, monkeypatch, state):
    sends, _ = wire(monkeypatch, "accepted", state)
    with farm.factory() as db:
        install_printer(db)
        commit = db.commit
        def lose_first_ack():
            commit()
            monkeypatch.setattr(db, "commit", commit)
            raise RuntimeError("commit acknowledgement lost")
        monkeypatch.setattr(db, "commit", lose_first_ack)
        result = controls.send_control(db, db.get(Printer, 9), "stop", {})
        assert sends == ["stop"]
        expected = ["printer.stop_requested"]
        if state == "cancelled":
            expected.append("printer.stop_confirmed")
        assert result["outcome"] == ("pending" if state == "unknown" else "confirmed")
        assert db.execute(text("SELECT action FROM audit_logs ORDER BY id")).scalars().all() == expected


@pytest.mark.parametrize("transport", ["accepted", "no_reply"])
def test_resume_route_with_only_cached_pause_waits_for_transition(control_client, review_db, monkeypatch, transport):
    client, issue, _ = control_client
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='fake.test',gcode_state='PAUSE' WHERE id=299"))
    review_db.commit()
    posts, _ = wire(monkeypatch, transport, "printing")
    response = client.post("/printers/299/resume", headers=issue(["agent:write", "write:printers"]))
    assert posts == ["resume"] and response.status_code == 200
    assert response.json()["outcome"] == "pending"
    assert controls._read(review_db, 299)["print_job_id"] is None
    assert review_db.execute(text("SELECT gcode_state FROM printers WHERE id=299")).scalar_one() == "PAUSE"


def test_stop_retry_discards_failed_transaction_when_rollback_fails(farm, monkeypatch):
    sends, _ = wire(monkeypatch, "accepted", "unknown")
    with farm.factory() as db:
        install_printer(db)
        begin = controls._begin
        def partial_write(db, printer_id, record, outcome):
            db.execute(text("INSERT INTO system_config(key,value) VALUES('partial-stop','discard')"))
            monkeypatch.setattr(controls, "_begin", begin)
            raise RuntimeError("write failed")
        def failed_rollback():
            raise RuntimeError("rollback failed")
        monkeypatch.setattr(controls, "_begin", partial_write)
        monkeypatch.setattr(db, "rollback", failed_rollback)
        receipt = controls.send_control(db, db.get(Printer, 9), "stop", {})
        assert receipt["outcome"] == "pending" and sends == ["stop"]
        assert controls._read(db, 9)["sent_at"] == receipt["pending_control"]["sent_at"]
        assert db.execute(text("SELECT value FROM system_config WHERE key='partial-stop'")).first() is None
