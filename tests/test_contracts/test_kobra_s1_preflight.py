"""Missing-file preflight contracts. All device, MQTT and clock boundaries are fakes."""
import copy
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from sqlalchemy import text

from modules.printers.kobra_s1 import DECOY_FILENAME, S1Refusal, build_payload, parse_ace
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_kobra_s1_release1 import HUB, FILAMENTS, S1_HEADER, _bed_clear_request


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    from core import webhook_utils
    def forbidden(*args, **kwargs):
        pytest.fail('Preflight tests must never open a network connection')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(webhook_utils, 'device_request', forbidden)


def decoy_payload():
    return build_payload(dict(path=DECOY_FILENAME, md5='0' * 32, size=0, filaments=FILAMENTS[:1]),
                         parse_ace(HUB)['slots'], [1], 123, 'decoy', decoy=True)


@pytest.fixture
def mqtt_device(monkeypatch):
    from modules.printers.adapters import anycubic_lan as module
    from core import webhook_utils
    import paho.mqtt.client as mqtt
    device = SimpleNamespace(sent=[], mode='report', model='20025', now=0.0, subscribed=False,
                             report_code=10115, report_state='failed', handshake_calls=0)
    report = f'{module.PREFIX}/printer/public/20025/fixture-device/print/report'
    class Client:
        def __init__(self, *args, **kwargs):
            assert kwargs['reconnect_on_failure'] is False
        def username_pw_set(self, *args): pass
        def tls_set_context(self, *args): pass
        def connect(self, *args, **kwargs):
            self.on_connect(self, None, None, SimpleNamespace(is_failure=False))
        def emit(self, topic=report):
            self.on_message(self, None, SimpleNamespace(topic=topic, payload=json.dumps(dict(
                msgid='unrelated-old-print', code=device.report_code, state=device.report_state, secret='discard this'))))
        def subscribe(self, topic, **kwargs):
            assert topic == report
            device.subscribed = True
            self.emit()  # An old report BEFORE publish must always be discarded.
            self.on_subscribe(self, None, 1, [SimpleNamespace(is_failure=False)])
        def publish(self, topic, payload, **kwargs):
            assert device.subscribed and kwargs == dict(qos=0, retain=False)
            device.sent.append(json.loads(payload))
            if device.mode == 'report': self.emit()
            if device.mode == 'response':
                self.on_message(self, None, SimpleNamespace(topic=report.replace('/report', '/response'),
                    payload=json.dumps(dict(msgid='decoy', code=200))))
            if device.mode == 'other_device': self.emit(report.replace('fixture-device', 'another-device'))
            if device.mode == 'exception': raise OSError('synthetic publish failure')
            return SimpleNamespace(rc=1 if device.mode == 'rejected' else 0, is_published=lambda: True)
        def loop(self, timeout):
            device.now += timeout
            if device.mode == 'late' and device.now > 3: self.emit()
            return 0
        def disconnect(self): pass
    printer = module.AnycubicLANPrinter('192.0.2.1')
    def handshake():
        device.handshake_calls += 1
        printer.model_id = device.model
        return dict(broker='mqtts://192.0.2.1:9883', deviceId='fixture-device', username='fixture',
                    password='fixture', devicecrt='fixture', devicepk='fixture')
    monkeypatch.setattr(printer, 'handshake', handshake)
    monkeypatch.setattr(mqtt, 'Client', Client)
    monkeypatch.setattr(module.ssl, 'SSLContext', lambda *args: SimpleNamespace(load_cert_chain=lambda *args: None))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: device.now))
    monkeypatch.setattr(webhook_utils, 'device_connect_ip', lambda *args: '192.0.2.1')
    device.printer = printer
    return device


@pytest.mark.parametrize('mode,processed', [('report', True), ('before_only', False), ('response', False),
    ('other_device', False), ('late', False), ('exception', False), ('rejected', False)])
def test_decoy_report_boundary(mqtt_device, mode, processed):
    device = mqtt_device
    device.mode = mode
    checked = []
    result = device.printer.send_decoy(decoy_payload(), before_publish=lambda: checked.append(True))
    assert result['processed'] is processed
    assert checked == [True] and len(device.sent) == 1
    assert set(result) <= {'processed', 'code', 'state'}
    assert not device.printer._state and not device.printer._connected
    assert not hasattr(device.printer, 'publish_entered') and not hasattr(device.printer, 'published')
    if mode in ('before_only', 'response', 'other_device', 'late'):
        assert 3 <= device.now < 3.02


@pytest.mark.parametrize('code,state', [(200, 'finished'), (10115, 'failed'), ('unsafe', 'x' * 99)])
def test_any_print_report_is_the_provisional_signal(mqtt_device, code, state):
    mqtt_device.report_code, mqtt_device.report_state = code, state
    result = mqtt_device.printer.send_decoy(decoy_payload(), before_publish=lambda: None)
    assert result == dict(processed=True, code=code if type(code) is int else None,
                          state=state if len(state) <= 32 else None)


@pytest.mark.parametrize('change', [dict(filename='real.gcode'), dict(filename='../' + DECOY_FILENAME),
    dict(filepath='/folder'), dict(filesize=1), dict(filesize=False), dict(md5='a' * 32), dict(filetype=2)])
def test_adapter_refuses_any_real_file_shape(mqtt_device, change):
    payload = decoy_payload()
    payload['data'].update(change)
    with pytest.raises(ValueError):
        mqtt_device.printer.send_decoy(payload, before_publish=lambda: pytest.fail('guard entered'))
    assert mqtt_device.handshake_calls == 0 and mqtt_device.sent == []


def test_handshake_model_is_required(mqtt_device):
    mqtt_device.model = '20029'
    with pytest.raises(S1Refusal, match='s1_model_mismatch'):
        mqtt_device.printer.send_decoy(decoy_payload(), before_publish=lambda: pytest.fail('guard entered'))
    assert not mqtt_device.sent and not mqtt_device.subscribed


def test_adapter_switch_off_sends_neither_decoy_nor_real(mqtt_device, monkeypatch):
    monkeypatch.delenv('ODIN_KOBRA_S1_SENDING')
    assert mqtt_device.printer.send_decoy(decoy_payload(), before_publish=lambda: None) == dict(processed=False)
    payload = decoy_payload()
    payload['data']['filename'] = 'real.gcode'
    assert not mqtt_device.printer.send_start(payload, before_publish=lambda: None)['published']
    assert mqtt_device.handshake_calls == 0 and mqtt_device.sent == []


def test_zero_size_exception_is_only_for_the_decoy():
    file = dict(path='real.gcode', md5='0' * 32, size=0, filaments=FILAMENTS[:1])
    for name, decoy in [('real.gcode', False), ('real.gcode', True), (DECOY_FILENAME, False)]:
        file['path'] = name
        with pytest.raises(S1Refusal, match='s1_file_identity_invalid'):
            build_payload(file, parse_ace(HUB)['slots'], [1], 0, 'fixture', decoy=decoy)


@pytest.fixture
def pipeline(review_db, tmp_path, monkeypatch):
    from core import job_timeline
    from modules.printers import dispatch, s1_dispatch, start_next
    from modules.printers.adapters import moonraker, anycubic_lan
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    path.write_text(S1_HEADER + '; filament_colour = #00AE42\n; filament_type = PLA\nG28\n')
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET model='Kobra S1',slot_count=4 WHERE id=59"))
        conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,2,'PLA')"))
        conn.execute(text("UPDATE jobs SET ams_mapping='[1]' WHERE id=53"))
    device = SimpleNamespace(engine=engine, reports=[True], decoys=[], starts=[], uploads=[], sleeps=[],
        absent=404, idle=True, now=0.0, silent=False, after_upload=lambda: None, on_sleep=lambda: None,
        after_decoy=lambda: None, events=[])
    def status(self):
        active = device.starts and not device.silent
        return SimpleNamespace(internal_state='RUNNING' if active or not device.idle else 'IDLE',
            raw_print_state='printing' if active or not device.idle else 'standby',
            raw_data={'webhooks': {'state': 'ready'}}, ace_status=parse_ace(HUB),
            nozzle_target=210 if active else 0, filename=device.starts[-1]['data']['filename'] if active else '')
    def upload(self, local, remote):
        device.uploads.append(remote)
        device.after_upload()
        return True
    def identity(self, name):
        if name == DECOY_FILENAME:
            if device.absent == 'error': raise OSError('synthetic error')
            if device.absent != 200: raise HTTPError('http://fixture.invalid', device.absent, 'Synthetic', {}, None)
        return dict(path=name, md5='a' * 32, size=999, filaments=FILAMENTS[:1])
    def decoy(self, payload, *, before_publish):
        before_publish()
        with engine.connect() as conn:
            assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one() == 'adapter_entered'
            assert conn.execute(text("SELECT count(*) FROM system_config WHERE key LIKE 's1_start:%'")).scalar_one() == 0
        device.decoys.append(copy.deepcopy(payload))
        device.now += 3
        device.after_decoy()
        return dict(processed=device.reports[min(len(device.decoys) - 1, len(device.reports) - 1)])
    def start(self, payload, *, before_publish):
        before_publish()
        device.starts.append(copy.deepcopy(payload))
        return dict(published=True, message='Published; no correlated report.')
    def sleep(seconds):
        device.now += seconds
        device.sleeps.append(seconds)
        device.on_sleep()
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'get_status', status)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'upload_file', upload)
    monkeypatch.setattr(moonraker.MoonrakerPrinter, 'device_file_identity', identity)
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter, 'send_decoy', decoy)
    monkeypatch.setattr(anycubic_lan.AnycubicLANPrinter, 'send_start', start)
    monkeypatch.setattr(s1_dispatch, 'time', SimpleNamespace(monotonic=lambda: device.now, time=lambda: 123, sleep=sleep))
    monkeypatch.setattr(job_timeline, 'enqueue', lambda item: device.events.append(item))
    def run():
        marker = _bed_clear_request(engine, 53, 59)
        # Each click has a new request ID; the legacy helper uses a fixed key.
        from core.durable_config import insert
        context = start_next._context.get()
        context['key'] = 'start_next_request:preflight-' + str(len(device.uploads))
        with engine.begin() as conn:
            insert(conn, context['key'], context['record'])
        try: return dispatch.dispatch_job(59, 53)
        finally: start_next._context.reset(marker)
    def sql(statement):
        with engine.begin() as conn: conn.execute(text(statement))
    device.run, device.sql = run, sql
    return device


def evidence(device):
    with device.engine.connect() as conn:
        claim = conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one_or_none()
        status = conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
        starts = conn.execute(text("SELECT count(*) FROM system_config WHERE key LIKE 's1_start:%'")).scalar_one()
        rows = conn.execute(text("SELECT details FROM audit_logs WHERE action='job.dispatch_preflight'")).scalars().all()
    return claim, status, starts, [r if isinstance(r, dict) else json.loads(r) for r in rows]


@pytest.mark.parametrize('reports,count,seconds', [([True], 1, 3), ([False, True], 2, 31), ([False], 3, 59)])
def test_preflight_pass_retry_and_clean_refusal(pipeline, reports, count, seconds):
    device = pipeline
    device.reports = reports
    ok, message = device.run()
    passed = any(reports)
    assert ok is passed
    assert len(device.decoys) == count and len(device.starts) == int(passed)
    assert device.sleeps == [25] * (count - 1)
    assert len({p['msgid'] for p in device.decoys + device.starts}) == count + int(passed)
    for payload in device.decoys:
        assert payload['data']['filename'] == DECOY_FILENAME
        assert payload['data']['filesize'] == 0 and payload['data']['md5'] == '0' * 32
    claim, status, starts, audits = evidence(device)
    assert (claim, status, starts) == (('accepted', 'printing', 1) if passed else (None, 'scheduled', 0))
    facts = dict(result='passed' if passed else 'refused', decoys_sent=count, seconds=seconds)
    assert audits == [dict(printer_id=59, **facts)]
    events = [e for e in device.events if e['kind'] == 'dispatch_starting']
    assert len(events) == 1
    detail = json.loads(events[0]['detail'])
    assert detail.pop('message').startswith('Printer start check ')
    detail['result'] = detail.pop('preflight_result')
    assert detail == facts
    if not passed:
        from modules.printers.dispatch import NoStartMessage
        assert isinstance(message, NoStartMessage)
        assert 's1_start_service_not_responding' in message and 'unknown' not in message.lower()
        assert 'Restart it on the printer, then try again.' in message
        # A clean refusal leaves this queued job eligible for a fresh attempt.
        device.reports = [True]
        retry = device.run()
        assert retry[0] is True, retry


@pytest.mark.parametrize('absent', [200, 403, 500, 'error'])
def test_only_a_device_404_proves_the_decoy_cannot_print(pipeline, absent):
    pipeline.absent = absent
    ok, message = pipeline.run()
    assert not ok and 's1_preflight_file_not_absent' in message
    assert not pipeline.decoys and not pipeline.starts
    assert evidence(pipeline)[:3] == (None, 'scheduled', 0)


@pytest.mark.parametrize('change,code', [('flag', 'slot_do_not_feed'), ('hold', 'printer_maintenance_hold'),
    ('job_held', 'changed before dispatch'), ('not_idle', 's1_printer_not_ready'), ('off', 'printer_dispatch_unsupported')])
@pytest.mark.parametrize('when', ['after_upload', 'retry'])
def test_every_decoy_rechecks_existing_refusals(pipeline, monkeypatch, change, code, when):
    def changed():
        if change == 'flag': pipeline.sql('UPDATE filament_slots SET do_not_feed=true WHERE printer_id=59 AND slot_number=2')
        if change == 'hold': pipeline.sql("INSERT INTO system_config(key,value) VALUES('printer.maintenance.59','{\"state\": \"hold\",\"note\":\"Service needed\"}')")
        if change == 'job_held': pipeline.sql('UPDATE jobs SET hold=true WHERE id=53')
        if change == 'not_idle': pipeline.idle = False
        if change == 'off': monkeypatch.delenv('ODIN_KOBRA_S1_SENDING')
    if when == 'after_upload': pipeline.after_upload = changed
    else:
        pipeline.reports = [False]
        pipeline.on_sleep = changed
    ok, message = pipeline.run()
    assert not ok and code in message
    assert len(pipeline.decoys) == int(when == 'retry') and not pipeline.starts
    assert evidence(pipeline)[:3] == (None, 'scheduled', 0)


@pytest.mark.parametrize('change', ['off', 'monitor_only', 'flag', 'maintenance', 'job_held', 'not_idle'])
def test_initial_refusals_never_reach_a_decoy(pipeline, monkeypatch, change):
    if change == 'off': monkeypatch.delenv('ODIN_KOBRA_S1_SENDING')
    if change == 'monitor_only': pipeline.sql("UPDATE printers SET api_type='anycubic_lan' WHERE id=59")
    if change == 'flag': pipeline.sql('UPDATE filament_slots SET do_not_feed=true WHERE printer_id=59 AND slot_number=2')
    if change == 'maintenance': pipeline.sql("INSERT INTO system_config(key,value) VALUES('printer.maintenance.59','{\"state\": \"hold\",\"note\":\"Service needed\"}')")
    if change == 'job_held': pipeline.sql('UPDATE jobs SET hold=true WHERE id=53')
    if change == 'not_idle': pipeline.idle = False
    assert pipeline.run()[0] is False
    assert not pipeline.decoys and not pipeline.starts
    assert evidence(pipeline)[:3] == (None, 'scheduled', 0)


def test_no_bed_clear_opt_in_means_no_decoy(pipeline):
    from modules.printers import dispatch
    assert 's1_bed_clear_required' in dispatch.dispatch_job(59, 53)[1]
    assert not pipeline.decoys and not pipeline.starts


def test_real_start_unknown_after_pass_remains_fenced(pipeline):
    pipeline.silent = True
    ok, message = pipeline.run()
    assert not ok and 'unknown' in message.lower() and 'tell support' in message
    assert '10 minutes' not in message and 'passed its start check' in message
    assert 'no print command was sent' not in message
    assert len(pipeline.decoys) == len(pipeline.starts) == 1
    assert evidence(pipeline)[:3] == ('uncertain', 'scheduled', 1)
    assert pipeline.run()[0] is False
    assert len(pipeline.decoys) == len(pipeline.starts) == 1


def test_real_start_rechecks_after_preflight_pass(pipeline):
    pipeline.after_decoy = lambda: pipeline.sql('UPDATE filament_slots SET do_not_feed=true WHERE printer_id=59 AND slot_number=2')
    assert 'slot_do_not_feed' in pipeline.run()[1]
    assert len(pipeline.decoys) == 1 and not pipeline.starts
    assert evidence(pipeline)[:3] == (None, 'scheduled', 0)


def test_missing_claim_cannot_send_a_decoy(pipeline):
    pipeline.after_upload = lambda: pipeline.sql('DELETE FROM printer_dispatch_claims')
    assert pipeline.run()[0] is False
    assert not pipeline.decoys and not pipeline.starts


def test_lost_target_ownership_is_a_clean_preflight_refusal(pipeline, monkeypatch):
    from modules.printers import dispatch_orchestration
    acquire = dispatch_orchestration.acquire_target_lock
    held = [True]
    def acquired(*args):
        result = acquire(*args)
        return SimpleNamespace(status=result.status, handle=SimpleNamespace(
            held=lambda: held[0] and result.handle.held(), close=result.handle.close))
    monkeypatch.setattr(dispatch_orchestration, 'acquire_target_lock', acquired)
    pipeline.after_upload = lambda: held.__setitem__(0, False)
    assert 's1_target_changed' in pipeline.run()[1]
    assert not pipeline.decoys and not pipeline.starts
    assert evidence(pipeline)[:3] == (None, 'scheduled', 0)


def test_preflight_does_not_consume_outer_single_use_gate(pipeline, monkeypatch):
    from modules.printers import license_start_gate
    entered = []
    def wrap(engine, *, previous, **kwargs):
        def gate():
            entered.append(True)
            assert len(entered) == 1
            return previous()
        gate.refusal = None
        gate.prepare = previous.prepare
        return gate
    monkeypatch.setattr(license_start_gate, 'create_license_gate', wrap)
    pipeline.reports = [False, True]
    assert pipeline.run()[0] is True
    assert entered == [True] and len(pipeline.decoys) == 2


def test_maintenance_command_boundary_refuses_cleanly(pipeline, monkeypatch):
    from modules.printers import maintenance_gate
    def held():
        raise maintenance_gate.MaintenanceHold('printer_maintenance_hold: Service needed')
    monkeypatch.setattr(maintenance_gate, 'check_command', held)
    assert 'printer_maintenance_hold' in pipeline.run()[1]
    assert not pipeline.decoys and not pipeline.starts
    assert evidence(pipeline)[:3] == (None, 'scheduled', 0)


def test_preflight_audit_failure_cannot_send_a_real_start(pipeline, monkeypatch):
    from core import audit
    def fail(*args, **kwargs):
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(audit, 'log_audit', fail)
    assert 's1_preflight_record_failed' in pipeline.run()[1]
    assert len(pipeline.decoys) == 1 and not pipeline.starts
    assert evidence(pipeline)[:3] == (None, 'scheduled', 0)


def test_preflight_timeline_rows_are_persisted(pipeline):
    from core.job_timeline import TimelineWriter
    pipeline.reports = [False]
    assert pipeline.run()[0] is False
    writer = TimelineWriter(pipeline.engine.url)
    try:
        for item in pipeline.events:
            writer.enqueue(item)
    finally:
        writer.close()
    with pipeline.engine.connect() as conn:
        row = conn.execute(text("SELECT printer_id,org_id,detail FROM job_timeline_events WHERE job_id=53 AND kind='dispatch_starting'")).one()
    detail = json.loads(row.detail)
    assert (row.printer_id, row.org_id) == (59, 1)
    assert detail == dict(preflight_result='refused', decoys_sent=3, seconds=59,
                          message='Printer start check did not pass. Checks sent: 3. Time: 59 seconds.')
