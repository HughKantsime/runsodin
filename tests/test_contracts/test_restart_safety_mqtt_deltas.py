"""Real MQTT ingestion: delta freshness, connection fencing and write frequency."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core import monitor_observations as evidence
from tests.test_contracts.test_hms_observation_boundaries import (
    hms_boundary, monitor_db, _running_packet_monitor,
)


@pytest.fixture
def delta_monitor(hms_boundary, monkeypatch):
    from modules.printers.monitors import mqtt_printer
    db, _, _, _ = hms_boundary
    monitor = _running_packet_monitor(hms_boundary, monkeypatch)
    clock = [1000.0]
    fake_time = SimpleNamespace(time=lambda: clock[0])
    monkeypatch.setattr(evidence, 'time', fake_time)
    monkeypatch.setattr(mqtt_printer, 'time', fake_time)
    monitor._last_heartbeat = monitor._last_spool_check = 10000
    monkeypatch.setattr(monitor, '_on_state_change', lambda *args: None)
    writes = []
    original = evidence.persist_dbapi
    def capture(conn, printer_id, state, **kwargs):
        original(conn, printer_id, state, **kwargs)
        writes.append((clock[0], state))
    monkeypatch.setattr(evidence, 'persist_dbapi', capture)
    def send(*, retained=False, age=None, generation=None, **packet):
        monitor._on_status(SimpleNamespace(raw_data={'print': packet},
                           mqtt_retained=retained, report_age_seconds=age), generation)
    def cached():
        value = db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                           {'key': evidence.cache_key(9)}).scalar_one_or_none()
        return json.loads(value) if value else None
    return monitor, clock, send, cached, writes


def test_delta_only_sequence_keeps_live_state_beyond_max_age(delta_monitor, hms_boundary):
    monitor, clock, send, cached, writes = delta_monitor
    send(gcode_state='RUNNING')
    for tick in range(1, 181):
        clock[0] = 1000 + tick
        send(mc_percent=tick // 3)
        with hms_boundary[0].get_bind().connect() as conn:
            assert evidence.live_state(conn, 9, 999) == 'RUNNING'
    assert monitor._state['gcode_state'] == 'RUNNING'
    assert cached() == {'state': 'RUNNING', 'at': 1180}
    assert writes == [(stamp, 'RUNNING') for stamp in range(1000, 1181, 15)]


def test_cached_state_alone_cannot_seed_live_deltas(delta_monitor):
    monitor, _, send, cached, writes = delta_monitor
    assert monitor._state['gcode_state'] == 'RUNNING'
    send(mc_percent=10)
    assert cached() is None and writes == []


@pytest.mark.parametrize('retained,age', [
    (True, None), (False, 61), (False, -1), (False, float('nan')),
])
def test_ineligible_reports_cannot_seed_or_refresh(delta_monitor, retained, age):
    _, clock, send, cached, _ = delta_monitor
    send(gcode_state='RUNNING', retained=retained, age=age)
    send(mc_percent=11)
    assert cached() is None
    send(gcode_state='RUNNING')
    clock[0] += 61
    send(mc_percent=12, retained=retained, age=age)
    assert cached()['at'] == 1000


def test_retained_state_cannot_poison_the_live_delta_seed(delta_monitor):
    monitor, clock, send, cached, _ = delta_monitor
    send(gcode_state='PAUSE')
    clock[0] += 15
    send(gcode_state='RUNNING', retained=True)
    assert monitor._state['gcode_state'] == 'RUNNING'  # display cache still merges
    send(mc_percent=12)
    assert cached() == {'state': 'PAUSE', 'at': 1015}


def test_explicit_state_change_bypasses_throttle(delta_monitor):
    _, clock, send, cached, writes = delta_monitor
    send(gcode_state='RUNNING')
    clock[0] += 1
    send(gcode_state='PAUSE')
    assert cached() == {'state': 'PAUSE', 'at': 1001}
    assert writes == [(1000, 'RUNNING'), (1001, 'PAUSE')]


def test_replacement_connection_requires_new_explicit_live_state(delta_monitor):
    monitor, clock, send, cached, _ = delta_monitor
    old = monitor._begin_connection()
    send(gcode_state='RUNNING', generation=old)
    clock[0] += 1
    new = monitor._begin_connection()
    assert monitor._state['gcode_state'] == 'RUNNING'
    assert cached()['state'] == 'UNKNOWN'
    send(gcode_state='RUNNING', generation=old)
    send(gcode_state='RUNNING', retained=True, generation=new)
    clock[0] += 15
    send(mc_percent=12, generation=new)
    assert cached()['state'] == 'UNKNOWN'
    send(gcode_state='RUNNING', generation=new)
    assert cached()['state'] == 'RUNNING'
    assert monitor._reset_live_connection(old) is False
    assert cached()['state'] == 'RUNNING'
    monitor.disconnect()
    send(gcode_state='RUNNING', generation=new)
    assert cached()['state'] == 'UNKNOWN'


def test_same_adapter_reconnect_requires_new_explicit_state(delta_monitor, monkeypatch):
    from modules.printers.adapters.bambu import BambuPrinter
    monitor, clock, _, cached, _ = delta_monitor
    generation = monitor._connection_generation
    adapter = BambuPrinter('192.0.2.1', 'SYNTHETIC', '',
        on_status_update=lambda status: monitor._on_status(status, generation),
        on_connection_change=lambda: monitor._reset_live_connection(generation))
    requests = []
    monkeypatch.setattr(adapter, '_publish_nowait', lambda packet: requests.append(packet) or True)
    client = SimpleNamespace(subscribe=lambda *args: None)
    adapter._on_connect(client, None, None, 0)
    adapter._parse_status({'print': {'gcode_state': 'RUNNING'}})
    assert cached()['state'] == 'RUNNING'
    adapter._on_disconnect(client, None, 1)
    assert cached()['state'] == 'UNKNOWN'
    adapter._on_connect(client, None, None, 0)
    clock[0] += 15
    adapter._parse_status({'print': {'mc_percent': 12}})
    assert cached()['state'] == 'UNKNOWN'
    adapter._parse_status({'print': {'gcode_state': 'RUNNING'}})
    assert cached()['state'] == 'RUNNING'
    assert sum(packet.get('pushing', {}).get('command') == 'pushall' for packet in requests) == 2


def test_v2_connect_and_reconnect_request_pushall_and_reset_evidence(delta_monitor, monkeypatch):
    from modules.printers.telemetry.bambu import adapter as adapters
    from modules.printers.telemetry.bambu import broker_policy
    from modules.printers.telemetry.events import ConnectionEvent, BambuReportEvent
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    monitor, clock, _, cached, _ = delta_monitor
    emitted = []
    requests = []
    class Adapter:
        def __init__(self, config, emitter):
            emitted.append(emitter)
        def start(self):
            emitted[0](ConnectionEvent(printer_id='9', ts=clock[0], kind='connected'))
        def request_version(self):
            requests.append('version')
        def request_full_status(self):
            requests.append('pushall')
    monkeypatch.setattr(adapters, 'BambuTelemetryAdapter', Adapter)
    monkeypatch.setattr(broker_policy, 'resolve_bambu_broker_config',
                        lambda *a, **kw: SimpleNamespace(host='192.0.2.1', port=8883, use_tls=True))
    monkeypatch.setattr(monitor, '_recover_orphaned_jobs', lambda: None)
    assert monitor._connect_v2()
    def report(**packet):
        emitted[0](BambuReportEvent(printer_id='9', ts=clock[0], section=BambuPrintSection(**packet)))
    report(gcode_state='RUNNING')
    assert cached()['state'] == 'RUNNING'
    emitted[0](ConnectionEvent(printer_id='9', ts=clock[0], kind='disconnected'))
    assert cached()['state'] == 'UNKNOWN'
    emitted[0](ConnectionEvent(printer_id='9', ts=clock[0], kind='connected'))
    clock[0] += 15
    report(mc_percent=12)
    assert cached()['state'] == 'UNKNOWN'
    report(gcode_state='RUNNING')
    assert cached()['state'] == 'RUNNING'
    assert requests == ['pushall', 'version', 'pushall', 'version']
