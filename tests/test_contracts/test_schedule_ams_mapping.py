"""Owned HTTP schedule/edit mapping contracts; no physical printer I/O."""
import json
import zipfile
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.cert_D_capture import publish

MAPPING = [{'source_slot': 0, 'target_slot': 4}]


@pytest.fixture
def mapping_client(creators, review_db, tmp_path):
    from modules.models_library.routes import print_files, models_crud
    from modules.jobs.routes.dispatch_status import router
    client, auth, _, _ = creators
    client.app.include_router(print_files.router, prefix='/api')
    client.app.include_router(models_crud.router, prefix='/api')
    client.app.include_router(router, prefix='/api')
    source = tmp_path / 'mapping.3mf'
    with zipfile.ZipFile(source, 'w') as package:
        package.writestr('Metadata/plate_1.gcode', '; Synthetic owned fixture')
    review_db.execute(text("INSERT INTO models(id,name,org_id,print_file_id) VALUES(501,'Mapping print',1,NULL)"))
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json) "
                           "VALUES(501,501,'mapping.3mf','mapping.3mf',:path,1,:filaments)"),
                      {'path': str(source), 'filaments': json.dumps([{'type': 'PETG', 'color': '#ffffff'}])})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,color_hex) VALUES(9,4,'PETG','White','ffffff'),(9,3,'PETG','Black','000000')"))
    review_db.commit()
    return client, auth


def schedule(mapping_client, kind='print-files', mapping=MAPPING, printer=9):
    client, auth = mapping_client
    suffix = '' if printer is None else f'?printer_id={printer}'
    body = {} if mapping is None else {'ams_mapping': mapping}
    return client.post(f'/api/{kind}/501/schedule{suffix}', headers=auth(), json=body)


@pytest.mark.parametrize('kind', ['print-files', 'models'])
def test_schedule_stores_pins_audits_and_exposes_read_only_suggestion(mapping_client, review_db, kind):
    client, auth = mapping_client
    response = schedule(mapping_client, kind)
    assert response.status_code == 200, response.text
    job = response.json()['job_id']
    row = review_db.execute(text('SELECT ams_mapping,printer_id,target_type,target_filter FROM jobs WHERE id=:id'), {'id': job}).one()
    assert (json.loads(row[0]), *row[1:]) == ([3], 9, 'specific', '9')
    audit = review_db.execute(text("SELECT actor_user_id,details FROM audit_logs WHERE action='job.ams_mapping_changed' AND entity_id=:id"), {'id': job}).one()
    assert audit[0] == 3
    assert json.loads(audit[1])['ams_mapping'] == [3]
    detail = client.get(f'/api/jobs/{job}', headers=auth())
    assert detail.status_code == 200, detail.text
    assert detail.json()['ams_mapping'] == [3]
    assert detail.json()['ams_mapping_editable'] is True
    assert detail.json()['ams_preview']['suggested_mapping'] == [{'source_slot': 0, 'target_slot': 4, 'confidence': 'exact'}]
    event = client.get(f'/api/jobs/{job}/timeline', headers=auth()).json()['events'][0]
    assert event['kind'] == 'ams_mapping_changed' and event['detail']['ams_mapping'] == [3]


@pytest.mark.parametrize('kind', ['print-files', 'models'])
@pytest.mark.parametrize('mapping,printer,message', [
    ([{'source_slot': 0, 'target_slot': 16}], 9, 'Invalid or duplicate AMS slot'),
    ([{'source_slot': 0, 'target_slot': 1}], 9, 'Selected tray material does not match the source filament'),
    (MAPPING, None, 'AMS mapping requires a specific target printer'),
    ([], 9, 'AMS mapping must cover every source filament'),
    ([{'source_slot': True, 'target_slot': 4}], 9, 'Invalid or duplicate AMS slot'),
])
def test_schedule_refusals_match_reprint(mapping_client, review_db, kind, mapping, printer, message):
    before = review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    response = schedule(mapping_client, kind, mapping, printer)
    assert response.status_code == 400, response.text
    assert response.json()['detail'] == message
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == before


@pytest.mark.parametrize('kind', ['print-files', 'models'])
def test_non_bambu_mapping_refused(mapping_client, review_db, kind):
    review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=9")); review_db.commit()
    response = schedule(mapping_client, kind)
    assert response.status_code == 400, response.text
    assert response.json()['detail'] == 'AMS mapping is supported only for Bambu printers'


def test_patch_explicit_set_clear_omission_and_target_guard(mapping_client, review_db):
    client, auth = mapping_client
    created = schedule(mapping_client, mapping=None)
    assert created.status_code == 200, created.text
    job = created.json()['job_id']
    def patch(body): return client.patch(f'/api/jobs/{job}', headers=auth(), json=body)
    assert patch({'ams_mapping': MAPPING}).json()['ams_mapping'] == [3]
    assert patch({'notes': 'Keep explicit mapping'}).json()['ams_mapping'] == [3]
    assert patch({'pinned_printer_id': None}).status_code == 409
    invalid = patch({'ams_mapping': [{'source_slot': 0, 'target_slot': 1}]})
    assert invalid.status_code == 400
    cleared = patch({'ams_mapping': None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()['ams_mapping'] is None
    rows = review_db.execute(text("SELECT actor_user_id,details FROM audit_logs WHERE entity_id=:id AND action='job.ams_mapping_changed' ORDER BY id"), {'id': job}).all()
    assert [r[0] for r in rows] == [3, 3]
    assert [json.loads(r[1]) for r in rows] == [
        {'ams_mapping': [3], 'previous_ams_mapping': None}, {'ams_mapping': None, 'previous_ams_mapping': [3]}]
    events = client.get(f'/api/jobs/{job}/timeline', headers=auth()).json()['events']
    assert events[-1]['detail']['ams_mapping'] is None


@pytest.mark.parametrize('state', ['held', 'scheduled', 'printing', 'paused', 'completed', 'dispatched', 'claim', 'education'])
def test_patch_lifecycle_and_ownership_guards(mapping_client, review_db, state):
    client, auth = mapping_client
    created = schedule(mapping_client)
    assert created.status_code == 200, created.text
    job = created.json()['job_id']
    if state == 'held':
        review_db.execute(text('UPDATE jobs SET hold=1 WHERE id=:id'), {'id': job})
    elif state == 'dispatched':
        review_db.execute(text("UPDATE jobs SET actual_start=CURRENT_TIMESTAMP WHERE id=:id"), {'id': job})
    elif state == 'education':
        review_db.execute(text('UPDATE education_submissions SET job_id=:id WHERE id=14'), {'id': job})
    elif state != 'claim':
        review_db.execute(text('UPDATE jobs SET status=:status WHERE id=:id'), {'id': job, 'status': state})
    review_db.commit()
    if state == 'claim':
        from core.dispatch_claims import acquire
        assert acquire(review_db.get_bind(), 9, job, 'a'*64, 'b'*64, 'c'*64, 1).status == 'acquired'
    for value in (MAPPING, None):
        response = client.patch(f'/api/jobs/{job}', headers=auth(), json={'ams_mapping': value})
        assert response.status_code == (200 if state in ('held', 'scheduled') else 409), response.text
    review_db.expire_all()
    if state not in ('held', 'scheduled'):
        assert json.loads(review_db.execute(text('SELECT ams_mapping FROM jobs WHERE id=:id'), {'id': job}).scalar_one()) == [3]


def test_mapping_rbac_and_preview_tenant_isolation(mapping_client, review_db):
    client, auth = mapping_client
    job = schedule(mapping_client).json()['job_id']
    # Student is a real viewer JWT, not a patched role dependency.
    for path in ('print-files', 'models'):
        response = client.post(f'/api/{path}/501/schedule?printer_id=9', headers=auth('student'), json={'ams_mapping': MAPPING})
        assert response.status_code == 403, response.text
    assert client.patch(f'/api/jobs/{job}', headers=auth('student'), json={'ams_mapping': None}).status_code == 403
    assert client.patch(f'/api/jobs/{job}', headers=auth('foreign'), json={'ams_mapping': MAPPING}).status_code == 404
    review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type,is_active) VALUES(99,'Foreign',2,'bambu',1)")); review_db.commit()
    assert client.get('/api/print-files/501?printer_id=99', headers=auth()).status_code == 404


def test_mapping_wire_fixture(mapping_client):
    client, auth = mapping_client
    file = client.get('/api/print-files/501?printer_id=9', headers=auth())
    assert file.status_code == 200, file.text
    # Reading a suggestion never changes the job or schedules a file.
    assert file.json()['job_id'] is None
    job = schedule(mapping_client).json()['job_id']
    detail = client.get(f'/api/jobs/{job}', headers=auth())
    assert detail.status_code == 200, detail.text
    publish('frontend/src/test-fixtures/schedule-ams-wire.json', {
        'preview': file.json()['ams_preview'],
        'job': {key: detail.json()[key] for key in ('id', 'status', 'printer_id', 'ams_mapping', 'ams_preview', 'ams_mapping_editable')},
    })


def test_missing_preview_does_not_hide_saved_job_mapping(mapping_client, review_db):
    client, auth = mapping_client
    job = schedule(mapping_client).json()['job_id']
    review_db.execute(text("UPDATE print_files SET filaments_json='invalid' WHERE id=501")); review_db.commit()
    response = client.get(f'/api/jobs/{job}', headers=auth())
    assert response.status_code == 200, response.text
    assert response.json()['ams_mapping'] == [3]
    assert response.json()['ams_preview'] is None


def test_audit_failure_rolls_back_mapping_and_other_edits(mapping_client, review_db):
    from fastapi.testclient import TestClient
    client, auth = mapping_client
    job = schedule(mapping_client).json()['job_id']
    review_db.execute(text("CREATE TRIGGER refuse_mapping_audit BEFORE INSERT ON audit_logs "
                           "WHEN NEW.action='job.ams_mapping_changed' BEGIN SELECT RAISE(ABORT, 'audit fixture'); END"))
    review_db.commit()
    response = TestClient(client.app, raise_server_exceptions=False).patch(f'/api/jobs/{job}', headers=auth(),
        json={'ams_mapping': None, 'notes': 'must not persist'})
    assert response.status_code == 500
    row = review_db.execute(text('SELECT ams_mapping,notes FROM jobs WHERE id=:id'), {'id': job}).one()
    assert json.loads(row[0]) == [3] and row[1] is None


def test_mapping_edit_rechecks_source_model_audience(mapping_client, review_db):
    client, auth = mapping_client
    job = schedule(mapping_client).json()['job_id']
    review_db.execute(text('UPDATE models SET org_id=2 WHERE id=501')); review_db.commit()
    response = client.patch(f'/api/jobs/{job}', headers=auth(), json={'ams_mapping': MAPPING})
    assert response.status_code == 400, response.text
    assert response.json()['detail'] == 'AMS mapping requires an accessible job print file'
