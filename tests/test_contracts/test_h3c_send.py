"""Offline H3C dispatch against a reduced CLI slice and captured Studio command."""
import hashlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace
from zipfile import BadZipFile, ZipFile

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.ams_source_inspection import inspect_ams_sources
from modules.printers import dispatch, dispatch_acquisition, dispatch_ams_gate
from modules.printers.nozzle_capability import installed_nozzles
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSAcquisitionCollector
from modules.printers.telemetry.bambu.ams_acquisition_session import _valid_snapshot
from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine
from tests.test_contracts.test_education_dispatch_policy import actual_education_dispatch
from tests.test_contracts.test_printer_support_levels import make_superadmin


FIXTURES = Path(__file__).parents[1] / 'fixtures/telemetry'
SLICE = FIXTURES / 'h3c-nine-tool-slice.3mf'
STUDIO = FIXTURES / 'h3c-studio-sent.3mf'
ECHO = json.loads((FIXTURES / 'h3c-project-file-echo-2026-11-21.json').read_bytes())['message']['print']
MAPPING = [8, 6, 1, 3, 4]
COLORS = ['FF6A13', 'AE835B', 'FFFFFF', '000000', 'C12E1F']


def install_reply_callbacks(wire):
    from threading import Lock
    from modules.printers.telemetry.bambu.project_reply import ProjectReplies
    wire.on_disconnect = None
    wire.project_replies = ProjectReplies(wire, Lock())


def failing_ftps_peer(monkeypatch, events, fault):
    import errno
    import ssl
    from unittest.mock import Mock
    from modules.printers.telemetry.bambu import ftp_upload

    errors = {
        'refused_connection': ConnectionRefusedError(errno.ECONNREFUSED, 'fixture detail'),
        'tls': ssl.SSLError('fixture detail'),
        'transfer': ConnectionResetError(errno.ECONNRESET, 'fixture detail'),
    }
    def connect():
        events.append('upload')
        peer = Mock()
        operation = peer.storbinary if fault == 'transfer' else peer.connect
        operation.side_effect = errors[fault]
        return peer
    monkeypatch.setattr(ftp_upload, '_ImplicitFTPS', connect)


def changed_member(name, before, after, source=SLICE):
    out = io.BytesIO()
    with ZipFile(source) as src, ZipFile(out, 'w') as dst:
        for member in src.namelist():
            data = src.read(member)
            if member == 'Metadata/' + name:
                assert before in data
                data = data.replace(before, after, 1)
            dst.writestr(member, data)
    return out.getvalue()


@pytest.mark.parametrize('before,after,reason', [
    (b'group_id="4"', b'group_id="8"', 'no matching nozzle'),
    (b'volume_type="Standard"', b'volume_type="Other"', 'tool type'),
    (b'nozzle_diameter="0.40"', b'nozzle_diameter="0.60"', 'diameter'),
])
def test_inconsistent_rack_slice(before, after, reason):
    with pytest.raises(ValueError, match=reason):
        inspect_ams_sources(changed_member('slice_info.config', before, after))


def test_one_nozzle_slice():
    with ZipFile(SLICE) as src:
        settings = src.read('Metadata/project_settings.config')
    project = json.loads(settings)
    project['extruder_nozzle_count'] = ['1']
    with pytest.raises(ValueError, match='nine tools'):
        inspect_ams_sources(changed_member('project_settings.config', settings, json.dumps(project).encode()))


def test_unlisted_gcode_tool():
    with pytest.raises(ValueError, match='tool change'):
        inspect_ams_sources(changed_member('plate_1.gcode', b'\nT4\n', b'\nT5\n'))


@pytest.mark.parametrize('fixture', [SLICE, STUDIO, *(FIXTURES / f'h3c-cli-{kind}.3mf' for kind in ('two', 'five', 'single'))])
def test_reduced_fixture_checksum(fixture):
    assert fixture.stat().st_size < 300_000
    with ZipFile(fixture) as z:
        code = z.read('Metadata/plate_1.gcode')
        assert hashlib.md5(code).hexdigest().upper().encode() == z.read('Metadata/plate_1.gcode.md5')


def test_studio_mapping_matches_captured_echo():
    from modules.printers.telemetry.bambu.ams_identity import print_mapping_fields
    groups = [s.group_id for s in inspect_ams_sources(STUDIO.read_bytes()).sources]
    fields = print_mapping_fields(groups + [-1] * 3, h3c=True)
    assert fields == {k: ECHO[k] for k in fields}


@pytest.fixture
def target(review_db, monkeypatch, tmp_path):
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, 'bambu', slot_count=9)
    path.write_bytes(STUDIO.read_bytes())
    report = json.loads((FIXTURES / 'h3c-ams3h-loaded-2026-11-21.json').read_bytes())['message']['print']
    slots = {n: ('888888', 'TPU' if n == 7 else 'PLA') for n in range(9)}
    slots.update({n: (color, 'PLA') for n, color in zip(MAPPING, COLORS)})
    report['ams']['ams'][0]['tray'] = [dict(id=str(n), tray_type=material, tray_color=color + 'FF')
                                       for n, (color, material) in slots.items()]
    report['ams']['tray_exist_bits'] = hex(511 << 28)
    from modules.models_library.threemf_parser import parse_3mf
    filaments = [dict(slot=f.slot, type=f.type, color=f.color) for f in parse_3mf(str(path)).filaments]
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET model='H3C', tags='[]', shared=FALSE, timelapse_enabled=FALSE, bed_x_mm=NULL,bed_y_mm=NULL,installed_nozzles=:tools WHERE id=59"),
                     {'tools': json.dumps(installed_nozzles(report, include_rack=True))})
        conn.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        for n, (color, material) in slots.items():
            conn.execute(text('INSERT INTO filament_slots(printer_id,slot_number,color_hex,filament_type) '
                              'VALUES(59,:n,:color,:material)'), dict(n=n+1,color=color,material=material))
        conn.execute(text('UPDATE jobs SET ams_mapping=:mapping,payload_sha256=:sha WHERE id=53'),
                     dict(mapping=json.dumps(MAPPING),sha=hashlib.sha256(path.read_bytes()).hexdigest()))
        conn.execute(text('UPDATE print_files SET filaments_json=:f WHERE id=52'), dict(f=json.dumps(filaments)))
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: report)
    monkeypatch.setattr(dispatch_ams_gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(dispatch_ams_gate, 'revalidate_runtime_identity', lambda runtime: None)

    def acquire(config, *, configuration_key, owner_org_id, selected_slots, operation_id, nozzle_dual, timeout):
        started = time.monotonic()
        collector = RawAMSAcquisitionCollector(topic=config.topic_report, configuration_key=configuration_key,
            owner_org_id=owner_org_id, selected_slots=selected_slots, subscribe_mid=1, incarnation=operation_id,
            allow_unreported=True, nozzle_dual=nozzle_dual, h3c=config.h3c)
        assert collector.acknowledge_subscription(operation_id, 1, [0])
        assert collector.observe(incarnation=operation_id, topic=config.topic_report, retained=False, packet=report)
        snapshot = collector.snapshot(configuration_key=configuration_key, owner_org_id=owner_org_id)
        assert _valid_snapshot(snapshot, key=configuration_key, owner=owner_org_id, slots=selected_slots,
                               incarnation=operation_id, started=started, deadline=started+timeout,
                               now=time.monotonic(), allow_unreported=True, h3c=config.h3c)
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(dispatch_ams_gate, 'read_current_selected_ams', acquire)
    return path, report


@pytest.mark.parametrize('problem,reason', [
    (None, None), ('missing', 'Filament 5 requires slot 5'), ('range', 'Filament 1 requires slot 9'), ('duplicate', 'Filament 1 requires slot 9'),
    ('empty', 'slot'), ('wrong_material', 'slot'), ('absent', 'Position 9'),
    ('diameter', 'Position 9'), ('standard_on_tpu', 'Position 9'), ('tpu_on_standard', 'Position 9'),
    ('wrong_model', 'printer_model_mismatch'), ('bed', '330 x 320'),
])
def test_offline_plate_checks_without_opt_in(target, review_db, problem, reason):
    from modules.printers.dispatch_capability_gate import static_refusal
    path, _ = target
    creds, job = dispatch._get_printer_info(59), dispatch._load_job(53)
    if problem in ('wrong_model', 'bed'):
        member = 'project_settings.config'
        with ZipFile(path) as z:
            old = z.read('Metadata/' + member)
        new = json.dumps({**json.loads(old), 'printer_model': 'Bambu Lab P1S'}).encode()
        if problem == 'bed':
            with ZipFile(path) as z:
                old = z.read('Metadata/plate_1.json')
            plate = json.loads(old)
            plate['bbox_all'][2] = 331
            member, new = 'plate_1.json', json.dumps(plate).encode()
        path.write_bytes(changed_member(member, old, new, path))
        refusal = static_refusal(path, 1, creds)
        assert reason in (refusal.code + refusal.detail)
        return
    mapping = MAPPING.copy()
    if problem == 'duplicate': mapping[0] = mapping[1]
    if problem == 'missing': mapping.pop()
    if problem == 'range': mapping[0] = 9
    if problem in ('empty', 'wrong_material'):
        review_db.execute(text('UPDATE filament_slots SET filament_type=:m WHERE printer_id=59 AND slot_number=9'),
                          {'m': 'empty' if problem == 'empty' else 'ABS'})
        review_db.commit()
    tools = json.loads(creds['installed_nozzles'])
    tool = next(row for row in tools if row.get('position') == 9)
    if problem == 'absent': tool['present'] = False
    if problem == 'diameter': tool['diameter'] = 0.6
    if problem == 'standard_on_tpu': tool['type'] = 'HT01'
    if problem == 'tpu_on_standard':
        with ZipFile(path) as z: xml = z.read('Metadata/slice_info.config')
        from xml.etree import ElementTree
        tree = ElementTree.fromstring(xml)
        for node in (tree.find('.//filament[@id="1"]'), tree.find('.//nozzle[@id="8"]')):
            node.set('volume_type', 'TPU285')
        path.write_bytes(changed_member('slice_info.config', xml, ElementTree.tostring(tree), path))
    creds['installed_nozzles'] = tools
    job['ams_mapping'] = mapping
    job['payload_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    options, error = dispatch._validated_print_options(job, creds, path, job['payload_sha256'])
    if reason:
        assert options is None and reason in error, error
    else:
        assert options['ams_mapping'] == MAPPING


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('outcome', ['manual', 'auto', 'timeout', 'busy', 'lock_unavailable', 'transfer', 'refused_connection', 'tls', 'no_suback', 'mqtt_down', 'mqtt_lost', 'sub_refused', 'write_failed', 'upload_rejected', 'start_rejected', 'no_reply', 'empty_tray', 'changed_tool'])
def test_one_checked_dispatch(target, review_db, monkeypatch, caplog, protocol, outcome):
    from modules.printers.telemetry.bambu import ftp_upload, project_reply
    from tests.test_contracts.test_multi_source_ams_dispatch import transport
    path, report = target
    events = []
    if outcome == 'auto':
        review_db.execute(text('UPDATE jobs SET ams_mapping=NULL WHERE id=53'))
        review_db.commit()
    # Reuse the offline upload and MQTT boundary. Exercise the real reply parser.
    def after_upload():
        if outcome == 'empty_tray':
            report['ams']['tray_exist_bits'] = hex(255 << 28)
        if outcome == 'changed_tool':
            report['device']['nozzle']['exist'] &= ~(1 << 24)
    from modules.printers.adapters.bambu import BambuPrinter as LegacyPrinter
    real_upload = ftp_upload.upload_file
    sent = transport(monkeypatch, path, protocol, events, after_upload=after_upload)
    original = project_reply.publish_project
    class Wire:
        on_message = None
        on_subscribe = None

        def subscribe(self, topic):
            if outcome != 'no_suback':
                self.on_subscribe(self, None, 1, [128 if outcome == 'sub_refused' else 0])
            return (0, 1)
    wire = Wire()
    install_reply_callbacks(wire)
    def local_reply(client, topic, publish, payload):
        def send(message):
            assert publish(message)
            if outcome == 'no_reply':
                return True
            reply = {**message['print'], 'result': 'FAIL' if outcome in ('start_rejected', 'write_failed') else 'SUCCESS',
                     'err_code': 42 if outcome in ('start_rejected', 'write_failed') else 0}
            reply['sequence_id'] = int(reply['sequence_id'])
            wire.on_message(None, None, SimpleNamespace(topic=topic, retain=False, payload=json.dumps({'print': reply}).encode()))
            return True
        return original(wire, topic, send, payload, timeout=0)
    monkeypatch.setattr(project_reply, 'publish_project', local_reply)
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu import session
    sender = BambuPrinter() if protocol == 'legacy' else session.BambuCommandAdapter(None)
    sender._client = wire
    if protocol == 'legacy':
        sender._topic_report = 'device/local/report'
    else:
        sender._config = SimpleNamespace(topic_report='device/local/report')
    if outcome == 'mqtt_down':
        sender.connect = lambda: False
        def offline():
            raise ConnectionRefusedError()
        sender.start = offline
    if outcome == 'mqtt_lost':
        # The connection drops between CONNACK and entry to start_print.
        sender._connected = False
    contention = outcome in ('timeout', 'busy', 'lock_unavailable')
    transport_errors = {'transfer': 'Connection reset', 'refused_connection': 'Connection refused', 'tls': 'SSLError'}
    if outcome in transport_errors:
        failing_ftps_peer(monkeypatch, events, outcome)
        monkeypatch.setattr(ftp_upload, 'upload_file', real_upload)
        sender.ip, sender.access_code, sender._configured_h3c = 'local', '', True
        sender.upload_file = LegacyPrinter.upload_file.__get__(sender)
    if outcome == 'upload_rejected' or contention:
        phase = outcome if contention else 'transfer (FTP 550)'
        failure = lambda *a, **kw: events.append('upload') or ftp_upload.UploadResult(False, phase)
        monkeypatch.setattr(ftp_upload, 'upload_file', failure)
        sender.upload_file = failure
    if outcome == 'write_failed':
        from modules.jobs import services
        original_transition = services.transition_job
        def fail_write(conn, job_id, status, *args, **kwargs):
            if status == 'failed':
                raise RuntimeError('failed-state write unavailable')
            return original_transition(conn, job_id, status, *args, **kwargs)
        monkeypatch.setattr(services, 'transition_job', fail_write)
        from modules.printers import dispatch_background
        handle = dispatch_background.stage(59, 53)
        deadline = time.monotonic() + 10
        while dispatch_background.active(handle['attempt_id']) and time.monotonic() < deadline:
            time.sleep(0.01)
        saved = dispatch_background.load(review_db, dispatch_background.result_key(53))
        assert saved['attempt_id'] == handle['attempt_id'] and saved['phase'] == 'needs_reconciliation'
        ok, message = False, saved['message']
    else:
        ok, message = dispatch.dispatch_job(59, 53)
    if contention:
        assert not ok and events == ['upload']
        assert review_db.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() in ('pending', 'scheduled')
        assert not review_db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=53')).first()
    elif outcome == 'write_failed':
        assert not ok and 'FAIL (code 42)' in message and 'reconciliation' in message
        assert 'confirmed no-start: H3C start reply: FAIL (code 42)' in caplog.text
        assert review_db.execute(text('SELECT state FROM printer_dispatch_claims WHERE job_id=53')).scalar_one() == 'uncertain'
        assert not dispatch.dispatch_job(59, 53)[0] and events == ['upload', 'start']
    elif outcome in (*transport_errors, 'no_suback', 'mqtt_down', 'mqtt_lost'):
        assert not ok and events == ['upload']
        expected = (transport_errors[outcome] if outcome in transport_errors else
                    'subscription' if outcome == 'no_suback' else 'connect')
        status, notes = review_db.execute(text('SELECT status,fail_notes FROM jobs WHERE id=53')).one()
        assert status == 'failed' and expected in notes
        assert not dispatch.dispatch_job(59, 53)[0] and events == ['upload']
    elif outcome == 'sub_refused':
        assert not ok and 'subscri' in message and events == ['upload']
        row = review_db.execute(text('SELECT status,fail_notes FROM jobs WHERE id=53')).one()
        assert row.status == 'failed' and 'subscription was refused (code 128)' in row.fail_notes
    elif outcome == 'changed_tool':
        assert not ok and 'Position 9' in message and events == ['upload']
    elif outcome == 'empty_tray':
        assert not ok and 'empty' in message and events == ['upload']
    elif outcome == 'no_reply':
        assert not ok and 'reconciliation' in message and len(sent) == 1
        assert not dispatch.dispatch_job(59, 53)[0]
        assert events == ['upload', 'start']
    elif outcome.endswith('rejected'):
        reply = 'FTP 550' if outcome == 'upload_rejected' else 'FAIL (code 42)'
        assert not ok and reply in message, message
        row = review_db.execute(text('SELECT status,fail_notes FROM jobs WHERE id=53')).one()
        assert row.status == 'failed' and reply in row.fail_notes
        assert len(sent) == int(outcome == 'start_rejected')
        assert not dispatch.dispatch_job(59, 53)[0] and events.count('upload') == 1
    else:
        assert ok, message
        assert events == ['upload', 'start']
        assert len(sent) == 1 and sent[0]['use_ams'] is True and sent[0]['command'] == 'project_file'
        for key in ('ams_mapping', 'ams_mapping2'):
            assert json.dumps(sent[0][key]) == json.dumps(ECHO[key][:5])


def test_h3c_refuses_non_rack_slice(target):
    path, _ = target
    path.write_bytes((FIXTURES / 'x1c-pla-source-sanitized.3mf').read_bytes())
    job = dispatch._load_job(53)
    job['payload_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    options, reason = dispatch._validated_print_options(job, dispatch._get_printer_info(59), path, job['payload_sha256'])
    assert options is None and 'needs a supported H3C rack slice' in reason


@pytest.mark.parametrize('explicit_file', [True, False])
def test_job_detail_previews_file_rack_positions_and_current_colours(target, review_db, explicit_file):
    from modules.jobs.ams_mapping import detail_view
    from modules.jobs.models import Job
    path, _ = target
    from modules.models_library.threemf_parser import parse_3mf
    path.write_bytes((FIXTURES / 'h3c-cli-five.3mf').read_bytes())
    sources = [dict(slot=f.slot, type=f.type, color=f.color) for f in parse_3mf(str(path)).filaments]
    review_db.execute(text('UPDATE print_files SET filaments_json=:sources WHERE id=52'),
                      {'sources': json.dumps(sources)})
    review_db.execute(text('UPDATE jobs SET ams_mapping=NULL,payload_sha256=:sha WHERE id=53'),
                      {'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
    if not explicit_file:
        review_db.execute(text('UPDATE jobs SET print_file_id=NULL WHERE id=53'))
    review_db.execute(text("UPDATE filament_slots SET color_hex='123456' WHERE printer_id=59 AND slot_number=5"))
    review_db.commit()
    job = review_db.get(Job, 53)
    actor = {'id': 1, 'role': 'admin', 'group_id': None}
    def preview():
        return detail_view(review_db, job, actor)['ams_preview']
    facts = preview()
    assert facts['rack_mapping'] == [dict(source_slot=n + 1, target_slot=5 - n) for n in range(5)]
    assert facts['suggested_mapping'] == []
    tray = next(row for row in facts['available'] if row['slot'] == 5)
    assert (tray['material'], tray['color_hex']) == ('PLA', '123456')
    assert facts['required'][0]['color'].lstrip('#') != tray['color_hex']
    review_db.execute(text("UPDATE filament_slots SET color_hex='AABBCC' WHERE printer_id=59 AND slot_number=5"))
    review_db.commit()
    assert next(row['color_hex'] for row in preview()['available'] if row['slot'] == 5) == 'AABBCC'


def test_unidentified_rack_stays_refused_with_saved_opt_in(target, review_db):
    seed_retired_opt_in(review_db)
    review_db.execute(text('UPDATE printers SET model=NULL,machine_type=NULL WHERE id=59'))
    review_db.commit()
    ok, reason = dispatch.dispatch_job(59, 53)
    assert not ok and reason.startswith('unsupported_rack_printer')


def test_corrupt_rack_file_keeps_job_and_next_suggestion_readable(target, review_db, token_client):
    from modules.jobs.routes import jobs_crud
    from modules.printers import routes_start_next
    path, _ = target
    path.write_bytes(b'truncated archive')
    review_db.execute(text("UPDATE jobs SET status='pending',quantity=1,payload_sha256=:sha WHERE id=53"),
                      {'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
    review_db.commit()
    client, issue = token_client
    client.app.include_router(jobs_crud.router)
    client.app.include_router(routes_start_next.router)
    headers = issue(['read'])
    detail = client.get('/jobs/53', headers=headers)
    suggestion = client.get('/printers/59/next-suggestion', headers=headers)
    assert detail.status_code == suggestion.status_code == 200
    assert detail.json()['ams_preview']['rack_mapping'] == []
    assert suggestion.json()['job_id'] == 53
    assert suggestion.json()['inputs']['rack_preview']['rack_mapping'] == []


@pytest.mark.parametrize('error', [BadZipFile, KeyError, OSError, ValueError])
def test_rack_preview_retries_failed_inspection_and_isolates_cached_slots(target, review_db, monkeypatch, error):
    from unittest.mock import Mock
    from core import ams_source_inspection
    from modules.jobs.ams_mapping import job_preview
    from modules.jobs.models import Job
    path, _ = target
    facts = inspect_ams_sources(path.read_bytes())
    inspect = Mock(side_effect=[error(), facts])
    monkeypatch.setattr(ams_source_inspection, 'inspect_ams_sources', inspect)
    job = review_db.get(Job, 53)
    def slots():
        return job_preview(review_db, job, {'id': 1, 'role': 'admin', 'group_id': None}, 59)['rack_mapping']

    assert slots() == []
    mapping = slots()
    assert mapping[0]['target_slot'] == MAPPING[0] + 1
    mapping[0]['target_slot'] = 99
    mapping.clear()
    assert slots()[0]['target_slot'] == MAPPING[0] + 1
    assert inspect.call_count == 2


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
def test_control_commands_at_publish_boundary(target, review_db, protocol):
    from unittest.mock import Mock
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    sender = (BambuPrinter('127.0.0.1', 'fixture', '', printer_id=59) if protocol == 'legacy'
              else BambuCommandAdapter(BambuAdapterConfig('59', 'fixture', '127.0.0.1', '')))
    sender._connected = True
    sender._client = Mock()
    sender._client.publish.return_value.rc = 0
    sender._client.publish.return_value.is_published.return_value = True
    actions = (sender.resume_print, sender.turn_light_on, lambda: sender.set_print_speed(2),
               lambda: sender.skip_objects([1]), sender.clear_print_errors)
    for command in (sender.pause_print, sender.stop_print, *actions):
        assert command()
    light = json.loads(sender._client.publish.call_args_list[3].args[1])['print']
    assert light['param'].strip() == 'M355 S1'
    published = [next(iter(json.loads(call.args[1]).values()))['command']
                 for call in sender._client.publish.call_args_list]
    assert published == ['pause', 'stop', 'resume', 'gcode_line', 'print_speed', 'skip_objects', 'clean_print_error']
    review_db.execute(text('UPDATE printers SET model=NULL,machine_type=NULL WHERE id=59'))
    review_db.commit()
    sender._client.reset_mock()
    assert not any(cmd() for cmd in (*actions, sender.pause_print, sender.stop_print))
    sender._client.publish.assert_not_called()


@pytest.mark.parametrize('change', ['retained', 'sequence', 'file', 'topic', 'command', 'unknown_result', 'telemetry'])
def test_unrelated_reply_does_not_resolve_start(change):
    from unittest.mock import Mock
    from modules.printers.telemetry.bambu.project_reply import publish_project
    wire = Mock()
    def subscribe(topic):
        wire.on_subscribe(wire, None, 1, [0], None)
        return 0, 1
    wire.subscribe.side_effect = subscribe
    handler = wire.on_message
    install_reply_callbacks(wire)
    callback = wire.on_message
    payload = {'print': {'command': 'project_file', 'url': 'ftp:///attempt.3mf', 'sequence_id': '80'}}
    topic = 'device/test/report'
    calls = []
    def publish(message):
        calls.append(message)
        reply = {**message['print'], 'result': 'SUCCESS'}
        if change == 'sequence': reply['sequence_id'] = '81'
        if change == 'file': reply['url'] = 'ftp:///other.3mf'
        if change == 'command': reply['command'] = 'pause'
        if change == 'unknown_result': reply['result'] = 'pending'
        if change == 'telemetry': reply = {'gcode_state': 'IDLE'}
        wire.on_message(None, None, SimpleNamespace(
            retain=change == 'retained', topic='other' if change == 'topic' else topic,
            payload=json.dumps({'print': reply}).encode()))
        return True
    assert not publish_project(wire, topic, publish, payload, timeout=0)
    assert calls == [payload]
    handler.assert_called_once()
    assert wire.on_message is callback


def seed_retired_opt_in(db):
    from core.models import SystemConfig
    from modules.printers.models import Printer
    from modules.printers.support_levels import CONFIG_KEY
    db.add(SystemConfig(key=CONFIG_KEY, value={'59': {'enabled': True, 'api_type': 'h3c', 'name': db.get(Printer, 59).name}}))
    db.commit()


def test_switch_from_printer_api(target, review_db, token_client):
    from modules.printers import routes_crud, routes_experimental
    client, issue = token_client
    client.app.include_router(routes_experimental.router)
    client.app.include_router(routes_crud.router)
    make_superadmin(review_db)
    headers = issue(['admin', 'write:printers', 'read:printers'])
    seed_retired_opt_in(review_db)
    store = text("SELECT value FROM system_config WHERE key='experimental_printer_actions'")
    before = review_db.execute(store).scalar_one()
    path = '/printers/59'
    for enabled in (True, False):
        response = client.put(path + '/experimental-actions', headers=headers, json={'enabled': enabled})
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'not_experimental'
        detail = client.get(path, headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()['experimental_actions_enabled'] is False
    assert review_db.execute(store).scalar_one() == before


@pytest.mark.postgresql
def test_postgresql_h3c_support_and_failed_state(pg_engine):
    from modules.jobs.models import Job
    from modules.jobs.services import transition_job
    from modules.printers.models import Printer
    from modules.printers.support_levels import experimental_refusal_for_printer
    with Session(pg_engine) as db:
        db.add(Printer(id=59, name='Rack test', model='H3C', api_type='bambu'))
        db.commit()
        assert experimental_refusal_for_printer(db, 59) is None
        seed_retired_opt_in(db)
        assert experimental_refusal_for_printer(db, 59) is None
        db.add(Job(id=53, item_name='Test plate', printer_id=59))
        db.commit()
        reply = 'H3C start reply: FAIL (code 42)'
        assert transition_job(db, 53, 'failed', 'dispatch', where='printer_id=:pid', params={'pid': 59},
                              extra_fields={'fail_notes': reply, 'fail_reason': 'dispatch'})
        db.commit()
        assert db.execute(text('SELECT status,fail_notes FROM jobs WHERE id=53')).one() == ('failed', reply)


def test_failed_education_reservation_records_reply(actual_education_dispatch):
    from modules.organizations.services import EducationPolicyService
    db, uploaded = actual_education_dispatch
    provider = EducationPolicyService()
    reservation = provider.reserve_dispatch(db, job_id=uploaded['job_id'], printer_id=9,
        expected_revision=db.execute(text('SELECT lifecycle_revision FROM education_submissions WHERE id=:id'),
                                     {'id': uploaded['id']}).scalar_one(), extension='.3mf')
    assert reservation
    db.commit()
    reply = 'H3C start reply: FAIL (code 42)'
    assert dispatch._cancel_education_reservation(provider, reservation, failure_notes=reply)
    row = db.execute(text('SELECT s.status,j.status,j.fail_notes FROM education_submissions s JOIN jobs j '
                          'ON s.job_id=j.id WHERE s.id=:id'), {'id': uploaded['id']}).one()
    assert row == ('failed', 'failed', reply)
    assert not dispatch.dispatch_job(9, uploaded['job_id'])[0]


def test_upload_records_only_the_ftp_reply_code(monkeypatch):
    import ftplib
    from unittest.mock import Mock
    from modules.printers.telemetry.bambu import ftp_upload
    peer = Mock()
    peer.storbinary.side_effect = ftplib.error_perm('550 private server detail')
    monkeypatch.setattr(ftp_upload, '_ImplicitFTPS', lambda: peer)
    assert ftp_upload._upload_once('fixture', '', SLICE, 'attempt.3mf', 1) == (False, 'transfer (FTP 550)')


def test_tpu_tool_matches_its_captured_rack_position():
    from modules.printers.nozzle_capability import rack_tool_refusal
    report = json.loads((FIXTURES / 'h3c-ams3h-loaded-2026-11-21.json').read_bytes())['message']['print']
    tools = installed_nozzles(report, include_rack=True)
    source = {'source_index': 0, 'material': 'TPU', 'volume_type': 'TPU285', 'nozzle_diameter': 0.4}
    assert rack_tool_refusal([source], [7], tools) is None
    assert 'Position 9' in rack_tool_refusal([source], [8], tools)


def test_swapped_gcode_tools():
    with ZipFile(SLICE) as z:
        code = z.read('Metadata/plate_1.gcode')
    swapped = code.replace(b'\nT0\n', b'\nTEMP\n', 1).replace(b'\nT1\n', b'\nT0\n', 1).replace(b'\nTEMP\n', b'\nT1\n')
    with pytest.raises(ValueError, match='tool change'):
        inspect_ams_sources(changed_member('plate_1.gcode', code, swapped))


@pytest.mark.parametrize('fixture,expected', [
    (SLICE, [4, 3, 2, 0, 1]), (FIXTURES / 'h3c-cli-two.3mf', [1, 0]),
    (FIXTURES / 'h3c-cli-five.3mf', [4, 3, 2, 1, 0]), (FIXTURES / 'h3c-cli-single.3mf', [0]),
])
def test_cli_mapping_is_bound_to_slice(target, review_db, fixture, expected):
    from modules.models_library.threemf_parser import parse_3mf
    path, _ = target
    data = fixture.read_bytes()
    path.write_bytes(data)
    filaments = parse_3mf(str(path)).filaments
    sha = hashlib.sha256(data).hexdigest()
    creds, job = dispatch._get_printer_info(59), dispatch._load_job(53)
    job.update(ams_mapping=None, payload_sha256=sha, filaments_json=json.dumps(
        [dict(slot=f.slot, type=f.type, color=f.color) for f in filaments]))
    options, reason = dispatch._validated_print_options(job, creds, path, sha)
    assert reason is None and options['ams_mapping'] == expected
    job['ams_mapping'] = MAPPING[:len(expected)]
    options, reason = dispatch._validated_print_options(job, creds, path, sha)
    assert options is None and 'Filament 1' in reason and f'slot {expected[0] + 1}' in reason and 'PLA' in reason
    job['ams_mapping'] = None
    if fixture == SLICE:
        review_db.execute(text("UPDATE filament_slots SET filament_type='empty' "
                               "WHERE printer_id=59 AND slot_number NOT IN (9,7,2,4,5)"))
        review_db.commit()
        options, reason = dispatch._validated_print_options(job, dispatch._get_printer_info(59), path, sha)
        assert options is None and 'Filament 3 requires slot 3' in reason and 'empty' in reason
    review_db.execute(text('UPDATE filament_slots SET filament_type=:m WHERE printer_id=59 AND slot_number=:s'),
                      {'m': 'ABS', 's': expected[0]+1})
    review_db.commit()
    options, reason = dispatch._validated_print_options(job, dispatch._get_printer_info(59), path, sha)
    assert options is None and 'Filament 1' in reason and f'slot {expected[0] + 1}' in reason and 'ABS' in reason


@pytest.mark.parametrize('model', ['H3C', 'P1S'])
@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
def test_lookup_failure_at_sender(model, protocol, monkeypatch, caplog):
    from unittest.mock import Mock
    from modules.printers import printer_models
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    monkeypatch.setattr(printer_models, '_lookup_rack_refusal', Mock(side_effect=RuntimeError()))
    if protocol == 'legacy':
        sender = BambuPrinter('local', 'fixture', '', printer_id=59)
        sender._parse_status({'info': {'module': [{'name': 'ota', 'product_name': model}]}})
    else:
        sender = BambuCommandAdapter(BambuAdapterConfig('59', 'fixture', 'local', '', h3c=model == 'H3C'))
    sender._connected = True
    sender._client = Mock()
    sender._client.publish.return_value.rc = 0
    sender._client.publish.return_value.is_published.return_value = True
    assert sender.pause_print() and sender.stop_print()
    for command in (sender.resume_print, sender.turn_light_on, lambda: sender.set_print_speed(2),
                    lambda: sender.skip_objects([1])):
        assert command()
    assert sender._client.publish.call_count == 6
    assert not sender.start_print('unchecked.3mf')
    assert sender._client.publish.call_count == 6


def test_failed_lookup_returns_checked_dispatch_reason_for_direct_start(monkeypatch):
    from unittest.mock import Mock
    from modules.printers import printer_models
    monkeypatch.setattr(printer_models, '_lookup_rack_refusal', Mock(side_effect=RuntimeError()))
    def reason(command):
        return printer_models.stored_rack_refusal(None, host='local', serial='fixture', payload={'print': {'command': command}})
    assert reason('pause') is None
    assert reason('project_file') == 'Send this job through the checked dispatch path.'


def test_education_multicolour_refusal(target):
    path, _ = target
    creds, job = dispatch._get_printer_info(59), dispatch._load_job(53)
    job['education_submission_id'] = 42
    options, reason = dispatch._validated_print_options(job, creds, path, job['payload_sha256'])
    assert options is None and 'Education jobs on an H3C are limited to one filament' in reason


@pytest.mark.parametrize('reason,code,detail', [
    ('Unconfirmed: try again', None, None),
    ('Could not identify the rack printer.', None, None),
    ('Could not confirm the setting: try again.', None, None),
    ('printer_model_changed: Check the model.', 'printer_model_changed', 'Check the model.'),
])
def test_experimental_refusal_code_and_text(monkeypatch, reason, code, detail):
    from modules.printers import maintenance_gate, support_levels
    seen = {}
    monkeypatch.setattr(dispatch, '_notify_start_outcome', lambda *args, **kw: seen.update(kw))
    maintenance_gate.report_experimental({'id': 53}, reason)
    assert seen['gate_refusal'].code == (code or support_levels.EXPERIMENTAL_NOT_ENABLED)
    assert seen['gate_refusal'].detail == seen['message'] == (detail or reason)


def test_bambu_support_copy():
    from modules.printers import support_levels
    assert support_levels.support_source('bambu') == (
        'Bambu Lab LAN-mode MQTT and FTPS. Skipping objects was verified on a P2S. '
        'Other supported controls were verified on real hardware.')


def test_missing_education_context_preserves_reservation(actual_education_dispatch, monkeypatch):
    from modules.organizations import education_policy
    from modules.organizations.services import EducationPolicyService
    db, uploaded = actual_education_dispatch
    provider = EducationPolicyService()
    reservation = provider.reserve_dispatch(db, job_id=uploaded['job_id'], printer_id=9,
        expected_revision=db.execute(text('SELECT lifecycle_revision FROM education_submissions WHERE id=:id'),
                                     {'id': uploaded['id']}).scalar_one(), extension='.3mf')
    db.commit()
    # Exercise the missing-row return from the final context read.
    monkeypatch.setattr(education_policy._ExecutionAdapter, 'fetchone', lambda *args: None)
    assert provider.cancel_dispatch_reservation(db, claim_id=reservation['claim_id'],
        submission_id=uploaded['id'], job_id=uploaded['job_id'], printer_id=9,
        expected_revision=reservation['authority_revision'], failure_notes='H3C rejected the start') is False
    assert db.execute(text('SELECT state FROM education_monitor_claims WHERE claim_id=:id'),
                      {'id': reservation['claim_id']}).scalar_one() == 'reserved'


@pytest.mark.parametrize('identity', ['_configured_h3c', '_identified_h3c', None])
def test_monitor_lookup_failure(identity, monkeypatch):
    from threading import Lock
    from unittest.mock import Mock
    from modules.printers import monitor_commands, printer_models
    client = Mock()
    client.publish.return_value.rc = 0
    monitor = SimpleNamespace(_lock=Lock(), serial='fixture', _bambu=SimpleNamespace(_connected=True, _client=client))
    if identity:
        setattr(monitor, identity, True)
    monkeypatch.setattr(printer_models, '_lookup_rack_refusal', Mock(side_effect=RuntimeError()))
    result = monitor_commands.execute({59: monitor}, dict(printer_id=59, action='light', on=True,
                                      deadline=time.monotonic()+2))
    assert result == 'accepted'
    client.publish.assert_called_once()


@pytest.mark.parametrize('ack_mid,codes,rejected', [(1, [128], True), (2, [0], False), (1, [], False)])
def test_subscription_must_be_granted_before_publish(ack_mid, codes, rejected):
    from unittest.mock import Mock
    from modules.printers.telemetry.bambu.project_reply import ProjectNotSent, ProjectRejected, publish_project
    handler, subscription_handler = Mock(), Mock()
    wire = SimpleNamespace(on_message=handler, on_subscribe=subscription_handler)
    install_reply_callbacks(wire)
    callbacks = (wire.on_message, wire.on_subscribe)
    telemetry = SimpleNamespace(topic='report', retain=False, payload=b'{"print":{"gcode_state":"IDLE"}}')
    def subscribe(topic):
        wire.on_message(wire, None, telemetry)
        wire.on_subscribe(wire, None, ack_mid, codes)
        return (0, 1)
    wire.subscribe = subscribe
    publish = Mock()
    error = ProjectRejected if rejected else ProjectNotSent
    with pytest.raises(error, match='subscription'):
        publish_project(wire, 'report', publish, {'print': {'url': 'ftp:///one.3mf', 'sequence_id': '1'}}, timeout=0)
    handler.assert_called_once_with(wire, None, telemetry)
    subscription_handler.assert_called_once_with(wire, None, ack_mid, codes)
    assert (wire.on_message, wire.on_subscribe) == callbacks
    publish.assert_not_called()

@pytest.mark.parametrize('group', [b'', b' group_id="x"', b' group_id="-1"', b' group_id="9"'])
def test_invalid_group_is_a_plate_error(group):
    from core.h3c_plate import RackPlateError
    with pytest.raises(RackPlateError, match='Filament .* has no matching nozzle'):
        inspect_ams_sources(changed_member('slice_info.config', b' group_id="4"', group))


def test_shared_group_names_both_filaments():
    from core.h3c_plate import RackPlateError
    with pytest.raises(RackPlateError, match=r'Filaments 1 and 2 '):
        inspect_ams_sources(changed_member('slice_info.config', b'group_id="3"', b'group_id="4"'))


@pytest.mark.parametrize('model', ['H3C', 'P1S'])
def test_saved_mapping_on_single_slot_printer(target, model):
    from modules.printers.dispatch_options import _requires_ams_mapping
    path, _ = target
    creds, job = dispatch._get_printer_info(59), dispatch._load_job(53)
    creds.update(model=model, machine_type=None, slot_count=1, filament_slots=[])
    assert _requires_ams_mapping(creds) is (model == 'H3C')
    options, error = dispatch._validated_print_options(job, creds, path, job['payload_sha256'])
    assert error is None and options['ams_mapping'] == MAPPING
    job['ams_mapping'] = []
    options, error = dispatch._validated_print_options(job, creds, path, job['payload_sha256'])
    assert options is None and 'Stored AMS mapping is invalid' in error


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('by_endpoint', [True, False])
@pytest.mark.parametrize('stored_model', ['P1S', None])
def test_live_h3c_controls_with_lagging_row(target, review_db, protocol, by_endpoint, stored_model):
    from unittest.mock import Mock
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    row_id = None if by_endpoint else 59
    sender = (BambuPrinter('127.0.0.1', 'SYNTHETICPRINTER', '', printer_id=row_id) if protocol == 'legacy'
              else BambuCommandAdapter(BambuAdapterConfig('test', 'SYNTHETICPRINTER', '127.0.0.1', '',
                                                          printer_row_id=row_id, h3c=True)))
    if protocol == 'legacy':
        sender._parse_status({'info': {'module': [{'name': 'ota', 'product_name': 'H3C'}]}})
    sender._connected = True
    sender._client = Mock()
    sender._client.publish.return_value.rc = 0
    sender._client.publish.return_value.is_published.return_value = True
    review_db.execute(text("UPDATE printers SET model=:model,machine_type=NULL WHERE id=59"), {'model': stored_model})
    review_db.commit()
    assert sender.resume_print()
    assert sender.pause_print()
    assert sender.stop_print()
    assert [json.loads(c.args[1])['print']['command'] for c in sender._client.publish.call_args_list] == ['resume', 'pause', 'stop']


@pytest.mark.parametrize('model', ['H3C', 'P1S'])
def test_support_text_from_printer_route(target, review_db, token_client, model):
    from modules.printers import routes_crud
    review_db.execute(text('UPDATE printers SET model=:model,machine_type=NULL WHERE id=59'), {'model': model})
    review_db.commit()
    client, issue = token_client
    client.app.include_router(routes_crud.router)
    response = client.get('/printers/59', headers=issue(['read:printers']))
    assert response.status_code == 200
    expected = ('Job sending, resume and stop were verified on a real Bambu Lab H3C. '
                'Multi-tool jobs were verified with up to five tools.' if model == 'H3C' else
                'Bambu Lab LAN-mode MQTT and FTPS. Skipping objects was verified on a P2S. '
                'Other supported controls were verified on real hardware.')
    assert response.json()['support_source'] == expected


def test_public_fixture_names():
    from xml.etree import ElementTree
    for path in FIXTURES.glob('h3c-*.3mf'):
        with ZipFile(path) as archive:
            plate = ElementTree.fromstring(archive.read('Metadata/slice_info.config'))
            bounds = json.loads(archive.read('Metadata/plate_1.json'))
            for obj in [*plate.findall('.//object'), *bounds['bbox_objects']]:
                name = obj.get('name')
                assert name == 'wipe_tower' or name.startswith(('plate-object-', 'slot '))
            model = ElementTree.fromstring(archive.read('Metadata/model_settings.config'))
            for field in model.findall('.//metadata[@key="source_file"]'):
                assert field.get('value') in ('h3c-test-plate.3mf', 'h3c.3mf')
    assert ECHO['file'] == 'h3c-test-plate.gcode.3mf'
    assert ECHO['subtask_name'] == 'h3c-test-plate'
    assert ECHO['url'] == 'brtc://emmc/' + ECHO['file']
    assert ECHO['md5'] == '0' * 32


def test_reconnect_during_reply_wait_keeps_current_handlers():
    from unittest.mock import Mock
    from modules.printers.telemetry.bambu.project_reply import publish_project
    normal, reconnected = Mock(), Mock()
    wire = SimpleNamespace(on_message=normal, on_subscribe=None)
    install_reply_callbacks(wire)
    def subscribe(topic):
        wire.on_subscribe(wire, None, 1, [0])
        return 0, 1
    wire.subscribe = subscribe
    def send(_):
        wire.on_disconnect(wire, None, 1)
        wire.on_message = reconnected
        wire.on_subscribe = reconnected
        return True
    assert not publish_project(wire, 'report', send, {'print': {'url': 'plate.3mf', 'sequence_id': '1'}}, timeout=0)
    assert wire.on_message is wire.on_subscribe is reconnected
    wire.on_message(wire, None, SimpleNamespace(payload=b'{"print":{"gcode_state":"IDLE"}}'))
    reconnected.assert_called_once()

@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('fault', ['refused_connection', 'tls', 'transfer', 'no_suback', 'mqtt_down'])
def test_p1s_transport_outcomes_match_757c1214(review_db, tmp_path, monkeypatch, protocol, fault):
    from modules.printers.adapters import bambu
    from modules.printers.telemetry.bambu import ftp_upload, session
    from tests.test_contracts.test_multi_source_ams_dispatch import setup_multi, transport, owned_read
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model='P1S')
    events = []
    legacy_upload = bambu.BambuPrinter.upload_file
    real_upload = ftp_upload.upload_file
    transport(monkeypatch, path, protocol, events)
    owned_read(monkeypatch, engine, report, events)
    sender = bambu.BambuPrinter() if protocol == 'legacy' else session.BambuCommandAdapter(None)
    if fault in ('refused_connection', 'tls', 'transfer'):
        failing_ftps_peer(monkeypatch, events, fault)
        monkeypatch.setattr(ftp_upload, 'upload_file', real_upload)
        sender.ip, sender.access_code, sender._configured_h3c = 'local', '', False
        sender.upload_file = legacy_upload.__get__(sender)
    elif fault == 'no_suback':
        from unittest.mock import Mock
        sender._client = Mock()
        sender._client.subscribe.return_value = (0, 1)  # No on_subscribe callback.
    elif fault == 'mqtt_down':
        sender.connect = lambda: False
        def offline():
            raise ConnectionRefusedError()
        sender.start = offline
    # At 757c1214, P1S never waited for SUBACK before project_file.
    # Upload failures released the claim. MQTT failure kept an uncertain claim.
    ok, _ = dispatch.dispatch_job(59, 53)
    assert events.count('upload') == 1
    assert ok is (fault == 'no_suback')
    assert events.count('start') == int(ok)
    status = review_db.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
    claim = review_db.execute(text('SELECT state FROM printer_dispatch_claims WHERE job_id=53')).scalar_one_or_none()
    assert status == ('printing' if ok else 'scheduled')
    assert claim == ('accepted' if ok else 'uncertain' if fault == 'mqtt_down' else None)


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
def test_reconnected_client_cannot_acknowledge_previous_start(monkeypatch, protocol):
    from unittest.mock import Mock
    from modules.printers.adapters import bambu
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    from modules.printers.telemetry.bambu.project_reply import publish_project
    monkeypatch.setattr('core.webhook_utils.device_connect_ip', lambda host, port: host)
    wire = Mock(on_message=None, on_subscribe=None)
    topic = 'device/test/report'
    if protocol == 'legacy':
        sender = bambu.BambuPrinter('127.0.0.1', 'test', '', h3c=True)
        monkeypatch.setattr(bambu.mqtt, 'Client', lambda **kw: wire)
        wire.loop_start.side_effect = lambda: wire.on_connect(wire, None, {}, 0)
        sender.request_full_status = Mock()
        assert sender.connect()
    else:
        sender = BambuCommandAdapter(BambuAdapterConfig('test', 'test', '127.0.0.1', '', h3c=True))
        monkeypatch.setattr(sender, '_client_factory', lambda: wire)
        wire.loop_start.side_effect = lambda: wire.on_connect(wire, None, {}, 0)
        sender.start()
    handlers = (wire.on_message, wire.on_subscribe)
    def subscribe(topic):
        wire.on_subscribe(wire, None, 1, [0])
        return 0, 1
    wire.subscribe.side_effect = subscribe
    def report(body):
        wire.on_message(wire, None, SimpleNamespace(topic=topic, retain=False, payload=json.dumps(body).encode()))
    def publish(payload):
        wire.on_disconnect(wire, None, 1)
        wire.on_connect(wire, None, {}, 0)
        report({'print': {**payload['print'], 'result': 'SUCCESS'}})
        return True
    payload = {'print': {'command': 'project_file', 'url': 'plate.3mf', 'sequence_id': '1'}}
    assert not publish_project(wire, topic, publish, payload, timeout=0)
    assert (wire.on_message, wire.on_subscribe) == handlers
    report({'print': {'gcode_state': 'RUNNING'}})
    if protocol == 'legacy':
        assert sender._status.raw_data['print']['gcode_state'] == 'RUNNING'
