"""'Runs on N of your printers' and the file's own facts (first-run follow-up 5).

The count is not a rule of its own: it must equal what the scheduler and a queue
request accept for the same file and printer, for real X1C / A1 / H2D / P2S packages.
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401
from tests.test_contracts.test_cert_C_models import library, _upload  # noqa: F401

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures' / 'telemetry'
PACKAGES = {'x1c': 'x1c-pla-source-sanitized.3mf', 'a1': 'a1-petg-source-sanitized.3mf',
            'h2d': 'h2d-petg-source-sanitized.3mf', 'p2s': 'p2s-pla-source-sanitized.3mf'}
ADMIN = {'id': 3, 'role': 'admin', 'group_id': 1}
FOREIGN = {'id': 5, 'role': 'admin', 'group_id': 2}
PRINTERS = [  # id, name, model, api_type, bed x, bed y, org, shared, active
    (20, 'X1C-1', 'Bambu Lab X1 Carbon', 'bambu', 256, 256, 1, 0, 1),
    (21, 'A1-1', 'Bambu Lab A1', 'bambu', 256, 256, 1, 0, 1),
    (22, 'H2D-1', 'Bambu Lab H2D', 'bambu', 350, 320, 1, 0, 1),
    (23, 'P2S-1', 'Bambu Lab P2S', 'bambu', 256, 256, 1, 0, 1),
    (24, 'Voron', 'Voron 2.4', 'moonraker', 350, 350, 1, 0, 1),
    (25, 'X1C-2', 'X1C', 'bambu', 256, 256, 1, 0, 1),
    (26, 'X1C-private-elsewhere', 'Bambu Lab X1 Carbon', 'bambu', 256, 256, 2, 0, 1),
    (27, 'X1C-shared', 'Bambu Lab X1 Carbon', 'bambu', 256, 256, 2, 1, 1),
    (28, 'X1C-off', 'Bambu Lab X1 Carbon', 'bambu', 256, 256, 1, 0, 0),
]


@pytest.fixture
def farm(review_db, tmp_path):
    """Several printer models and one stored file per real package, all parsed by ODIN's own parser."""
    from modules.models_library.print_file_meta import extract_print_file_meta
    from modules.models_library.threemf_parser import parse_3mf
    db = review_db
    for row in PRINTERS:
        db.execute(text("INSERT INTO printers(id,name,model,api_type,bed_x_mm,bed_y_mm,org_id,shared,is_active,slot_count) "
                        "VALUES(:a,:b,:c,:d,:e,:f,:g,:h,:i,4)"), dict(zip('abcdefghi', row)))
    files = {}
    for number, (key, name) in enumerate(PACKAGES.items(), start=101):
        stored = tmp_path / f'{key}.3mf'
        shutil.copy(FIXTURES / name, stored)
        parsed, meta = parse_3mf(str(stored)), extract_print_file_meta(str(stored), '.3mf')
        db.execute(text("INSERT INTO models(id,name,print_file_id,org_id,build_time_hours) VALUES(:id,:name,:id,1,1.0)"),
                   {'id': number, 'name': key})
        db.execute(text(
            "INSERT INTO print_files(id,filename,original_filename,stored_path,model_id,org_id,printer_model,bed_x_mm,bed_y_mm,"
            "compatible_api_types,nozzle_diameter,layer_height,bed_type,supports_used,filaments_json,blob_state) "
            "VALUES(:id,:fn,:fn,:path,:id,1,:pm,:x,:y,:types,:nozzle,:layer,:bed,:supports,:filaments,'present')"),
            {'id': number, 'fn': stored.name, 'path': str(stored), 'pm': parsed.printer_model, 'x': meta['bed_x_mm'],
             'y': meta['bed_y_mm'], 'types': meta['compatible_api_types'], 'nozzle': parsed.nozzle_diameter,
             'layer': parsed.layer_height, 'bed': parsed.bed_type, 'supports': parsed.supports_used,
             'filaments': json.dumps([{'slot': f.slot, 'type': f.type, 'color': f.color} for f in parsed.filaments])})
        files[key] = number
    db.commit()
    return SimpleNamespace(db=db, files=files)


def rows(db, ids, user=ADMIN):
    from core.model_preview_access import accessible_preview_file
    return [accessible_preview_file(db, file_id, user) for file_id in ids]


def fit(db, file_id, user=ADMIN):
    from modules.models_library.printer_fit import printer_fit_for_files
    return printer_fit_for_files(db, user, rows(db, [file_id], user))[file_id]


def names(result):
    return [printer['name'] for printer in result['printers']]


def scheduler_takes(db, file_id):
    """Printer ids the scheduler's own candidate filter accepts for a job from this file."""
    from modules.jobs.models import Job
    from modules.jobs.scheduler import PrinterState, Scheduler, SchedulerConfig
    from modules.printers.models import Printer
    job_id = 9000 + file_id
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,status,priority,hold,duration_hours,quantity,charged_to_org_id,"
                    "created_at,updated_at) VALUES(:id,:m,'fit','pending',3,FALSE,1.0,1,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
               {'id': job_id, 'm': file_id})
    db.flush()
    # The requirement exactly as SchedulerPlanning reads it for a pending job.
    required = db.execute(text("SELECT printer_model FROM print_files WHERE model_id=:m AND printer_model IS NOT NULL "
                               "AND printer_model != '' ORDER BY id DESC"), {'m': file_id}).scalar()
    states = {p.id: PrinterState(p) for p in db.query(Printer).filter(Printer.is_active.is_(True))}
    scheduler, job = Scheduler(SchedulerConfig()), db.get(Job, job_id)
    start = datetime.now(timezone.utc)
    accepted = {printer_id for printer_id in states if scheduler._find_best_fit(
        job, states, {}, start, 7 * 96, required_printer_model=required, required_printer_id=printer_id)}
    db.rollback()
    return accepted


def queue_accepts(db, file_id):
    """Printer ids for which queue-time payload verification accepts this file."""
    from modules.models_library.queue_payload import verified_queue_payload
    file = rows(db, [file_id])[0]
    accepted = set()
    for printer in db.execute(text('SELECT id,api_type,is_active,bed_x_mm,bed_y_mm FROM printers')).all():
        try:
            with verified_queue_payload(file, printer):
                accepted.add(printer.id)
        except HTTPException as refused:
            assert refused.status_code == 400, refused.detail
    return accepted


@pytest.mark.parametrize('key,expected', [
    ('x1c', ['X1C-1', 'X1C-2', 'X1C-shared']), ('a1', ['A1-1']), ('h2d', ['H2D-1']), ('p2s', ['P2S-1'])])
def test_count_equals_scheduler_and_queue_eligibility(farm, key, expected):
    file_id = farm.files[key]
    result = fit(farm.db, file_id)
    assert names(result) == expected and result['count'] == len(expected)
    assert result['sliced_for'] == key.upper()   # "N2S" and "Bambu Lab A1" both read "A1"
    eligible = scheduler_takes(farm.db, file_id) & queue_accepts(farm.db, file_id)
    assert {printer['id'] for printer in result['printers']} == eligible


def test_private_printer_of_another_organization_is_never_counted_or_named(farm):
    db, file_id = farm.db, farm.files['x1c']
    assert 'X1C-private-elsewhere' not in names(fit(db, file_id)) and 'X1C-off' not in names(fit(db, file_id))
    # The other organization's own file: only its printers, none of organization 1's.
    db.execute(text('UPDATE print_files SET org_id=2 WHERE id=:id'), {'id': file_id})
    db.execute(text('UPDATE models SET org_id=2 WHERE id=:id'), {'id': file_id})
    db.commit()
    assert names(fit(db, file_id, FOREIGN)) == ['X1C-private-elsewhere', 'X1C-shared']
    from modules.models_library.printer_fit import visible_printers
    assert {p.id for p in visible_printers(db, FOREIGN)} == {26, 27}
    assert {p.id for p in visible_printers(db, ADMIN)} == {9, 10, 20, 21, 22, 23, 24, 25, 27, 28}


def test_no_fit_names_the_printer_the_file_was_sliced_for(farm):
    db = farm.db
    db.execute(text('UPDATE printers SET is_active=0 WHERE id=23'))
    db.commit()
    assert fit(db, farm.files['p2s']) == {'count': 0, 'printers': [], 'sliced_for': 'P2S'}
    db.execute(text("UPDATE print_files SET printer_model=NULL, compatible_api_types='moonraker' WHERE id=:id"),
               {'id': farm.files['p2s']})
    db.commit()
    assert fit(db, farm.files['p2s']) == {'count': 0, 'printers': [], 'sliced_for': None}


def test_upload_bound_to_a_printer_fits_only_that_printer(farm):
    from core.print_file_binding import bind_file
    bind_file(farm.db, farm.files['x1c'], 25)
    farm.db.commit()
    assert names(fit(farm.db, farm.files['x1c'])) == ['X1C-2']


def test_education_submission_file_states_no_count(farm):
    # File 11 is the fixture's Education submission: its approved printer governs, so nothing is claimed.
    from modules.models_library.printer_fit import printer_fit_for_files
    submission = farm.db.execute(text('SELECT * FROM print_files WHERE id=11')).one()
    answers = printer_fit_for_files(farm.db, ADMIN, [submission] + rows(farm.db, [farm.files['a1']]))
    assert answers[11] is None and answers[farm.files['a1']]['count'] == 1


def test_one_query_set_however_many_files(farm):
    from modules.models_library.printer_fit import printer_fit_for_files
    db = farm.db
    one, four = rows(db, [farm.files['x1c']]), rows(db, list(farm.files.values()))
    statements = []
    engine = db.get_bind()

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        assert len(printer_fit_for_files(db, ADMIN, four)) == 4
        many = len(statements)
        del statements[:]
        printer_fit_for_files(db, ADMIN, one)
        single = len(statements)
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert many == single and 0 < many <= 6
    assert sum('FROM printers' in statement for statement in statements) == 1


def test_file_facts_come_from_the_file_and_missing_ones_stay_missing(farm):
    from modules.models_library.printer_fit import file_facts, row_file_facts
    x1c, p2s, a1 = (row_file_facts(row) for row in rows(farm.db, [farm.files[k] for k in ('x1c', 'p2s', 'a1')]))
    assert x1c == {'nozzle_diameter': 0.4, 'layer_height': 0.2, 'plate_type': 'textured_plate', 'supports': False,
                   'filament_colors': ['#00AE42']}
    # The P2S package states neither a layer height nor supports: no default is invented.
    assert p2s == {'nozzle_diameter': 0.4, 'layer_height': None, 'plate_type': 'textured_plate', 'supports': None,
                   'filament_colors': ['#D1D3D5']}
    assert a1['plate_type'] is None and a1['layer_height'] is None and a1['supports'] is False
    assert file_facts() == {'nozzle_diameter': None, 'layer_height': None, 'plate_type': None, 'supports': None,
                            'filament_colors': []}
    assert file_facts(bed_type='Unknown', layer_height=0, filaments='not json')['plate_type'] is None
    # A G-code row carries the column default for supports, which the file never stated.
    gcode = SimpleNamespace(stored_path='/x/part.gcode', filename='part.gcode', nozzle_diameter=None, layer_height=None,
                            bed_type=None, supports_used=False, filaments_json='[]')
    assert row_file_facts(gcode)['supports'] is None


def test_upload_list_and_variants_carry_the_same_fit(library, review_db):
    client, issue = library
    for row in PRINTERS[:2]:
        review_db.execute(text("INSERT INTO printers(id,name,model,api_type,bed_x_mm,bed_y_mm,org_id,shared,is_active,slot_count) "
                               "VALUES(:a,:b,:c,:d,:e,:f,:g,:h,:i,4)"), dict(zip('abcdefghi', row)))
    review_db.commit()
    headers = issue(['write:models', 'read:models'])
    body = _upload(client, headers, (FIXTURES / PACKAGES['x1c']).read_bytes(), name='x1c part.3mf')
    assert body['printer_fit'] == {'count': 1, 'printers': [{'id': 20, 'name': 'X1C-1'}], 'sliced_for': 'X1C'}
    assert body['file_facts']['layer_height'] == 0.2 and body['file_facts']['supports'] is False
    listed = {m['id']: m for m in client.get('/models-with-pricing', headers=headers).json()}[body['model_id']]
    assert listed['printer_fit'] == body['printer_fit'] and listed['file_facts'] == body['file_facts']
    variants = client.get(f"/models/{body['model_id']}/variants", headers=headers).json()
    assert variants['printer_fit'] == body['printer_fit'] and variants['variants'][0]['file_facts'] == body['file_facts']
    # A package that leaves facts out still uploads, and stores no invented values.
    sparse = _upload(client, headers, (FIXTURES / PACKAGES['p2s']).read_bytes(), name='p2s part.3mf')
    assert sparse['printer_fit'] == {'count': 0, 'printers': [], 'sliced_for': 'P2S'}
    assert sparse['file_facts']['layer_height'] is None and sparse['file_facts']['supports'] is None
    stored = review_db.execute(text('SELECT layer_height, supports_used FROM print_files WHERE id=:id'), {'id': sparse['id']}).one()
    assert tuple(stored) == (None, None)


def test_model_fit_matches_scheduler_after_uploading_another_variant(farm, library):
    from tests.test_contracts.test_cert_D_jobsB_scheduler import run
    db = farm.db
    model_id = farm.files['x1c']
    # A later A1 variant belongs to the same model; the primary file remains X1C.
    db.execute(text('UPDATE print_files SET model_id=:model WHERE id=:file'),
               {'model': model_id, 'file': farm.files['a1']})
    db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(20,1,'PLA')"))
    db.commit()
    client, issue = library
    headers = issue(['read:models', 'write:jobs'])
    options = client.get(f'/models/{model_id}/schedule-options', headers=headers).json()
    assert options['print_file_id'] == model_id
    advertised = client.get(f'/models/{model_id}/variants', headers=headers).json()['printer_fit']
    assert {p['id'] for p in advertised['printers']} == {20, 25, 27}
    assert advertised['count'] == 3
    scheduled = client.post(f'/models/{model_id}/schedule?printer_id=20', headers=headers,
                            json={key: options[key] for key in ('print_file_id', 'payload_sha256')})
    assert scheduled.status_code == 200, scheduled.text
    run(db)
    assert db.execute(text('SELECT status,printer_id FROM jobs WHERE id=:id'),
                      {'id': scheduled.json()['job_id']}).one() == ('scheduled', 20)


@pytest.mark.parametrize('payload', ['unsliced', 'empty', 'missing', 'invalid', 'invalid_utf8'])
def test_unready_bambu_payload_does_not_advertise_printers(farm, payload):
    import zipfile
    file_id = farm.files['x1c']
    file = rows(farm.db, [file_id])[0]
    path = Path(file.stored_path)
    if payload == 'missing':
        path.unlink()
    elif payload == 'invalid':
        path.write_bytes(b'not a zip')
    elif payload == 'invalid_utf8':
        # A valid ZIP directory structure with a malformed UTF-8 member name.
        with zipfile.ZipFile(path, 'w') as package:
            package.writestr('Metadata/plate_1.gcode', 'G28\n')
        data = bytearray(path.read_bytes())
        central = data.index(b'PK\x01\x02')
        data[central + 8:central + 10] = (0x800).to_bytes(2, 'little')
        data[central + 46] = 0xff
        path.write_bytes(data)
    else:
        with zipfile.ZipFile(path, 'w') as package:
            package.writestr('Metadata/project_settings.config', '{"printer_model":"Bambu Lab X1 Carbon"}')
            if payload == 'empty':
                package.writestr('Metadata/plate_1.gcode', '')
    assert queue_accepts(farm.db, file_id) == set()
    assert fit(farm.db, file_id) == {'count': 0, 'printers': [], 'sliced_for': 'X1C'}


@pytest.mark.parametrize('filament,expected', [
    ({'color': '#888888'}, []),
    ({'color': '#00AE42'}, ['#00AE42']),
    ({'color': '#888888', 'color_stated': True}, ['#888888']),
    ({'color': '#888888', 'color_stated': False}, []),
])
def test_legacy_color_facts_do_not_claim_the_parser_fallback(farm, filament, expected):
    from modules.models_library.printer_fit import row_file_facts
    file_id = farm.files['x1c']
    farm.db.execute(text('UPDATE print_files SET filaments_json=:filaments WHERE id=:id'),
                    {'id': file_id, 'filaments': json.dumps([filament])})
    assert row_file_facts(rows(farm.db, [file_id])[0])['filament_colors'] == expected


def test_platform_admin_fit_uses_the_scheduling_tenant(farm):
    from core.dispatch_identity import ownership_refusal
    user = {'id': 3, 'role': 'admin', 'group_id': None}
    file_id = farm.files['x1c']
    assert ownership_refusal({'charged_to_org_id': None, 'print_file_org_id': 1},
                             {'org_id': 1, 'shared': False}) is not None
    assert fit(farm.db, file_id, user) == {'count': 0, 'printers': [], 'sliced_for': 'X1C'}


@pytest.mark.parametrize('sliced', [True, False])
@pytest.mark.parametrize('stated_color', [None, '#888888'])
def test_upload_color_facts_preserve_missing_versus_explicit_grey(library, sliced, stated_color):
    import io
    import zipfile
    from tests.test_contracts.test_cert_C_models import sliced_3mf
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(sliced_3mf())) as source, zipfile.ZipFile(buffer, 'w') as target:
        for member in source.infolist():
            if not sliced and member.filename.endswith('.gcode'):
                continue
            content = source.read(member)
            if member.filename == 'Metadata/slice_info.config':
                color = '' if stated_color is None else f' color="{stated_color}"'
                content = f'<config><plate><filament id="1" type="PLA"{color}/></plate></config>'.encode()
            if member.filename == 'Metadata/project_settings.config':
                settings = {'printer_model': 'Bambu Lab A1', 'filament_type': ['PLA']}
                if stated_color is not None:
                    settings['filament_colour'] = [stated_color]
                content = json.dumps(settings).encode()
            target.writestr(member, content)
    client, issue = library
    headers = issue(['write:models', 'read:models'])
    body = _upload(client, headers, buffer.getvalue(), name='colour provenance.3mf')
    expected = [] if stated_color is None else [stated_color]
    assert body['file_facts']['filament_colors'] == expected
    variants = client.get(f"/models/{body['model_id']}/variants", headers=headers).json()
    assert variants['variants'][0]['file_facts']['filament_colors'] == expected
