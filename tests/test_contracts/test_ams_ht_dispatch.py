"""Experimental HT selection through scheduling, acquisition and both command builders."""
import hashlib
import json
import time
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


def switch(db, printer_id, enabled):
    support_levels.set_experimental_enabled(db, db.get(Printer, printer_id), enabled, 3, ams_ht=True)
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


def test_schedule_and_editor_accept_reported_ht_and_refuse_switch_off_unknown_and_unplugged(mapping_client, review_db, captured_ams):
    client, auth = mapping_client
    next(u for u in captured_ams['ams'] if u['id'] == '128')['tray'][0]['tray_type'] = 'PETG'
    report_topology(review_db, 9, captured_ams)
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,17,'PETG'),(9,18,'PETG')"))
    printer = review_db.get(Printer, 9)
    support_levels.set_experimental_enabled(review_db, printer, True, 3)  # existing skip-objects opt-in
    review_db.commit()
    mapping = [{'source_slot': 1, 'target_slot': 17}]
    def available():
        preview = client.get('/api/print-files/501?printer_id=9', headers=auth()).json()['ams_preview']
        return {s['slot'] for s in preview['available']}
    assert 17 not in available()
    refused = schedule(mapping_client, mapping=mapping)
    assert refused.status_code == 400 and 'Experimental features' in refused.json()['detail']
    switch(review_db, 9, True)
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


@pytest.mark.parametrize('material', SIX)
def test_ht_validation_uses_exact_material(material):
    from core.print_payload import MappingMaterialMismatch, validate_mapping
    mapping = [{'source_slot': 1, 'target_slot': 24}]
    file = json.dumps([{'slot': 1, 'type': material}])
    assert validate_mapping(mapping, file, {24: material}, 'bambu', ht_enabled=True) == [23]
    with pytest.raises(MappingMaterialMismatch):
        validate_mapping(mapping, file, {24: 'PLA'}, 'bambu', ht_enabled=True)


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('case,reason', [
    ('single_pa6_gf', None), ('single_wrong_nozzle', 'ams_binding_mismatch'),
    ('fresh', None), ('missing_unit', 'ams_read_failed'), ('missing_tray', 'ams_read_failed'),
    ('empty_with_preset', 'tray_material_unreported'), ('wrong_material', 'material_mismatch'), ('wrong_nozzle', 'ams_binding_mismatch'),
    ('off', 'Experimental features'), ('disabled_after_upload', 'Experimental features'),
])
def test_h2d_ht_dispatch_requires_fresh_tray_exact_material_nozzle_and_opt_in(
        review_db, tmp_path, monkeypatch, captured_ams, case, reason, protocol):
    single = case.startswith('single_')
    ht = next(u for u in captured_ams['ams'] if u['id'] == '128')
    if single:
        ht['tray'][0]['tray_type'] = 'PA6-GF'
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model='H2D',
        ids=(0,) if single else (0, 1), mapping=[16] if single else [4, 16],
        materials=('PA6-GF',) if single else ('PETG', 'PLA-S'), extruders=[1] if single else [0, 1])
    if single:
        # The H2D multi-source fixture appends tool-change commands for two materials.
        path.write_bytes(payload(ids=(0,), materials=('PA6-GF',), extruders=[1]))
        review_db.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id=53'),
                          {'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
    review_db.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=59 AND slot_number=5"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,17,:material)"),
                      {'material': slot_filament_value(ht['tray'][0]['tray_type'])})
    report_topology(review_db, 59, captured_ams)
    if case != 'off':
        switch(review_db, 59, True)
    events = []
    def after_upload():
        if case == 'disabled_after_upload':
            switch(review_db, 59, False)
    sent = transport(monkeypatch, path, protocol, events, after_upload=after_upload)
    if case == 'missing_unit':
        captured_ams['ams'].remove(ht)
    if case == 'missing_tray':
        ht['tray'] = []
    if case == 'empty_with_preset':
        captured_ams['tray_exist_bits'] = 'ff'
    if case == 'wrong_material':
        ht['tray'][0]['tray_type'] = 'PLA'
    if case in ('wrong_nozzle', 'single_wrong_nozzle'):
        ht['info'] = '10002004'
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    def acquire(config, *, configuration_key, owner_org_id, selected_slots, operation_id, nozzle_dual, timeout):
        events.append('fresh_read')
        assert selected_slots == ((17,) if single else (5, 17))
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
    ok, message = dispatch.dispatch_job(59, 53)
    assert ok is (reason is None), message
    if reason:
        assert reason in message and not sent
        assert events == ([] if case == 'off' else ['upload'] if case == 'disabled_after_upload' else ['upload', 'fresh_read'])
    else:
        assert events == ['upload', 'fresh_read', 'start']
        assert sent[0]['ams_mapping'] == ([128] if single else [4, 128])
        pairs = [{'ams_id': 1, 'slot_id': 0}, {'ams_id': 128, 'slot_id': 0}]
        assert sent[0]['ams_mapping2'] == (pairs[1:] if single else pairs)


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('mapping,fields', [
    (None, {}), ([-1, 15], {'ams_mapping': [-1, 15]}),
    ([2, 3, 0, 6], {'ams_mapping': [2, 3, 0, 6]}),
    ([16, 23], {'ams_mapping': [128, 135], 'ams_mapping2': [{'ams_id': 128, 'slot_id': 0}, {'ams_id': 135, 'slot_id': 0}]}),
    ([-1, 16], None),
])
def test_exact_print_command_payload_for_ht_and_unchanged_four_tray(protocol, mapping, fields, monkeypatch):
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    sender = object.__new__(BambuPrinter if protocol == 'legacy' else BambuCommandAdapter)
    sender._connected = True
    sent = []
    sender._publish = lambda payload: sent.append(json.dumps(payload)) or True
    monkeypatch.setattr(time, 'time', lambda: 123456)
    options = {'ams_ht_enabled': True} if mapping and any(16 <= n < 24 for n in mapping) else {}
    if fields is None:
        with pytest.raises(ValueError, match='This file leaves a filament unused; sending it to an AMS HT tray is not supported yet'):
            sender.start_print('part.3mf', plate_num=2, ams_mapping=mapping, **options)
        assert sent == []
        return
    assert sender.start_print('part.3mf', plate_num=2, ams_mapping=mapping, **options)
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
                         {17: 'PC'}, 'bambu', ht_enabled=True, check_material=False)
    assert exc.value.detail == 'Invalid or duplicate AMS slot'


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('mapping', [[16], [23], [128]])
def test_direct_start_requires_explicit_ht_permission(protocol, mapping):
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    sender = object.__new__(BambuPrinter if protocol == 'legacy' else BambuCommandAdapter)
    sender._connected = True
    sent = []
    sender._publish = sent.append
    with pytest.raises(ValueError, match='AMS HT'):
        sender.start_print('part.3mf', ams_mapping=mapping)
    assert sent == []


@pytest.mark.parametrize('path', ['archive', 'repeat', 'release', 'bulk_release'])
def test_switched_off_ht_blocks_queue_paths(reprint_client, review_db, captured_ams, monkeypatch, path):
    client, _, source = reprint_client
    events = []
    sent = transport(monkeypatch, source, 'legacy', events)
    review_db.execute(text("UPDATE print_files SET filaments_json=:f WHERE id=101"),
                      {'f': json.dumps([{'slot': 1, 'type': 'PLA-S'}])})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,17,:material)"),
                      {'material': slot_filament_value('PLA-S')})
    report_topology(review_db, 9, captured_ams)
    switch(review_db, 9, True)
    body = {'printer_id': 9, 'ams_mapping': [{'source_slot': 1, 'target_slot': 17}]}
    created = client.post('/archives/101/reprint', json=body)
    assert created.status_code == 200, created.text
    job = created.json()['job_id']
    if path in ('release', 'bulk_release'):
        review_db.execute(text('UPDATE jobs SET hold=TRUE WHERE id=:id'), {'id': job})
    switch(review_db, 9, False)
    count = review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    if path == 'archive':
        response = client.post('/archives/101/reprint', json=body)
    elif path == 'repeat':
        response = client.post(f'/jobs/{job}/repeat')
    elif path == 'release':
        response = client.patch(f'/jobs/{job}', json={'hold': False})
    else:
        response = client.post('/jobs/bulk-update', json={'job_ids': [job], 'action': 'unhold'})
    assert response.status_code == 400, response.text
    assert response.json()['detail'] == support_levels.AMS_HT_MESSAGE
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == count
    if path in ('release', 'bulk_release'):
        assert review_db.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': job}).scalar_one()
    assert not sent and not events


@pytest.mark.parametrize('path', ['start_next', 'reset_reprint', 'sparse'])
def test_ht_dispatch_entry_points_refuse_before_upload(review_db, tmp_path, monkeypatch, captured_ams, path):
    from modules.printers import start_next
    from modules.jobs.routes.jobs_lifecycle import reset_job
    from tests.test_contracts.test_completion_summary import request
    sparse = path == 'sparse'
    _, source, _ = setup_multi(review_db, tmp_path, monkeypatch, model='H2D',
        ids=(1,) if sparse else (0,), mapping=[-1, 16] if sparse else [16],
        materials=('PLA-S',), extruders=[0, 1] if sparse else [1])
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,17,:material)"),
                      {'material': slot_filament_value('PLA-S')})
    report_topology(review_db, 59, captured_ams)
    events = []
    sent = transport(monkeypatch, source, 'legacy', events)
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
            assert reset_job(53, USER, review_db).ams_mapping == [16]
            review_db.execute(text("UPDATE jobs SET printer_id=59,status='scheduled' WHERE id=53"))
            review_db.commit()
        else:
            switch(review_db, 59, True)
        ok, message = dispatch.dispatch_job(59, 53)
    assert not ok
    assert (('This file leaves a filament unused; sending it to an AMS HT tray is not supported yet'
             if sparse else support_levels.AMS_HT_MESSAGE) in message), message
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
def test_ht_switch_off_at_job_start_paths(review_db, tmp_path, monkeypatch, captured_ams, path):
    from fastapi import HTTPException
    from modules.jobs.routes.jobs_lifecycle import dispatch_job_to_printer, start_job
    from modules.printers import dispatch_background
    _, source, _ = setup_multi(review_db, tmp_path, monkeypatch, mapping=[16], ids=(0,), materials=('PLA-S',))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,17,:material)"),
                      {'material': slot_filament_value('PLA-S')})
    report_topology(review_db, 59, captured_ams)
    events = []
    sent = transport(monkeypatch, source, 'legacy', events)
    if path == 'manual_status':
        assert start_job(53, USER, review_db).status.value == 'printing'
    elif path == 'wait':
        with pytest.raises(HTTPException) as exc:
            dispatch_job_to_printer(53, USER, review_db, wait=True)
        assert exc.value.status_code == 400 and support_levels.AMS_HT_MESSAGE in exc.value.detail
    elif path == 'background':
        handle = dispatch_job_to_printer(53, USER, review_db)
        try:
            assert dispatch_background.drain(timeout=5)
        finally:
            dispatch_background.startup()
        result = dispatch.read_dispatch_progress(review_db, handle['attempt_id'], 53)
        assert result['phase'] == 'refused' and support_levels.AMS_HT_MESSAGE in result['message']
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
        assert not ok and support_levels.AMS_HT_MESSAGE in message
    assert not sent and not events
