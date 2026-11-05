"""Bounded, content-free diagnostics preserve the existing admission decisions."""
import json
import pytest
from sqlalchemy import text
from core.ams_source_inspection import inspect_ams_sources
from modules.printers.dispatch_admission import admit, _printer_change_reason
from tests.test_contracts.test_ams_source_inspection import archive
from tests.test_contracts.test_dispatch_final_admission_behavior import setup, review_db, state


def test_zero_nozzle_map_without_plate_evidence_names_missing_layout():
    settings = {'filament_type': ['PETG'] * 6, 'physical_extruder_map': [0, 1],
                'filament_nozzle_map': [0] * 6, 'filament_map': [1, 2, 2, 2, 1, 2]}
    with pytest.raises(ValueError, match='two_extruder_layout_unverified'):
        inspect_ams_sources(archive(settings=json.dumps(settings),
            code='; filament_type = PETG;PETG;PETG;PETG;PETG;PETG\nM620 S0A\nM621 S0A\n'))


def test_missing_extruder_field_names_only_line_number():
    with pytest.raises(ValueError) as error:
        inspect_ams_sources(archive(code='; filament_type = PETG\nM620 S0A\nM621 S0A\nM620.11 P0 I0 E0 ; PRIVATE_FILE_CONTENT\n'))
    assert str(error.value) == 'an M620.11 tool change has no B (extruder) field, line 4'


def test_project_plate_difference_is_not_a_conflict():
    xml = '<config><plate><metadata key="index" value="1"/><metadata key="filament_maps" value="2 1"/><filament id="1" type="PETG"/></plate></config>'
    result = inspect_ams_sources(archive(xml=xml, code='; CONFIG_BLOCK_START\n; physical_extruder_map = 0,1\n; filament_map = 2,1\n; CONFIG_BLOCK_END\n; filament_type = PETG;PETG\nM620 S0A\nM621 S0A\n', settings=json.dumps({
        'filament_type': ['PETG', 'PETG'], 'nozzle_diameter': ['0.4', '0.4'], 'extruder_max_nozzle_count': [1, 1],
        'physical_extruder_map': [0, 1], 'filament_map': ['1', '1']})))
    assert result.sources[0].extruder == 1


def test_untrusted_parser_exception_is_not_echoed(monkeypatch):
    import core.ams_source_inspection as source
    def broken(*args, **kwargs): raise ValueError('PRIVATE_PARSER_CONTENT')
    monkeypatch.setattr(source.json, 'loads', broken)
    with pytest.raises(ValueError, match='^Unverifiable source mapping$'):
        inspect_ams_sources(archive())


@pytest.mark.parametrize('statement,expected', [
    ("UPDATE printers SET nozzle_diameter=.6 WHERE id=59", 'capability changed (nozzle_diameter)'),
    ("UPDATE printers SET org_id=2 WHERE id=59", 'ownership changed (org_id)'),
    ("UPDATE printers SET api_key='PRIVATE_CREDENTIAL' WHERE id=59", 'configuration changed (api_key)'),
    ("UPDATE printers SET slot_count=9 WHERE id=59", 'capability changed (slot_count)'),
    ("UPDATE printers SET plug_auth_token='PRIVATE_PLUG' WHERE id=59", 'configuration changed (plug_auth_token)'),
])
def test_admission_names_changed_fields_and_keeps_claim_preparing(setup, statement, expected):
    engine, _, inputs = setup
    with engine.begin() as conn: conn.execute(text(statement))
    result = admit(engine, **inputs)
    assert not result.admitted
    assert result.reason == 'Printer ' + expected
    assert 'PRIVATE' not in result.reason
    assert state(engine) == 'preparing'


def test_diagnostics_bound_all_categories_and_ignore_unknown_keys():
    old = dict(api_key='old', nozzle_diameter=.4, org_id=1)
    new = dict(api_key='PRIVATE' * 10000, nozzle_diameter=.6, org_id=2, PRIVATE_FIELD='secret')
    reason = _printer_change_reason(old, new)
    assert reason == 'Printer configuration changed (api_key); capability changed (nozzle_diameter); ownership changed (org_id)'
    assert len(reason) < 600 and 'PRIVATE' not in reason


def test_oversized_gcode_names_its_size_and_the_limit(monkeypatch):
    from core import ams_source_inspection as inspection
    monkeypatch.setattr(inspection, 'MAX_GCODE_BYTES', 1024 * 1024)
    payload = archive(code='; filament_type = PETG\n' + ';' + 'x' * (2 * 1024 * 1024) + '\n')
    with pytest.raises(ValueError) as error:
        inspect_ams_sources(payload, 0)
    assert str(error.value) == 'plate_1.gcode is too large to check (2.0 MiB uncompressed; the limit is 1 MiB)'


def test_oversized_archive_names_its_size_and_the_limit(monkeypatch):
    from core import ams_source_inspection as inspection
    monkeypatch.setattr(inspection, 'MAX_ARCHIVE_BYTES', 1024 * 1024)
    with pytest.raises(ValueError) as error:
        inspection.inspect_ams_sources(b'x' * (3 * 1024 * 1024), 0)
    assert str(error.value) == 'the file is too large to check (3.0 MiB; the limit is 1 MiB)'
