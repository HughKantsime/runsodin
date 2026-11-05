"""Capability admission must retain HEAD d6bdb8bc's non-AMS scope.

The expected facts below were also compared to that revision's actual reader.
All payloads are synthetic; none are derived from private production files.
"""
import io
import json
import zipfile
from xml.etree import ElementTree as ET

import pytest
from modules.printers.dispatch_capability_payload import inspect_payload
from modules.printers.dispatch_capability_gate import static_refusal
from tests.test_contracts.test_h2d_source_mapping import fixture

SHAPES = ['h2c_single', 'multi_plate_1', 'multi_plate_2', 'unknown_command',
          'archive_over_64_mib', 'gcode_over_96_mib', 'plate_7', 'h2d_pro']


def scope_payload(tmp_path, shape):
    model = 'H2C' if shape == 'h2c_single' else 'H2D Pro' if shape == 'h2d_pro' else 'H2D'
    plate_number = 2 if shape == 'multi_plate_2' else 7 if shape == 'plate_7' else 1
    def project(settings):
        settings.update(printer_model='Bambu Lab ' + model, filament_map=[1], filament_nozzle_map=[1])
        if model == 'H2C':
            settings['extruder_max_nozzle_count'] = [1, 6]
    payload = fixture(used=(0,), mapping=(1,), tower=True, project=project)
    path = tmp_path / (shape + '.3mf')
    with zipfile.ZipFile(io.BytesIO(payload)) as src, zipfile.ZipFile(path, 'w') as dst:
        for name in src.namelist():
            data = src.read(name)
            if name.endswith('.gcode'):
                # Studio's current layout: settings block at the front.
                body, block = data.split(b'; CONFIG_BLOCK_START', 1)
                data = b'; CONFIG_BLOCK_START' + block + body
                if shape in ('unknown_command', 'h2d_pro'):
                    data += b'M620.11 H1\nT1001 O0\n'
            if name.endswith('slice_info.config'):
                root = ET.fromstring(data)
                if shape.startswith('multi_plate'):
                    second = ET.fromstring(ET.tostring(root.find('plate')))
                    second.find("metadata[@key='index']").set('value', '2')
                    root.append(second)
                if shape == 'plate_7':
                    root.find("plate/metadata[@key='index']").set('value', '7')
                data = ET.tostring(root)
            target = name.replace('plate_1.', 'plate_7.') if shape == 'plate_7' else name
            with dst.open(target, 'w') as stream:
                stream.write(data)
                if shape == 'gcode_over_96_mib' and name.endswith('.gcode'):
                    for _ in range(97 * 16):
                        stream.write(b'G1 X1\n' * 10923)
            if shape.startswith('multi_plate') and name.endswith(('.gcode', '.json')):
                dst.writestr(name.replace('plate_1.', 'plate_2.'), data)
        if shape == 'archive_over_64_mib':
            with dst.open('synthetic_padding', 'w') as stream:
                for _ in range(65):
                    stream.write(b'x' * 1024 * 1024)
    return path, plate_number, model


def head_facts(model):
    return {'printer_model': 'Bambu Lab ' + model, 'notes': [], 'requirements': [
        {'extruder': 1, 'side': 'left', 'diameter': .4, 'flow': 'Standard', 'hrc': 0}],
        'incomplete': False, 'bbox': None, 'objects': [], 'filament_map': {0: 1}}


@pytest.mark.parametrize('shape', SHAPES)
def test_capability_preserves_head_non_ams_shapes(tmp_path, monkeypatch, shape):
    import core.ams_source_inspection as ams
    monkeypatch.setattr(ams, 'inspect_ams_sources', lambda *a, **kw: pytest.fail('AMS parser in capability gate'))
    path, plate_number, model = scope_payload(tmp_path, shape)
    if shape == 'archive_over_64_mib':
        assert path.stat().st_size > ams.MAX_ARCHIVE_BYTES
    if shape == 'gcode_over_96_mib':
        with zipfile.ZipFile(path) as z:
            assert z.getinfo('Metadata/plate_1.gcode').file_size > ams.MAX_GCODE_BYTES
    facts = inspect_payload(path, plate_number, model)
    assert facts.pop('mapping_refusal') is None
    assert facts == head_facts(model)
    assert static_refusal(path, plate_number, {'model': model, 'slot_count': 1}) is None


def test_settings_reader_reads_bounded_windows_without_parsing_commands(tmp_path, monkeypatch):
    from core.ams_source_inspection import MAX_GCODE_LINE_BYTES
    path, plate, model = scope_payload(tmp_path, 'gcode_over_96_mib')
    original = zipfile.ZipExtFile.read
    reads = []
    seeking = False
    original_seek = zipfile.ZipExtFile.seek
    def seek(stream, *args, **kwargs):
        nonlocal seeking
        seeking = True
        try:
            return original_seek(stream, *args, **kwargs)
        finally:
            seeking = False
    def bounded_read(stream, n=-1):
        if stream.name.endswith('.gcode') and not seeking:
            reads.append(n)
            assert 0 <= n <= 2 * 1024 * 1024 + 2 * (MAX_GCODE_LINE_BYTES + 1)
        return original(stream, n)
    monkeypatch.setattr(zipfile.ZipExtFile, 'read', bounded_read)
    monkeypatch.setattr(zipfile.ZipExtFile, 'seek', seek)
    assert inspect_payload(path, plate, model)['mapping_refusal'] is None
    assert reads


def test_older_trailing_settings_block_is_supported(tmp_path):
    payload = fixture(code=lambda c: c.replace('; CONFIG_BLOCK_START', 'G1 X1\n' * 400000 + '; CONFIG_BLOCK_START'))
    path = tmp_path / 'trailing.3mf'; path.write_bytes(payload)
    assert inspect_payload(path, 1, 'H2D')['filament_map'] == {0: 0, 1: 1}
