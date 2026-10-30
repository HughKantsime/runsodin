"""Explicit multi-source evidence through the real dispatch and MQTT builders."""
import hashlib
import io
import json
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text
from core.ams_source_inspection import inspect_ams_sources, inspect_single_source_ams
from modules.printers.dispatch_ams_gate import preflight_ams_source
from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSAcquisitionCollector
from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_ams_evidence_transactions import RUNTIME
from tests.test_contracts.test_ams_default_mapping import progress_record
from tests.test_contracts.test_education_review_workflow import review_db


# Instantiated M620 forms from BambuStudio's change_filament_gcode templates.
CHANGE_COMMANDS = {
    'A1': 'M620.11 S0\nM620.10 A0 F300\nM620.10 A1 F300 L40 H0.4 T250\nM620.11 S1 I0 E-10 F300\n',
    'P1S': 'M620.1 X30 Y50 F21000 P0\nM620.1 X40 Y60 F21000 P1\nM620.1 X50 Y70 F21000 P2\nM620.11 S0\n',
    'X1C': 'M620.1 X30 Y50 F21000 P0\nM620.11 S1 I0 E10 F300\n',
    'H2D': 'M620.10 A0 F300 L40 H0.4 T250 P240 S1\nM620.11 P1 I0 B-1 E-10 F300\nM620.15 C240\nM620.11 H0\nM620.10 R0.8\nM620.13 W0 L20 T240 R0.0\nM620 Q3\n',
}


def payload(*, ids=(0, 1), materials=('PETG', 'PLA'), extruders=None, plate_ids=None, code=None, model=None):
    profiles = ['PETG'] * (max(ids) + 1)
    for index, material in zip(ids, materials):
        profiles[index] = material
    project = {'filament_type': profiles}
    if extruders is not None:
        project.update(physical_extruder_map=[0, 1], filament_nozzle_map=[e + 1 for e in extruders],
                       nozzle_diameter=['0.4', '0.4'])
    xml = '<config><plate><metadata key="index" value="1"/>' + ''.join(
        f'<filament id="{index + 1}" type="{material}" used_g="{10 + i * 20}"/>'
        for i, (index, material) in enumerate(zip(ids, materials))) + '</plate></config>'
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as package:
        package.writestr('Metadata/slice_info.config', xml)
        package.writestr('Metadata/project_settings.config', json.dumps(project))
        package.writestr('Metadata/plate_1.json', json.dumps({'filament_ids': list(ids) if plate_ids is None else plate_ids}))
        package.writestr('Metadata/plate_1.gcode', code if code is not None else
            '; filament_type = ' + ';'.join(profiles) + '\n' + ''.join(
                f'M620 S{index}A\nT{index}\nM621 S{index}A\n' for index in ids)
            + CHANGE_COMMANDS.get(model, '').replace('I0 ', f'I{ids[0]} '))
    return raw.getvalue()


def setup_multi(review_db, tmp_path, monkeypatch, *, model='A1', mapping=(2, 1), ids=(0, 1),
                materials=('PETG', 'PLA'), extruders=None):
    from modules.printers import dispatch, dispatch_acquisition
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu', slot_count=8)
    path.write_bytes(payload(ids=ids, materials=materials, extruders=extruders, model=model))
    filaments = [{'slot': index + 1, 'type': material, 'used_grams': 10 + i * 20}
                 for i, (index, material) in enumerate(zip(ids, materials))]
    with engine.begin() as conn:
        conn.execute(text('UPDATE printers SET model=:model,machine_type=:model WHERE id=59'), {'model': model})
        conn.execute(text('UPDATE jobs SET ams_mapping=:mapping,payload_sha256=:sha WHERE id=53'),
                     {'mapping': json.dumps(mapping) if mapping is not None else None, 'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
        conn.execute(text('UPDATE print_files SET filaments_json=:filaments WHERE id=52'), {'filaments': json.dumps(filaments)})
        conn.execute(text('DELETE FROM filament_slots WHERE printer_id=59'))
        for target, material in {2: 'PLA', 3: 'PETG', 5: 'PLA'}.items():
            conn.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,:slot,:material)'),
                         {'slot': target, 'material': material})
    report = {'command': 'push_status', 'gcode_state': 'IDLE', 'print_error': 0, 'hms': [],
              'nozzle_type': 'hardened_steel', 'nozzle_diameter': .4,
              'device': {'nozzle': {'info': [{'id': i, 'type': 'HH01', 'diameter': .4} for i in (0, 1)]}}}
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: report)
    monkeypatch.setattr(dispatch.time, 'sleep', lambda *a: None)
    return engine, path, report


def transport(monkeypatch, path, protocol, events, after_upload=None, before_publish=None):
    from modules.printers.telemetry import feature_flag
    from modules.printers.adapters import bambu
    from modules.printers.telemetry.bambu import commands, ftp_upload, session
    monkeypatch.setattr(feature_flag, 'is_v2_enabled', lambda: protocol == 'v2')
    cls = bambu.BambuPrinter if protocol == 'legacy' else commands.BambuCommandAdapter
    sender = object.__new__(cls)
    sender._connected = True
    sent = []
    def publish(command):
        events.append('start')
        sent.append(command['print'])
        return True
    def uploaded(staged, remote):
        events.append('upload')
        assert Path(staged).read_bytes() == path.read_bytes()
        if after_upload:
            after_upload()
        return True
    sender._publish = publish
    sender.upload_file = uploaded
    def connect():
        if before_publish:
            before_publish()
        return True
    sender.connect = connect
    sender.disconnect = lambda: None
    sender.start = connect
    sender.stop = lambda: None
    sender.is_connected = lambda: True
    if protocol == 'legacy':
        monkeypatch.setattr(bambu, 'BambuPrinter', lambda **kw: sender)
    else:
        monkeypatch.setattr(session, 'BambuCommandAdapter', lambda config: sender)
        monkeypatch.setattr(ftp_upload, 'upload_file', lambda host, code, staged, remote: uploaded(staged, remote))
    return sent


def owned_read(monkeypatch, engine, report, events, *, slots=(2, 3), change=None):
    from modules.printers import dispatch_ams_gate as gate
    monkeypatch.setattr(gate, 'read_runtime_identity', lambda: RUNTIME)
    monkeypatch.setattr(gate, 'revalidate_runtime_identity', lambda runtime: None)
    def acquired(config, **options):
        events.append('fresh_read')
        assert events[0] == 'upload'
        assert engine.pool.checkedout() == 0
        assert options['selected_slots'] == slots
        packet = {**report, 'ams': {'ams': [
            {'id': '0', 'info': '10001003', 'tray': [{'id': '1', 'tray_type': 'PLA'}, {'id': '2', 'tray_type': 'PETG'}]},
            {'id': '1', 'info': '10001103', 'tray': [{'id': '0', 'tray_type': 'PLA'}]}]}}
        collector = RawAMSAcquisitionCollector(topic=config.topic_report, configuration_key=options['configuration_key'],
            owner_org_id=1, selected_slots=slots, subscribe_mid=1, incarnation=options['operation_id'],
            allow_unreported=True, nozzle_dual=options['nozzle_dual'])
        assert collector.acknowledge_subscription(collector.incarnation, 1, [0])
        assert collector.observe(incarnation=collector.incarnation, topic=config.topic_report, retained=False, packet=packet)
        snapshot = collector.snapshot(configuration_key=options['configuration_key'], owner_org_id=1)
        if change:
            snapshot = change(snapshot)
        return RuntimeBoundAMSResult(True, snapshot, RUNTIME.incarnation, RUNTIME.clock_domain, True, None)
    monkeypatch.setattr(gate, 'read_current_selected_ams', acquired)


@pytest.mark.parametrize('model', ['A1', 'P1S', 'X1C'])
@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('sparse', [False, True])
def test_multi_source_dispatch_checks_every_tray_and_publishes_mapping(review_db, tmp_path, monkeypatch, model, protocol, sparse):
    from modules.printers import dispatch
    ids, mapping = ((1, 3), [-1, 2, -1, 1]) if sparse else ((0, 1), [2, 1])
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model=model, ids=ids, mapping=mapping)
    events = []
    sent = transport(monkeypatch, path, protocol, events)
    owned_read(monkeypatch, engine, report, events)
    result = dispatch.dispatch_job(59, 53)
    assert result[0], result
    assert events == ['upload', 'fresh_read', 'start']
    assert len(sent) == 1 and sent[0]['ams_mapping'] == mapping
    assert sent[0]['param'] == 'Metadata/plate_1.gcode'


@pytest.mark.parametrize('model', ['A1', 'P1S', 'X1C'])
@pytest.mark.parametrize('failure', ['first_material', 'second_material', 'missing', 'stale', 'unknown', 'empty'])
def test_any_bad_tray_refuses_and_surfaces_reason(review_db, tmp_path, monkeypatch, model, failure):
    from modules.printers import dispatch
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model=model)
    events = []
    sent = transport(monkeypatch, path, 'legacy', events)
    def change(snapshot):
        if failure == 'missing':
            return replace(snapshot, trays=snapshot.trays[1:])
        if failure == 'stale':
            return replace(snapshot, received_monotonic=time.monotonic() - 11)
        index = 1 if failure == 'first_material' else 0
        trays = list(snapshot.trays)
        trays[index] = replace(trays[index], material='ABS' if 'material' in failure else None,
            material_state='reported' if 'material' in failure else ('explicit_empty' if failure == 'empty' else 'unknown'))
        return replace(snapshot, trays=tuple(trays))
    owned_read(monkeypatch, engine, report, events, change=change)
    with progress_record(engine) as background:
        result = dispatch.dispatch_job(59, 53)
        background.progress('refused', message=result[1])
    assert not result[0] and not sent
    assert events == ['upload', 'fresh_read']
    code = 'snapshot_stale' if failure == 'stale' else 'tray_missing' if failure == 'missing' else 'material_mismatch' if 'material' in failure else 'tray_material_unreported'
    assert code in result[1]
    if failure != 'stale':
        assert f'AMS slot {3 if failure == "first_material" else 2}' in result[1]
    with engine.connect() as conn:
        assert code in background.load(conn, background.result_key(53))['message']


@pytest.mark.parametrize('case', ['unmapped', 'derived', 'stored_change', 'education', 'mixed_shared'])
def test_unsupported_mapping_stops_before_upload(review_db, tmp_path, monkeypatch, case):
    from modules.printers import dispatch
    mapping = None if case == 'derived' else [2, -1] if case == 'unmapped' else [2, 2] if case == 'mixed_shared' else [2, 1]
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, mapping=mapping)
    job = dispatch._load_job(53)
    if case in ('stored_change', 'education'):
        job['ams_mapping' if case == 'stored_change' else 'education_submission_id'] = '[1,2]' if case == 'stored_change' else 900
        with pytest.raises(ValueError):
            preflight_ams_source(job, mapping=[2, 1], staged_path=path, payload_sha256=job['payload_sha256'])
    else:
        monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda *a, **kw: pytest.fail('transport reached'))
        ok, reason = dispatch.dispatch_job(59, 53)
        assert not ok
        if case == 'derived':
            assert 'ams_mapping_required' in reason and 'AMS preview suggestion' in reason
        if case == 'mixed_shared':
            assert 'AMS slot 3' in reason


def test_two_sources_can_share_one_matching_tray(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, materials=('PETG', 'PETG'), mapping=[2, 2])
    events = []
    sent = transport(monkeypatch, path, 'legacy', events)
    owned_read(monkeypatch, engine, report, events, slots=(3,))
    result = dispatch.dispatch_job(59, 53)
    assert result[0], result
    assert sent[0]['ams_mapping'] == [2, 2]
    assert events == ['upload', 'fresh_read', 'start']


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('failure', [None, 'wrong_binding', 'missing_binding', 'missing_source_extruder'])
def test_h2d_requires_each_sources_nozzle_binding(review_db, tmp_path, monkeypatch, protocol, failure):
    from modules.printers import dispatch
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model='H2D', mapping=[2, 4],
        extruders=None if failure == 'missing_source_extruder' else [0, 1])
    events = []
    sent = transport(monkeypatch, path, protocol, events)
    def change(snapshot):
        if failure in ('wrong_binding', 'missing_binding'):
            return replace(snapshot, trays=(snapshot.trays[0], replace(snapshot.trays[1], extruder=0 if failure == 'wrong_binding' else None)))
        return snapshot
    owned_read(monkeypatch, engine, report, events, slots=(3, 5), change=change)
    result = dispatch.dispatch_job(59, 53)
    assert result[0] is (failure is None), result
    if failure is None:
        assert sent[0]['ams_mapping'] == [2, 4]
    else:
        assert not sent
        assert {'wrong_binding': 'ams_binding_mismatch', 'missing_binding': 'ams_binding_unverified',
                'missing_source_extruder': 'source_extruder_unverified'}[failure] in result[1]


@pytest.mark.parametrize('change', ['material', 'plate', 'commands', 'extra_tool'])
def test_file_sources_require_independent_agreement(change):
    options = {'plate_ids': [0]} if change == 'plate' else {}
    if change in ('material', 'commands', 'extra_tool'):
        options['code'] = '; filament_type = PETG;' + ('ABS' if change == 'material' else 'PLA') + '\nM620 S0A\nM621 S0A\n'
        if change != 'commands':
            options['code'] += 'M620 S1A\nM621 S1A\n'
        if change == 'extra_tool':
            options['code'] += 'T2\n'
    with pytest.raises(ValueError):
        inspect_ams_sources(payload(**options))


def test_file_facts_preserve_sparse_ids_materials_extruders_and_grams():
    result = inspect_ams_sources(payload(ids=(1, 3), extruders=[1, 0, 0, 1]))
    assert [(s.source_index, s.material, s.extruder, s.used_grams) for s in result.sources] == [(1, 'PETG', 0, 10), (3, 'PLA', 1, 30)]
    with pytest.raises(ValueError):
        inspect_single_source_ams(payload())


@pytest.mark.parametrize('binding,expected', [('10001003', 0), ('10001103', 1), ('10001e03', None), (None, None), ('invalid', None)])
def test_binding_comes_only_from_reported_ams_unit(binding, expected):
    from modules.printers.telemetry.bambu.ams_evidence import parse_raw_ams_facts
    unit = {'id': '1', 'tray': [{'id': '0', 'tray_type': 'PLA'}]}
    if binding is not None:
        unit['info'] = binding
    fact, = parse_raw_ams_facts({'ams': {'ams': [unit]}})
    assert fact.slot_number == 5 and fact.extruder == expected


@pytest.mark.parametrize('change', ['hash', 'size', 'plate'])
def test_multi_source_service_retains_payload_binding(tmp_path, change):
    from modules.printers.services import inspect_ams_payload
    path = tmp_path / 'multi.3mf'
    path.write_bytes(payload())
    with pytest.raises((ValueError, RuntimeError)):
        inspect_ams_payload(path, expected_sha256='0' * 64 if change == 'hash' else hashlib.sha256(path.read_bytes()).hexdigest(),
            expected_bytes=path.stat().st_size + int(change == 'size'), plate_index=int(change == 'plate'), multi_source=True)


@pytest.mark.parametrize('command', [
    'M620.11 S1 I9 E-10 F300', 'M620.1 X30 Y50 F21000 P3',
    'M620.10 A2 F300 L40 H0.4 T250', 'M620.11 P1 I2 B-1 E-10 F300',
    'M620.15 C240 S2', 'M620 Q2 S0', 'M620.10 R0.8 T2',
])
def test_change_template_exceptions_cannot_hide_other_sources_or_shapes(command):
    code = '; filament_type = PETG;PLA\nM620 S0A\nM621 S0A\nM620 S1A\nM621 S1A\n'
    with pytest.raises(ValueError):
        inspect_ams_sources(payload(code=code + command + '\n'))


def cross_nozzle_file(filament_map, *commands, nozzle_map=('1',), plate_map=None):
    """A two-colour H2D slice across both nozzles, shaped like the slicer CLI's output."""
    project = {'filament_type': ['PLA', 'PLA'], 'filament_map': list(filament_map),
               'filament_nozzle_map': list(nozzle_map), 'physical_extruder_map': ['1', '0'],
               'nozzle_diameter': ['0.4', '0.4'], 'nozzle_volume_type': ['High Flow', 'High Flow']}
    xml = ('<config><plate><metadata key="index" value="1"/>'
           f'<metadata key="filament_maps" value="{plate_map or " ".join(filament_map)}"/>'
           '<filament id="1" type="PLA" used_g="206.93" used_m="68.28" nozzle_diameter="0.40" volume_type="High Flow"/>'
           '<filament id="2" type="PLA" used_g="8.28" used_m="2.73" nozzle_diameter="0.40" volume_type="High Flow"/></plate></config>')
    code = ('; filament_type = PLA;PLA\nT1001\nM620 S0A H-1 ; switch material if AMS exist\nT0 H-1\nM621 S0A\n'
            'M620.11 P0 I0 B-1 E0\nM620.11 K1 I0 B-1 R10 F997.797\nM620.11 S1 L0 I0 B-1 R10 D8 E-10 F997.797\n'
            'M620 S1A H-1\nT1 H-1\nM621 S1A\n' + ''.join(command + '\n' for command in commands) + 'T65535\nT65279\n')
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as package:
        package.writestr('Metadata/slice_info.config', xml)
        package.writestr('Metadata/project_settings.config', json.dumps(project))
        package.writestr('Metadata/plate_1.json', json.dumps({'filament_ids': [0, 1]}))
        package.writestr('Metadata/plate_1.gcode', code)
    return raw.getvalue()


@pytest.mark.parametrize('filament_map,commands,expected', [
    # Real prints: the fireplace (filament_map 2 1) and the dual cap (filament_map 1 2).
    (('2', '1'), ('M620.11 P0 I1 B0 E0', 'M620.11 K1 I1 B0 R10 F997.797', 'M620.11 S1 L0 I1 B0 R10 D8 E-10 F997.797',
                  'M620.11 P0 I0 B1 E0', 'M620.11 K1 I0 B1 R10 F997.797'), {0: 0, 1: 1}),
    (('1', '2'), ('M620.11 P0 I1 B1 E0', 'M620.11 K1 I1 B1 R10 F997.797', 'M620.11 S1 L0 I1 B1 R10 D8 E-10 F997.797'),
     {0: 1, 1: 0}),
    # One nozzle: B stays -1, and the second source's nozzle is now known.
    (('1', '1'), (), {0: 1, 1: 1}),
])
def test_cli_slice_across_nozzles_is_verified_with_each_sources_nozzle(filament_map, commands, expected):
    result = inspect_ams_sources(cross_nozzle_file(filament_map, *commands))
    assert {source.source_index: source.extruder for source in result.sources} == expected


@pytest.mark.parametrize('command', [
    'M620.11 P0 I0 B0 E0',  # Source 1 is on hotend 1 in this file.
    'M620.11 K1 I1 B1 R10 F997.797', 'M620.11 S1 L0 I1 B1 R10 D8 E-10 F997.797',
    'M620.11 P0 I2 B0 E0',  # Not a source of this file.
    'M620.11 P0 I1 B2 E0', 'M620.11 P2 I1 B0 E0', 'M620.11 K1 I1 B0', 'M620.11 S1 L2 I1 B0 E-10 F300',
    'M620.11 P0 I1 B0 E0 S1',
])
def test_cross_nozzle_commands_must_agree_with_the_files_own_map(command):
    assert inspect_ams_sources(cross_nozzle_file(('2', '1'), 'M620.11 P0 I1 B0 E0'))
    with pytest.raises(ValueError, match='^Unverifiable source mapping$'):
        inspect_ams_sources(cross_nozzle_file(('2', '1'), command))


def test_cross_nozzle_command_needs_a_filament_map_to_agree_with():
    from core.ams_source_inspection import _cross_nozzle_agrees
    assert _cross_nozzle_agrees('M620.11 P0 I1 B0 E0', {}, {0, 1}) is False
    assert _cross_nozzle_agrees('M620.11 P0 I1 B0 E0', {'filament_map': ['2', '3']}, {0, 1}) is False
    assert _cross_nozzle_agrees('M620.11 P0 I1 B-1 E0', {'filament_map': ['2', '1']}, {0, 1}) is None


@pytest.mark.parametrize('nozzle_map,plate_map', [
    (('2', '2'), None),  # A full map still has to agree: source 1 would be on another nozzle.
    (('1', '1'), None),
    (('1',), '1 1'),  # The plate's own map still has to agree.
])
def test_only_the_one_entry_default_nozzle_map_is_ignored(nozzle_map, plate_map):
    with pytest.raises(ValueError, match='^Unverifiable source mapping$'):
        inspect_ams_sources(cross_nozzle_file(('2', '1'), nozzle_map=nozzle_map, plate_map=plate_map))


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('filament_map,commands,accepted', [
    (('2', '1'), ('M620.11 P0 I1 B0 E0', 'M620.11 P0 I0 B1 E0'), True),
    # The same trays, but the file puts each source on the other nozzle.
    (('1', '2'), ('M620.11 P0 I1 B1 E0',), False),
])
def test_h2d_cli_slice_across_nozzles_dispatches_only_on_the_right_trays(review_db, tmp_path, monkeypatch, protocol,
                                                                         filament_map, commands, accepted):
    from modules.printers import dispatch
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model='H2D', mapping=[1, 4], materials=('PLA', 'PLA'))
    path.write_bytes(cross_nozzle_file(filament_map, *commands))
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id=53'), {'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
    events = []
    sent = transport(monkeypatch, path, protocol, events)
    owned_read(monkeypatch, engine, report, events, slots=(2, 5))
    result = dispatch.dispatch_job(59, 53)
    assert result[0] is accepted, result
    if accepted:
        assert sent[0]['ams_mapping'] == [1, 4]
    else:
        assert not sent and 'ams_binding_mismatch' in result[1]


@pytest.mark.parametrize('filament_map', [('2', '1'), ('1', '2'), ('1', '1')])
def test_nozzle_gate_and_source_inspection_agree_on_a_cli_slice(tmp_path, filament_map):
    from modules.printers.dispatch_capability_payload import inspect_payload
    path = tmp_path / 'slice.3mf'
    path.write_bytes(cross_nozzle_file(filament_map))
    facts = inspect_payload(path, 1, 'H2D')
    sources = inspect_ams_sources(path.read_bytes()).sources
    assert not facts['incomplete']
    assert {int(index): extruder for index, extruder in facts['filament_map'].items()} == {
        source.source_index: source.extruder for source in sources}
    assert {requirement['extruder'] for requirement in facts['requirements']} == {source.extruder for source in sources}


@pytest.mark.parametrize('change,side', [
    ({'id': 0, 'diameter': .6}, 'right'), ({'id': 1, 'diameter': .6}, 'left'),
    ({'id': 0, 'type': 'HS01'}, 'right'), ({'id': 1, 'type': 'HS01'}, 'left'),
])
def test_cli_slice_compares_both_installed_nozzles(review_db, tmp_path, monkeypatch, change, side):
    from modules.printers import dispatch
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch, model='H2D', mapping=[1, 4], materials=('PLA', 'PLA'))
    path.write_bytes(cross_nozzle_file(('2', '1'), 'M620.11 P0 I1 B0 E0', 'M620.11 P0 I0 B1 E0'))
    with engine.begin() as conn:
        conn.execute(text('UPDATE jobs SET payload_sha256=:sha WHERE id=53'), {'sha': hashlib.sha256(path.read_bytes()).hexdigest()})
    report['device']['nozzle']['info'][change['id']].update(change)
    events = []
    sent = transport(monkeypatch, path, 'legacy', events)
    owned_read(monkeypatch, engine, report, events, slots=(2, 5))
    result = dispatch.dispatch_job(59, 53)
    assert not result[0] and not sent, result
    assert 'nozzle_mismatch' in result[1] and side in result[1]


@pytest.mark.parametrize('protocol', ['legacy', 'v2'])
@pytest.mark.parametrize('change', ['mapping', 'hold', 'stale'])
def test_final_command_gate_rechecks_original_all_tray_proof(review_db, tmp_path, monkeypatch, protocol, change):
    from modules.printers import dispatch, dispatch_ams_gate as gate
    engine, path, report = setup_multi(review_db, tmp_path, monkeypatch)
    events = []
    def before_publish():
        assert events == ['upload', 'fresh_read']
        if change == 'stale':
            now = time.monotonic()
            monkeypatch.setattr(gate.time, 'monotonic', lambda: now + 11)
        else:
            with engine.begin() as conn:
                conn.execute(text("UPDATE jobs SET ams_mapping='[1,2]' WHERE id=53" if change == 'mapping' else
                                  'UPDATE jobs SET hold=TRUE WHERE id=53'))
    sent = transport(monkeypatch, path, protocol, events, before_publish=before_publish)
    owned_read(monkeypatch, engine, report, events)
    result = dispatch.dispatch_job(59, 53)
    assert not result[0] and not sent
    assert events == ['upload', 'fresh_read']
    assert ('snapshot_stale' if change == 'stale' else 'admission_changed') in result[1]


def test_collector_does_not_merge_partial_tray_packets():
    collector = RawAMSAcquisitionCollector(topic='device/SYNTHETIC/report', configuration_key='a' * 64,
        owner_org_id=1, selected_slots=(2, 3), subscribe_mid=1)
    assert collector.acknowledge_subscription(collector.incarnation, 1, [0])
    for tray in (1, 2):
        packet = {'gcode_state': 'IDLE', 'print_error': 0, 'hms': [],
                  'ams': {'ams': [{'id': '0', 'tray': [{'id': str(tray), 'tray_type': 'PLA'}]}]}}
        assert not collector.observe(incarnation=collector.incarnation, topic='device/SYNTHETIC/report', retained=False, packet=packet)
        assert collector.snapshot(configuration_key='a' * 64, owner_org_id=1) is None


def test_education_approval_refuses_multi_source_before_consuming_approval(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from core.errors import OdinError
    from modules.organizations import education_ams_approval_service as service
    path = tmp_path / 'multi.3mf'
    path.write_bytes(payload())
    graph = SimpleNamespace(stored_path=path, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), storage_bytes=path.stat().st_size)
    monkeypatch.setattr(service, 'capture_payload_graph', lambda *a, **kw: graph)
    monkeypatch.setattr(service, 'approve_submission', lambda *a, **kw: pytest.fail('approval reached'))
    db = SimpleNamespace(execute=lambda *a: SimpleNamespace(first=lambda: None), rollback=lambda: None, close=lambda: None)
    with pytest.raises(OdinError, match='single-source'):
        asyncio.run(service.approve_with_explicit_ams(None, db=db, principal={'id': 3, 'group_id': 1},
            submission_id=14, body=SimpleNamespace(command_id='synthetic', revision=1, plate_index=0)))


@pytest.mark.parametrize('conflict', [False, True])
def test_h2d_slice_and_project_extruders_must_agree(conflict):
    original = payload(extruders=[0, 1])
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(result, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith('slice_info.config'):
                data = data.replace(b'<plate>', b'<plate><metadata key="filament_maps" value="' + (b'1 1' if conflict else b'1 2') + b'"/>')
            target.writestr(name, data)
    if conflict:
        with pytest.raises(ValueError):
            inspect_ams_sources(result.getvalue())
    else:
        assert [source.extruder for source in inspect_ams_sources(result.getvalue()).sources] == [0, 1]


@pytest.mark.parametrize('command,accepted', [('M620.17 T0 S245 L0', True), ('M620.17 T1 S245 L1', True),
                                            ('M620.17 T0 S245 L1', False), ('M620.17 T1 S245 L0', False)])
def test_h2d_gcode_nozzle_source_declaration_must_agree(command, accepted):
    code = '; filament_type = PETG;PLA\nM620 S0A\nM621 S0A\nM620 S1A\nM621 S1A\n' + command + '\n'
    if accepted:
        assert len(inspect_ams_sources(payload(extruders=[0, 1], code=code)).sources) == 2
    else:
        with pytest.raises(ValueError):
            inspect_ams_sources(payload(extruders=[0, 1], code=code))
