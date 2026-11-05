"""Round-3 review reproducers. All archives are synthetic; no hardware or network."""
import dataclasses
import io
import json
from pathlib import Path
import re
import struct
import types
import zipfile
import zlib
from xml.etree import ElementTree as ET

import pytest

from core.ams_source_inspection import MAX_GCODE_LINE_BYTES, inspect_ams_sources, inspect_single_source_ams
from modules.printers.dispatch_capability_gate import static_refusal, create_capability_gate
from modules.printers.dispatch_capability_payload import inspect_payload
from tests.test_contracts.test_h2d_source_mapping import fixture

BLOCK = '; CONFIG_BLOCK_START\n; physical_extruder_map = 1,0\n; filament_map = 2,1\n; filament_nozzle_map = 1,0\n; CONFIG_BLOCK_END\n'
BAD = BLOCK.replace('filament_map = 2,1', 'filament_map = 1,2')


def with_code(code):
    return fixture(code=lambda c: code(re.sub(r'; CONFIG_BLOCK_START.*?; CONFIG_BLOCK_END\n', '', c, flags=re.S)))


def assert_cap_refusal(tmp_path, payload, reason, model='H2D'):
    path = tmp_path / 'synthetic.3mf'
    path.write_bytes(payload)
    facts = inspect_payload(path, 1, model)
    assert facts['mapping_refusal'] == reason
    assert facts['filament_map'] == {}
    assert facts['requirements'] == []
    refusal = static_refusal(path, 1, {'model': model})
    assert (refusal.code, refusal.detail) == ('source_mapping_unverified', reason)
    return path


@pytest.mark.parametrize('prefix', ['', '\u00a0', '\u2003', '\t'])
@pytest.mark.parametrize('nested', [False, True])
def test_second_start_is_refused_even_with_no_conflicting_keys(tmp_path, prefix, nested):
    first = BLOCK.replace('; CONFIG_BLOCK_END\n', '') if nested else BLOCK
    payload = with_code(lambda body: first + body + prefix + '; CONFIG_BLOCK_START\n; CONFIG_BLOCK_END\n')
    assert_cap_refusal(tmp_path, payload, 'gcode_settings_block_duplicate')


def test_disagreeing_second_block_in_other_window_is_not_ignored(tmp_path):
    payload = with_code(lambda body: BLOCK + body + 'G1 X1\n' * 800000 + BAD)
    assert_cap_refusal(tmp_path, payload, 'gcode_settings_block_duplicate')


@pytest.mark.parametrize('where', ['inside', 'before', 'after', 'fake_start', 'tail_boundary'])
def test_long_lines_cannot_supply_fake_settings(tmp_path, where):
    huge = ';' + 'x' * MAX_GCODE_LINE_BYTES
    def code(body):
        if where == 'inside':
            return body + BLOCK.replace('; CONFIG_BLOCK_END', huge + '\n; CONFIG_BLOCK_END')
        if where == 'before':
            return huge + '\n' + BLOCK + body
        if where == 'after':
            return BLOCK + body + huge + '\n'
        if where == 'fake_start':
            return huge + BLOCK + body + BAD
        # A long line crossing the beginning of the tail window.
        return body + 'G1 X1\n' * 600000 + huge * 3 + '\n' + 'G1 X1\n' * 330000 + BLOCK
    payload = with_code(code)
    assert_cap_refusal(tmp_path, payload, 'gcode_line_too_long')
    with pytest.raises(ValueError):
        inspect_ams_sources(payload)


@pytest.mark.parametrize('length', [MAX_GCODE_LINE_BYTES - 1, MAX_GCODE_LINE_BYTES, MAX_GCODE_LINE_BYTES + 1])
def test_exact_line_bound(tmp_path, length):
    payload = with_code(lambda body: body + BLOCK.replace('; CONFIG_BLOCK_END', ';' + 'x' * (length - 2) + '\n; CONFIG_BLOCK_END'))
    if length > MAX_GCODE_LINE_BYTES:
        assert_cap_refusal(tmp_path, payload, 'gcode_line_too_long')
    else:
        path = tmp_path / 'exact.3mf'; path.write_bytes(payload)
        assert inspect_payload(path, 1, 'H2D')['mapping_refusal'] is None


@pytest.mark.parametrize('position', ['front', 'end'])
def test_incomplete_block_has_specific_reason(tmp_path, position):
    partial = BLOCK.replace('; CONFIG_BLOCK_END\n', '')
    payload = with_code(lambda body: partial + body if position == 'front' else body + partial)
    assert_cap_refusal(tmp_path, payload, 'gcode_settings_block_incomplete')


@pytest.mark.parametrize('model', ['H2D', 'H2D Pro'])
def test_target_model_enforces_physical_order_without_project_model(tmp_path, model):
    payload = fixture(physical=(0, 1), project=lambda s: s.pop('printer_model'))
    assert_cap_refusal(tmp_path, payload, 'h2d_physical_extruder_map_unsupported', model)


@pytest.mark.parametrize('indexes', [[], ['1', '1'], ['2', '1']])
def test_selected_plate_requires_one_exact_index(tmp_path, indexes):
    def change(plate):
        plate.set('index', '1')  # Still selects this plate if metadata check is removed.
        plate.remove(plate.find("metadata[@key='index']"))
        for index in indexes:
            ET.SubElement(plate, 'metadata', key='index', value=index)
    assert_cap_refusal(tmp_path, fixture(xml=change), 'slice_plate_index_invalid')


@pytest.mark.parametrize('padding', [0, 240000, 800000])
def test_capability_omits_location_and_ams_reports_true_file_line(tmp_path, padding):
    code = with_code(lambda body: body + 'G1 X1 Y1 E0.1\n' * padding + BAD)
    with zipfile.ZipFile(io.BytesIO(code)) as package:
        lines = package.read('Metadata/plate_1.gcode').decode().splitlines()
    expected_line = lines.index('; filament_map = 1,2') + 1
    with pytest.raises(ValueError) as error:
        inspect_ams_sources(code)
    assert str(error.value) == f'gcode_filament_map_disagrees, line {expected_line}'
    # Exact diagnostic: do not strip a line suffix from either result.
    assert_cap_refusal(tmp_path, code, 'gcode_filament_map_disagrees')


def damaged_member(kind):
    payload = fixture()
    if kind == 'stored_crc':
        data = bytearray(payload)
        with zipfile.ZipFile(io.BytesIO(data)) as package:
            member = package.getinfo('Metadata/plate_1.gcode')
        start = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
        data[start + 5] ^= 1
        return bytes(data)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(payload)) as src, zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
    data = bytearray(out.getvalue())
    with zipfile.ZipFile(io.BytesIO(data)) as package:
        member = package.getinfo('Metadata/plate_1.gcode')
        central_start = package.start_dir
    start = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    if kind == 'deflate_invalid':
        data[start] = 7  # Reserved deflate block type: zlib.error.
        return bytes(data)
    # Last member: remove all but eight compressed bytes, preserving a valid
    # ZIP directory and the original CRC/uncompressed length. The deflate
    # member itself is truncated, rather than truncating the archive footer.
    kept = 8
    removed = member.compress_size - kept
    del data[start + kept:start + member.compress_size]
    struct.pack_into('<I', data, member.header_offset + 18, kept)
    central = central_start - removed
    cursor = central
    while data[cursor:cursor + 4] == b'PK\x01\x02':
        name_len, extra_len, comment_len = struct.unpack_from('<HHH', data, cursor + 28)
        name = data[cursor + 46:cursor + 46 + name_len].decode()
        if name == member.filename:
            struct.pack_into('<I', data, cursor + 20, kept)
        cursor += 46 + name_len + extra_len + comment_len
    struct.pack_into('<I', data, cursor + 16, central)
    return bytes(data)


@pytest.mark.parametrize('kind,exception', [('stored_crc', zipfile.BadZipFile),
    ('deflate_truncated', zipfile.BadZipFile), ('deflate_invalid', zlib.error)])
def test_corrupt_gcode_refuses_before_claim_and_printer_read(tmp_path, monkeypatch, kind, exception):
    from modules.printers import dispatch_acquisition
    monkeypatch.setattr(dispatch_acquisition, 'bounded_bambu_report', lambda *a: pytest.fail('printer read'))
    payload = damaged_member(kind)
    with zipfile.ZipFile(io.BytesIO(payload)) as package, pytest.raises(exception):
        package.read('Metadata/plate_1.gcode')
    path = assert_cap_refusal(tmp_path, payload, 'gcode_unreadable')
    gate = create_capability_gate(job={'id': 1, 'printer_id': 1}, creds={'model': 'H2D'}, staged_path=path,
        plate_number=1, target_handle=types.SimpleNamespace(held=lambda: True))
    assert gate.prepare() is False
    assert (gate.refusal.code, gate.refusal.detail) == ('source_mapping_unverified', 'gcode_unreadable')


def test_eof_during_settings_read_is_named(tmp_path, monkeypatch):
    original = zipfile.ZipExtFile.read
    def eof(stream, *args, **kwargs):
        if stream.name.endswith('.gcode'):
            raise EOFError()
        return original(stream, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', eof)
    assert_cap_refusal(tmp_path, fixture(), 'gcode_unreadable')


@pytest.mark.parametrize('error', [NotImplementedError('compression method 99'), RuntimeError('encrypted member')])
def test_unsupported_or_encrypted_gcode_member_is_named(tmp_path, monkeypatch, error):
    original = zipfile.ZipExtFile.read
    def fail(stream, *args, **kwargs):
        if stream.name.endswith('.gcode'):
            raise error
        return original(stream, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', fail)
    assert_cap_refusal(tmp_path, fixture(), 'gcode_unreadable')


# Results of the pre-change code (d6bdb8bc), recorded once so the test needs no git history.
HEAD_RESULTS = {'bad_physical|False': {'plate_index': 0,
                        'sources': [{'extruder': None,
                                     'material': 'PLA',
                                     'source_index': 0,
                                     'used_grams': 1.0},
                                    {'extruder': 1,
                                     'material': 'PLA',
                                     'source_index': 1,
                                     'used_grams': 1.0}]},
 'bad_physical|True': 'AMS read and approval require a single-source file; multi-source files are '
                      'unsupported',
 'comma_empty|False': {'plate_index': 0,
                       'sources': [{'extruder': 0, 'material': 'PLA', 'source_index': 0, 'used_grams': 1.0},
                                   {'extruder': 1, 'material': 'PLA', 'source_index': 1, 'used_grams': 1.0}]},
 'comma_empty|True': 'AMS read and approval require a single-source file; multi-source files are unsupported',
 'conflict|False': "the project's filament map disagrees with the plate's",
 'conflict|True': 'AMS read and approval require a single-source file; multi-source files are unsupported',
 'multi_cross|False': {'plate_index': 0,
                       'sources': [{'extruder': 0, 'material': 'PLA', 'source_index': 0, 'used_grams': 1.0},
                                   {'extruder': 1, 'material': 'PLA', 'source_index': 1, 'used_grams': 1.0}]},
 'multi_cross|True': 'AMS read and approval require a single-source file; multi-source files are unsupported',
 'multi_tower|False': {'plate_index': 0,
                       'sources': [{'extruder': 0, 'material': 'PLA', 'source_index': 0, 'used_grams': 1.0},
                                   {'extruder': 1, 'material': 'PLA', 'source_index': 1, 'used_grams': 1.0}]},
 'multi_tower|True': 'AMS read and approval require a single-source file; multi-source files are unsupported',
 'nozzle_conflict|False': "the slicer's extruder maps disagree",
 'nozzle_conflict|True': 'AMS read and approval require a single-source file; multi-source files are '
                         'unsupported',
 'single|False': {'plate_index': 0,
                  'sources': [{'extruder': 1, 'material': 'PLA', 'source_index': 0, 'used_grams': 1.0}]},
 'single|True': {'material': 'PLA', 'plate_index': 0, 'source_index': 0},
 'zero_defaults|False': {'plate_index': 0,
                         'sources': [{'extruder': None,
                                      'material': 'PLA',
                                      'source_index': 0,
                                      'used_grams': 1.0},
                                     {'extruder': None,
                                      'material': 'PLA',
                                      'source_index': 1,
                                      'used_grams': 1.0}]},
 'zero_defaults|True': 'AMS read and approval require a single-source file; multi-source files are '
                       'unsupported'}


@pytest.mark.parametrize('single', [True, False])
@pytest.mark.parametrize('shape', ['single', 'multi_cross', 'multi_tower', 'zero_defaults', 'conflict', 'nozzle_conflict', 'bad_physical', 'comma_empty'])
def test_h2c_ams_matches_actual_head(single, shape):
    mapping = (1,) if shape == 'single' else (2, 1)
    def project(settings):
        settings.update(printer_model='Bambu Lab H2C', extruder_max_nozzle_count=[1, 6],
                        filament_map=list(mapping), filament_nozzle_map=list(mapping))
        if shape == 'zero_defaults': settings.update(filament_map=[1, 1], filament_nozzle_map=[0, 0])
        if shape == 'conflict': settings['filament_map'] = [1, 1]
        if shape == 'nozzle_conflict': settings['filament_nozzle_map'] = [1, 2]
        if shape == 'bad_physical': settings['physical_extruder_map'] = [1, 6]
        if shape == 'comma_empty': settings['filament_map'] = '2,,1'
    payload = fixture(mapping=mapping, tower=shape != 'multi_cross', project=project)
    def result(fn):
        try:
            return dataclasses.asdict(fn(payload))
        except ValueError as error:
            return str(error)
    fn = 'inspect_single_source_ams' if single else 'inspect_ams_sources'
    actual = result(inspect_single_source_ams if single else inspect_ams_sources)
    # Recorded via JSON, so tuples are lists there.
    assert json.loads(json.dumps(actual)) == HEAD_RESULTS[f'{shape}|{single}']
    if shape == 'single' and single:
        assert actual == {'plate_index': 0, 'source_index': 0, 'material': 'PLA'}
    if shape.startswith('multi_') and not single:
        assert [s['extruder'] for s in actual['sources']] == [0, 1]
