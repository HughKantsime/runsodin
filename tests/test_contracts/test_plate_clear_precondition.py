"""A confirmation must match the effect the reader was shown."""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_plate_clear_state import plate_client, read, waiting  # noqa: F401
from tests.test_contracts.test_plate_clear_confirmation import floor, print_record  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401

CHANGED = 'The queue changed since you looked. Check what is waiting and confirm again.'


def writes(db):
    return tuple(db.execute(text(sql)).all() for sql in (
        'SELECT id,status FROM jobs ORDER BY id',
        'SELECT * FROM job_timeline_events ORDER BY id',
        'SELECT * FROM audit_logs ORDER BY id',
        "SELECT key,value FROM system_config WHERE key LIKE 'plate_clear:%' ORDER BY key",
    ))


@pytest.mark.parametrize('expected,current', [(None, 803), (803, None), (803, 804)])
def test_changed_selection_refuses_without_writes(plate_client, expected, current):
    client, issue, db = plate_client
    print_record(db)
    if current is not None:
        waiting(db, identifier=current)
    headers = issue(['read', 'write:printers'])
    before = writes(db)
    response = client.post('/printers/199/plate-cleared', headers=headers,
                           json={'expected_release_job_id': expected})
    assert response.status_code == 409
    detail = response.json()['detail']
    assert detail['message'] == CHANGED
    assert detail['plate_clear'] == read(client, headers, 'detail')['plate_clear']
    assert writes(db) == before


@pytest.mark.parametrize('job_id', [None, 803])
def test_matching_selection_succeeds(plate_client, job_id):
    client, issue, db = plate_client
    print_record(db)
    if job_id is not None:
        waiting(db)
    response = client.post('/printers/199/plate-cleared', headers=issue(['read', 'write:printers']),
                           json={'expected_release_job_id': job_id})
    assert response.status_code == 200
    assert response.json()['message'] == ('Plate clear recorded' if job_id is None else 'Job 803 released for dispatch')


@pytest.mark.parametrize('body', [None, {}])
def test_omitted_precondition_keeps_legacy_release(plate_client, body):
    client, issue, db = plate_client
    waiting(db)
    response = client.post('/printers/199/plate-cleared', headers=issue(['write:printers']),
                           **({} if body is None else {'json': body}))
    assert response.status_code == 200
    assert response.json()['message'] == 'Job 803 released for dispatch'


@pytest.mark.parametrize('expected,current', [(True, True), (True, False), (False, True)])
def test_hidden_job_checks_only_the_disclosed_presence(plate_client, expected, current):
    client, issue, db = plate_client
    print_record(db)
    if current:
        waiting(db)
    headers = issue(['read:printers', 'write:printers'])
    plate = read(client, headers, 'detail')['plate_clear']
    assert plate['releases_job'] is None
    before = writes(db)
    response = client.post('/printers/199/plate-cleared', headers=headers,
                           json={'expected_has_waiting_job': expected})
    assert response.status_code == (200 if expected == current else 409)
    if expected != current:
        assert response.json()['detail'] == {'message': CHANGED, 'plate_clear': plate}
        assert writes(db) == before
    else:
        assert db.execute(text('SELECT status FROM jobs WHERE id=803')).scalar_one() == 'pending'


@pytest.mark.parametrize('body', [
    {'unexpected': True}, {'expected_release_job_id': '803'}, {'expected_release_job_id': True},
    {'expected_release_job_id': 803.5}, {'expected_has_waiting_job': 'true'},
    {'expected_has_waiting_job': None},
])
def test_strict_body_refuses_invalid_fields_without_writes(plate_client, body):
    client, issue, db = plate_client
    waiting(db)
    headers = issue(['write:printers'])
    before = writes(db)
    assert client.post('/printers/199/plate-cleared', headers=headers, json=body).status_code == 422
    assert writes(db) == before
