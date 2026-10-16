"""cert-D jobsB: real jobs/models/presets/scheduler HTTP routers on a bootstrapped SQLite store.

Claims: F04-4.2-06 (Upload -> Model Library -> Schedule -> Jobs), F04-4.2-07 (Quick Print
creates a pending job immediately), F04-4.2-08 (quick-schedule presets), F04-4.2-09 (due
dates and priority levels), F04-4.2-10 (GET /api/jobs/filament-check). Producer responses
used by the frontend wire tests are captured into a certD fixture file.
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_print_file_upload_ownership_behavior import upload_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.test_license_user_limits import signed_license
from tests.test_contracts.cert_D_capture import capturing

ROOT = Path(__file__).resolve().parents[2]
WIRE = ROOT / 'frontend/src/test-fixtures/certD-jobsB-wire.json'
CAPTURED = {}


def record_wire(key, value):
    """Producer contract: compare with the committed fixture, or create it on first capture."""
    existing = json.loads(WIRE.read_text()) if WIRE.exists() else {}
    if capturing():
        existing[key] = value
        WIRE.write_text(json.dumps(existing, indent=2, sort_keys=True) + '\n')
        return
    assert key in existing, f'{WIRE.name}:{key} is not committed; capture it with CAPTURE=1'
    assert existing[key] == value, f'Producer contract for {key} changed; reconcile the consumer explicitly'


@pytest.fixture
def api(review_db, signed_license, monkeypatch):
    """Assembled jobs router (+ presets) mounted at /api exactly as the app mounts it."""
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.errors import OdinError
    from fastapi.responses import JSONResponse
    from modules.jobs.routes import router as jobs_router
    from modules.jobs import scheduler_routes
    from modules.notifications import alert_dispatcher
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    signed_license(tier='pro', features=[])
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda **_: True)
    app = FastAPI()
    app.include_router(jobs_router, prefix='/api')
    app.include_router(scheduler_routes.router, prefix='/api')

    @app.exception_handler(OdinError)
    async def error(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    def request_db():
        with Session(review_db.get_bind()) as db:
            yield db
    user = {'id': 3, 'username': 'admin', 'role': 'operator', 'group_id': 1, 'is_active': True}
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: dict(user)
    with TestClient(app) as client:
        yield client, user, review_db


# ---------------- F04-4.2-10 filament-check ----------------

def slots(db, printer_id, values):
    db.execute(text('DELETE FROM filament_slots WHERE printer_id=:p'), {'p': printer_id})
    for number, (material, color) in enumerate(values, start=1):
        db.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type,color) VALUES(:p,:n,:m,:c)'),
                   {'p': printer_id, 'n': number, 'm': material, 'c': color})
    db.commit()


def check(client, **params):
    response = client.get('/api/jobs/filament-check', params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['printer_id'] == params['printer_id']
    return body['filament_warnings']


def test_filament_check_compatibility_matrix_on_loaded_slots(api):
    client, _, db = api
    slots(db, 9, [('PLA', '#112233'), ('PETG', '#ffffff')])
    assert check(client, printer_id=9, filament_type='PLA', colors='#112233') == []
    assert check(client, printer_id=9, filament_type='petg', colors='#FFFFFF, #112233') == []
    assert check(client, printer_id=9, filament_type='ABS') == ['Job requires ABS but printer has PETG, PLA loaded'] or \
        check(client, printer_id=9, filament_type='ABS') == ['Job requires ABS but printer has PLA, PETG loaded']
    assert check(client, printer_id=9, colors='#112233,#ff0000') == [
        "Required color '#ff0000' not found in loaded filament slots"]
    warnings = check(client, printer_id=9, filament_type='TPU', colors='#000000,#ff0000')
    assert len(warnings) == 3 and warnings[0].startswith('Job requires TPU but printer has')
    assert check(client, printer_id=9) == []


def test_filament_check_is_advisory_and_creates_nothing(api):
    client, _, db = api
    slots(db, 9, [('PLA', '#112233')])
    before = db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
    assert len(check(client, printer_id=9, filament_type='PETG', colors='#abcdef')) == 2
    assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one() == before


def test_filament_check_privacy_before_printer_access(api):
    client, user, db = api
    db.execute(text("INSERT INTO printers(id,name,api_type,org_id,shared) VALUES(991,'Foreign private','bambu',2,0),"
                    "(992,'Foreign shared','bambu',2,1)"))
    db.commit()
    slots(db, 991, [('PETG', '#000000')])
    slots(db, 992, [('PLA', '#000000')])
    private = client.get('/api/jobs/filament-check', params={'printer_id': 991, 'filament_type': 'PLA'})
    missing = client.get('/api/jobs/filament-check', params={'printer_id': 9999, 'filament_type': 'PLA'})
    assert private.status_code == missing.status_code == 404
    assert private.json() == missing.json()  # foreign private printer is indistinguishable from absent
    assert 'PETG' not in private.text
    assert check(client, printer_id=992, filament_type='PLA') == []


def test_filament_check_still_flags_material_mismatch_when_one_slot_is_empty(api):
    client, _, db = api
    slots(db, 9, [('PETG', '#ffffff'), ('empty', None)])
    assert check(client, printer_id=9, filament_type='PLA') != []


# ---------------- F04-4.2-09 due dates and priority levels ----------------

NAMED_PRIORITY_DEFECT = (
    "DEFECT: backend/modules/jobs/schemas.py:18 accepts priority as str and backend/modules/jobs/routes/"
    "jobs_crud.py:470 stores it unnormalized, so POST /api/jobs priority='urgent'|'high'|'normal'|'low' "
    "persists TEXT; JobResponse masks it on read, but scheduler.py:790 (5 - job.priority) raises TypeError "
    "and aborts the whole scheduler run")


@pytest.mark.parametrize('label,stored', [('urgent', 1), ('high', 2), ('normal', 3), ('low', 4)])
def test_named_priority_levels_persist_as_scheduler_ranks(api, label, stored):
    client, _, db = api
    response = client.post('/api/jobs', json={'item_name': f'Synthetic {label}', 'priority': label,
                                              'due_date': '2026-10-20T00:00:00Z'})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body['priority'] == stored and body['due_date'].startswith('2026-10-20T00:00:00')
    raw = db.execute(text('SELECT priority,typeof(priority),due_date FROM jobs WHERE id=:id'), {'id': body['id']}).one()
    assert (raw[0], raw[1]) == (stored, 'integer')
    assert str(raw[2]).startswith('2026-10-20 00:00:00')


def test_numeric_priority_and_due_date_round_trip_and_order_queue(api):
    client, _, db = api
    for name, priority, due in (('Synthetic low', 4, '2026-10-30T00:00:00'), ('Synthetic urgent', 1, '2026-10-13T00:00:00'),
                                ('Synthetic normal', 3, None)):
        response = client.post('/api/jobs', json={'item_name': name, 'priority': priority, 'due_date': due})
        assert response.status_code == 201, response.text
    listed = client.get('/api/jobs', params={'status': 'pending'}).json()
    ordered = [j['item_name'] for j in listed if j['item_name'].startswith('Synthetic ')]
    assert ordered == ['Synthetic urgent', 'Synthetic normal', 'Synthetic low']
    urgent = next(j for j in listed if j['item_name'] == 'Synthetic urgent')
    patched = client.patch(f"/api/jobs/{urgent['id']}", json={'priority': 2, 'due_date': '2026-10-15T00:00:00'})
    assert patched.status_code == 200, patched.text
    assert patched.json()['priority'] == 2 and patched.json()['due_date'].startswith('2026-10-15T00:00:00')
    assert client.post('/api/jobs', json={'item_name': 'Synthetic bad', 'priority': 11}).status_code == 422
    CAPTURED['jobs'] = listed
    record_wire('jobs_list_priority_due', [
        {k: j[k] for k in ('item_name', 'status', 'priority', 'due_date', 'quantity', 'hold', 'is_locked', 'colors_list')}
        for j in listed if j['item_name'].startswith('Synthetic ')])


def _rank_run(api, low_value, urgent_value):
    from datetime import datetime, timezone
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    client, _, db = api
    db.execute(text('UPDATE printers SET is_active=CASE WHEN id=9 THEN 1 ELSE 0 END')); db.commit()
    low = client.post('/api/jobs', json={'item_name': 'Synthetic first low', 'priority': low_value, 'duration_hours': 1}).json()['id']
    urgent = client.post('/api/jobs', json={'item_name': 'Synthetic later urgent', 'priority': urgent_value, 'duration_hours': 1}).json()['id']
    start = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    Scheduler(SchedulerConfig(horizon_days=1)).run(db, start_date=start)
    db.expire_all()
    times = dict(db.execute(text('SELECT id,scheduled_start FROM jobs WHERE id IN (:a,:b)'), {'a': low, 'b': urgent}).all())
    assert times[urgent] is not None and times[low] is not None
    assert str(times[urgent]) < str(times[low])


def test_numeric_priority_rank_orders_scheduler_assignment(api):
    _rank_run(api, 4, 1)


def test_named_priority_rank_orders_scheduler_assignment(api):
    _rank_run(api, 'low', 'urgent')


# ---------------- F04-4.2-08 quick-schedule presets ----------------

def test_preset_crud_and_quick_schedule_creates_configured_pending_job(api):
    client, user, db = api
    created = client.post('/api/presets', json={'name': 'Synthetic bracket x4', 'item_name': 'Synthetic bracket',
                                                'quantity': 4, 'priority': 2, 'duration_hours': 1.5,
                                                'colors_required': 'black, white', 'filament_type': 'PETG',
                                                'required_tags': ['Room A'], 'notes': 'Synthetic preset notes'})
    assert created.status_code == 201, created.text
    preset_id = created.json()['id']
    listed = client.get('/api/presets')
    assert listed.status_code == 200
    preset = next(p for p in listed.json() if p['id'] == preset_id)
    assert preset == {'id': preset_id, 'name': 'Synthetic bracket x4', 'model_id': None, 'model_name': None,
                      'item_name': 'Synthetic bracket', 'quantity': 4, 'priority': 2, 'duration_hours': 1.5,
                      'colors_required': 'black, white', 'filament_type': 'PETG', 'required_tags': ['Room A'],
                      'notes': 'Synthetic preset notes'}
    record_wire('presets_list', listed.json())
    scheduled = client.post(f'/api/presets/{preset_id}/schedule')
    assert scheduled.status_code == 200, scheduled.text
    record_wire('preset_schedule_response_keys', sorted(scheduled.json()))
    body = scheduled.json()
    assert body['status'] == 'pending' and body['item_name'] == 'Synthetic bracket'
    row = dict(db.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id': body['id']}).mappings().one())
    assert (row['status'], row['quantity'], row['priority'], row['duration_hours'], row['colors_required'],
            row['filament_type'], json.loads(row['required_tags']), row['notes'], row['charged_to_org_id'],
            row['charged_to_user_id']) == ('pending', 4, 2, 1.5, 'black, white', 'PETG', ['Room A'],
                                           'Synthetic preset notes', 1, 3)
    # Repeated quick-schedule makes another independent job from the same configuration.
    again = client.post(f'/api/presets/{preset_id}/schedule').json()['id']
    assert again != body['id']
    assert client.delete(f'/api/presets/{preset_id}').status_code == 204
    assert client.post(f'/api/presets/{preset_id}/schedule').status_code == 404
    assert all(p['id'] != preset_id for p in client.get('/api/presets').json())


def test_preset_from_model_inherits_build_time(api):
    client, _, db = api
    db.execute(text("INSERT INTO models(id,name,org_id,build_time_hours) VALUES(77,'Synthetic model',1,2.5)")); db.commit()
    preset_id = client.post('/api/presets', json={'name': 'Synthetic model preset', 'model_id': 77}).json()['id']
    job_id = client.post(f'/api/presets/{preset_id}/schedule').json()['id']
    row = db.execute(text('SELECT item_name,model_id,duration_hours,quantity,priority FROM jobs WHERE id=:id'), {'id': job_id}).one()
    assert tuple(row) == ('Synthetic model preset', 77, 2.5, 1, 3)


def test_preset_invalid_input_and_role_are_refused_without_writes(api):
    client, user, db = api
    assert client.post('/api/presets', json={'item_name': 'No name'}).status_code == 422
    assert client.post('/api/presets', json={'name': 'Bad quantity', 'quantity': 'many'}).status_code == 422
    user['role'] = 'viewer'
    assert client.post('/api/presets', json={'name': 'Viewer preset'}).status_code == 403
    assert db.execute(text('SELECT COUNT(*) FROM print_presets')).scalar_one() == 0
    user['role'] = 'operator'
    preset_id = client.post('/api/presets', json={'name': 'Operator preset'}).json()['id']
    user['role'] = 'viewer'
    assert client.post(f'/api/presets/{preset_id}/schedule').status_code == 403
    assert client.delete(f'/api/presets/{preset_id}').status_code == 403
    assert db.execute(text("SELECT COUNT(*) FROM jobs WHERE item_name='Operator preset'")).scalar_one() == 0


# ---------------- F04-4.2-06 / F04-4.2-07 upload pipeline and Quick Print ----------------

@pytest.fixture
def pipeline(upload_client, review_db, monkeypatch):
    from modules.models_library.routes import models_crud
    from modules.jobs.routes import router as jobs_router
    from modules.jobs import scheduler_routes
    from modules.organizations.services import EducationPolicyService
    from core.registry import registry
    client, issue, storage = upload_client
    client = TestClient(client.app)
    client.app.include_router(models_crud.router)
    client.app.include_router(jobs_router)
    client.app.include_router(scheduler_routes.router)
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    headers = issue(['write:models', 'read:models', 'write:jobs', 'read:jobs', 'write'])
    payload = (ROOT / 'tests/fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes()
    uploaded = client.post('/print-files/upload', headers=headers,
                           files={'file': ('Synthetic quick cube.3mf', payload, 'application/octet-stream')})
    assert uploaded.status_code == 200, uploaded.text
    return client, headers, review_db, uploaded.json()


def wire_upload(body):
    # project_name is omitted: for this sanitized fixture it is the server temp-file stem (see xfail below).
    keep = ('model_id', 'is_sliced', 'filaments', 'print_time_formatted', 'total_filament_grams',
            'layer_count', 'thumbnail_b64', 'printer_model', 'duplicate', 'filename', 'original_filename')
    return {k: body[k] for k in keep if k in body}


def test_quick_print_without_options_creates_pending_job_immediately(pipeline):
    client, headers, db, uploaded = pipeline
    before = db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
    response = client.post(f"/models/{uploaded['model_id']}/schedule", headers=headers)  # exactly what Quick Print sends
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'pending' and body['model_id'] == uploaded['model_id']
    row = dict(db.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id': body['job_id']}).mappings().one())
    assert (row['status'], row['model_id'], row['print_file_id'], row['quantity'], row['hold'], row['printer_id']) == \
        ('pending', uploaded['model_id'], uploaded['id'], 1, 0, None)
    assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one() == before + 1
    listed = client.get('/jobs', headers=headers, params={'status': 'pending'})
    assert body['job_id'] in {j['id'] for j in listed.json()}
    record_wire('quick_print_upload', wire_upload(uploaded))
    record_wire('quick_print_schedule', {'status': body['status'], 'model_id': body['model_id'],
                                         'has_job_id': isinstance(body['job_id'], int)})


def _pipeline_to_scheduled(pipeline, printer_model):
    client, headers, db, uploaded = pipeline
    # Printer 20 is an A1 identified by its Bambu code ('N2S') or its auto-detected friendly name ('A1').
    db.execute(text("INSERT INTO printers(id,name,model,api_type,is_active,org_id) VALUES(20,'Owned A1',:m,'bambu',1,1)"),
               {'m': printer_model})
    db.commit()
    library = client.get('/models', headers=headers)
    assert library.status_code == 200 and uploaded['model_id'] in {m['id'] for m in library.json()}
    options = client.get(f"/models/{uploaded['model_id']}/schedule-options", headers=headers).json()
    options.pop('plate_indices')
    queued = client.post(f"/models/{uploaded['model_id']}/schedule", headers=headers,
                         json={**options, 'quantity': 1, 'priority': 2})
    assert queued.status_code == 200, queued.text
    job_id = queued.json()['job_id']
    run = client.post('/scheduler/run', headers=headers)
    assert run.status_code == 200, run.text
    job = client.get(f'/jobs/{job_id}', headers=headers).json()
    assert (job['status'], job['printer_id']) == ('scheduled', 20), run.json()
    assert job['model_id'] == uploaded['model_id'] and job['scheduled_start'] and job['scheduled_end']
    pinned = db.execute(text('SELECT print_file_id FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one()
    assert pinned == uploaded['id']
    assert job_id in {j['id'] for j in run.json()['jobs']}


def test_upload_library_schedule_jobs_pipeline_ends_scheduled_on_compatible_printer(pipeline):
    """P1S printers 9/10 are refused for the A1-sliced (N2S) file; the A1 identified by its code receives it."""
    _pipeline_to_scheduled(pipeline, 'N2S')


def test_printer_with_no_model_identity_never_receives_a_model_specific_file(pipeline):
    """Fail closed (previously an unset-model printer accepted any model-specific file)."""
    client, headers, db, uploaded = pipeline
    db.execute(text("INSERT INTO printers(id,name,model,machine_type,api_type,is_active,org_id) VALUES(20,'Unknown',NULL,NULL,'bambu',1,1)"))
    db.execute(text("UPDATE printers SET is_active=0 WHERE id IN (9,10)"))
    db.commit()
    options = client.get(f"/models/{uploaded['model_id']}/schedule-options", headers=headers).json()
    options.pop('plate_indices')
    job_id = client.post(f"/models/{uploaded['model_id']}/schedule", headers=headers,
                         json={**options, 'quantity': 1, 'priority': 2}).json()['job_id']
    assert client.post('/scheduler/run', headers=headers).status_code == 200
    assert client.get(f'/jobs/{job_id}', headers=headers).json()['status'] == 'pending'


def test_pipeline_schedules_on_auto_detected_a1(pipeline):
    _pipeline_to_scheduled(pipeline, 'A1')


def test_uploaded_model_library_name_falls_back_to_uploaded_filename(pipeline):
    client, headers, db, uploaded = pipeline
    name = db.execute(text('SELECT name FROM models WHERE id=:id'), {'id': uploaded['model_id']}).scalar_one()
    assert name == 'Synthetic quick cube'



def test_model_library_schedule_with_a_chosen_printer_pins_it(pipeline):
    from modules.jobs.pinning import pinned_printer_id
    client, headers, db, uploaded = pipeline
    db.execute(text("INSERT INTO printers(id,name,model,machine_type,api_type,is_active,org_id) VALUES(20,'Owned A1','A1','A1','bambu',1,1)"))
    db.commit()
    options = client.get(f"/models/{uploaded['model_id']}/schedule-options", headers=headers).json()
    options.pop('plate_indices')
    made = client.post(f"/models/{uploaded['model_id']}/schedule?printer_id=20", headers=headers, json={**options, 'quantity': 1})
    assert made.status_code == 200, made.text
    db.expire_all()
    row = db.execute(text('SELECT target_type,target_filter FROM jobs WHERE id=:id'), {'id': made.json()['job_id']}).one()
    assert pinned_printer_id(*row) == 20
