import io
import zipfile
from pathlib import Path
import pytest
from core.ams_source_inspection import inspect_single_source_ams


def archive(xml=None, code=None, settings=None, duplicate=False):
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w') as z:
        z.writestr('Metadata/slice_info.config', xml or '<config><plate><metadata key="index" value="1"/><filament id="1" type="PETG"/></plate></config>')
        z.writestr('Metadata/project_settings.config',settings or '{"filament_type":["PETG"]}')
        z.writestr('Metadata/plate_1.gcode', code if code is not None else '; filament_type = PETG\nM620 M\nM620 S0A ; select\nM621 S0A\nT1000\nT255\nM620 S255\nM621 S255\nM620.3 W1\n')
        if duplicate:z.writestr('Metadata/plate_1.gcode','duplicate')
    return result.getvalue()


def test_genuine_petg_coupon_source_is_zero_not_reserved_t_commands():
    path=Path(__file__).resolve().parents[2]/'tests/fixtures/telemetry/a1-petg-source-sanitized.3mf'
    result=inspect_single_source_ams(path.read_bytes())
    assert (result.plate_index,result.source_index,result.material)==(0,0,'PETG')


def test_known_synthetic_source_and_sentinels():
    assert inspect_single_source_ams(archive()).source_index==0


def test_genuine_x1c_pla_coupon_calibration_preserves_source_zero():
    path=Path(__file__).resolve().parents[2]/'tests/fixtures/telemetry/x1c-pla-source-sanitized.3mf'
    result=inspect_single_source_ams(path.read_bytes())
    assert (result.plate_index,result.source_index,result.material)==(0,0,'PLA')


@pytest.mark.parametrize('command',['T1101','T11000','T1100.0','T1100 S0','T1100 T0'])
def test_calibration_exception_does_not_allow_other_tool_commands(command):
    with pytest.raises(ValueError,match='^Unverifiable source mapping$'):
        inspect_single_source_ams(archive(code='; filament_type = PETG\nM620 S0A\nM621 S0A\n'+command+'\n'))


def test_calibration_command_does_not_override_conflicting_source():
    with pytest.raises(ValueError,match='^Unverifiable source mapping$'):
        inspect_single_source_ams(archive(code='; filament_type = PETG\nM620 S1A\nM621 S1A\nT1100\n'))


@pytest.mark.parametrize('code',[
    '; filament_type = PETG\n; M620 S0A\nM621 S0A',
    '; filament_type = PETG\nM620 S1A\nM621 S1A',
    '; filament_type = PETG\nM620 S0A\nM621 S1A',
    '; filament_type = PETG;PLA\nM620 S0A\nM621 S0A',
    '; filament_type = PETG\nM620 S0\nM621 S0A',
    '; filament_type = PETG\nM620.2 S0A\nM621 S0A',
    '; filament_type = PETG\nprefixM620 S0A\nM621 S0A',
    '; filament_type = PETG\nM620 S0A\nM621 S0A\nT1',
    '; filament_type = PETG\nM620 S0A\nM621 S0A\nT999',
    '',
])
def test_ambiguous_or_unsupported_commands_refuse(code):
    with pytest.raises(ValueError,match='^Unverifiable source mapping$'):
        inspect_single_source_ams(archive(code=code))


@pytest.mark.parametrize('xml',[
    '<config><plate><metadata key="index" value="1"/><filament id="2" type="PETG"/></plate></config>',
    '<config><plate><metadata key="index" value="1"/><filament id="1" type="PLA"/></plate></config>',
    '<config><plate><metadata key="index" value="1"/><filament id="1" type="PETG"/><filament id="1" type="PETG"/></plate></config>',
    '<config><plate/><plate/></config>', '<bad',
    '<!DOCTYPE config [<!ENTITY x "PETG">]><config>&x;</config>',
])
def test_ambiguous_metadata_refuses(xml):
    with pytest.raises(ValueError):inspect_single_source_ams(archive(xml=xml))


def test_duplicate_member_refuses():
    with pytest.raises(ValueError):inspect_single_source_ams(archive(duplicate=True))


def test_project_conflict_refuses():
    with pytest.raises(ValueError):inspect_single_source_ams(archive(settings='{"filament_type":["PLA"]}'))


def test_bounded_member_refuses(monkeypatch):
    import core.ams_source_inspection as module
    monkeypatch.setattr(module,'MAX_GCODE_BYTES',8)
    with pytest.raises(ValueError):inspect_single_source_ams(archive())


def test_additional_gcode_plate_refuses():
    raw=io.BytesIO(archive())
    with zipfile.ZipFile(raw,'a') as z:z.writestr('Metadata/plate_2.gcode','unused')
    with pytest.raises(ValueError):inspect_single_source_ams(raw.getvalue())


@pytest.mark.parametrize('material',['PLA','PETG','ABS','PETG-CF'])
def test_single_source_requires_three_agreeing_recognized_material_facts(material):
    import json
    payload=archive(xml=f'<config><plate><metadata key="index" value="1"/><filament id="1" type="{material}"/></plate></config>',
        settings=json.dumps({'filament_type':[material]}),
        code=f'; filament_type = {material}\nM620 S0A\nM621 S0A\nT0\n')
    assert inspect_single_source_ams(payload).material==material


def test_unused_saved_material_profiles_do_not_create_used_sources():
    result = inspect_single_source_ams(archive(settings='{"filament_type":["PETG","PLA","PLA"]}',
        code='; filament_type = PETG;PLA;PLA\nM620 S0A\nM621 S0A\nT0\n'))
    assert (result.source_index, result.material) == (0, 'PETG')


@pytest.mark.parametrize('settings',[
    '{"filament_type":[]}',
    '{"filament_type":["PLA","PETG"]}',
    '{"filament_type":"PETG"}',
    '{"filament_type":' + str(['PETG'] * 17).replace("'", '"') + '}',
])
def test_extra_profiles_do_not_weaken_source_zero_material_proof(settings):
    with pytest.raises(ValueError):
        inspect_single_source_ams(archive(settings=settings))


def test_extra_profiles_do_not_allow_second_source_commands():
    with pytest.raises(ValueError):
        inspect_single_source_ams(archive(settings='{"filament_type":["PETG","PLA"]}',
            code='; filament_type = PETG;PLA\nM620 S0A\nM621 S0A\nM620 S1A\nM621 S1A\n'))


def test_unused_header_profiles_must_agree_with_saved_profiles():
    with pytest.raises(ValueError):
        inspect_single_source_ams(archive(settings='{"filament_type":["PETG","PLA"]}',
            code='; filament_type = PETG;ABS\nM620 S0A\nM621 S0A\nT0\n'))


def test_compressed_many_short_lines_does_not_materialize_a_line_list():
    import tracemalloc
    source = archive(code='; filament_type = PETG\nM620 S0A\nM621 S0A\n' + ';\n' * 500000)
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED) as compressed:
        for name in original.namelist():
            compressed.writestr(name, original.read(name))
    payload = result.getvalue()
    tracemalloc.start()
    try:
        assert inspect_single_source_ams(payload).material == 'PETG'
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


def test_oversized_gcode_line_refuses(monkeypatch):
    import core.ams_source_inspection as module
    monkeypatch.setattr(module, 'MAX_GCODE_LINE_BYTES', 128)
    with pytest.raises(ValueError):
        inspect_single_source_ams(archive(code=';' + 'a' * 129))


@pytest.mark.parametrize('separator', ['\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'])
@pytest.mark.parametrize('hidden', ['T1', 'M620 S1A'])
def test_non_lf_separator_cannot_hide_source_command_in_comment(separator, hidden):
    with pytest.raises(ValueError):
        inspect_single_source_ams(archive(code='; filament_type = PETG\nM620 S0A\nM621 S0A\n; comment' + separator + hidden + '\n'))


def test_normal_crlf_source_remains_supported():
    assert inspect_single_source_ams(archive(code='; filament_type = PETG\r\nM620 S0A\r\nM621 S0A\r\n')).source_index == 0
