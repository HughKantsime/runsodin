"""Crossed H3C positions, sparse file IDs, colour ties, and legacy configuration."""
import hashlib
import json
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from modules.printers import dispatch
from modules.printers.h3c_mapping import derive_mapping, rack_candidates, rack_preview
from modules.printers.telemetry.bambu.ams_identity import print_mapping_fields
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine
from tests.test_contracts.test_h3c_send import review_db, FIXTURES

GROUPS = {1: 3, 2: 2, 3: 8, 4: 0, 5: 5, 6: 4, 7: 6, 9: 1}
IDENTITY = [0, 1, 2, 3, 4, 5, 6, -1, 8]


def plate(path):
    root = Element('config')
    plate = SubElement(root, 'plate')
    SubElement(plate, 'metadata', key='index', value='1')
    files = []
    code = ['; printer_model = H3C', '; extruder_nozzle_count = 9', '; filament_type = ' + ';'.join(['PLA'] * 9)]
    for filament, group in GROUPS.items():
        color = f'#{filament * 0x101010:06X}'
        files.append(dict(slot=filament, type='PLA', color=color))
        SubElement(plate, 'filament', id=str(filament), type='PLA', group_id=str(group), color=color,
                   nozzle_diameter='0.4', volume_type='Standard')
        SubElement(plate, 'nozzle', id=str(group), nozzle_diameter='0.4', volume_type='Standard')
        source = filament - 1
        code.extend([f'M620 S{source}A', f'T{source}', f'M621 S{source}A'])
    with ZipFile(path, 'w') as archive:
        archive.writestr('Metadata/plate_1.gcode', '\n'.join(code))
        archive.writestr('Metadata/slice_info.config', tostring(root))
        archive.writestr('Metadata/project_settings.config', json.dumps(
            dict(printer_model='H3C', extruder_nozzle_count=['9'], filament_type=['PLA'] * 9)))
    return files


@pytest.mark.parametrize('dialect', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def test_sparse_rack_mapping(dialect, request, tmp_path, monkeypatch):
    from core import db as core_db
    from modules.printers.models import Printer
    from modules.printers.nozzle_capability import installed_nozzles
    from modules.printers.h3c_mapping import onboard_inventory

    engine = request.getfixturevalue('review_db').get_bind() if dialect == 'sqlite' else request.getfixturevalue('pg_engine')
    monkeypatch.setattr(core_db, 'engine', engine)
    report = json.loads((FIXTURES / 'h3c-ams3h-loaded-2026-11-21.json').read_bytes())['message']['print']
    tools = installed_nozzles(report, include_rack=True)
    path = tmp_path / 'plate.3mf'
    files = plate(path)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    job = dict(id=53, printer_id=901, print_file_id=52, payload_sha256=sha, plate_index=0,
               filaments_json=json.dumps(files), ams_mapping=IDENTITY)
    creds = dict(api_type='bambu', model='H3C', installed_nozzles=tools)
    def options():
        return dispatch._validated_print_options(job, creds, path, sha)
    with Session(engine) as db:
        db.add(Printer(id=901, name='Test rack', model='H3C', api_type='bambu', installed_nozzles=tools))
        db.flush()
        for file in files:
            db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) "
                            "VALUES(901,:slot,'PLA',:color)"), file)
        db.commit()
        from core.ams_mapping import validate_for_printer
        from fastapi import HTTPException
        entries = [dict(source_slot=i, target_slot=i) for i in GROUPS]
        actor = dict(id=1, role='admin', group_id=None)
        assert validate_for_printer(db, entries, job['filaments_json'], 901, actor, stored_path=path) == IDENTITY
        for position, sentence in [(2, 'Choose separate positions for the two filaments assigned to position 2.'),
                                   (10, 'Choose an onboard tray in positions 1 to 9.')]:
            changed = [{**entries[0], 'target_slot': position}, *entries[1:]]
            with pytest.raises(HTTPException) as exc:
                validate_for_printer(db, changed, job['filaments_json'], 901, actor, stored_path=path)
            assert (exc.value.status_code, exc.value.detail) == (400, sentence)
        for selection in (IDENTITY, None):
            job['ams_mapping'] = selection
            accepted, error = options()
            assert error is None and accepted['ams_mapping'] == IDENTITY
        sources = accepted.source_facts['sources']
        trays = onboard_inventory(db, 901)
        preview = rack_preview(sources, trays, tools, None)
        assert preview['rack_mapping'] == [dict(source_slot=f, target_slot=f) for f in GROUPS]
        fields = print_mapping_fields(accepted['ams_mapping'], h3c=True)
        assert fields == {'ams_mapping': [28, 29, 30, 31, 32, 33, 34, -1, 36],
                          'ams_mapping2': [dict(ams_id=32, slot_id=n) if n >= 0 else dict(ams_id=255, slot_id=255)
                                           for n in IDENTITY]}
        job['ams_mapping'] = IDENTITY.copy()
        job['ams_mapping'][2] = 7
        assert options() == (None, 'Filament 3 was sliced for a 0.4 mm standard tool. Position 8 holds a TPU tool.')
        job['ams_mapping'] = None
        db.execute(text("UPDATE filament_slots SET color_hex='#101010' WHERE printer_id=901 AND slot_number=2"))
        db.commit()
        assert options() == (None, 'Choose a mapping for filament 1.')
        db.execute(text("UPDATE filament_slots SET filament_type='empty' WHERE printer_id=901 AND slot_number=2"))
        db.commit()
        assert options() == (None, 'No free compatible position is left for filament 9.')


@pytest.mark.parametrize('dialect', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def test_retired_position_setting_is_ignored(dialect, request):
    from core.system_config_values import write_config_text
    from modules.system.routes_config import get_config, update_config
    from modules.system.schemas import ConfigUpdate

    engine = request.getfixturevalue('review_db').get_bind() if dialect == 'sqlite' else request.getfixturevalue('pg_engine')
    key = 'h3c_allow_different_positions'
    with Session(engine) as db:
        write_config_text(db, key, 'false')
        db.commit()
        query = text("SELECT value FROM system_config WHERE key=:key")
        original = db.execute(query, {'key': key}).scalar_one()
        assert key not in get_config({}, db)
        assert key not in ConfigUpdate.model_json_schema()['properties']
        update_config(ConfigUpdate.model_validate({key: True}), {}, db)
        assert db.execute(query, {'key': key}).scalar_one() == original


@pytest.mark.parametrize('dialect', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def test_dispatch_audit_keeps_file_and_loaded_colours(dialect, request):
    from core import dispatch_claims
    from core.audit_dispatch import caller_records_acceptance
    from modules.jobs.models import Job
    from modules.printers.models import Printer
    from modules.printers.h3c_mapping import color_warning
    engine = request.getfixturevalue('review_db').get_bind() if dialect == 'sqlite' else request.getfixturevalue('pg_engine')
    warning = color_warning({'source_index': 2, 'color': '#FFFFFF'}, 4, '123456FF')
    assert warning == dict(filament=3, position=4, file_color='#FFFFFF', loaded_color='#123456')
    detail = {'color_warnings': [warning]}
    with Session(engine) as db:
        if dialect == 'sqlite':
            # PostgreSQL shares this database with test_sparse_rack_mapping.
            db.add(Printer(id=901, name='Test rack'))
            db.flush()
        db.add(Printer(id=902, name='Audit rack', model='H3C', api_type='bambu'))
        db.flush()
        db.add(Job(id=902, item_name='Colour warning', printer_id=902))
        db.commit()
        for caller in (False, True):
            token = ('b' if caller else 'a') * 32
            db.execute(dispatch_claims.TABLE.insert().values(printer_id=902, job_id=902, physical_key='a'*64,
                       configuration_key='b'*64, payload_sha256='c'*64, token=token, state='adapter_entered'))
            db.commit()
            if caller:
                with caller_records_acceptance() as received:
                    assert dispatch_claims.record_outcome(engine, token, 'accepted', audit_detail=detail)
                assert received == detail
            else:
                assert dispatch_claims.record_outcome(engine, token, 'accepted', audit_detail=detail)
                saved = db.execute(text("SELECT details FROM audit_logs WHERE entity_id=902 AND action='job.dispatched'")).scalar_one()
                assert (json.loads(saved) if isinstance(saved, str) else saved)['color_warnings'] == [warning]
            db.execute(dispatch_claims.TABLE.delete().where(dispatch_claims.TABLE.c.token == token))
            db.commit()


def test_nearest_colour_and_ambiguous_or_missing_candidates():
    sources = [dict(source_index=i, color='#000000', material='PLA', nozzle_diameter=.4, volume_type='Standard')
               for i in range(2)]
    trays = [dict(slot=i+1, color_hex=color, material='PLA') for i, color in enumerate(('111111', '999999', 'FFFFFF'))]
    tools = [dict(position=i+1, side='rack', present=True, diameter=.4, type='HS01') for i in range(3)]
    trays[2]['do_not_feed'] = True
    candidates = rack_candidates(sources, trays, tools)
    assert derive_mapping(sources[:1], candidates[:1]) == [0]
    with pytest.raises(ValueError, match='^Choose a mapping for filament 1[.]$'):
        derive_mapping(sources, candidates)
    sources[1]['color'] = '#111111'
    assert derive_mapping(sources, candidates) == [1, 0]
    sources[1]['material'] = 'PETG'
    with pytest.raises(ValueError, match='^No free compatible position is left for filament 2[.]$'):
        derive_mapping(sources, rack_candidates(sources, trays, tools))


@pytest.mark.parametrize('loaded,description', [
    ({'color_hex': '112233'}, 'the same exact colour'),
    ({'color': '#112233'}, 'the same exact colour'),
    ({'color_hex': '334455'}, 'the same loaded colour'),
])
def test_uncontested_identical_colours_use_lowest_position_in_preview(loaded, description):
    source = dict(source_index=2, group_id=0, color='#112233', material='PLA', nozzle_diameter=.4, volume_type='Standard')
    trays = [dict(slot=i, material='PLA', **loaded) for i in (5, 2)]
    tools = [dict(position=t['slot'], side='rack', present=True, diameter=.4, type='HS01') for t in trays]
    result = rack_preview([source], trays, tools, None)
    assert result['rack_mapping'] == [dict(source_slot=3, target_slot=2)]
    assert result['required'][0]['selection_note'] == f'Position 2 is the lowest position with {description}.'


def test_incomplete_assignment_names_filament_reached_when_positions_run_out():
    sources = [dict(source_index=i, color='#111111') for i in (0, 3, 8)]
    trays = [dict(slot=n, color_hex='111111') for n in (1, 2)]
    with pytest.raises(ValueError, match='^No free compatible position is left for filament 9[.]$'):
        derive_mapping(sources, [trays] * 3)


@pytest.mark.parametrize('colors', [('000001', '000100'), (None, None)])
def test_distinct_or_unknown_tied_colours_need_a_mapping(colors):
    trays = [dict(slot=i, color_hex=color) for i, color in enumerate(colors, 1)]
    with pytest.raises(ValueError, match='^Choose a mapping for filament 3[.]$'):
        derive_mapping([dict(source_index=2, color='#000000')], [trays])


@pytest.mark.parametrize('mapping,message', [
    ([99, 0], 'Stored AMS mapping is invalid; no command was sent'),
    ([1, 0], 'Stored AMS mapping is invalid; no command was sent'),
    ([-2, 0], 'Stored AMS mapping is invalid; no command was sent'),
    ([-1, 99], 'Filament 2 needs an onboard tray in positions 1 to 9.'),
    ([-1, -1], 'Choose a position for filament 2.'),
])
def test_sparse_mapping_checks_used_and_unused_entries(mapping, message):
    from modules.printers.dispatch_ams_gate import preflight_ams_source
    from modules.printers.dispatch_options import _mapping_error
    source = dict(source_index=1, group_id=0, volume_type='Standard', material='PLA', nozzle_diameter=.4)
    job = dict(plate_index=0, ams_mapping=mapping)
    assert _mapping_error(job, mapping, rack_sources=[source]) == message
    with pytest.raises(ValueError) as exc:
        preflight_ams_source(job, mapping=mapping, staged_path=None, payload_sha256=None,
                             inspected_facts={'sources': [source]}, h3c=True)
    assert str(exc.value) == message


def test_accepted_progress_caps_message_and_preserves_warnings(review_db, monkeypatch):
    from core import db as core_db
    from core.durable_config import insert
    from modules.printers import dispatch_background as background
    engine = review_db.get_bind()
    monkeypatch.setattr(core_db, 'engine', engine)
    token = 'f' * 32
    with engine.begin() as conn:
        insert(conn, background.progress_key(token), dict(job_id=53, attempt_id=token, phase='preparing'))
    warning = dict(filament=1, position=9, file_color='#FF6A13', loaded_color='#FFFFFF')
    marker = background._context.set({'token': token})
    try:
        background.progress('starting', color_warnings=[warning])
        background.progress('start_accepted', message='a' * 501)
    finally:
        background._context.reset(marker)
    result = background.load(review_db, background.result_key(53))
    assert result['message'] == 'a' * 500 and result['color_warnings'] == [warning]
