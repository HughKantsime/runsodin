"""AMS HT selection through scheduling, acquisition and both command builders."""
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core import printer_display_facts
from core.base import slot_filament_value
from modules.printers import dispatch, dispatch_ams_gate as gate, support_levels
from modules.printers.models import Printer
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSAcquisitionCollector
from modules.printers.telemetry.bambu.ams_acquisition_session import _valid_snapshot
from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_spool_slot_prevention import graph, USER
from tests.test_contracts.test_cert_A_harness import load_frames
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_multi_source_ams_dispatch import payload, setup_multi, transport
from tests.test_contracts.test_schedule_ams_mapping import mapping_client, schedule
from tests.test_contracts.test_six_materials import SIX


@pytest.fixture
def captured_ams():
    for _, frame in load_frames('bambu-h2d-recovery'):
        ams = frame.get('print', {}).get('ams', {})
        if any(u['id'] == '128' for u in ams.get('ams', [])):
            return ams
    pytest.fail('The H2D capture must contain AMS HT unit 128')


def report_topology(db, printer_id, ams):
    monitor = SimpleNamespace(printer_id=printer_id)
    printer_display_facts.observe(monitor, {'ams': ams})
    printer_display_facts.persist(db.connection().connection, monitor)
    db.commit()


def test_new_report_without_ht_invalidates_prior_fresh_ht_read(captured_ams):
    collector = RawAMSAcquisitionCollector(topic='device/synthetic/report', configuration_key='a'*64,
        owner_org_id=1, selected_slots=[17], subscribe_mid=1)
    assert collector.acknowledge_subscription(collector.incarnation, 1, [0])
    def observe(ams):
        return collector.observe(incarnation=collector.incarnation, topic='device/synthetic/report',
                                 retained=False, packet={'ams': ams})
    def snapshot():
        return collector.snapshot(configuration_key='a'*64, owner_org_id=1)
    assert observe(captured_ams)
    first = snapshot()
    assert first.trays[0].unit_id == 128
    delta = {'tray_now': '128'}
    assert not observe(delta)
    assert snapshot() is first
    monitor = SimpleNamespace()
    printer_display_facts.observe(monitor, {'ams': captured_ams})
    topology = monitor._display_facts['ht_trays']
    printer_display_facts.observe(monitor, {'ams': delta})
    assert monitor._display_facts['ht_trays'] is topology
    assert '17' in topology['value']
    captured_ams['ams'] = [unit for unit in captured_ams['ams'] if unit['id'] != '128']
    assert not observe(captured_ams)
    assert snapshot() is None
    printer_display_facts.observe(monitor, {'ams': captured_ams})
    assert monitor._display_facts['ht_trays']['value'] == {}


@pytest.mark.parametrize('skip_objects', [False, True])
def test_schedule_and_editor_accept_reported_ht_and_refuse_unknown_and_unplugged(
        mapping_client, review_db, captured_ams, skip_objects):
    client, auth = mapping_client
    next(u for u in captured_ams['ams'] if u['id'] == '128')['tray'][0]['tray_type'] = 'PETG'
    report_topology(review_db, 9, captured_ams)
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,17,'PETG'),(9,18,'PETG')"))
    printer = review_db.get(Printer, 9)
    support_levels.set_experimental_enabled(review_db, printer, skip_objects, 3)
    review_db.commit()
    mapping = [{'source_slot': 1, 'target_slot': 17}]
    def available():
        preview = client.get('/api/print-files/501?printer_id=9', headers=auth()).json()['ams_preview']
        return {s['slot'] for s in preview['available']}
    assert 17 in available() and 18 not in available()
    created = schedule(mapping_client, mapping=mapping)
    assert created.status_code == 200, created.text
    job = created.json()['job_id']
    def patch(vector):
        return client.patch(f'/api/jobs/{job}', headers=auth(), json={'ams_mapping': vector})
    assert patch([16]).json()['ams_mapping'] == [16]
    assert patch([17]).json()['detail'] == 'Invalid or duplicate AMS slot'
    report_topology(review_db, 9, {'ams': []})
    assert 17 not in available()
    assert patch([16]).json()['detail'] == 'Invalid or duplicate AMS slot'


def test_schedule_refuses_slot_17_on_four_tray_only_printer(mapping_client, review_db, captured_ams):
    captured_ams['ams'] = [u for u in captured_ams['ams'] if u['id'] != '128']
    report_topology(review_db, 9, captured_ams)
    response = schedule(mapping_client, mapping=[{'source_slot': 1, 'target_slot': 17}])
    assert response.status_code == 400
    assert response.json()['detail'] == 'Invalid or duplicate AMS slot'


def test_kobra_s1_refuses_ht_slot_even_with_a_stale_inventory_row(mapping_client, review_db):
    review_db.execute(text("UPDATE printers SET api_type='moonraker',model='Kobra S1' WHERE id=9"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,17,'PLA')"))
    review_db.commit()
    response = schedule(mapping_client, mapping=[{'source_slot': 1, 'target_slot': 17}])
    assert response.status_code == 400
    assert response.json()['detail'] == 'Invalid or duplicate AMS slot'


@pytest.fixture
def ht_editor_job(mapping_client, review_db, captured_ams):
    client, auth = mapping_client
    next(u for u in captured_ams['ams'] if u['id'] == '128')['tray'][0]['tray_type'] = 'PETG'
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,17,'PETG')"))
    report_topology(review_db, 9, captured_ams)
    job = schedule(mapping_client, mapping=[{'source_slot': 1, 'target_slot': 17}]).json()['job_id']
    return client, auth, job


@pytest.mark.parametrize('vector', [[16, -1, -1, -1], [0, -1, -1, -1], [16] + [-1] * 15,
                                    [16, -1, 0], [16] + [-1] * 16])
def test_editor_rejects_vectors_longer_than_file(ht_editor_job, vector):
    client, auth, job = ht_editor_job
    response = client.patch(f'/api/jobs/{job}', headers=auth(), json={'ams_mapping': vector})
    assert response.status_code == 400
    assert response.json()['detail'] == 'AMS mapping must cover every source filament'
    assert client.get(f'/api/jobs/{job}', headers=auth()).json()['ams_mapping'] == [16]


def test_repeat_rebuilds_preexisting_padded_ht_mapping(ht_editor_job, review_db):
    client, auth, job = ht_editor_job
    review_db.execute(text('UPDATE jobs SET ams_mapping=:mapping WHERE id=:id'),
                      {'mapping': json.dumps([16, -1, -1, -1]), 'id': job})
    review_db.commit()
    repeat = client.post(f'/api/jobs/{job}/repeat', headers=auth())
    assert repeat.status_code == 200, repeat.text
    repeated = repeat.json()['new_job_id']
    assert client.get(f'/api/jobs/{repeated}', headers=auth()).json()['ams_mapping'] == [16]


def test_editor_round_trips_sparse_ht_and_refuses_unused_marker_on_used_source(ht_editor_job, review_db):
    client, auth, job = ht_editor_job
    def patch(vector):
        return client.patch(f'/api/jobs/{job}', headers=auth(), json={'ams_mapping': vector})
    def set_sources(slots):
        review_db.execute(text('UPDATE print_files SET filaments_json=:f WHERE id=501'),
                          {'f': json.dumps([{'slot': slot, 'type': 'PETG'} for slot in slots])})
        review_db.commit()
    set_sources([2])
    vector = [-1, 16]
    response = patch(vector)
    assert response.status_code == 200, response.text
    assert response.json()['ams_mapping'] == vector
    assert client.get(f'/api/jobs/{job}', headers=auth()).json()['ams_mapping'] == vector
    assert patch([-1, 17]).json()['detail'] == 'Invalid or duplicate AMS slot'
    set_sources([1, 2])
    response = patch(vector)
    assert response.status_code == 400
    assert response.json()['detail'] == 'Invalid or duplicate AMS slot'


@pytest.mark.parametrize('material', SIX)
def test_ht_validation_uses_exact_material(material):
    from core.print_payload import MappingMaterialMismatch, validate_mapping
    mapping = [{'source_slot': 1, 'target_slot': 24}]
    file = json.dumps([{'slot': 1, 'type': material}])
    assert validate_mapping(mapping, file, {24: material}, 'bambu') == [23]
    with pytest.raises(MappingMaterialMismatch):
        validate_mapping(mapping, file, {24: 'PLA'}, 'bambu')


def mock_ht_read(monkeypatch, report, captured_ams, events, expected_slots):
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    def acquire(config, *, configuration_key, owner_org_id, selected_slots, operation_id, nozzle_dual, timeout):
        events.append('fresh_read')
        assert selected_slots == expected_slots
        started = time.monotonic()
        collector = RawAMSAcquisitionCollector(topic=config.topic_report, configuration_key=configuration_key,
            owner_org_id=owner_org_id, selected_slots=selected_slots, subscribe_mid=1, incarnation=operation_id,
            allow_unreported=True, nozzle_dual=nozzle_dual)
        assert collector.acknowledge_subscription(operation_id, 1, [0])
        ok = collector.observe(incarnation=operation_id, topic=config.topic_report, retained=False,
                               packet={**report, 'ams': captured_ams})
        snapshot = collector.snapshot(configuration_key=configuration_key, owner_org_id=owner_org_id)
        if ok:
            assert _valid_snapshot(snapshot, key=configuration_key, owner=owner_org_id, slots=selected_slots,
                                   incarnation=operation_id, started=started, deadline=started+timeout,
                                   now=time.monotonic(), allow_unreported=True)
        return RuntimeBoundAMSResult(ok, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquire)


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('extra_gap', [False, True], ids=['as-selected', 'unused-before-ht'])
@pytest.mark.parametrize('case,reason', [
    ('single_pa6_gf', None), ('single_wrong_nozzle', 'ams_binding_mismatch'),
    ('single_leading_gap', None), ('duplicate_tray', 'ams_read_failed'),
    ('single_trailing_padding', 'Stored AMS mapping differs from file sources'),
    ('fresh', None), ('missing_unit', 'ams_read_failed'), ('missing_tray', 'ams_read_failed'),
    ('empty_with_preset', 'tray_material_unreported'), ('wrong_material', 'material_mismatch'), ('wrong_nozzle', 'ams_binding_mismatch'),
    ('inventory_only', 'Invalid or duplicate AMS slot'), ('unplugged_after_upload', 'Invalid or duplicate AMS slot'),
])
def test_h2d_ht_dispatch_requires_fresh_tray_exact_material_nozzle(
        review_db, tmp_path, monkeypatch, captured_ams, case, reason, protocol, extra_gap):
    single = case.startswith('single_')
    ids, mapping, extruders = (0, 1), [4, 16], [0, 1]
    if case == 'single_leading_gap':
        ids, mapping = (1,), [-1, 16]
    elif single:
        ids, mapping, extruders = (0,), [16], [1]
    if extra_gap:
        ids = (*ids[:-1], ids[-1] + 1)
        mapping.insert(-1, -1)
        extruders.insert(-1, 0)
    if case == 'single_trailing_padding':
        mapping.extend([-1] * (4 - len(mapping)))
    ht = next(u for u in captured_ams['ams'] if u['id'] == '128')
    if single:
        ht['tray'][0]['tray_type'] = 'PA6-GF'
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model='H2D',
        ids=ids, mapping=mapping, materials=('PA6-GF',) if single else ('PETG', 'PLA-S'), extruders=extruders)
    if single:
        # The H2D multi-source fixture appends tool-change commands for two materials.
        path.write_bytes(payload(ids=ids, materials=('PA6-GF',), extruders=extruders))
        review_db.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id=53'),
                          {'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
    review_db.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=59 AND slot_number=5"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,17,:material)"),
                      {'material': slot_filament_value(ht['tray'][0]['tray_type'])})
    report_topology(review_db, 59, captured_ams)
    if case == 'inventory_only':
        report_topology(review_db, 59, {'ams': []})
    events = []
    def after_upload():
        if case == 'unplugged_after_upload':
            report_topology(review_db, 59, {'ams': []})
    sent = transport(monkeypatch, path, protocol, events, after_upload=after_upload)
    if case == 'missing_unit':
        captured_ams['ams'].remove(ht)
    if case == 'missing_tray':
        ht['tray'] = []
    if case == 'duplicate_tray':
        ht['tray'].append(ht['tray'][0].copy())
    if case == 'empty_with_preset':
        captured_ams['tray_exist_bits'] = 'ff'
    if case == 'wrong_material':
        ht['tray'][0]['tray_type'] = 'PLA'
    if case in ('wrong_nozzle', 'single_wrong_nozzle'):
        ht['info'] = '10002004'
    mock_ht_read(monkeypatch, report, captured_ams, events, (17,) if single else (5, 17))
    ok, message = dispatch.dispatch_job(59, 53)
    assert ok is (reason is None), message
    if reason:
        assert reason in message and not sent
        assert events == ([] if case in ('inventory_only', 'single_trailing_padding') else ['upload'] if case == 'unplugged_after_upload' else ['upload', 'fresh_read'])
    else:
        assert events == ['upload', 'fresh_read', 'start']
        assert sent[0]['ams_mapping'] == [128 if n == 16 else n for n in mapping]
        pairs = {-1: (255, 255), 4: (1, 0), 16: (128, 0)}
        assert sent[0]['ams_mapping2'] == [{'ams_id': pairs[n][0], 'slot_id': pairs[n][1]} for n in mapping]


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('mapping,fields', [
    (None, {}), ([-1, 15], {'ams_mapping': [-1, 15]}),
    ([2, 3, 0, 6], {'ams_mapping': [2, 3, 0, 6]}),
    ([16, 23], {'ams_mapping': [128, 135], 'ams_mapping2': [{'ams_id': 128, 'slot_id': 0}, {'ams_id': 135, 'slot_id': 0}]}),
    pytest.param([-1, 16], {'ams_mapping': [-1, 128], 'ams_mapping2': [
        {'ams_id': 255, 'slot_id': 255}, {'ams_id': 128, 'slot_id': 0}]}, id='leading-unused-ht'),
    pytest.param([0, -1, 16], {'ams_mapping': [0, -1, 128], 'ams_mapping2': [
        {'ams_id': 0, 'slot_id': 0}, {'ams_id': 255, 'slot_id': 255}, {'ams_id': 128, 'slot_id': 0}]},
        id='mixed-ht-inner-unused'),
    pytest.param([0, 16, -1], {'ams_mapping': [0, 128, -1], 'ams_mapping2': [
        {'ams_id': 0, 'slot_id': 0}, {'ams_id': 128, 'slot_id': 0}, {'ams_id': 255, 'slot_id': 255}]},
        id='mixed-ht-trailing-unused'),
    pytest.param([16, -1, -1, -1], None, id='studio-project-file-echo'),
])
def test_exact_print_command_payload_for_ht_and_unchanged_four_tray(protocol, mapping, fields, monkeypatch):
    """ODIN never stores trailing padding. The encoder also accepts Studio's captured vector."""
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    sender = object.__new__(BambuPrinter if protocol == 'legacy' else BambuCommandAdapter)
    sender._connected = True
    sent = []
    sender._publish = lambda payload: sent.append(json.dumps(payload)) or True
    monkeypatch.setattr(time, 'time', lambda: 123456)
    if fields is None:
        from modules.printers.telemetry.bambu.ams_identity import print_mapping_fields
        echo = json.loads((Path(__file__).parents[1] / 'fixtures/h2d_studio_ht_project_file_echo.json').read_text())
        assert print_mapping_fields(mapping) == {key: echo[key] for key in ('ams_mapping', 'ams_mapping2')}
        fields = {key: echo[key] for key in ('use_ams', 'ams_mapping', 'ams_mapping2')}
    assert sender.start_print('part.3mf', plate_num=2, ams_mapping=mapping)
    assert sent == [json.dumps({'print': {
        'sequence_id': '23456', 'command': 'project_file', 'param': 'Metadata/plate_2.gcode',
        'subtask_name': 'part', 'url': 'ftp:///part.3mf', 'bed_type': 'auto', 'timelapse': False,
        'bed_leveling': True, 'flow_cali': False, 'vibration_cali': True, 'layer_inspect': False,
        'use_ams': True, 'profile_id': '0', 'project_id': '0', 'subtask_id': '0', 'task_id': '0', **fields,
    }})]


@pytest.mark.parametrize('writer', ['load', 'scan'])
def test_load_and_scan_bind_the_spool_to_reported_ht_slot(review_db, captured_ams, writer):
    from modules.inventory.routes._helpers import ScanAssignRequest, SpoolLoadRequest
    from modules.inventory.routes.spool_ops import scan_assign_spool
    from modules.inventory.routes.spools import load_spool
    from tests.test_contracts.test_spool_slot_prevention import USER, assert_moved
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(199,'Synthetic','Test','PETG')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,qr_code,org_id) VALUES(199,199,'test-ht',1)"))
    report_topology(review_db, 9, captured_ams)
    if writer == 'load':
        assert load_spool(199, SpoolLoadRequest(printer_id=9, slot_number=17), USER, review_db)['success']
    else:
        assert scan_assign_spool(ScanAssignRequest(qr_code='test-ht', printer_id=9, slot=17), USER, review_db).success
    assert_moved(review_db, printer_id=9, number=17)


def test_ht_duplicate_source_is_refused_even_for_held_jobs():
    from core.print_payload import validate_mapping
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        validate_mapping([{'source_slot': 1, 'target_slot': 17}] * 2,
                         json.dumps([{'slot': 1, 'type': 'PC'}, {'slot': 2, 'type': 'PC'}]),
                         {17: 'PC'}, 'bambu', check_material=False)
    assert exc.value.detail == 'Invalid or duplicate AMS slot'


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('mapping,wire', [([16], [128]), ([23], [135]), ([128], None), ([-1, 16], [-1, 128])])
def test_direct_start_encodes_physical_ht_slots_and_refuses_wire_ids(protocol, mapping, wire):
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    sender = object.__new__(BambuPrinter if protocol == 'legacy' else BambuCommandAdapter)
    sender._connected = True
    sent = []
    sender._publish = lambda msg: sent.append(msg) or True
    if wire is None:
        with pytest.raises(ValueError, match='Select an AMS HT tray by its physical slot'):
            sender.start_print('part.3mf', ams_mapping=mapping)
        assert sent == []
    else:
        assert sender.start_print('part.3mf', ams_mapping=mapping)
        assert [msg['print']['ams_mapping'] for msg in sent] == [wire]


@pytest.mark.parametrize('invalid', [-2, -1.0])
def test_ht_encoder_refuses_invalid_unused_positions(invalid):
    from modules.printers.telemetry.bambu.ams_identity import print_mapping_fields
    with pytest.raises(ValueError):
        print_mapping_fields([16, invalid])


@pytest.mark.parametrize('path', ['archive', 'repeat', 'release', 'bulk_release'])
@pytest.mark.parametrize('reported', [False, True], ids=['unplugged', 'sparse-reported'])
def test_ht_queue_paths(reprint_client, review_db, captured_ams, monkeypatch, path, reported):
    client, _, source = reprint_client
    source_slot = 2 if reported else 1
    events = []
    sent = transport(monkeypatch, source, 'legacy', events)
    review_db.execute(text("UPDATE print_files SET filaments_json=:f WHERE id=101"),
                      {'f': json.dumps([{'slot': source_slot, 'type': 'PLA-S'}])})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,17,:material)"),
                      {'material': slot_filament_value('PLA-S')})
    report_topology(review_db, 9, captured_ams)
    body = {'printer_id': 9, 'ams_mapping': [{'source_slot': source_slot, 'target_slot': 17}]}
    created = client.post('/archives/101/reprint', json=body)
    assert created.status_code == 200, created.text
    job = created.json()['job_id']
    if path in ('release', 'bulk_release'):
        review_db.execute(text('UPDATE jobs SET hold=TRUE WHERE id=:id'), {'id': job})
    if not reported:
        report_topology(review_db, 9, {'ams': []})
    review_db.commit()
    count = review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    if path == 'archive':
        response = client.post('/archives/101/reprint', json=body)
    elif path == 'repeat':
        response = client.post(f'/jobs/{job}/repeat')
    elif path == 'release':
        response = client.patch(f'/jobs/{job}', json={'hold': False})
    else:
        response = client.post('/jobs/bulk-update', json={'job_ids': [job], 'action': 'unhold'})
    if reported:
        assert response.status_code == 200, response.text
        if path in ('archive', 'repeat'):
            job = response.json()['job_id' if path == 'archive' else 'new_job_id']
        row = review_db.execute(text('SELECT ams_mapping,hold FROM jobs WHERE id=:id'), {'id': job}).one()
        assert json.loads(row.ams_mapping) == [-1, 16] and not row.hold
        assert not sent and not events
        return
    assert response.status_code == 400, response.text
    assert response.json()['detail'] == 'Invalid or duplicate AMS slot'
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == count
    if path in ('release', 'bulk_release'):
        assert review_db.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': job}).scalar_one()
    assert not sent and not events


@pytest.mark.parametrize('path', ['start_next', 'reset_reprint', 'direct'])
@pytest.mark.parametrize('reported', [False, True], ids=['unplugged', 'sparse-reported'])
def test_ht_dispatch_entry_points(review_db, tmp_path, monkeypatch, captured_ams, path, reported):
    from modules.printers import start_next
    from modules.jobs.routes.jobs_lifecycle import reset_job
    from tests.test_contracts.test_completion_summary import request
    sparse = reported or path == 'direct'
    ids, mapping, extruders = ((1,), [-1, 16], [0, 1]) if sparse else ((0,), [16], [1])
    _, source, report = setup_multi(review_db, tmp_path, monkeypatch, model='H2D',
        ids=ids, mapping=mapping, materials=('PLA-S',), extruders=extruders)
    if reported:
        source.write_bytes(payload(ids=ids, materials=('PLA-S',), extruders=extruders))
        review_db.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id=53'),
                          {'sha': hashlib.sha256(source.read_bytes()).hexdigest()})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,17,:material)"),
                      {'material': slot_filament_value('PLA-S')})
    report_topology(review_db, 59, captured_ams)
    events = []
    sent = transport(monkeypatch, source, 'legacy', events)
    if not reported:
        report_topology(review_db, 59, {'ams': []})
    else:
        mock_ht_read(monkeypatch, report, captured_ams, events, (17,))
    if path == 'start_next':
        from modules.jobs import completion_summary
        monkeypatch.setattr(completion_summary, 'finished_attempt', lambda *_: {'economic_uid': 'previous-attempt', 'ended_at': '2026-01-01T00:00:00Z'})
        review_db.execute(text("UPDATE jobs SET status='pending',quantity=1 WHERE id=53"))
        review_db.commit()
        printer = review_db.get(Printer, 59)
        suggestion = start_next.next_suggestion(review_db, printer, actor=USER)
        assert suggestion['job_id'] == 53, suggestion
        result = start_next.start(review_db, printer, USER, request(suggestion))
        ok, message = result['success'], result['message']
    else:
        if path == 'reset_reprint':
            review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=53"))
            assert reset_job(53, USER, review_db).ams_mapping == mapping
            review_db.execute(text("UPDATE jobs SET printer_id=59,status='scheduled' WHERE id=53"))
            review_db.commit()
        ok, message = dispatch.dispatch_job(59, 53)
    if reported:
        assert ok, message
        assert events == ['upload', 'fresh_read', 'start']
        assert sent[0]['ams_mapping'] == [-1, 128]
        return
    assert not ok
    assert 'Invalid or duplicate AMS slot' in message, message
    assert not sent and not events


@pytest.mark.parametrize('count', [None, 0])
def test_load_keeps_original_slot_count_errors(graph, count):
    from fastapi import HTTPException
    from modules.inventory.routes._helpers import SpoolLoadRequest
    from modules.inventory.routes.spools import load_spool
    graph.execute(text('UPDATE printers SET slot_count=:count WHERE id=9'), {'count': count})
    graph.commit()
    with pytest.raises(TypeError if count is None else HTTPException) as error:
        load_spool(199, SpoolLoadRequest(printer_id=9, slot_number=1), USER, graph)
    if count == 0:
        assert error.value.status_code == 400 and error.value.detail == 'Invalid slot number (1-0)'


@pytest.mark.parametrize('count', [None, 0])
def test_scan_keeps_four_slot_fallback(graph, count):
    from modules.inventory.routes._helpers import ScanAssignRequest
    from modules.inventory.routes.spool_ops import scan_assign_spool
    graph.execute(text('UPDATE printers SET slot_count=:count WHERE id=9'), {'count': count})
    graph.commit()
    def scan(slot):
        return scan_assign_spool(ScanAssignRequest(qr_code='move-test', printer_id=9, slot=slot), USER, graph)
    assert scan(4).success
    refusal = scan(5)
    assert not refusal.success
    assert refusal.message == f"Invalid slot 5 for {graph.get(Printer, 9).name} (has 4 slots)"


@pytest.mark.parametrize('writer', ['load', 'scan'])
@pytest.mark.parametrize('slot,count', [(17, 4), (24, 24)])
def test_spool_assignment_explains_inventory_only_ht(graph, writer, slot, count):
    from fastapi import HTTPException
    from modules.inventory.routes._helpers import ScanAssignRequest, SpoolLoadRequest
    from modules.inventory.routes.spool_ops import scan_assign_spool
    from modules.inventory.routes.spools import load_spool
    graph.execute(text("INSERT INTO filament_slots(printer_id,slot_number) VALUES(9,:slot)"), {'slot': slot})
    graph.execute(text('UPDATE printers SET slot_count=:count WHERE id=9'), {'count': count})
    graph.commit()
    if writer == 'load':
        with pytest.raises(HTTPException) as exc:
            load_spool(199, SpoolLoadRequest(printer_id=9, slot_number=slot), USER, graph)
        assert exc.value.status_code == 400
        message = exc.value.detail
    else:
        refusal = scan_assign_spool(ScanAssignRequest(qr_code='move-test', printer_id=9, slot=slot), USER, graph)
        assert not refusal.success
        message = refusal.message
    assert message == f'AMS HT {slot - 16} is not reported by the printer right now.'


@pytest.mark.parametrize('writer', ['load', 'scan'])
def test_unknown_slot_on_four_tray_printer_keeps_its_original_message(graph, writer):
    from fastapi import HTTPException
    from modules.inventory.routes._helpers import ScanAssignRequest, SpoolLoadRequest
    from modules.inventory.routes.spool_ops import scan_assign_spool
    from modules.inventory.routes.spools import load_spool
    if writer == 'load':
        with pytest.raises(HTTPException) as exc:
            load_spool(199, SpoolLoadRequest(printer_id=9, slot_number=17), USER, graph)
        assert exc.value.detail == 'Invalid slot number (1-4)'
    else:
        result = scan_assign_spool(ScanAssignRequest(qr_code='move-test', printer_id=9, slot=17), USER, graph)
        assert result.message == f"Invalid slot 17 for {graph.get(Printer, 9).name} (has 4 slots)"


@pytest.mark.parametrize('path', ['wait', 'background', 'automatic', 'automatic_fallback', 'manual_status'])
def test_unreported_ht_at_job_start_paths(review_db, tmp_path, monkeypatch, captured_ams, path):
    from fastapi import HTTPException
    from modules.jobs.routes.jobs_lifecycle import dispatch_job_to_printer, start_job
    from modules.printers import dispatch_background
    _, source, _ = setup_multi(review_db, tmp_path, monkeypatch, mapping=[16], ids=(0,), materials=('PLA-S',))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,17,:material)"),
                      {'material': slot_filament_value('PLA-S')})
    captured_ams['ams'] = [u for u in captured_ams['ams'] if u['id'] != '128']
    report_topology(review_db, 59, captured_ams)
    events = []
    sent = transport(monkeypatch, source, 'legacy', events)
    if path == 'manual_status':
        assert start_job(53, USER, review_db).status.value == 'printing'
    elif path == 'wait':
        with pytest.raises(HTTPException) as exc:
            dispatch_job_to_printer(53, USER, review_db, wait=True)
        assert exc.value.status_code == 400 and 'Invalid or duplicate AMS slot' in exc.value.detail
    elif path == 'background':
        handle = dispatch_job_to_printer(53, USER, review_db)
        try:
            assert dispatch_background.drain(timeout=5)
        finally:
            dispatch_background.startup()
        result = dispatch.read_dispatch_progress(review_db, handle['attempt_id'], 53)
        assert result['phase'] == 'refused' and 'Invalid or duplicate AMS slot' in result['message']
    else:
        monkeypatch.setenv('AUTO_DISPATCH', 'true')
        select = dispatch._get_next_scheduled_job
        if path == 'automatic_fallback':
            # A missing Education head takes the automatic fallback to the real generic job.
            monkeypatch.setattr(dispatch, '_get_next_scheduled_job', lambda pid, generic_only=False:
                select(pid, generic_only=True) if generic_only else
                {'id': 999, 'item_name': 'Removed head', 'education_submission_id': 999})
        outcomes = []
        dispatch_job = dispatch.dispatch_job
        def record(printer_id, job_id):
            result = dispatch_job(printer_id, job_id)
            outcomes.append((job_id, result))
            return result
        monkeypatch.setattr(dispatch, 'dispatch_job', record)
        dispatch.attempt_dispatch(59)
        assert [job for job, _ in outcomes] == ([999, 53] if path == 'automatic_fallback' else [53])
        ok, message = outcomes[-1][1]
        assert not ok and 'Invalid or duplicate AMS slot' in message
    assert not sent and not events
