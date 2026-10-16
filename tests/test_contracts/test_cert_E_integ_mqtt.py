"""Cert-E integ: F14 14.1 MQTT republish driven from real production event origins.

Printer side: captured Bambu LAN report frames enter the shipped monitor through the
paho-shaped printer socket (test_cert_A_harness). Republish side: the REAL paho client
created by modules.notifications.mqtt_republish connects to an owned local mosquitto
(127.0.0.1, ephemeral port) configured through system_config, and an independent paho
subscriber on '#' records what an external consumer (Home Assistant / Node-RED /
Ignition) would receive. Only the printer socket and the broker process are external.
"""
import json
import threading
import time

import paho.mqtt.client as _paho
import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    ACCESS, SERIAL, broker_socket, fernet_env, load_frames, replay, review_db, wire_monitor,
)
from tests.test_contracts.test_claims_backend_mqtt_republish_broker import broker  # noqa: F401
from tests.test_contracts.test_job_approval_notification_behavior import approval_client  # noqa: F401
from tests.test_contracts.test_license_user_limits import signed_license  # noqa: F401

REAL_CLIENT = _paho.Client  # captured before any fixture substitutes the printer socket
PREFIX = "certe-integ"


class Subscriber:
    def __init__(self, port):
        self.messages = []
        self.cond = threading.Condition()
        ready = threading.Event()
        self.client = REAL_CLIENT(_paho.CallbackAPIVersion.VERSION2, client_id="certe-integ-sub",
                                  protocol=_paho.MQTTv311)
        self.client.on_connect = lambda c, u, f, rc, p=None: c.subscribe("#", qos=0)
        self.client.on_subscribe = lambda *a: ready.set()
        self.client.on_message = self._on_message
        self.client.connect("127.0.0.1", port)
        self.client.loop_start()
        assert ready.wait(5), "subscriber never subscribed"

    def _on_message(self, client, userdata, message):
        with self.cond:
            self.messages.append((message.topic, json.loads(message.payload), message.retain))
            self.cond.notify_all()

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        with self.cond:
            while not predicate(self.messages):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.cond.wait(remaining)
            return True

    def flush(self, republish_module):
        """Barrier: a sentinel published after everything else proves earlier messages arrived."""
        republish_module._publish("certe-barrier", {"barrier": True})
        assert self.wait_for(lambda ms: any(t == f"{PREFIX}/certe-barrier" for t, _, _ in ms))
        with self.cond:
            return [m for m in self.messages if m[0] != f"{PREFIX}/certe-barrier"]

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


def _configure(db, port):
    rows = {"mqtt_republish_enabled": "true", "mqtt_republish_host": "127.0.0.1",
            "mqtt_republish_port": str(port), "mqtt_republish_topic_prefix": PREFIX}
    for key, value in rows.items():
        db.execute(text("DELETE FROM system_config WHERE key=:k"), {"k": key})
        db.execute(text("INSERT INTO system_config(key,value) VALUES(:k,:v)"), {"k": key, "v": value})
    db.commit()


@pytest.fixture
def republish(review_db, broker, monkeypatch):  # noqa: F811
    """Real republish module on the owned schema + broker; production handlers on a fresh bus."""
    import core.event_bus as event_bus
    from sqlalchemy import event as sa_event
    from modules.notifications import mqtt_republish
    engine = review_db.get_bind()

    def _fk_on(dbapi_connection, record):  # mirrors core/db.py SQLite connect listener
        cursor = dbapi_connection.cursor(); cursor.execute("PRAGMA foreign_keys=ON"); cursor.close()
    sa_event.listen(engine, "connect", _fk_on)
    monkeypatch.setattr(mqtt_republish, "engine", engine)
    monkeypatch.setattr(mqtt_republish, "_config_cache", None)
    monkeypatch.setattr(mqtt_republish, "_config_ts", 0)
    monkeypatch.setattr(mqtt_republish, "_client", None)
    bus = event_bus.InMemoryEventBus()
    mqtt_republish.register_subscribers(bus)
    monkeypatch.setattr(event_bus, "_bus", bus)
    _configure(review_db, broker)
    sub = Subscriber(broker)
    yield mqtt_republish, sub
    mqtt_republish.disconnect()
    sub.close()
    sa_event.remove(engine, "connect", _fk_on)


@pytest.fixture
def bridged_monitor(wire_monitor, republish, monkeypatch):  # noqa: F811
    """Bambu monitor whose printer socket is simulated but whose republish client is real paho."""
    from modules.printers.monitors import mqtt_printer
    from tests.test_contracts.test_cert_A_harness import FakeBrokerSocket
    module, sub = republish

    def client_factory(*args, **kwargs):
        if str(kwargs.get("client_id", "")).startswith("odin-republish"):
            return REAL_CLIENT(*args, **kwargs)
        return FakeBrokerSocket(*args, **kwargs)
    monkeypatch.setattr(_paho, "Client", client_factory)
    monkeypatch.setattr(mqtt_printer, "mqtt_republish", module)
    monitor, sock, clock, events = wire_monitor
    return monitor, sock, clock, module, sub


def _topics(messages):
    return [t for t, _, _ in messages]


def test_printer_status_and_print_lifecycle_reach_external_broker_from_real_capture(bridged_monitor, review_db):
    monitor, sock, clock, module, sub = bridged_monitor
    replay(sock, clock, load_frames("bambu-a1-happy-path"))
    messages = sub.flush(module)
    status = [p for t, p, _ in messages if t == f"{PREFIX}/cert_bambu/status"]
    # Lifecycle is published under the printer's own topic name, like its status.
    jobs = [p for t, p, _ in messages if t == f"{PREFIX}/cert_bambu/job"]
    assert status, _topics(messages)[:20]
    assert all(p["printer_id"] == 301 and p["name"] == "Cert Bambu" for p in status)
    assert {"bed_temp", "nozzle_temp", "state", "progress"} <= set(status[-1])
    assert max(p["progress"] or 0 for p in status) == 100
    assert "RUNNING" in {p["state"] for p in status}
    events = [p["event"] for p in jobs]
    assert events[:1] == ["started"] and "completed" in events, events
    assert all(p["printer_id"] == 301 for p in jobs)
    assert all(r is False for _, _, r in messages)
    # The DB lifecycle matches what the external consumer saw.
    review_db.expire_all()
    assert review_db.execute(text("SELECT status FROM print_jobs WHERE printer_id=301")).scalars().all() == ["completed"]


def test_print_lifecycle_uses_the_same_printer_topic_as_status(bridged_monitor, review_db):
    monitor, sock, clock, module, sub = bridged_monitor
    replay(sock, clock, load_frames("bambu-a1-happy-path"))
    messages = sub.flush(module)
    status_printers = {t.split("/")[1] for t in _topics(messages) if t.endswith("/status")}
    job_printers = {t.split("/")[1] for t in _topics(messages) if t.endswith("/job")}
    assert job_printers and job_printers == status_printers, (job_printers, status_printers)


def test_failed_print_and_printer_error_alerts_reach_external_broker(bridged_monitor, review_db):
    monitor, sock, clock, module, sub = bridged_monitor
    replay(sock, clock, load_frames("bambu-h2d-failure-arc"))
    messages = sub.flush(module)
    alerts = [p for t, p, _ in messages if t == f"{PREFIX}/alerts"]
    review_db.expire_all()
    db_types = set(review_db.execute(text("SELECT DISTINCT alert_type FROM alerts WHERE printer_id=301")).scalars())
    assert db_types, "fixture must dispatch alerts"
    assert {a["type"] for a in alerts} == db_types
    assert all(a["printer_id"] == 301 for a in alerts)


def test_ams_changes_have_no_republish_topic_or_status_fields(bridged_monitor, review_db):
    """AMS tray swap capture: AMS state changes in ODIN but nothing AMS-shaped is republished."""
    monitor, sock, clock, module, sub = bridged_monitor
    replay(sock, clock, load_frames("bambu-x1c-ams-swap"))
    messages = sub.flush(module)
    review_db.expire_all()
    ams_rows = review_db.execute(text("SELECT COUNT(*) FROM ams_telemetry WHERE printer_id=301")).scalar_one()
    assert ams_rows > 0, "AMS frames must have been ingested"
    suffixes = {t[len(PREFIX) + 1:].split("/")[-1] for t in _topics(messages)}
    assert suffixes <= {"status", "job", "alerts"}, suffixes
    assert not any("ams" in t.lower() for t in _topics(messages))
    status_keys = set().union(*(set(p) for t, p, _ in messages if t.endswith("/status")))
    assert not any("ams" in k.lower() or "tray" in k.lower() or "slot" in k.lower() for k in status_keys), status_keys


def test_overdue_maintenance_never_produces_an_alert_or_republish(bridged_monitor, review_db):
    """A printer past its maintenance interval completes a print; no maintenance alert exists anywhere."""
    monitor, sock, clock, module, sub = bridged_monitor
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,interval_print_hours,interval_days,is_active) "
                           "VALUES(91,'Synthetic nozzle wipe',0.001,1,1)"))
    review_db.execute(text("UPDATE printers SET created_at='2000-01-01 00:00:00', total_print_hours=50 WHERE id=301"))
    review_db.commit()
    replay(sock, clock, load_frames("bambu-a1-happy-path"))
    messages = sub.flush(module)
    review_db.expire_all()
    assert review_db.execute(text("SELECT COUNT(*) FROM alerts WHERE alert_type='maintenance_overdue'")).scalar_one() == 0
    assert not any(p.get("type") == "maintenance_overdue" for t, p, _ in messages if t.endswith("/alerts"))


def _queue_pref(review_db, uid, in_app):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    review_db.add(AlertPreference(user_id=uid, alert_type=AlertType.QUEUE_ADDED, in_app=in_app,
                                  email=False, browser_push=False))
    review_db.commit()


def test_queue_added_event_from_real_job_creation_reaches_broker_when_in_app_enabled(approval_client, republish, review_db):  # noqa: F811
    module, sub = republish
    client, user = approval_client
    user.update(id=3, role="operator", username="Synthetic operator")
    _queue_pref(review_db, 3, True)
    response = client.post("/api/v1/jobs", json={"item_name": "Synthetic queued MQTT"})
    assert response.status_code == 201, response.text
    messages = sub.flush(module)
    queue = [p for t, p, _ in messages if t == f"{PREFIX}/alerts" and p["type"] == "queue_added"]
    assert len(queue) == 1 and queue[0]["title"] == "Job added to queue: Synthetic queued MQTT"


def test_queue_added_with_default_preferences_publishes_nothing_external(approval_client, republish, review_db):  # noqa: F811
    """Default QUEUE_ADDED preference is in_app=False; MQTT republish is gated on in-app recipients."""
    module, sub = republish
    client, user = approval_client
    user.update(id=3, role="operator", username="Synthetic operator")
    response = client.post("/api/v1/jobs", json={"item_name": "Synthetic default queue"})
    assert response.status_code == 201, response.text
    messages = sub.flush(module)
    assert not [p for t, p, _ in messages if t.endswith("/alerts")]
