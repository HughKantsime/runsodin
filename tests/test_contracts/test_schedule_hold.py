"""Schedule endpoints can create a job already held; no pending-unheld window."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_schedule_ams_mapping import mapping_client  # noqa: F401


def _schedule(mapping_client, kind, body):
    client, auth = mapping_client
    return client.post(f'/api/{kind}/501/schedule?printer_id=9', headers=auth(), json=body)


@pytest.mark.parametrize('kind', ['print-files', 'models'])
def test_schedule_creates_job_held(mapping_client, review_db, kind):
    response = _schedule(mapping_client, kind, {'hold': True})
    assert response.status_code == 200, response.text
    assert response.json()['hold'] is True
    job = response.json()['job_id']
    row = review_db.execute(text('SELECT status,hold FROM jobs WHERE id=:id'), {'id': job}).one()
    assert (row[0], bool(row[1])) == ('pending', True)


@pytest.mark.parametrize('kind', ['print-files', 'models'])
@pytest.mark.parametrize('body', [{}, {'hold': False}])
def test_schedule_default_stays_unheld(mapping_client, review_db, kind, body):
    response = _schedule(mapping_client, kind, body)
    assert response.status_code == 200, response.text
    assert response.json()['hold'] is False
    held = review_db.execute(text('SELECT hold FROM jobs WHERE id=:id'), {'id': response.json()['job_id']}).scalar()
    assert not held


@pytest.mark.parametrize('kind', ['print-files', 'models'])
@pytest.mark.parametrize('value', ['yes', 'true', 1, None])
def test_schedule_hold_requires_a_real_boolean(mapping_client, review_db, kind, value):
    before = review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar()
    response = _schedule(mapping_client, kind, {'hold': value})
    assert response.status_code == 422, response.text
    assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar() == before


def test_print_file_schedule_audit_records_hold(mapping_client, review_db):
    response = _schedule(mapping_client, 'print-files', {'hold': True})
    assert response.status_code == 200, response.text
    details = review_db.execute(text("SELECT details FROM audit_logs WHERE action='print_file.scheduled'")).scalar()
    assert json.loads(details) == {'job_id': response.json()['job_id'], 'hold': True}


def test_manual_start_refuses_a_held_job(mapping_client, review_db):
    from modules.jobs.routes.jobs_lifecycle import router as lifecycle
    client, auth = mapping_client
    client.app.include_router(lifecycle, prefix='/api')
    job = _schedule(mapping_client, 'print-files', {'hold': True}).json()['job_id']
    response = client.post(f'/api/jobs/{job}/start', headers=auth())
    assert response.status_code == 409, response.text
    row = review_db.execute(text('SELECT status,hold,actual_start FROM jobs WHERE id=:id'), {'id': job}).one()
    assert (row[0], bool(row[1]), row[2]) == ('pending', True, None)
