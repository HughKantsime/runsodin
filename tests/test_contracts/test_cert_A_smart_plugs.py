"""Group A: smart plugs certified against a real local HTTP plug.

A real HTTP server on 127.0.0.1 plays the Tasmota device and the Home
Assistant REST API (the outermost boundary). MQTT plugs use the shipped
republish client with only paho's socket object replaced. Routes, auth,
crypto, schema, event bus and the dispatch power worker process are real.
"""
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    bind_engine, broker_socket, cert_app, cert_client, fernet_env, review_db,
)


class PlugDevice:
    def __init__(self):
        self.power = False
        self.total_kwh = 1.5
        self.requests = []


@pytest.fixture
def plug_server():
    device = PlugDevice()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, code=200):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            device.requests.append(("GET", self.path, dict(self.headers), None))
            if self.path.startswith("/cm?cmnd="):
                command = self.path.split("cmnd=", 1)[1]
                if command.lower() == "power%20on":
                    device.power = True
                elif command.lower() == "power%20off":
                    device.power = False
                elif command.lower() == "power%20toggle":
                    device.power = not device.power
                elif command == "Status%208":
                    return self.reply({"StatusSNS": {"Time": "2026-10-14T12:00:00", "ENERGY": {
                        "Total": device.total_kwh, "Yesterday": 0.1, "Today": 0.2, "Power": 42,
                        "Voltage": 230, "Current": 0.18}}})
                return self.reply({"POWER": "ON" if device.power else "OFF"})
            if self.path == "/api/states/switch.cert_plug":
                return self.reply({"entity_id": "switch.cert_plug", "state": "on" if device.power else "off"})
            if self.path == "/api/states/sensor.cert_plug_energy":
                return self.reply({"entity_id": "sensor.cert_plug_energy", "state": str(device.total_kwh)})
            self.reply({"message": "not found"}, 404)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else None
            device.requests.append(("POST", self.path, dict(self.headers), body))
            if self.path in ("/api/services/switch/turn_on", "/api/services/switch/turn_off",
                             "/api/services/switch/toggle"):
                device.power = {"turn_on": True, "turn_off": False}.get(self.path.rsplit("/", 1)[1], not device.power)
                return self.reply([])
            self.reply({"message": "not found"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    device.host = f"127.0.0.1:{server.server_address[1]}"
    yield device
    server.shutdown()
    server.server_close()


@pytest.fixture
def plugs(cert_app, review_db, plug_server, monkeypatch):
    import modules.printers.smart_plug as smart_plug
    # The plug device of this test is a real HTTP server on 127.0.0.1, which the
    # device destination policy rejects in production. Exempt exactly that address
    # (also in the dispatch worker subprocess); test_cert_A_destination_policy
    # covers the policy itself.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
    bind_engine(monkeypatch, review_db.get_bind())
    monkeypatch.setattr(smart_plug, "engine", review_db.get_bind())
    monkeypatch.setattr(smart_plug, "_cooldown_timers", {})
    monkeypatch.setattr(smart_plug, "_energy_starts", {})
    monkeypatch.setattr(smart_plug, "_energy_ended", {})
    return SimpleNamespace(client=cert_app, db=review_db, device=plug_server, module=smart_plug)


def configure(plugs, body, role="operator"):
    response = plugs.client.put("/printers/301/plug", headers=plugs.client.roles[role], json=body)
    assert response.status_code == 200, response.text
    return response


def stored(plugs, columns):
    plugs.db.expire_all()
    return dict(plugs.db.execute(text(f"SELECT {columns} FROM printers WHERE id=301")).mappings().one())


def test_tasmota_plug_on_off_status_and_energy_over_http(plugs):
    configure(plugs, {"type": "tasmota", "host": plugs.device.host, "auto_on": True, "auto_off": True, "cooldown_minutes": 7})
    config = plugs.client.get("/printers/301/plug", headers=plugs.client.roles["viewer"]).json()
    assert config == {"type": "tasmota", "host": plugs.device.host, "entity_id": None, "auto_on": True, "auto_off": True,
                      "cooldown_minutes": 7, "power_state": None, "energy_kwh": 0, "configured": True}
    on = plugs.client.post("/printers/301/plug/on", headers={**plugs.client.roles["operator"], "X-Power-Request-Nonce": "a"*32})
    assert on.status_code == 200 and on.json() == {"power_state": True}
    assert stored(plugs, "plug_power_state")["plug_power_state"] in (1, True)
    off = plugs.client.post("/printers/301/plug/off", headers={**plugs.client.roles["admin"], "X-Power-Request-Nonce": "b"*32})
    assert off.json() == {"power_state": False}
    state = plugs.client.get("/printers/301/plug/state", headers=plugs.client.roles["viewer"])
    assert state.json() == {"power_state": False}
    energy = plugs.client.get("/printers/301/plug/energy", headers=plugs.client.roles["viewer"])
    assert energy.json() == {"total_kwh": 1.5, "power_w": 42, "voltage": 230, "current": 0.18}
    assert [(m, p) for m, p, _, _ in plugs.device.requests] == [
        ("GET", "/cm?cmnd=Power%20ON"), ("GET", "/cm?cmnd=Power%20OFF"),
        ("GET", "/cm?cmnd=Power%20"), ("GET", "/cm?cmnd=Status%208")]


def test_home_assistant_plug_uses_rest_api_with_bearer_token(plugs):
    token = secrets.token_urlsafe(24)  # generated per run, not a real credential
    configure(plugs, {"type": "homeassistant", "host": f"http://{plugs.device.host}", "entity_id": "switch.cert_plug",
                      "auth_token": token})
    row = stored(plugs, "plug_auth_token")
    assert row["plug_auth_token"] and token not in row["plug_auth_token"]
    assert plugs.client.post("/printers/301/plug/on", headers=plugs.client.roles["operator"]).json() == {"power_state": True}
    method, path, headers, body = plugs.device.requests[0]
    assert (method, path, json.loads(body)) == ("POST", "/api/services/switch/turn_on", {"entity_id": "switch.cert_plug"})
    assert headers["Authorization"] == f"Bearer {token}"
    assert plugs.device.requests[1][:2] == ("GET", "/api/states/switch.cert_plug")
    assert plugs.client.post("/printers/301/plug/off", headers=plugs.client.roles["operator"]).json() == {"power_state": False}
    assert plugs.client.get("/printers/301/plug/energy", headers=plugs.client.roles["viewer"]).json()["total_kwh"] == 1.5
    assert "sensor.cert_plug_energy" in plugs.device.requests[-1][1]


def test_mqtt_plug_publishes_command_through_configured_broker(plugs, broker_socket):
    import modules.notifications.mqtt_republish as republish
    plugs.db.execute(text("INSERT INTO system_config(key,value) VALUES('mqtt_republish_enabled','true'),"
                          "('mqtt_republish_host','192.0.2.70'),('mqtt_republish_port','1883')"))
    plugs.db.commit()
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(republish, "_client", None)
    mp.setattr(republish, "_config_cache", None)
    try:
        configure(plugs, {"type": "mqtt", "entity_id": "cmnd/certplug/POWER"})
        response = plugs.client.post("/printers/301/plug/on", headers=plugs.client.roles["operator"])
        assert response.status_code == 200 and response.json() == {"accepted": True, "power_state": None}
        sock = broker_socket.instances[-1]
        assert sock.endpoint[:2] == ("192.0.2.70", 1883)
        assert sock.published == [("cmnd/certplug/POWER", {"raw": "ON", "qos": 1, "retain": False})]
    finally:
        republish._client = None
        mp.undo()


@pytest.mark.parametrize("method,path,body", [
    ("put", "/printers/301/plug", {"type": "tasmota", "host": "127.0.0.1:9"}),
    ("post", "/printers/301/plug/on", None), ("post", "/printers/301/plug/off", None),
    ("post", "/printers/301/plug/toggle", None),
])
def test_viewer_cannot_configure_or_power_plug(plugs, method, path, body):
    configure(plugs, {"type": "tasmota", "host": plugs.device.host})
    response = getattr(plugs.client, method)(path, headers=plugs.client.roles["viewer"], json=body)
    assert response.status_code == 403
    assert plugs.device.requests == []


def test_invalid_plug_type_rejected(plugs):
    response = plugs.client.put("/printers/301/plug", headers=plugs.client.roles["operator"], json={"type": "zigbee"})
    assert response.status_code == 400


class ManualTimer:
    created = []

    def __init__(self, interval, function):
        self.interval, self.function, self.cancelled, self.daemon = interval, function, False, False
        ManualTimer.created.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


def test_completion_event_powers_off_after_configured_cooldown(plugs, monkeypatch):
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    from core import events
    configure(plugs, {"type": "tasmota", "host": plugs.device.host, "auto_off": True, "cooldown_minutes": 7})
    plugs.device.power = True
    plugs.db.execute(text("UPDATE printers SET gcode_state='IDLE',last_seen=:now WHERE id=301"),
                     {"now": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())})
    plugs.db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,observed_received_at) VALUES(901,301,'fixture.gcode','fixture',CURRENT_TIMESTAMP,'completed',CURRENT_TIMESTAMP)"))
    plugs.db.commit()
    ManualTimer.created = []
    monkeypatch.setattr(plugs.module.threading, "Timer", ManualTimer)
    monkeypatch.setattr(plugs.module, "_in_background", lambda function, *args: None)
    bus = InMemoryEventBus()
    plugs.module.register_subscribers(bus)
    bus.publish(Event(events.JOB_COMPLETED, "cert", {"printer_id": 301, "print_job_id": 901, "success": True}))
    assert [t.interval for t in ManualTimer.created] == [7 * 60]
    assert plugs.device.requests == []  # nothing sent before the cooldown elapses
    ManualTimer.created[0].function()
    assert [p for _, p, _, _ in plugs.device.requests] == ["/cm?cmnd=Power%20OFF"]
    assert plugs.device.power is False


def test_auto_off_disabled_keeps_plug_on_after_completion(plugs, monkeypatch):
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    from core import events
    configure(plugs, {"type": "tasmota", "host": plugs.device.host, "auto_off": False, "cooldown_minutes": 1})
    ManualTimer.created = []
    monkeypatch.setattr(plugs.module.threading, "Timer", ManualTimer)
    monkeypatch.setattr(plugs.module, "_in_background", lambda function, *args: None)
    bus = InMemoryEventBus()
    plugs.module.register_subscribers(bus)
    bus.publish(Event(events.JOB_COMPLETED, "cert", {"printer_id": 301, "success": True}))
    assert ManualTimer.created == [] and plugs.device.requests == []


def test_dispatch_power_preparation_turns_plug_on_before_job(plugs):
    """The real bounded worker process sends ON to the plug before dispatch proceeds."""
    configure(plugs, {"type": "tasmota", "host": plugs.device.host, "auto_on": True})
    preparation = plugs.module.prepare_bounded_dispatch_power(301, time.monotonic() + 20)
    assert preparation.accepted is True and preparation.required is True
    assert [p for _, p, _, _ in plugs.device.requests] == ["/cm?cmnd=Power%20ON"]
    assert stored(plugs, "plug_power_state")["plug_power_state"] in (1, True)


def test_dispatch_power_preparation_skips_when_auto_on_disabled(plugs):
    configure(plugs, {"type": "tasmota", "host": plugs.device.host, "auto_on": False})
    preparation = plugs.module.prepare_bounded_dispatch_power(301, time.monotonic() + 20)
    assert preparation.accepted is True and preparation.required is False
    assert plugs.device.requests == []


def test_per_job_energy_kwh_and_cost_from_plug_counter(plugs):
    configure(plugs, {"type": "tasmota", "host": plugs.device.host})
    plugs.db.execute(text("INSERT INTO system_config(key,value) VALUES('energy_cost_per_kwh','0.25')"))
    plugs.db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at) VALUES(7301,301,'e.3mf','Energy','completed',CURRENT_TIMESTAMP)"))
    plugs.db.execute(text("INSERT INTO print_archives(print_job_id,printer_id,print_name,status) VALUES(7301,301,'Energy','completed')"))
    plugs.db.execute(text("UPDATE printers SET plug_energy_kwh=0 WHERE id=301"))
    plugs.db.commit()
    plugs.device.total_kwh = 10.0
    plugs.module.record_energy_start(301, 7301)
    plugs.device.total_kwh = 10.8
    assert plugs.module.record_energy_for_print(301, 7301) == pytest.approx(0.8)
    plugs.db.expire_all()
    archive = plugs.db.execute(text("SELECT energy_kwh,energy_cost FROM print_archives WHERE print_job_id=7301")).one()
    assert archive[0] == pytest.approx(0.8) and archive[1] == pytest.approx(0.2)
    assert stored(plugs, "plug_energy_kwh")["plug_energy_kwh"] == pytest.approx(0.8)


@pytest.mark.parametrize("body", [
    {"type": "tasmota", "host": "127.0.0.1:8080"}, {"type": "tasmota", "host": "169.254.169.254"},
    {"type": "tasmota", "host": "localhost"}, {"type": "homeassistant", "host": "http://169.254.169.254:8123"},
    {"type": "homeassistant", "host": "file:///etc/passwd"}, {"type": "tasmota", "host": "http://192.0.2.60/x"},
])
def test_plug_put_rejects_blocked_or_malformed_hosts(cert_app, review_db, body):
    response = cert_app.put("/printers/301/plug", headers=cert_app.roles["operator"], json=body)
    assert response.status_code == 400
    review_db.expire_all()
    assert review_db.execute(text("SELECT plug_type FROM printers WHERE id=301")).scalar_one() is None


@pytest.mark.parametrize("value", [-1, 1441, 2.5, "5", True, None])
def test_plug_put_rejects_invalid_cooldown(plugs, value):
    response = plugs.client.put("/printers/301/plug", headers=plugs.client.roles["operator"],
                                json={"type": "tasmota", "host": plugs.device.host, "cooldown_minutes": value})
    assert response.status_code == 422


def test_plug_put_merges_partial_updates(plugs):
    configure(plugs, {"type": "tasmota", "host": plugs.device.host, "auto_on": True, "auto_off": True, "cooldown_minutes": 7})
    configure(plugs, {"auto_off": False})
    row = stored(plugs, "plug_type,plug_host,plug_auto_on,plug_auto_off,plug_cooldown_minutes")
    assert row["plug_type"] == "tasmota" and row["plug_host"] == plugs.device.host
    assert bool(row["plug_auto_on"]) is True and bool(row["plug_auto_off"]) is False and row["plug_cooldown_minutes"] == 7
    assert plugs.client.put("/printers/301/plug", headers=plugs.client.roles["operator"], json={"type": None}).status_code == 400
    assert stored(plugs, "plug_type")["plug_type"] == "tasmota"


def test_plug_token_kept_only_for_same_type_and_host(plugs):
    import core.crypto as crypto
    token = secrets.token_urlsafe(24)  # generated per run, not a real credential
    ha = f"http://{plugs.device.host}"
    configure(plugs, {"type": "homeassistant", "host": ha, "entity_id": "switch.cert_plug", "auth_token": token})
    configure(plugs, {"type": "homeassistant", "host": ha, "entity_id": "switch.other"})
    assert crypto.decrypt(stored(plugs, "plug_auth_token")["plug_auth_token"]) == token
    configure(plugs, {"host": "http://192.0.2.61:8123"})
    assert stored(plugs, "plug_auth_token")["plug_auth_token"] is None
