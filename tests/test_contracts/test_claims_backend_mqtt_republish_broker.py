"""Claim F14-14.1-03: real broker round trip for MQTT republish (generic MQTT consumer side).

Starts an owned local mosquitto bound to 127.0.0.1, points the real republish module at
it, publishes through the public API, and receives with an independent paho subscriber
(the same transport Home Assistant / Node-RED / Ignition MQTT clients use).
"""
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("enterprise_license")

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / 'artifacts' / 'application-certification' / 'claims-protocols-backend'


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


@pytest.fixture
def broker(tmp_path):
    binary = shutil.which('mosquitto') or '/Users/ollama/homebrew/sbin/mosquitto'
    if not Path(binary).exists():
        pytest.skip('mosquitto unavailable')
    port = _free_port()
    conf = tmp_path / 'mosquitto.conf'
    conf.write_text(f'listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n')
    proc = subprocess.Popen([binary, '-c', str(conf)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10
    while True:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=0.2).close(); break
        except OSError:
            assert time.monotonic() < deadline, 'mosquitto did not start'
            time.sleep(0.1)
    yield port
    proc.terminate(); proc.wait(timeout=10)


def test_republish_round_trip_through_real_broker(broker, monkeypatch):
    import paho.mqtt.client as mqtt
    from modules.notifications import mqtt_republish as module
    received, ready = [], threading.Event()
    sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id='claims-subscriber', protocol=mqtt.MQTTv311)
    sub.on_connect = lambda c, u, f, rc, p=None: (c.subscribe('#', qos=0), None)
    sub.on_subscribe = lambda *a: ready.set()
    sub.on_message = lambda c, u, m: received.append((m.topic, json.loads(m.payload), m.retain))
    sub.connect('127.0.0.1', broker); sub.loop_start()
    assert ready.wait(5)
    config = {'host': '127.0.0.1', 'port': broker, 'username': '', 'password': '', 'topic_prefix': 'odin-claims', 'use_tls': False}
    monkeypatch.setattr(module, '_get_config', lambda: config)
    monkeypatch.setattr(module, '_client', None)
    try:
        module.republish_telemetry(7, 'Bay 1/X1C', {'gcode_state': 'RUNNING', 'progress': 42})
        module.republish_job(7, 'Bay 1/X1C', 'completed', {'job_name': 'bracket'})
        module.republish_alert('print_failed', 'error', 'Print Failed', 'nozzle clog', printer_id=7, printer_name='Bay 1/X1C')
        module.republish_fleet(3, 4, 2, 1)
        deadline = time.monotonic() + 5
        while len(received) < 4 and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        module.disconnect(); sub.loop_stop(); sub.disconnect()
    topics = {t: p for t, p, _ in received}
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / 'mqtt-republish-broker.json').write_text(json.dumps(
        {'broker': 'mosquitto (local, 127.0.0.1)', 'messages': [{'topic': t, 'retain': r, 'keys': sorted(p)} for t, p, r in received]}, indent=1))
    assert set(topics) == {'odin-claims/bay_1_x1c/status', 'odin-claims/bay_1_x1c/job', 'odin-claims/alerts', 'odin-claims/fleet'}
    assert topics['odin-claims/bay_1_x1c/status']['gcode_state'] == 'RUNNING' and topics['odin-claims/bay_1_x1c/status']['printer_id'] == 7
    assert topics['odin-claims/bay_1_x1c/job']['event'] == 'completed'
    assert topics['odin-claims/alerts']['severity'] == 'error'
    assert topics['odin-claims/fleet'] == {**topics['odin-claims/fleet'], 'online': 3, 'total': 4, 'printing': 2, 'idle': 1}
    assert all(r is False for _, _, r in received)  # not retained; no HA discovery config topics published
    assert not any(t.startswith('homeassistant/') for t, _, _ in received)
