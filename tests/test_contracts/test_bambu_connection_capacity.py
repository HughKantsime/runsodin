"""Characterize capacity contention with a real local broker and real paho.

The monitor command handler must publish without allocating another session.
Mosquitto refuses excess clients, whereas the observed P1S silently timed out. Both demonstrate that a working session proves no spare slot.
"""
import json
import shutil
import socket
import subprocess
import threading
import time
import uuid

import paho.mqtt.client as mqtt
import pytest

pytestmark = pytest.mark.integration
SERIAL = 'SYNTHETIC-CAPACITY-PRINTER'
LED = {'system': {'sequence_id': '0', 'command': 'ledctrl',
                  'led_node': 'chamber_light', 'led_mode': 'on'}}


def until(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), 'Local broker condition did not become ready'


@pytest.fixture
def capacity_broker(tmp_path):
    binary = shutil.which('mosquitto')
    if not binary:
        pytest.skip('Real broker proof requires local mosquitto; run explicitly in integration lane')
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    config = tmp_path / 'mosquitto.conf'
    config.write_text(f'listener {port} 127.0.0.1\nallow_anonymous true\nmax_connections 2\npersistence false\n')
    with (tmp_path / 'broker.log').open('w') as log:
        process = subprocess.Popen([binary, '-c', str(config)], stdout=log, stderr=log)
        try:
            def ready():
                assert process.poll() is None, 'Task-owned broker exited before readiness'
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.1):
                        return True
                except OSError:
                    return False
            until(ready)
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


@pytest.mark.parametrize('generation', ['legacy', 'v2'])
def test_monitor_and_studio_at_capacity_refuse_extra_command_but_existing_transport_publishes(
        capacity_broker, monkeypatch, generation):
    from core import webhook_utils
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig, BambuTelemetryAdapter
    from modules.printers.telemetry.bambu.session import run_command
    from modules.printers.telemetry.events import BambuReportEvent, ConnectionEvent

    port = capacity_broker
    # Only a task-owned loopback broker; no production TLS/policy changes.
    monkeypatch.setattr(webhook_utils, 'device_connect_ip', lambda host, port: '127.0.0.1')
    monkeypatch.setattr(BambuPrinter, 'MQTT_PORT', port)
    monkeypatch.setattr(BambuPrinter, 'MQTT_TIMEOUT', .5)
    monkeypatch.setattr(mqtt.Client, 'tls_set', lambda *a, **kw: None)
    monkeypatch.setattr(mqtt.Client, 'tls_insecure_set', lambda *a, **kw: None)
    report_seen = threading.Event()
    connected = threading.Event()
    config = BambuAdapterConfig(printer_id='capacity', serial=SERIAL,
                               host='127.0.0.1', access_code='', port=port,
                               use_tls=False, replay_broker=True)
    if generation == 'legacy':
        monitor = BambuPrinter(ip='127.0.0.1', serial=SERIAL, access_code='synthetic',
                               client_id='monitor-' + uuid.uuid4().hex,
                               on_status_update=lambda status: report_seen.set())
    else:
        def emitter(event):
            if isinstance(event, ConnectionEvent) and event.kind == 'connected':
                connected.set()
            if isinstance(event, BambuReportEvent):
                report_seen.set()
        monitor = BambuTelemetryAdapter(config, emitter=emitter)
    studio = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id='studio-' + uuid.uuid4().hex)
    subscribed = threading.Event()
    received_led = threading.Event()
    wire = []
    def on_connect(client, userdata, flags, reason, properties):
        client.subscribe(f'device/{SERIAL}/request', qos=1)
    def on_message(client, userdata, message):
        payload = json.loads(message.payload)
        if payload.get('system', {}).get('command') == 'ledctrl':
            wire.append(payload)
            received_led.set()
    studio.on_connect = on_connect
    studio.on_subscribe = lambda *args: subscribed.set()
    studio.on_message = on_message
    extra = None
    try:
        if generation == 'legacy':
            assert monitor.connect()
        else:
            monitor.start()
            assert connected.wait(3)
        # Wait for monitor subscription to be processed before publishing reports.
        until(lambda: monitor._client is not None and monitor._client.is_connected())
        studio.connect('127.0.0.1', port, keepalive=10)
        studio.loop_start()
        assert subscribed.wait(3)
        for _ in range(20):
            studio.publish(f'device/{SERIAL}/report', json.dumps({'print': {'gcode_state': 'IDLE'}}))
            if report_seen.wait(.05):
                break
        assert report_seen.is_set(), 'Existing monitor must still receive telemetry at capacity'
        socket_before = monitor._client.socket()
        # An ephemeral command strategy needs a third connection and fails.
        if generation == 'legacy':
            extra = BambuPrinter(ip='127.0.0.1', serial=SERIAL, access_code='synthetic',
                                 client_id='extra-' + uuid.uuid4().hex)
            assert extra.connect() is False
            extra.disconnect()
            extra = None
        else:
            assert run_command(config, 'set_chamber_light', True, connect_timeout=.5) is False
        assert wire == [], 'Refused extra connection must not deliver the command'
        report_seen.clear()
        studio.publish(f'device/{SERIAL}/report', json.dumps({'print': {'gcode_state': 'IDLE'}}))
        assert report_seen.wait(3), 'Failed command must not displace the working monitor'
        # Exercise the shipped monitor command admission using the real client.
        from types import SimpleNamespace
        from modules.printers.monitor_commands import execute
        owner = SimpleNamespace(_lock=threading.Lock(), serial=SERIAL,
            _bambu=monitor if generation == 'legacy' else None,
            _v2_adapter=monitor if generation == 'v2' else None)
        assert execute({1: owner}, dict(printer_id=1, action='light', on=True,
            deadline=time.monotonic()+3)) == 'accepted'
        assert received_led.wait(3)
        assert wire == [LED]
        assert monitor._client.socket() is socket_before
    finally:
        if extra is not None:
            extra.disconnect()
        studio.disconnect()
        studio.loop_stop()
        if generation == 'legacy':
            monitor.disconnect()
        else:
            monitor.stop()
