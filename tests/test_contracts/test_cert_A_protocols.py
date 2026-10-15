"""Group A: Moonraker, PrusaLink and Elegoo SDCP at the transport boundary.

Protocol-accurate payloads (Moonraker REST object-query JSON, PrusaLink
/api/v1 JSON, SDCP v3 status frames) are served through the outermost
transport only (urllib for Moonraker, requests for PrusaLink, the
websocket-client WebSocketApp for Elegoo). Real adapters, parsers and
monitors write the real bootstrapped schema.
"""
import json
import sys
import threading
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import bind_engine, fernet_env, review_db  # noqa: F401


@pytest.fixture
def proto(review_db, fernet_env, monkeypatch):  # noqa: F811
    from modules.printers.monitors import moonraker_monitor, prusalink_monitor, elegoo_monitor
    import modules.notifications.job_events as job_events
    import modules.printers.route_utils as route_utils
    engine = review_db.get_bind()
    bind_engine(monkeypatch, engine)
    monkeypatch.setenv("ODIN_HOST_IP", "192.0.2.250")
    review_db.execute(text(
        "INSERT INTO printers(id,name,model,is_active,api_type,api_host,org_id,shared,gcode_state,slot_count,"
        "tags,timelapse_enabled,camera_enabled) VALUES "
        "(401,'Cert Voron','Voron 2.4',1,'moonraker','192.0.2.41',1,0,'IDLE',1,'[]',0,1),"
        "(402,'Cert MK4','MK4',1,'prusalink','192.0.2.42',1,0,'IDLE',1,'[]',0,1),"
        "(403,'Cert Centauri','Centauri Carbon',1,'elegoo','192.0.2.43',1,0,'IDLE',1,'[]',0,1)"))
    review_db.commit()
    clock = [5_000_000.0]
    events = []
    go2rtc = []
    for module in (moonraker_monitor, prusalink_monitor, elegoo_monitor):
        monkeypatch.setattr(module, "ws_push", lambda kind, payload=None, **kw: events.append((kind, payload)))
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None))
    monkeypatch.setattr(moonraker_monitor, "mqtt_republish", None, raising=False)
    monkeypatch.setattr(prusalink_monitor, "_mqtt_republish", None, raising=False)
    monkeypatch.setattr(elegoo_monitor, "_mqtt_republish", None, raising=False)
    monkeypatch.setattr(job_events, "_active_print_jobs", {})
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: go2rtc.append(config))
    return SimpleNamespace(db=review_db, clock=clock, events=events, go2rtc=go2rtc)


def row(db, printer_id, columns="*"):
    db.expire_all()
    return dict(db.execute(text(f"SELECT {columns} FROM printers WHERE id=:id"), {"id": printer_id}).mappings().one())


def telemetry(events, printer_id):
    return [p for kind, p in events if kind == "printer_telemetry" and p["printer_id"] == printer_id]


def decrypt(value):
    import core.crypto as crypto
    return crypto.decrypt(value)


# ==========================================================================
# Moonraker (REST)
# ==========================================================================

MOONRAKER_OBJECTS = ["webhooks", "configfile", "heater_bed", "extruder", "fan", "print_stats",
                     "virtual_sdcard", "display_status", "idle_timeout", "gcode_move",
                     "temperature_sensor chamber"]


def moonraker_status(state="printing", progress=0.25, fan=0.5, duration=600.0, message="Printer is ready"):
    return {"result": {"eventtime": 1234.5, "status": {
        "heater_bed": {"temperature": 60.1, "target": 60.0, "power": 0.31},
        "extruder": {"temperature": 214.8, "target": 215.0, "power": 0.45, "pressure_advance": 0.04},
        "print_stats": {"filename": "benchy.gcode", "total_duration": duration + 20, "print_duration": duration,
                        "filament_used": 1234.5, "state": state, "message": "",
                        "info": {"total_layer": 120, "current_layer": 30}},
        "virtual_sdcard": {"file_path": "/home/pi/printer_data/gcodes/benchy.gcode", "progress": progress,
                           "is_active": state == "printing", "file_position": 1000, "file_size": 4000},
        "display_status": {"progress": progress, "message": None},
        "idle_timeout": {"state": "Printing" if state == "printing" else "Idle", "printing_time": duration},
        "gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0, "speed": 3000.0},
        "fan": {"speed": fan, "rpm": None},
        "webhooks": {"state": "ready" if state != "error" else "error", "state_message": message},
        "temperature_sensor chamber": {"temperature": 35.2, "measured_min_temp": 20.0, "measured_max_temp": 40.0},
    }}}


class MoonrakerHTTP:
    """urllib.request.urlopen stand-in serving Moonraker JSON-RPC-over-HTTP replies."""

    def __init__(self):
        self.status = moonraker_status()
        self.requests = []
        self.down = False

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append((request.get_method(), url))
        if self.down:
            raise URLError("Synthetic host unreachable")
        path = url.split("192.0.2.41", 1)[1]
        if path == "/server/info":
            body = {"result": {"klippy_connected": True, "klippy_state": "ready", "moonraker_version": "v0.9.3"}}
        elif path == "/printer/info":
            body = {"result": {"state": "ready", "state_message": "Printer is ready", "hostname": "voron",
                               "software_version": "v0.12.0-300"}}
        elif path == "/printer/objects/list":
            body = {"result": {"objects": MOONRAKER_OBJECTS}}
        elif path == "/printer/objects/query?configfile":
            body = {"result": {"eventtime": 1.0, "status": {"configfile": {"settings": {"extruder": {"nozzle_diameter": 0.4}}}}}}
        elif path == "/server/webcams/list":
            body = {"result": {"webcams": [{"name": "Camera", "location": "printer", "service": "mjpegstreamer",
                                            "enabled": True, "stream_url": "/webcam/?action=stream",
                                            "snapshot_url": "/webcam/?action=snapshot"}]}}
        elif path.startswith("/printer/objects/query?"):
            body = self.status
        else:
            raise AssertionError(f"unexpected Moonraker request {path}")
        payload = json.dumps(body).encode()
        return _Response(payload)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


@pytest.fixture
def moonraker(proto, monkeypatch):
    from modules.printers.adapters import moonraker as adapter
    from modules.printers.monitors import moonraker_monitor as module
    http = MoonrakerHTTP()
    monkeypatch.setattr(adapter, "urlopen", http)
    trap = SimpleNamespace(WebSocketApp=lambda *a, **k: (_ for _ in ()).throw(AssertionError("websocket used")),
                           create_connection=lambda *a, **k: (_ for _ in ()).throw(AssertionError("websocket used")))
    monkeypatch.setitem(sys.modules, "websocket", trap)
    monitor = module.MoonrakerMonitor(401, "Cert Voron", "192.0.2.41")
    monkeypatch.setattr(monitor, "_try_dispatch", lambda: None)
    monkeypatch.setattr(module, "threading", SimpleNamespace(
        Thread=lambda *a, **kw: SimpleNamespace(start=lambda: None, is_alive=lambda: False),
        RLock=threading.RLock, Lock=threading.Lock, Event=threading.Event))
    assert monitor.connect()

    def poll(times=1, step=0):
        for _ in range(times):
            # One iteration of the real _poll_loop body.
            calls = []

            def sleep(seconds):
                calls.append(seconds)
                monitor._running = False
            monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: proto.clock[0], sleep=sleep))
            monitor._running = True
            monitor._poll_loop()
            proto.clock[0] += step
    return SimpleNamespace(monitor=monitor, http=http, poll=poll, module=module)


def test_moonraker_rest_poll_persists_temps_progress_state_fan(proto, moonraker):
    moonraker.poll()
    r = row(proto.db, 401, "bed_temp,bed_target_temp,nozzle_temp,nozzle_target_temp,gcode_state,fan_speed,last_seen,nozzle_diameter")
    assert (r["bed_temp"], r["bed_target_temp"], r["nozzle_temp"], r["nozzle_target_temp"]) == (60.1, 60.0, 214.8, 215.0)
    assert r["gcode_state"] == "PRINTING" and r["fan_speed"] == 50 and r["last_seen"] and r["nozzle_diameter"] == 0.4
    pushed = telemetry(proto.events, 401)[-1]
    assert pushed["progress"] == 25.0 and pushed["remaining_min"] == 30
    assert (pushed["current_layer"], pushed["total_layers"]) == (30, 120)


def test_moonraker_job_lifecycle_from_polled_state(proto, moonraker):
    moonraker.poll(step=11)
    moonraker.http.status = moonraker_status(state="complete", progress=1.0)
    moonraker.poll()
    proto.db.expire_all()
    jobs = proto.db.execute(text("SELECT status,job_name FROM print_jobs WHERE printer_id=401")).all()
    assert [tuple(j) for j in jobs] == [("completed", "benchy.gcode")]


def test_moonraker_webcam_url_discovered_and_streamed(proto, moonraker):
    r = row(proto.db, 401, "camera_url")
    assert decrypt(r["camera_url"]) == "http://192.0.2.41/webcam/?action=stream"
    assert proto.go2rtc[-1]["streams"]["printer_401"] == "ffmpeg:http://192.0.2.41/webcam/?action=stream#video=h264"


def test_moonraker_uses_rest_only_and_never_requests_a_file_list(proto, moonraker):
    """Evidence for claim text: no WebSocket transport and no file-list retrieval."""
    moonraker.poll(times=3, step=4)
    paths = [url.split("192.0.2.41", 1)[1] for _, url in moonraker.http.requests]
    assert {m for m, _ in moonraker.http.requests} == {"GET"}
    assert not any(p.startswith("/server/files") for p in paths)
    assert sum(p.startswith("/printer/objects/query?heater_bed") for p in paths) == 3
    assert moonraker.module.POLL_INTERVAL == 3


def test_moonraker_snapshots_every_60s_while_printing(proto, moonraker):
    for step in (30, 31, 1):
        moonraker.poll(step=step)
    proto.db.expire_all()
    rows = proto.db.execute(text("SELECT bed_temp,nozzle_temp,fan_speed FROM printer_telemetry WHERE printer_id=401")).all()
    assert [tuple(r) for r in rows] == [(60.1, 214.8, 50.0), (60.1, 214.8, 50.0)]


def test_moonraker_unreachable_printer_is_marked_offline(proto, moonraker):
    limit = moonraker.module.MAX_CONSECUTIVE_FAILURES
    moonraker.poll(step=11)
    seen = row(proto.db, 401, "last_seen")["last_seen"]
    moonraker.http.down = True
    moonraker.poll(times=limit - 1, step=11)
    r = row(proto.db, 401, "gcode_state,last_seen")
    assert r["gcode_state"] != "OFFLINE"  # transient failures are debounced
    assert r["last_seen"] == seen  # but never refresh the heartbeat
    moonraker.poll(step=11)
    r = row(proto.db, 401, "gcode_state,last_seen")
    assert r["gcode_state"] == "OFFLINE"
    assert r["last_seen"] == seen


def test_moonraker_successful_poll_resets_failure_debounce(proto, moonraker):
    limit = moonraker.module.MAX_CONSECUTIVE_FAILURES
    moonraker.poll(step=11)
    moonraker.http.down = True
    moonraker.poll(times=limit - 1, step=11)
    moonraker.http.down = False
    moonraker.poll(step=11)
    moonraker.http.down = True
    moonraker.poll(times=limit - 1, step=11)
    assert row(proto.db, 401, "gcode_state")["gcode_state"] != "OFFLINE"


def test_moonraker_disconnect_handler_marks_offline(proto, moonraker):
    moonraker.poll(step=11)
    moonraker.monitor._running = False
    moonraker.monitor._handle_disconnect()
    assert row(proto.db, 401, "gcode_state")["gcode_state"] == "OFFLINE"


def test_moonraker_fan_turning_off_is_reported_as_zero(proto, moonraker):
    moonraker.poll(step=11)
    moonraker.http.status = moonraker_status(fan=0.0)
    moonraker.poll()
    assert row(proto.db, 401, "fan_speed")["fan_speed"] == 0


# ==========================================================================
# PrusaLink (REST polling)
# ==========================================================================

def prusa_status(state="PRINTING", progress=42.0, fan_print=5964, remaining=1980, message=None):
    printer = {"state": state, "temp_bed": 60.0, "target_bed": 60.0, "temp_nozzle": 214.9, "target_nozzle": 215.0,
               "axis_z": 1.2, "flow": 100, "speed": 100, "fan_hotend": 3099, "fan_print": fan_print}
    if message:
        printer["status_printer"] = {"ok": False, "message": message}
    return {"job": {"id": 297, "progress": progress, "time_remaining": remaining, "time_printing": 1440},
            "storage": {"path": "/usb/", "name": "usb", "read_only": False},
            "printer": printer}


class PrusaHTTP:
    """requests.get/head stand-in serving PrusaLink v1 replies."""

    def __init__(self):
        self.status = prusa_status()
        self.requests = []
        self.down = False

    def _reply(self, code, body=None):
        return SimpleNamespace(status_code=code, json=lambda: body, text=json.dumps(body))

    def get(self, url, headers=None, auth=None, timeout=None):
        import requests
        self.requests.append(("GET", url, bool(headers and headers.get("X-Api-Key")), auth is not None))
        if self.down:
            raise requests.exceptions.ConnectionError("Synthetic host unreachable")
        path = url.split("192.0.2.42:80", 1)[1]
        if path == "/api/v1/status":
            return self._reply(200, self.status)
        if path == "/api/v1/cameras":
            return self._reply(200, [{"camera_id": "cam0", "config": {"name": "Buddy3D", "resolution": "1280x720"},
                                      "connected": True, "detected": True, "stored": True, "registered": False}])
        return self._reply(404, {})

    def head(self, url, headers=None, **kwargs):
        self.requests.append(("HEAD", url, False, False))
        return self._reply(404)


@pytest.fixture
def prusalink(proto, monkeypatch):
    from modules.printers.adapters import prusalink as adapter
    from modules.printers.monitors import prusalink_monitor as module
    http = PrusaHTTP()
    monkeypatch.setattr(adapter.requests, "get", http.get)
    monkeypatch.setattr(adapter.requests, "head", http.head)
    monitor = module.PrusaLinkMonitorThread(402, "Cert MK4", "192.0.2.42", api_key="synthetic-key")
    monkeypatch.setattr(monitor, "_try_dispatch", lambda: None)
    monkeypatch.setattr(module, "threading", SimpleNamespace(
        Thread=lambda *a, **kw: SimpleNamespace(start=lambda: None),
        RLock=threading.RLock, Lock=threading.Lock, Event=threading.Event))

    def run(polls, step=11, before_poll=None):
        count = [0]

        def sleep(seconds):
            count[0] += 1
            proto.clock[0] += step
            if before_poll:
                before_poll(count[0])
            if count[0] >= polls:
                monitor._running = False
        monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: proto.clock[0], sleep=sleep))
        monitor._running = True
        monitor.run()
    return SimpleNamespace(monitor=monitor, http=http, run=run, module=module)


def test_prusalink_v1_status_poll_persists_temps_progress_state(proto, prusalink):
    prusalink.run(1)
    r = row(proto.db, 402, "bed_temp,nozzle_temp,nozzle_target_temp,gcode_state,last_seen,fan_speed")
    assert (r["bed_temp"], r["nozzle_temp"], r["nozzle_target_temp"]) == (60.0, 214.9, 215.0)
    assert r["gcode_state"] == "PRINTING" and r["last_seen"]
    pushed = telemetry(proto.events, 402)[-1]
    assert pushed["progress"] == 42.0 and pushed["remaining_min"] == 33
    assert pushed["fan_speed"] == 5964  # PrusaLink reports part-fan RPM
    assert prusalink.module.POLL_INTERVAL == 10
    assert all(r[2] for r in prusalink.http.requests if r[1].endswith("/api/v1/status"))  # X-Api-Key auth


def test_prusalink_camera_snapshot_endpoint_discovered(proto, prusalink):
    prusalink.run(1)
    assert decrypt(row(proto.db, 402, "camera_url")["camera_url"]) == "http://192.0.2.42:80/api/v1/cameras/snap"
    assert proto.go2rtc[-1]["streams"]["printer_402"] == "ffmpeg:http://192.0.2.42:80/api/v1/cameras/snap#video=h264"


def test_prusalink_error_state_records_hms_history_and_fails_job(proto, prusalink):
    def transition(poll):
        if poll == 1:
            prusalink.http.status = prusa_status(state="ERROR", message="Thermal runaway")
    prusalink.run(2, before_poll=transition)
    proto.db.expire_all()
    history = proto.db.execute(text("SELECT code,message,severity,source FROM hms_error_history WHERE printer_id=402")).all()
    assert [tuple(h) for h in history] == [("PRUSALINK_ERROR", "Thermal runaway", "error", "prusalink")]
    jobs = proto.db.execute(text("SELECT status FROM print_jobs WHERE printer_id=402")).all()
    assert [j[0] for j in jobs] == ["failed"]


def test_prusalink_snapshots_every_60s_while_printing(proto, prusalink):
    prusalink.run(8, step=10)
    proto.db.expire_all()
    rows = proto.db.execute(text("SELECT nozzle_temp FROM printer_telemetry WHERE printer_id=402")).all()
    assert len(rows) == 2  # t=0 and t=60 within 80s of 10s polls


def test_prusalink_real_transport_failure_goes_offline_on_fifth_poll(proto, prusalink):
    observed = []

    def outage(poll):
        observed.append(row(proto.db, 402, "gcode_state")["gcode_state"])
        prusalink.http.down = True
    prusalink.run(6, before_poll=outage)
    # observed[i] is the state after poll i (poll 0 healthy, polls 1..5 failing)
    assert observed[1:5] == ["PRINTING"] * 4
    assert observed[5] == "OFFLINE"


# ==========================================================================
# Elegoo SDCP (WebSocket push)
# ==========================================================================

def sdcp_frame(print_status=13, current=1, progress=10, box=28.5, fan=100, ticks=(300, 3000), filename="cube.gcode"):
    return {"Status": {
        "CurrentStatus": [current], "TimeLapseStatus": 0, "PlatFormType": 1,
        "TempOfHotbed": 60.2, "TempOfNozzle": 219.8, "TempOfBox": box,
        "TempTargetHotbed": 60, "TempTargetNozzle": 220, "TempTargetBox": 0,
        "CurrenCoord": "120.00,110.00,0.40", "CurrentFanSpeed": {"ModelFan": fan, "AuxiliaryFan": 0, "BoxFan": 30},
        "ZOffset": 0.0, "LightStatus": {"SecondLight": 1},
        "PrintInfo": {"Status": print_status, "CurrentLayer": 12, "TotalLayer": 120, "CurrentTicks": ticks[0],
                      "TotalTicks": ticks[1], "Filename": filename, "ErrorNumber": 0,
                      "TaskId": "synthetic-task", "PrintSpeedPct": 100, "Progress": progress}},
        "MainboardID": "synthetic-mainboard", "TimeStamp": 1760000000, "Topic": "sdcp/status/synthetic-mainboard"}


class SdcpSocket:
    """websocket.WebSocketApp stand-in: opens, then delivers frames on demand."""
    instances = []

    def __init__(self, url, **callbacks):
        self.url = url
        self.callbacks = callbacks
        self.closed = threading.Event()
        SdcpSocket.instances.append(self)

    def run_forever(self, **kwargs):
        self.callbacks["on_open"](self)
        self.closed.wait(10)

    def close(self):
        self.closed.set()

    def send(self, data):
        pass

    def push(self, frame):
        self.callbacks["on_message"](self, json.dumps(frame))

    def drop(self):
        self.callbacks["on_close"](self, None, None)


@pytest.fixture
def elegoo(proto, monkeypatch):
    from modules.printers.monitors import elegoo_monitor as module
    SdcpSocket.instances = []
    monkeypatch.setitem(sys.modules, "websocket", SimpleNamespace(WebSocketApp=SdcpSocket))
    monitor = module.ElegooMonitorThread(403, "Cert Centauri", "192.0.2.43")
    monkeypatch.setattr(monitor, "_discover_and_save_camera", lambda: None)
    assert monitor.client.connect()
    socket = SdcpSocket.instances[-1]

    def push(frame, step=11):
        socket.push(frame)
        proto.clock[0] += step
    yield SimpleNamespace(monitor=monitor, socket=socket, push=push, module=module)
    monitor.client.disconnect()


def test_elegoo_sdcp_websocket_push_persists_temps_progress_state_fan(proto, elegoo):
    assert elegoo.socket.url == "ws://192.0.2.43:3030/websocket"
    elegoo.push(sdcp_frame(print_status=8))
    r = row(proto.db, 403, "bed_temp,nozzle_temp,bed_target_temp,nozzle_target_temp,gcode_state,fan_speed,last_seen")
    assert (r["bed_temp"], r["nozzle_temp"], r["bed_target_temp"], r["nozzle_target_temp"]) == (60.2, 219.8, 60, 220)
    assert r["gcode_state"] == "PRINTING" and r["fan_speed"] == 100 and r["last_seen"]
    pushed = telemetry(proto.events, 403)[-1]
    assert pushed["progress"] == 10 and pushed["remaining_min"] == 45 and pushed["fan_speed"] == 100


def test_elegoo_enclosure_temperature_captured(proto, elegoo):
    elegoo.push(sdcp_frame(print_status=8, box=31.5))
    proto.db.expire_all()
    env = proto.db.execute(text("SELECT ams_unit,humidity,temperature FROM ams_telemetry WHERE printer_id=403")).all()
    assert [tuple(e) for e in env] == [(0, None, 31.5)]


def test_elegoo_print_failure_detected_on_state_transition(proto, elegoo):
    elegoo.push(sdcp_frame(print_status=8))
    elegoo.push(sdcp_frame(print_status=0, current=0))
    proto.db.expire_all()
    jobs = proto.db.execute(text("SELECT status,job_name FROM print_jobs WHERE printer_id=403")).all()
    assert [tuple(j) for j in jobs] == [("failed", "cube.gcode")]


def test_elegoo_completion_is_not_a_failure(proto, elegoo):
    elegoo.push(sdcp_frame(print_status=8))
    elegoo.push(sdcp_frame(print_status=16, current=0, progress=100))
    proto.db.expire_all()
    assert [j[0] for j in proto.db.execute(text("SELECT status FROM print_jobs WHERE printer_id=403")).all()] == ["completed"]


def test_elegoo_snapshots_every_60s_while_printing(proto, elegoo):
    for _ in range(8):
        elegoo.push(sdcp_frame(print_status=8), step=10)
    proto.db.expire_all()
    assert proto.db.execute(text("SELECT COUNT(*) FROM printer_telemetry WHERE printer_id=403")).scalar_one() == 2


def run_elegoo_watchdog(elegoo, proto, monkeypatch, silence):
    """Real ElegooMonitorThread.run health check after `silence` seconds without a status frame."""
    elegoo.push(sdcp_frame(print_status=8), step=0)
    started = proto.clock[0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            proto.clock[0] = started + silence
        else:
            elegoo.monitor._running = False
    monkeypatch.setattr(elegoo.module, "time", SimpleNamespace(time=lambda: proto.clock[0], sleep=sleep))
    elegoo.monitor._running = True
    elegoo.monitor.run()
    return sleeps


@pytest.mark.parametrize("silence,expected", [(60, "PRINTING"), (61, "OFFLINE")])
def test_elegoo_status_timeout_marks_offline_after_60_seconds(proto, elegoo, monkeypatch, silence, expected):
    run_elegoo_watchdog(elegoo, proto, monkeypatch, silence)
    assert row(proto.db, 403, "gcode_state")["gcode_state"] == expected


def test_elegoo_closed_socket_with_failed_reconnect_is_marked_offline(proto, elegoo, monkeypatch):
    elegoo.push(sdcp_frame(print_status=8), step=0)
    elegoo.socket.drop()
    attempts = [0]

    def failing_connect():
        attempts[0] += 1
        proto.clock[0] += 31
        if attempts[0] >= 4:
            elegoo.monitor._running = False
        return False
    monkeypatch.setattr(elegoo.monitor.client, "connect", failing_connect)
    monkeypatch.setattr(elegoo.module, "time", SimpleNamespace(time=lambda: proto.clock[0], sleep=lambda s: None))
    elegoo.monitor._running = True
    elegoo.monitor.run()
    assert attempts[0] == 4  # > 120s without status
    assert row(proto.db, 403, "gcode_state")["gcode_state"] == "OFFLINE"
