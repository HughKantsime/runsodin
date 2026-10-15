"""Group A certification harness (fixtures only, no tests).

Real FastAPI routers + real bootstrapped SQLite schema. Only the outermost
boundary is replaced: the paho MQTT client object (the physical printer's
broker socket) and HTTP/WebSocket transports to printers.

Credentials are generated at runtime (Fernet key, synthetic serial/access
code); nothing here is a real secret.
"""
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

SERIAL = "CERTA0SERIAL01"
ACCESS = "certa-access"


class FakeBrokerSocket:
    """Stand-in for paho.mqtt.client.Client: records what would hit the wire.

    Behaves like a broker that accepts the connection (CONNACK rc=0) and
    acknowledges every QoS0 publish. `refuse=True` simulates a CONNACK
    failure; `publish_rc` simulates a local publish refusal.
    """

    instances = []
    refuse = False
    publish_rc = 0

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.client_id = kwargs.get("client_id")
        self.protocol = kwargs.get("protocol")
        self.credentials = None
        self.tls = None
        self.tls_insecure = None
        self.endpoint = None
        self.subscriptions = []
        self.published = []
        self.disconnected = False
        self.loop_stopped = False
        self.on_connect = self.on_message = self.on_disconnect = None
        FakeBrokerSocket.instances.append(self)

    def username_pw_set(self, username, password=None):
        self.credentials = (username, password)

    def tls_set(self, *args, **kwargs):
        self.tls = kwargs or True

    def tls_set_context(self, context):
        self.tls = context

    def tls_insecure_set(self, value):
        self.tls_insecure = value

    def connect(self, host, port, keepalive=60):
        self.endpoint = (host, port, keepalive)
        return 0

    def connect_async(self, host, port, keepalive=60):
        return self.connect(host, port, keepalive)

    def loop_start(self):
        rc = 5 if FakeBrokerSocket.refuse else 0
        if self.on_connect:
            try:
                self.on_connect(self, None, {}, rc)
            except TypeError:
                self.on_connect(self, None, {}, rc, None)
        return 0

    def is_connected(self):
        return not FakeBrokerSocket.refuse and not self.disconnected

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False):
        try:
            body = json.loads(payload)
        except (TypeError, ValueError):
            body = payload
        self.published.append((topic, body) if isinstance(body, dict) else (topic, {"raw": body, "qos": qos, "retain": retain}))
        rc = FakeBrokerSocket.publish_rc
        return SimpleNamespace(rc=rc, wait_for_publish=lambda timeout=None: None,
                               is_published=lambda: rc == 0)

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        self.loop_stopped = True

    def deliver(self, payload, retain=False):
        """Printer -> ODIN: deliver one report frame to the subscribed client."""
        message = SimpleNamespace(payload=json.dumps(payload).encode(), retain=retain,
                                  topic=self.subscriptions[0] if self.subscriptions else "")
        self.on_message(self, None, message)


def commands(sockets=None):
    """All (topic, payload) pairs published except the automatic pushall/get_version."""
    sockets = FakeBrokerSocket.instances if sockets is None else sockets
    return [(topic, body) for sock in sockets for topic, body in sock.published
            if body.get("pushing", {}).get("command") != "pushall"
            and body.get("info", {}).get("command") != "get_version"]


@pytest.fixture
def fernet_env(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("ODIN_TELEMETRY_V2", "0")


@pytest.fixture
def broker_socket(monkeypatch):
    """Replace the paho client constructor used by the legacy Bambu adapter."""
    import paho.mqtt.client as paho
    FakeBrokerSocket.instances = []
    FakeBrokerSocket.refuse = False
    FakeBrokerSocket.publish_rc = 0
    monkeypatch.setattr(paho, "Client", FakeBrokerSocket)
    return FakeBrokerSocket


def bearer(username, role, group_id=1):
    from core.auth import create_access_token
    token = create_access_token(data={"sub": username, "user_id": 0, "role": role, "group_id": group_id})
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def cert_client(review_db, fernet_env, monkeypatch):  # noqa: F811
    """Real printer routers on the real bootstrapped schema with role-bearing users."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from core.db import get_db
    from core.errors import OdinError
    from modules.printers import (routes_controls, routes_ams_env, routes_status, routes_crud,
                                  routes_nozzle, routes_smart_plug)
    monkeypatch.delenv("API_KEY", raising=False)
    review_db.execute(text(
        "INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
        "(6,'operator','operator@example.test','fixture','operator',1,1)"))
    # Bootstrap fixture rows predate several NOT NULL response fields.
    review_db.execute(text("UPDATE printers SET slot_count=1,tags='[]',timelapse_enabled=0,shared=0 WHERE id IN (9,10)"))
    review_db.commit()
    app = FastAPI()

    @app.exception_handler(OdinError)
    async def structured_error(request, error):
        return JSONResponse(status_code=error.status, content=error.to_envelope())

    def database():
        with Session(review_db.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = database
    for module in (routes_crud, routes_controls, routes_ams_env, routes_status, routes_nozzle,
                   routes_smart_plug):
        app.include_router(module.router)
    # Short command sessions sleep for socket teardown; no wall-clock needed here.
    monkeypatch.setattr(routes_controls, "time", SimpleNamespace(sleep=lambda s: None, time=__import__("time").time))
    client = TestClient(app)
    client.roles = {
        "viewer": bearer("student", "viewer"),
        "operator": bearer("operator", "operator"),
        "admin": bearer("admin", "admin"),
    }
    return client


@pytest.fixture
def cert_app(cert_client, review_db):  # noqa: F811
    import core.crypto as crypto
    review_db.execute(text(
        "INSERT INTO printers(id,name,model,machine_type,is_active,api_type,api_host,api_key,org_id,shared,"
        "gcode_state,slot_count,tags,timelapse_enabled) VALUES "
        "(301,'Cert X1C','X1C','X1C',1,'bambu','192.0.2.31',:key,1,0,'RUNNING',4,'[]',0),"
        "(302,'Cert Voron','Voron','Voron',1,'moonraker','192.0.2.32',NULL,1,0,'RUNNING',1,'[]',0)"),
        {"key": crypto.encrypt(f"{SERIAL}|{ACCESS}")})
    review_db.commit()
    return cert_client


def bind_engine(monkeypatch, engine):
    """Point every daemon-side DB entry point at the owned real-schema engine."""
    import sys
    import core.db as core_db
    import core.db_utils  # noqa: F401
    import modules.notifications.event_dispatcher  # noqa: F401
    import modules.notifications.alert_dispatch  # noqa: F401
    import modules.notifications.quiet_hours  # noqa: F401
    import modules.printers.monitors.mqtt_printer  # noqa: F401
    import modules.printers.monitors.mqtt_monitor  # noqa: F401
    import modules.printers.monitors.moonraker_monitor  # noqa: F401
    import modules.printers.monitors.prusalink_monitor  # noqa: F401
    import modules.printers.monitors.elegoo_monitor  # noqa: F401
    original = core_db.engine
    for name, module in list(sys.modules.items()):
        if (name.startswith("modules.") or name.startswith("core.")) and getattr(module, "engine", None) is original:
            monkeypatch.setattr(module, "engine", engine)


def load_frames(name):
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / f"{name}.jsonl"
    frames = []
    for line in path.read_text().splitlines():
        record = json.loads(line)
        if record.get("direction") == "recv" and isinstance(record.get("payload"), dict):
            frames.append((record["ts"], record["payload"]))
    return frames


@pytest.fixture
def wire_monitor(review_db, broker_socket, fernet_env, monkeypatch):  # noqa: F811
    """Legacy (default) Bambu monitor connected through the real BambuPrinter.

    Returns (monitor, socket, clock, ws_events). Frames delivered through
    socket.deliver() travel BambuPrinter._on_message -> _parse_status ->
    PrinterMonitor._on_status -> owned real-schema DB.
    """
    from modules.printers.monitors import mqtt_printer as module
    engine = review_db.get_bind()
    bind_engine(monkeypatch, engine)
    review_db.execute(text(
        "INSERT INTO printers(id,name,model,machine_type,is_active,api_type,api_host,org_id,shared,"
        "gcode_state,slot_count,tags,timelapse_enabled,nozzle_diameter) VALUES "
        "(301,'Cert Bambu',NULL,NULL,1,'bambu','192.0.2.31',1,0,'IDLE',4,'[]',0,0.4)"))
    review_db.commit()
    clock = [1_000_000.0]
    events = []
    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None))
    monkeypatch.setattr(module, "ws_push", lambda kind, payload=None, **kw: events.append((kind, payload)))
    monkeypatch.setattr(module, "mqtt_republish", None)
    monkeypatch.setattr(module, "Thread", lambda *a, **kw: SimpleNamespace(start=lambda: None))
    go2rtc = []
    import modules.printers.route_utils as route_utils
    # go2rtc is an external process; capture the config it would be given.
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: go2rtc.append(config))
    monitor = module.PrinterMonitor(301, "Cert Bambu", "192.0.2.31", SERIAL, ACCESS)
    assert monitor.connect()
    sock = broker_socket.instances[-1]
    monitor.go2rtc_configs = go2rtc
    return monitor, sock, clock, events


def replay(sock, clock, frames, limit=None):
    for index, (ts, payload) in enumerate(frames):
        if limit is not None and index >= limit:
            break
        clock[0] = ts
        sock.deliver(payload)


@contextmanager
def monitor_db(engine):
    """core.db_utils-compatible connection on the owned real-schema engine."""
    from core.db_utils import _ConnectionAdapter
    raw = engine.raw_connection()
    try:
        yield _ConnectionAdapter(raw, False)
    finally:
        raw.close()
