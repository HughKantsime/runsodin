"""Short synthetic Studio-shaped files; no copied profiles or machine templates."""
import io
import json
import zipfile
from xml.etree import ElementTree as ET

import pytest
from core.ams_source_inspection import inspect_ams_sources, inspect_single_source_ams
from modules.printers.dispatch_capability_payload import inspect_payload
from modules.printers.dispatch_capability_gate import static_refusal, create_capability_gate


def fixture(*, mapping=(2, 1), physical=(1, 0), mode='Auto For Flush', tower=False,
            used=None, rows=True, project=None, xml=None, code=None):
    used = tuple(range(len(mapping))) if used is None else used
    settings = {'printer_model': 'Bambu Lab H2D', 'filament_type': ['PLA'] * len(mapping),
                'filament_map': [1] * len(mapping), 'filament_nozzle_map': [0] * len(mapping),
                'physical_extruder_map': list(physical), 'nozzle_diameter': ['0.4', '0.6'],
                'extruder_max_nozzle_count': [1, 1], 'nozzle_volume_type': ['Standard', 'High Flow']}
    if project:
        project(settings)
    plate = ET.Element('plate')
    ET.SubElement(plate, 'metadata', key='index', value='1')
    ET.SubElement(plate, 'metadata', key='filament_maps', value=' '.join(map(str, mapping)))
    ET.SubElement(plate, 'metadata', key='filament_map_mode', value=mode)
    for i in used:
        ET.SubElement(plate, 'filament', id=str(i + 1), type='PLA', used_g='1', group_id=str(mapping[i] - 1))
    if rows:
        for logical in sorted({mapping[i] - 1 for i in used}):
            ET.SubElement(plate, 'nozzle', id=str(logical), extruder_id=str(logical + 1))
    if xml:
        xml(plate)
    root = ET.Element('config'); root.append(plate)
    lines = ['; filament_type = ' + ';'.join(settings['filament_type']), 'T1001']
    for i in used:
        lines.extend([f'M620 S{i}A H-1', f'T{i} H-1', f'M621 S{i}A',
                      f'M620.11 P0 I{i} B{-1 if tower else mapping[i] - 1} E0'])
    for logical in sorted({mapping[i] - 1 for i in used}):
        i = next(i for i in used if mapping[i] - 1 == logical)
        lines.append(f'M620.17 T{physical[logical]} S200 L{i}')
    lines.extend(['; CONFIG_BLOCK_START', '; physical_extruder_map = ' + ','.join(map(str, physical)), '; filament_map = ' + ','.join(map(str, mapping)),
                  '; filament_nozzle_map = ' + ','.join(str(v - 1) for v in mapping), '; CONFIG_BLOCK_END'])
    gcode = '\n'.join(lines) + '\n'
    if code:
        gcode = code(gcode)
    result = io.BytesIO()
    with zipfile.ZipFile(result, 'w') as package:
        package.writestr('Metadata/project_settings.config', json.dumps(settings))
        package.writestr('Metadata/slice_info.config', ET.tostring(root))
        package.writestr('Metadata/plate_1.json', json.dumps({'filament_ids': list(used)}))
        package.writestr('Metadata/plate_1.gcode', gcode)
    return result.getvalue()


def facts(tmp_path, payload):
    path = tmp_path / 'synthetic.3mf'; path.write_bytes(payload)
    return inspect_payload(path, 1, 'H2D')


def accepted(tmp_path, payload, expected):
    assert {s.source_index: s.extruder for s in inspect_ams_sources(payload).sources} == expected
    capability = facts(tmp_path, payload)
    assert capability['mapping_refusal'] is None
    assert capability['incomplete'] is False
    assert capability['filament_map'] == expected
    assert [r['extruder'] for r in capability['requirements']] == list(expected.values())


def refused(tmp_path, payload, reason):
    with pytest.raises(ValueError, match=reason) as error:
        inspect_ams_sources(payload)
    capability = facts(tmp_path, payload)
    # Capability deliberately has no location; AMS retains its exact file line.
    diagnostic, separator, location = str(error.value).partition(', line ')
    assert capability['mapping_refusal'] == diagnostic
    if separator:
        assert location.isdecimal()
    assert capability['filament_map'] == {}
    assert capability['requirements'] == []
    path = tmp_path / 'synthetic.3mf'
    refusal = static_refusal(path, 1, {'model': capability['printer_model']})
    assert refusal.code == 'source_mapping_unverified'
    assert refusal.detail == capability['mapping_refusal']


@pytest.mark.parametrize('mode', ['Auto For Flush', 'Auto For Match', 'Manual'])
@pytest.mark.parametrize('tower', [False, True])
@pytest.mark.parametrize('rows', [False, True])
@pytest.mark.parametrize('physical', [(1, 0)])
def test_job181_plate_overrides_project_and_both_consumers_agree(tmp_path, mode, tower, rows, physical):
    accepted(tmp_path, fixture(mode=mode, tower=tower, rows=rows, physical=physical), {0: physical[1], 1: physical[0]})


def test_job164_six_zero_project_nozzles_are_not_assignment_evidence(tmp_path):
    accepted(tmp_path, fixture(mapping=(2, 1, 1, 1, 2, 1), tower=True), {0: 0, 1: 1, 2: 1, 3: 1, 4: 0, 5: 1})


@pytest.mark.parametrize('key', ['filament_map', 'filament_nozzle_map'])
@pytest.mark.parametrize('value', [None, [], [1], [99, 99], [True, False], 'garbage'])
def test_project_maps_are_neither_evidence_nor_conflicts(tmp_path, key, value):
    accepted(tmp_path, fixture(project=lambda s: s.update({key: value})), {0: 0, 1: 1})


@pytest.mark.parametrize('physical', [[], [0], [1], [0, 0], [1, 1], [0, 1, 0], [False, 1], [0.0, 1], ['0', '2'], None])
def test_invalid_physical_map_refuses(tmp_path, physical):
    refused(tmp_path, fixture(project=lambda s: s.update(physical_extruder_map=physical)), 'physical_extruder_map_invalid')


@pytest.mark.parametrize('kind,reason', [('missing', 'missing'), ('duplicate', 'duplicate'), ('short', 'incomplete'), ('bad', 'out_of_range')])
def test_plate_map_required_once_and_in_range(tmp_path, kind, reason):
    def change(plate):
        node = plate.find("metadata[@key='filament_maps']")
        if kind == 'missing': plate.remove(node)
        elif kind == 'duplicate': ET.SubElement(plate, 'metadata', node.attrib)
        else: node.set('value', '2' if kind == 'short' else '2 0')
    refused(tmp_path, fixture(xml=change), 'plate_filament_map_' + reason)


@pytest.mark.parametrize('key,value,reason', [
    ('printer_model', 'Bambu Lab H3C', 'unsupported_rack_printer'),
    ('extruder_max_nozzle_count', [1, 6], 'one_nozzle_per_extruder_unverified'),
    ('extruder_max_nozzle_count', [1], 'one_nozzle_per_extruder_unverified'),
    ('extruder_max_nozzle_count', [True, 1], 'one_nozzle_per_extruder_unverified'),
    ('nozzle_diameter', ['0.4'], 'two_extruder_layout_unverified'),
    ('handle_hotend_as_extruder', '1', 'hotend_as_extruder_unsupported'),
    ('has_filament_switcher', True, 'filament_switcher_unsupported'),
    ('has_filament_switcher', 'unknown', 'filament_switcher_unsupported'),
    ('enable_filament_dynamic_map', '1', 'dynamic_filament_map_unsupported'),
])
def test_unsupported_layouts_refuse(tmp_path, key, value, reason):
    payload = fixture(project=lambda s: s.update({key: value}))
    if key == 'printer_model':
        with pytest.raises(ValueError, match=reason):
            inspect_ams_sources(payload)
    else:
        refused(tmp_path, payload, reason)


@pytest.mark.parametrize('value', ['1', 'true', '', 'unknown'])
def test_plate_dynamic_mapping_refuses(tmp_path, value):
    refused(tmp_path, fixture(xml=lambda p: ET.SubElement(p, 'metadata', key='enable_filament_dynamic_map', value=value)),
            'dynamic_filament_map_unsupported')


@pytest.mark.parametrize('change,reason', [
    (lambda p: p.find('filament').set('group_id', '0,1'), 'slice_filament_group_unsupported'),
    (lambda p: p.find('filament').set('group_id', '2'), 'slice_filament_group_unsupported'),
    (lambda p: p.find('filament').set('group_id', '0'), 'slice_filament_group_disagrees'),
    (lambda p: p.find('filament').attrib.pop('group_id'), 'slice_filament_group_missing'),
    (lambda p: p.remove(p.find("nozzle[@id='1']")), 'slice_filament_nozzle_missing'),
    (lambda p: ET.SubElement(p, 'nozzle', id='0', extruder_id='1'), 'slice_nozzle_duplicate'),
    (lambda p: p.find('nozzle').set('extruder_id', '2'), 'slice_nozzle_extruder_disagrees'),
    (lambda p: p.find('nozzle').set('id', '7'), 'slice_nozzle_layout_unverified'),
])
def test_slice_cross_checks_refuse_without_guessing_gui_shape(tmp_path, change, reason):
    refused(tmp_path, fixture(xml=change), reason)


@pytest.mark.parametrize('key,bad', [('filament_map', '1,1'), ('filament_map', '2'),
                                    ('filament_nozzle_map', '0,0'), ('filament_nozzle_map', '')])
def test_embedded_maps_must_agree(tmp_path, key, bad):
    import re
    refused(tmp_path, fixture(code=lambda c: re.sub(r'; ' + key + r' = [^\n]*', '; ' + key + ' = ' + bad, c)),
            'gcode_' + key + '_disagrees, line')


@pytest.mark.parametrize('key', ['filament_map', 'filament_nozzle_map'])
def test_duplicate_embedded_maps_refuse(tmp_path, key):
    refused(tmp_path, fixture(code=lambda c: c.replace('; CONFIG_BLOCK_END', '; ' + key + ' = 2,1\n; CONFIG_BLOCK_END')), 'gcode_' + key + '_duplicate, line')


@pytest.mark.parametrize('command,reason', [
    ('M620.11 P0 I0 B0 E0', 'gcode_B_disagrees'),
    ('M620.11 K0 I1 B1 R0', 'gcode_B_disagrees'),
    ('M620.11 S0 L0 I1 B1 E0 F200', 'gcode_B_disagrees'),
    ('M620.11 P0 I2 B0 E0', 'gcode_source_not_on_plate'),
    ('M620.11 P0 I0 B2 E0', 'gcode_B_disagrees'),
    ('M620.11 P0 I0 B-2 E0', 'gcode_B_disagrees'),
    ('M620.11 P0 I0 B-1 B0 E0', 'gcode_B_disagrees'),
    ('M620 S0A B0', 'gcode_B_disagrees'),
    ('M620 S0A B1', 'unsupported_M620_M621_command'),
    ('M620 S0A H0', 'dynamic_hotend_unsupported'),
    ('T0 H0', 'dynamic_hotend_unsupported'),
    ('M620.6 I0 H0 W1', 'dynamic_hotend_unsupported'),
    ('M620.17 T1 S200 L0', 'gcode_M620.17_disagrees'),
    ('T1001 O0', 'unsupported_T1001_O0'),
    ('M620.11 P0 I0 E0', 'an M620.11 tool change has no B'),
    ('M620.11 S1 I0 E0 F200', 'an M620.11 tool change has no B'),
])
def test_command_cross_checks_and_unknown_shapes_refuse_with_line(tmp_path, command, reason):
    payload = fixture(code=lambda c: c + command + ' ; PRIVATE_FILE_CONTENT\n')
    with pytest.raises(ValueError, match=reason) as error:
        inspect_ams_sources(payload)
    assert 'line ' in str(error.value) and 'PRIVATE' not in str(error.value)
    assert facts(tmp_path, payload)['mapping_refusal'] is None


def test_single_source_h2d_cannot_bypass_metadata_proof(tmp_path):
    payload = fixture(used=(0,), tower=True, project=lambda s: s.update(physical_extruder_map=[1, 1]))
    with pytest.raises(ValueError, match='physical_extruder_map_invalid'):
        inspect_single_source_ams(payload)
    refused(tmp_path, payload, 'physical_extruder_map_invalid')


def test_unused_entries_do_not_override_used_filament_evidence(tmp_path):
    payload = fixture(used=(1,), tower=True, code=lambda c: c.replace('; filament_map = 2,1', '; filament_map = 99,1'))
    accepted(tmp_path, payload, {1: 1})


def test_missing_optional_gcode_maps_do_not_invent_a_project_fallback(tmp_path):
    import re
    strip = lambda c: re.sub(r'; filament_(?:nozzle_)?map = [^\n]*\n|M620\.17[^\n]*\n', '', c)
    # group_id alone is independent slice evidence, even without nozzle rows.
    accepted(tmp_path, fixture(rows=False, tower=True, code=strip), {0: 0, 1: 1})
    refused(tmp_path, fixture(rows=False, tower=True, code=strip,
        xml=lambda p: [f.attrib.pop('group_id') for f in p.findall('filament')]),
        'source_mapping_cross_check_missing')


def test_capability_refuses_before_printer_read(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from modules.printers import dispatch_acquisition
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: pytest.fail('printer read'))
    path = tmp_path / 'bad.3mf'; path.write_bytes(fixture(code=lambda c: c.replace('; physical_extruder_map = 1,0', '; physical_extruder_map = 0,1')))
    gate = create_capability_gate(job={'id': 1, 'printer_id': 1}, creds={'model': 'H2D'}, staged_path=path,
                                  plate_number=1, target_handle=SimpleNamespace(held=lambda: True))
    assert gate.prepare() is False
    assert gate.refusal.code == 'source_mapping_unverified'
    assert gate.refusal.detail == 'gcode_physical_extruder_map_disagrees'


@pytest.mark.parametrize('value', [[], None, [1], [0, 1]])
def test_declared_nozzle_counts_must_prove_one_each(tmp_path, value):
    refused(tmp_path, fixture(project=lambda s: s.update(extruder_max_nozzle_count=value)), 'one_nozzle_per_extruder_unverified')


def test_missing_counts_require_identified_h2d(tmp_path):
    def unidentified(s):
        s.pop('printer_model'); s.pop('extruder_max_nozzle_count')
    refused(tmp_path, fixture(project=unidentified), 'one_nozzle_per_extruder_unverified')
    accepted(tmp_path, fixture(project=lambda s: s.pop('extruder_max_nozzle_count')), {0: 0, 1: 1})


@pytest.mark.parametrize('where', ['physical', 'plate', 'gcode'])
def test_empty_comma_entries_cannot_shift_indices(tmp_path, where):
    options = {'physical': {'project': lambda s: s.update(physical_extruder_map='1,,0')},
               'plate': {'xml': lambda p: p.find("metadata[@key='filament_maps']").set('value', '2,,1')},
               'gcode': {'code': lambda c: c.replace('; filament_map = 2,1', '; filament_map = 2,,1')}}
    reason = {'physical': 'physical_extruder_map_invalid', 'plate': 'plate_filament_map_out_of_range',
              'gcode': 'gcode_filament_map_disagrees'}[where]
    refused(tmp_path, fixture(**options[where]), reason)


def test_unseen_nested_gui_nozzle_rows_refuse(tmp_path):
    def nested(p):
        holder = ET.SubElement(p, 'nozzles')
        for row in p.findall('nozzle'):
            p.remove(row); holder.append(row)
    refused(tmp_path, fixture(xml=nested), 'slice_nozzle_structure_unsupported')


def test_malformed_embedded_map_is_not_treated_as_absent(tmp_path):
    refused(tmp_path, fixture(code=lambda c: c.replace('; filament_map =', '; filament_map :')), 'gcode_filament_map_malformed')


def test_h2d_physical_order_is_required_without_m620_17(tmp_path):
    import re
    for physical, expected in [((1, 0), {0: 0, 1: 1}), ((0, 1), {0: 1, 1: 0})]:
        payload = fixture(physical=physical, code=lambda c: re.sub(r'M620\.17[^\n]*\n', '', c))
        if physical == (1, 0):
            accepted(tmp_path, payload, expected)
        else:
            refused(tmp_path, payload, 'h2d_physical_extruder_map_unsupported')


def test_nozzle_properties_follow_logical_extruder(tmp_path):
    for physical in [(1, 0)]:
        requirements = facts(tmp_path, fixture(physical=physical))['requirements']
        assert [(r['extruder'], r['diameter'], r['flow']) for r in requirements] == [
            (physical[1], .6, 'High Flow'), (physical[0], .4, 'Standard')]


def test_capability_rejects_case_colliding_metadata(tmp_path):
    payload = io.BytesIO(fixture())
    with zipfile.ZipFile(payload, 'a') as z:
        z.writestr('metadata/project_settings.config', json.dumps({
            'printer_model': 'H2D', 'physical_extruder_map': [0, 1]}))
    refused(tmp_path, payload.getvalue(), 'archive_member_case_collision')


def test_nested_plate_cannot_shadow_verified_plate(tmp_path):
    original = fixture(); result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(result, 'w') as target:
        for name in source.namelist():
            data = source.read(name)
            if name.endswith('slice_info.config'):
                data = data.replace(b'<config>', b'<config><nested><plate><metadata key="index" value="1"/></plate></nested>')
            target.writestr(name, data)
    refused(tmp_path, result.getvalue(), 'slice_plate_structure_unsupported')


@pytest.mark.parametrize('key,value', [('extruder_nozzle_count', [1, 6]), ('extruder_nozzle_count', []),
                                     ('extruder_nozzle_slot', [1, 0]), ('extruder_nozzle_slot', [0, 2]),
                                     ('extruder_nozzle_slot', [False, 1])])
def test_installed_slot_layout_cannot_override_static_h2d_proof(tmp_path, key, value):
    refused(tmp_path, fixture(project=lambda s: s.update({key: value})), 'installed_nozzle_layout_unsupported')


def test_canonical_installed_slot_layout_is_supported(tmp_path):
    accepted(tmp_path, fixture(project=lambda s: s.update(extruder_nozzle_count=['1', '1'], extruder_nozzle_slot=['0', '1'])),
             {0: 0, 1: 1})


def test_single_source_m620_17_must_name_the_proven_physical_side(tmp_path):
    payload = fixture(used=(1,), tower=True, code=lambda c: c + 'M620.17 T0 S200 L1\n')
    assert facts(tmp_path, payload)['mapping_refusal'] is None
    with pytest.raises(ValueError, match='gcode_M620.17_disagrees'):
        inspect_single_source_ams(payload)


@pytest.mark.parametrize('group', ['0', '0,1'])
def test_group_id_is_checked_even_without_nozzle_rows(tmp_path, group):
    refused(tmp_path, fixture(rows=False, xml=lambda p: p.find('filament').set('group_id', group)),
            'slice_filament_group_' + ('disagrees' if group == '0' else 'unsupported'))


@pytest.mark.parametrize('change,reason', [
    (lambda c: c.replace('; physical_extruder_map = 1,0\n', ''), 'gcode_physical_extruder_map_missing'),
    (lambda c: c.replace('; physical_extruder_map = 1,0', '; physical_extruder_map = 0,1'), 'gcode_physical_extruder_map_disagrees'),
    (lambda c: c.replace('; physical_extruder_map = 1,0', '; physical_extruder_map = 1'), 'gcode_physical_extruder_map_disagrees'),
    (lambda c: c.replace('; CONFIG_BLOCK_END', '; physical_extruder_map = 1,0\n; CONFIG_BLOCK_END'), 'gcode_physical_extruder_map_duplicate'),
])
def test_physical_settings_block_is_required_and_cross_checked(tmp_path, change, reason):
    refused(tmp_path, fixture(code=change), reason)


@pytest.mark.parametrize('model', ['Bambu Lab H2D', 'Bambu Lab H2D Pro'])
def test_known_h2d_reversed_physical_map_refuses_even_when_gcode_agrees(tmp_path, model):
    refused(tmp_path, fixture(physical=(0, 1), project=lambda s: s.update(printer_model=model)),
            'h2d_physical_extruder_map_unsupported')


def test_b_minus_one_with_unknown_source_names_source_refusal(tmp_path):
    payload = fixture(code=lambda c: c + 'M620.11 P0 I5 B-1 E0\n')
    with pytest.raises(ValueError, match='gcode_source_not_on_plate, line'):
        inspect_ams_sources(payload)
    assert facts(tmp_path, payload)['mapping_refusal'] is None


@pytest.mark.parametrize('plate_number', [None, '1', True, 0, -1, 1.0])
def test_plate_number_is_explicitly_a_positive_integer(tmp_path, plate_number):
    path = tmp_path / 'test.3mf'; path.write_bytes(fixture())
    with pytest.raises(ValueError, match='^plate_number_invalid$'):
        inspect_payload(path, plate_number, 'H2D')


@pytest.mark.parametrize('name', ['Metadata/plate_1.json', 'metadata/plate_1.json'])
def test_plate_json_cannot_remove_verified_nozzle_requirements(tmp_path, name):
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(fixture())) as src, zipfile.ZipFile(result, 'w') as dst:
        for entry in src.namelist():
            dst.writestr(name if entry.endswith('plate_1.json') else entry,
                         json.dumps({'filament_ids': [0]}) if entry.endswith('plate_1.json') else src.read(entry))
    cap = facts(tmp_path, result.getvalue())
    assert cap['mapping_refusal'] is None
    assert cap['filament_map'] == {0: 0, 1: 1}
    assert [r['extruder'] for r in cap['requirements']] == [0, 1]


def test_gcode_filament_map_alone_is_an_independent_record(tmp_path):
    accepted(tmp_path, fixture(rows=False, tower=True,
        xml=lambda p: [f.attrib.pop('group_id') for f in p.findall('filament')]), {0: 0, 1: 1})
