import io
import json
import zipfile
from pathlib import Path
import pytest
from core.ams_source_inspection import inspect_ams_sources, inspect_single_source_ams


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
    with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=0) as compressed:
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


H2D = Path(__file__).parents[1] / 'fixtures/telemetry/h2d-petg-source-sanitized.3mf'


def h2d_archive(*, command=None, replacements=None):
    result = io.BytesIO()
    with zipfile.ZipFile(H2D) as original, zipfile.ZipFile(result, 'w') as package:
        for name in original.namelist():
            content = original.read(name)
            if name == 'Metadata/plate_1.gcode':
                # Sanitization removed the settings block; supply synthetic evidence.
                content += b'; CONFIG_BLOCK_START\n; physical_extruder_map = 1,0\n; CONFIG_BLOCK_END\n'
                if command is not None:
                    content += (command + '\n').encode()
            if replacements and name in replacements:
                old, new = replacements[name]
                content = content.replace(old.encode(), new.encode())
            package.writestr(name, content)
    return result.getvalue()


def test_genuine_h2d_petg_source_is_one():
    with pytest.raises(ValueError, match='gcode_physical_extruder_map_missing'):
        inspect_single_source_ams(H2D.read_bytes())
    result = inspect_single_source_ams(h2d_archive())
    assert (result.plate_index, result.source_index, result.material) == (0, 1, 'PETG')


def h2d_changed_commands():
    """Shape changes: an extra or omitted parameter on every retained command."""
    with zipfile.ZipFile(H2D) as package:
        commands = package.read('Metadata/plate_1.gcode').decode().splitlines()
    for command in dict.fromkeys(commands):
        if command.startswith(';') or command == 'M620 M':
            continue
        yield command + ' X0'
        parts = command.split()
        if len(parts) == 1:
            yield 'T1002' if command in ('T1000', 'T1001') else 'T' + str(int(command[1:]) + 1)
        for index in range(1, len(parts)):
            omitted = ' '.join(parts[:index] + parts[index + 1:])
            if omitted not in ('M620 S1A', 'T1'):
                yield omitted


@pytest.mark.parametrize('command', list(dict.fromkeys(h2d_changed_commands())) + [
    # Selectors and the filament index stay exact; I must be the verified source.
    'M620.10 A2 F498.898 H0.4 T270 P245 S1', 'M620.10 A0 F498.898 H0.4 T270 P245 S2',
    'M620.11 P0 I0 B-1 E0', 'M620.11 P2 I1 B-1 E0', 'M620.11 K0 I2 B-1 R0', 'M620.11 K0 I1 B0 R0',
    'M620.11 S1 L0 I0 B-1 R10 D8 E-10 F623.623', 'M620.11 S1 L2 I1 B-1 R10 D8 E-10 F623.623',
    'M620.6 I0 H-1 W1', 'M620.6 I1 H0 W1', 'M620.6 I1 H-1 W2', 'M620 D0', 'M620 Q2', 'M620.17 T2 S245 L1',
    'M620.17 T0 S245 L2', 'M620.11 K0 I1 B-1 R0 F', 'M620.11 S0 L0 I1 B-1 R10 E-10 F623.623'])
def test_h2d_allowlist_refuses_changed_shape_selector_or_source(command):
    with pytest.raises(ValueError):
        inspect_single_source_ams(h2d_archive(command=command))


@pytest.mark.parametrize('command', [
    # Another material or profile changes feeds, temperatures and lengths only.
    'M620.10 A1 F300 H0.6 T220 P210 S0', 'M620.11 P1 I1 B-1 E-5.5', 'M620.11 K1 I1 B-1 R2',
    'M620.11 S0 L1 I1 B-1 R5 D3 E-8 F400', 'M620.17 T1 S220 L1',
    # Bambu template branches: long retraction with feed, TPU without R/D,
    # and either nozzle with the source or the empty-nozzle fallback.
    'M620.11 K1 I1 B-1 R2 F623.623', 'M620.11 S0 L0 I1 B-1 E-10 F623.623',
    'M620.17 T0 S245 L0', 'M620.17 T1 S245 L1'])
def test_h2d_profile_values_do_not_refuse(command):
    result = inspect_single_source_ams(h2d_archive(command=command))
    assert (result.source_index, result.material) == (1, 'PETG')


@pytest.mark.parametrize('command', ['T0 H-1', 'M620 S0A H-1', 'M621 S0A',
    'T1 H0', 'M621 S1A H-1', 'M620.17 T0 S245 L2', 'M620.17 T1 S245 L3',
    'M620 S65535A', 'M621 S65279A', 'T65535 H-1', 'T65279 H-1'])
def test_h2d_source_conflicts_and_parameterized_sentinels_refuse(command):
    with pytest.raises(ValueError):
        inspect_single_source_ams(h2d_archive(command=command))


@pytest.mark.parametrize('member,old,new', [
    ('Metadata/slice_info.config', 'id="2"', 'id="1"'),
    ('Metadata/slice_info.config', 'type="PETG"', 'type="PLA"'),
    ('Metadata/plate_1.json', '"filament_ids": [1]', '"filament_ids": [0]'),
    ('Metadata/project_settings.config', '"PETG", "PETG"', '"PETG", "PLA"'),
    ('Metadata/plate_1.gcode', '; filament_type = PETG;PETG', '; filament_type = PETG;PLA'),
])
def test_h2d_metadata_conflicts_refuse(member, old, new):
    with pytest.raises(ValueError):
        inspect_single_source_ams(h2d_archive(replacements={member: (old, new)}))


def test_h2d_gcode_size_from_production_fits_bounded_inspection():
    result = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(h2d_archive())) as original, zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=0) as package:
        for name in original.namelist():
            with package.open(name, 'w') as member:
                member.write(original.read(name))
                if name == 'Metadata/plate_1.gcode':
                    # Exceed both the old 64 MiB cap and the 75,041,281-byte evidence.
                    for _ in range(1150):
                        member.write(b';' + b'x' * 65534 + b'\n')
    assert inspect_single_source_ams(result.getvalue()).source_index == 1


P2S = Path(__file__).parents[1] / 'fixtures/telemetry/p2s-pla-source-sanitized.3mf'
P2S_END = '; MACHINE_END_GCODE_START\n'
P2S_PULL_BACK = 'M620 S65535\nT65535\nM621 S65535\n'
P2S_ONLY_LINES = ('M620.11 P0 L0 I0 E0\n', 'M620.11 K0 I0 R0\n', 'M620.6 I0 W1\n')


def p2s_archive(gcode=lambda code: code, model='Bambu Lab P2S', replacements=None):
    """The real P2S lines, changed; model is the one the project settings declare."""
    result = io.BytesIO()
    with zipfile.ZipFile(P2S) as original, zipfile.ZipFile(result, 'w') as package:
        for name in original.namelist():
            content = original.read(name).decode()
            if name == 'Metadata/plate_1.gcode':
                content = gcode(content)
            if name == 'Metadata/project_settings.config' and model != 'Bambu Lab P2S':
                settings = json.loads(content)
                settings.pop('printer_model')
                settings.update({} if model is None else {'printer_model': model})
                content = json.dumps(settings)
            if replacements and name in replacements:
                content = content.replace(*replacements[name])
            package.writestr(name, content)
    return result.getvalue()


def p2s_before_end(*commands):
    return lambda code: code.replace(P2S_END, ''.join(command + '\n' for command in commands) + P2S_END)


def assert_refused(archive, missing_b=False):
    reason = r'^an M620\.11 tool change has no B \(extruder\) field, line [0-9]+$' if missing_b else '^Unverifiable source mapping$'
    with pytest.raises(ValueError, match=reason):
        inspect_single_source_ams(archive)


def test_genuine_p2s_pla_source_is_zero():
    result = inspect_single_source_ams(P2S.read_bytes())
    assert (result.plate_index, result.source_index, result.material) == (0, 0, 'PLA')
    multi = inspect_ams_sources(P2S.read_bytes())
    assert [(source.source_index, source.material, source.extruder, source.used_grams)
            for source in multi.sources] == [(0, 'PLA', None, 60.84)]


def test_p2s_fixture_holds_the_refused_lines_in_their_real_places():
    with zipfile.ZipFile(P2S) as package:
        code = package.read('Metadata/plate_1.gcode').decode()
    assert code.index(P2S_ONLY_LINES[0]) < code.index(P2S_ONLY_LINES[1]) < code.index('T0\n') < code.index(P2S_ONLY_LINES[2])
    assert code.endswith(P2S_END + P2S_PULL_BACK)


@pytest.mark.parametrize('command', [
    # Shape, selector or source changes on each admitted line.
    'M620.11 P0 L0 I1 E0', 'M620.11 P0 L0 I0 E0 S1', 'M620.11 P0 L1 I0 E0', 'M620.11 P2 L0 I0 E0',
    'M620.11 P0 L0 I0 E1', 'M620.11 P0 L0 I0', 'M620.11 P0 I0 E0', 'M620.11 L0 I0 E0', 'M620.11 P0 L0 E0',
    'M620.11 P1 L0 I0 E0', 'M620.11  P0 L0 I0 E0', 'M620.11 P0 L0 I00 E0', 'M620.11 K0 I00 R0', 'M620.6 I00 W1',
    'M620.11 K0 I1 R0', 'M620.11 K1 I0 R0', 'M620.11 K0 I0 R1', 'M620.11 K0 I0', 'M620.11 K0 R0', 'M620.11 K0 I0 R0 S1',
    'M620.6 I1 W1', 'M620.6 I0 W0', 'M620.6 I0 W2', 'M620.6 I0', 'M620.6 W1', 'M620.6 I0 W1 S1', 'M620.6  I0 W1',
    # Bambu's shared M620.10 line, with what the P2S template never has.
    'M620.10 A2 F523.843 H0.4 T240 P220 S1', 'M620.10 A0 F523.843 H0.4 T240 P220 S0', 'M620.10 A0 F523.843 H0.4 T240 P220',
    # A second source.
    'T1', 'M620 S1A', 'M621 S1A',
    # Change-template forms, on a file with nothing to change.
    'M620.11 P1 L0 I0 E-18 F523.843', 'M620.10 A0 F523.843 L0 H0.4 T240 P220 S1', 'M620.15 C220', 'M620.10 R2',
    # Allowed for other models, but no P2S template has them.
    'T255', 'T1000', 'T1001', 'T1100', 'T65279', 'T0 H-1', 'M620 S255', 'M621 S255', 'M620 S255A', 'M621 S255A',
    'M620 S65279', 'M621 S65279', 'M620 S0A H-1', 'M620.3 W1', 'M620.1 E F300 T240', 'M620 Q1', 'M620 D-1',
    'M620.17 T0 S220 L0', 'M620.6 I0 H-1 W1', 'M620.11 P0 I0 B-1 E0', 'M620.11 K0 I0 B-1 R0', 'M620.11 S0',
    'M620.1 X30 Y50 F21000 P0',
])
def test_p2s_near_miss_commands_refuse(command):
    assert_refused(p2s_archive(p2s_before_end(command)))


def test_p2s_air_detect_needs_its_tool_loaded_first():
    assert_refused(p2s_archive(lambda code: P2S_ONLY_LINES[2] + code.replace(P2S_ONLY_LINES[2], '')))


def test_p2s_retraction_lines_need_a_tool_to_agree_with():
    assert_refused(p2s_archive(lambda code: code.replace('T0\n', '').replace(P2S_ONLY_LINES[2], '')))


@pytest.mark.parametrize('gcode', [
    # Before the end template, where a tray could still feed the print.
    lambda code: 'M620 S65535\n' + code.replace('M620 S65535\n', ''),
    lambda code: code.replace(P2S_END + P2S_PULL_BACK, P2S_PULL_BACK + P2S_END),
    lambda code: code.replace('T65535\n', '', 1).replace(P2S_END, 'T65535\n' + P2S_END),
    lambda code: code.replace('M621 S65535\n', '', 1).replace(P2S_END, 'M621 S65535\n' + P2S_END),
    # Out of order, repeated, or not all there.
    lambda code: code.replace(P2S_PULL_BACK, 'T65535\nM620 S65535\nM621 S65535\n'),
    lambda code: code.replace(P2S_PULL_BACK, 'M620 S65535\nM621 S65535\nT65535\n'),
    lambda code: code + P2S_PULL_BACK,
    lambda code: code + 'T65535\n',
    lambda code: code.replace(P2S_PULL_BACK, ''),
    lambda code: code.replace('M620 S65535\n', ''),
    lambda code: code.replace('T65535\n', ''),
    lambda code: code.replace('M621 S65535\n', ''),
    # No end marker to be after, or something that only looks like one.
    lambda code: code.replace(P2S_END, ''),
    lambda code: code.replace(P2S_END, '; note MACHINE_END_GCODE_START\n'),
    lambda code: code.replace(P2S_END, 'G4 ; MACHINE_END_GCODE_START\n'),
    # A marker early on doesn't make what follows the end template.
    lambda code: P2S_END + code.replace(P2S_END, ''),
    lambda code: P2S_END + code,
    # With a parameter, or spelled another way.
    lambda code: code.replace('M620 S65535\n', 'M620 S65535A\n'),
    lambda code: code.replace('M620 S65535\n', 'M620  S65535\n'),
    lambda code: code.replace('T65535\n', 'T65535 H-1\n'),
    lambda code: code.replace('M621 S65535\n', 'M621 S65534\n'),
])
def test_p2s_pull_back_only_as_the_end_template_has_it(gcode):
    assert_refused(p2s_archive(gcode))


@pytest.mark.parametrize('command', [
    'T0', 'M620 S0A', 'M621 S0A', 'M620 M', 'M620.6 I0 W1', 'M620.11 K0 I0 R0', 'M620.11 P0 L0 I0 E0',
    'M620.10 A0 F523.843 H0.4 T240 P220 S1',
    'G1 E1 F300', 'G0 X1 E1', 'G1 X10 Y10 E-1', 'G1E1', 'G01 E1', 'G2 X1 Y1 I1 J1 E1', 'G3 X1 Y1 I1 J1 E1',
])
@pytest.mark.parametrize('place', [
    lambda code, command: code + command + '\n',
    lambda code, command: code.replace('T65535\n', 'T65535\n' + command + '\n'),
    lambda code, command: code.replace('M620 S65535\n', 'M620 S65535\n' + command + '\n'),
])
def test_p2s_nothing_feeds_after_the_pull_back_starts(command, place):
    assert_refused(p2s_archive(lambda code: place(code, command)))


def test_p2s_end_template_after_the_pull_back_is_not_refused():
    # The real end template, including M1006's E note and the moves that lift Z.
    tail = 'G150.3\nM104 S0\nM400\nM17 S\nG1 Z145.8 F600\nG1 Z143.8\nM622 J1\nM623\nM1006 A53 B10 L50 C53 D10 M50 E53 F10 N50\nM18\n'
    def real(code):
        return code.replace('T65535\n', 'T65535\nG150.1 F8000\n') + tail
    assert inspect_single_source_ams(p2s_archive(real)).source_index == 0


@pytest.mark.parametrize('model', ['Bambu Lab X1 Carbon', 'Bambu Lab X1', 'Bambu Lab X1E', 'Bambu Lab P1P', 'Bambu Lab P1S',
                                   'Bambu Lab A1', 'Bambu Lab A1 mini', 'Bambu Lab H2D', 'Bambu Lab H2D Pro', 'Bambu Lab H2S',
                                   'Bambu Lab H2C', 'Bambu Lab X2D', 'Bambu Lab A2L', 'Bambu Lab P2S ', 'P2S', 'N7', '', None])
def test_p2s_lines_refuse_on_any_other_model_and_the_rest_stays_as_it_was(model):
    def declared(*kept):
        def change(code):
            code = code.replace('; printer_model = Bambu Lab P2S', f'; printer_model = {model}')
            for line in P2S_ONLY_LINES:
                code = code if line in kept else code.replace(line, '')
            return code
        return change
    if model in ('Bambu Lab H2D', 'Bambu Lab H2D Pro'):
        # A single-nozzle P2S archive cannot prove a declared H2D layout.
        with pytest.raises(ValueError, match='two_extruder_layout_unverified'):
            inspect_single_source_ams(p2s_archive(declared(), model=model))
        return
    assert_refused(p2s_archive(declared(*P2S_ONLY_LINES), model=model), missing_b=True)
    for line in P2S_ONLY_LINES:
        assert_refused(p2s_archive(declared(line), model=model), missing_b=line.startswith('M620.11 '))
    assert inspect_single_source_ams(p2s_archive(declared(), model=model)).source_index == 0
    # The shared allowances stay as they were: no position, and the other sentinels.
    early = lambda code: 'T65535\nM620 S65535\nT1000\nT255\n' + declared()(code).replace(P2S_END + P2S_PULL_BACK, 'M621 S65535\nG1 E1\n')
    assert inspect_single_source_ams(p2s_archive(early, model=model)).source_index == 0


@pytest.mark.parametrize('gcode', [
    lambda code: code.replace('; printer_model = Bambu Lab P2S', '; printer_model = Bambu Lab X1 Carbon'),
    lambda code: code.replace('; printer_model = Bambu Lab P2S\n', ''),
    lambda code: code.replace('; printer_model = Bambu Lab P2S\n', '; printer_model = Bambu Lab P2S\n; printer_model = Bambu Lab P2S\n'),
])
def test_p2s_project_and_gcode_must_agree_on_the_model(gcode):
    assert_refused(p2s_archive(gcode))


@pytest.mark.parametrize('member,old,new', [
    ('Metadata/slice_info.config', 'id="1"', 'id="2"'),
    ('Metadata/slice_info.config', 'type="PLA"', 'type="PETG"'),
    ('Metadata/plate_1.json', '"filament_ids": [0]', '"filament_ids": [1]'),
    ('Metadata/project_settings.config', '"filament_type": ["PLA"]', '"filament_type": ["PETG"]'),
    ('Metadata/plate_1.gcode', '; filament_type = PLA', '; filament_type = PETG'),
])
def test_p2s_metadata_conflicts_refuse(member, old, new):
    assert_refused(p2s_archive(replacements={member: (old, new)}))
