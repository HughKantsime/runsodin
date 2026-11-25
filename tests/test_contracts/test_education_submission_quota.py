"""A student spends one job from their print quota when review queues a submission."""
import io
import uuid
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.app_errors import register_error_handlers
from core.db import get_db
from core.dependencies import get_current_user
from core.quota import _get_period_key
from modules.organizations import education_review_service, routes_education_reviews
from modules.organizations.education_policy import reconcile_dispatch_denial, reconcile_schedule_denial
from modules.organizations.education_schemas import SubmissionRejection
from modules.organizations.education_submission_service import process_sliced_3mf_submission
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_education_classes import attach, setup
from tests.test_contracts.test_education_classes_edges import another_submission
from tests.test_contracts.test_education_classes_migration import native_engine, pg_engine
from tests.test_contracts.test_education_mapped_approval_endpoint import mapped_case
from tests.test_contracts.test_education_review_workflow import _principal, review_session
from tests.test_contracts.test_education_submission_upload import _ample_disk, _sliced_3mf


REFUSAL = "This student has used up their print quota for this period."
pytestmark = pytest.mark.parametrize('native_engine', [
    'sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql),
], indirect=True)


@pytest.fixture
def review_db(native_engine):
    with review_session(native_engine) as db:
        yield db


@pytest.fixture
def approve(review_db, tier_license):
    tier_license('education')
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(routes_education_reviews.router)
    app.dependency_overrides[get_current_user] = lambda: _principal(2)

    def database():
        with Session(review_db.get_bind()) as session:
            yield session

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        def post(submission=14, revision=1, command_id=None):
            return client.post(f'/education/submissions/{submission}/approve', json={
                'revision': revision, 'printer_id': 9, 'command_id': command_id or str(uuid.uuid4()),
            })
        yield post


def assert_refused(response):
    assert response.status_code == 429, response.text
    assert response.json() == {'detail': REFUSAL, 'error': {
        'code': 'quota_exceeded', 'detail': REFUSAL, 'retriable': False,
    }}


def usage(db):
    return db.execute(text('SELECT user_id,jobs_used,grams_used,hours_used FROM quota_usage ORDER BY user_id')).all()


def state(db, submission_id):
    return db.execute(text('SELECT s.status,j.status,s.lifecycle_revision FROM education_submissions s '
                           'JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'), {'id': submission_id}).one()


@pytest.mark.parametrize('reuse_command', [True, False], ids=['same-command', 'new-command'])
def test_refused_approval_rolls_back_and_can_be_retried(review_db, approve, reuse_command):
    db = review_db
    another_submission(db, 20)
    db.execute(text('UPDATE print_files SET compatibility_facts_json='
                    '(SELECT compatibility_facts_json FROM print_files WHERE id=11) WHERE id=20'))
    db.execute(text('UPDATE users SET quota_jobs=1 WHERE id=1'))
    db.commit()

    first_id = str(uuid.uuid4())
    first = approve(command_id=first_id)
    assert first.status_code == 200 and first.json()['status'] == 'pending'
    assert approve(command_id=first_id).json() == first.json()
    assert usage(db) == [(1, 1, 0, 0)]

    refused_id = str(uuid.uuid4())
    assert_refused(approve(20, command_id=refused_id))
    assert state(db, 20) == ('submitted', 'submitted', 1)
    assert usage(db) == [(1, 1, 0, 0)]
    assert db.execute(text("SELECT count(*) FROM education_audit_events WHERE action='submission.approve' "
                           "AND resource_id='20'")).scalar_one() == 0
    assert db.execute(text('SELECT count(*) FROM education_commands WHERE command_id=:cmd'),
                      {'cmd': refused_id}).scalar_one() == 0

    db.execute(text('UPDATE users SET quota_jobs=2 WHERE id=1'))
    db.commit()
    assert approve(20, command_id=refused_id if reuse_command else None).status_code == 200
    assert state(db, 20) == ('pending', 'pending', 2)
    assert usage(db) == [(1, 2, 0, 0)]


@pytest.mark.parametrize('grams,hours', [(20, 0), (0, 3)])
def test_current_period_material_or_time_limit(review_db, approve, grams, hours):
    db = review_db
    db.execute(text("UPDATE users SET quota_grams=20,quota_hours=3,quota_period='weekly' WHERE id=1"))
    db.execute(text('INSERT INTO quota_usage(user_id,period_key,jobs_used,grams_used,hours_used) '
                    'VALUES(1,:period,0,:grams,:hours)'),
               {'period': _get_period_key('weekly'), 'grams': grams, 'hours': hours})
    db.commit()
    assert_refused(approve())
    assert state(db, 14) == ('submitted', 'submitted', 1)
    assert usage(db) == [(1, 0, grams, hours)]


def test_unlimited_student_can_print_when_teacher_is_at_limit(review_db, approve):
    db = review_db
    db.execute(text('UPDATE users SET quota_jobs=1 WHERE id=2'))
    db.execute(text('INSERT INTO quota_usage(user_id,period_key,jobs_used) VALUES(2,:period,1)'),
               {'period': _get_period_key('monthly')})
    db.commit()
    assert approve().status_code == 200
    assert usage(db) == [(2, 1, 0, 0)]


@pytest.mark.parametrize('charged_to,submitter,owner', [(4, 1, 4), (None, 4, 4), (None, None, 1)])
def test_owner_precedence(review_db, approve, charged_to, submitter, owner):
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=:charged_to,submitted_by=:submitter WHERE id=13'),
                      {'charged_to': charged_to, 'submitter': submitter})
    review_db.execute(text('UPDATE users SET quota_jobs=1 WHERE id IN (1,4)'))
    review_db.commit()
    assert approve().status_code == 200
    assert usage(review_db) == [(owner, 1, 0, 0)]


def test_missing_job_quota_owner_allows_approval(review_db, approve):
    db = review_db
    db.execute(text('UPDATE jobs SET charged_to_user_id=4 WHERE id=13'))
    db.execute(text('DELETE FROM users WHERE id=4'))
    db.execute(text('UPDATE users SET quota_jobs=1 WHERE id IN (1,2)'))
    db.commit()
    assert approve().status_code == 200
    assert state(db, 14) == ('pending', 'pending', 2)
    assert usage(db) == []


@pytest.mark.parametrize('return_to_review,second_period', [
    (reconcile_dispatch_denial, '2026-10'),
    (reconcile_schedule_denial, '2026-11'),
], ids=['dispatch-same-month', 'scheduler-later-month'])
def test_return_to_review_only_charges_first_approval(review_db, approve, return_to_review, second_period):
    db = review_db
    db.execute(text("UPDATE users SET quota_jobs=1,quota_period='monthly' WHERE id=1"))
    db.commit()
    with patch('core.quota._get_period_key', return_value='2026-10') as period:
        assert approve().status_code == 200
        assert usage(db) == [(1, 1, 0, 0)]
        period.return_value = second_period
        assert return_to_review(db, submission_id=14, job_id=13, expected_revision=2,
                                reason='Printer compatibility changed')
        db.commit()
        assert state(db, 14) == ('submitted', 'submitted', 3)
        assert approve(revision=3).status_code == 200
    assert state(db, 14) == ('pending', 'pending', 4)
    assert db.execute(text('SELECT user_id,period_key,jobs_used FROM quota_usage')).all() == [(1, '2026-10', 1)]


def test_revised_upload_after_rejection_counts_as_new_job(review_db, approve, tmp_path, monkeypatch):
    db = review_db
    db.execute(text('UPDATE users SET quota_jobs=2 WHERE id=1'))
    db.commit()
    assert approve().status_code == 200
    assert reconcile_dispatch_denial(db, submission_id=14, job_id=13, expected_revision=2,
                                     reason='Printer compatibility changed')
    education_review_service.reject_submission(
        db, submission_id=14, principal=_principal(2),
        body=SubmissionRejection(revision=3, reason='Revise supports', command_id=uuid.uuid4()),
    )
    _ample_disk(monkeypatch)
    revised = process_sliced_3mf_submission(
        db, source=io.BytesIO(_sliced_3mf()), original_filename='revised.3mf',
        operation_id=str(uuid.uuid4()), principal=_principal(1), cost_center_id=7,
        storage_root=tmp_path / 'uploads',
    )
    assert revised['id'] != 14 and revised['job_id'] != 13
    assert usage(db) == [(1, 1, 0, 0)]
    assert approve(revised['id']).status_code == 200
    assert usage(db) == [(1, 2, 0, 0)]


def test_auto_approval_stops_at_quota_and_keeps_assignment(setup):
    db, _, assignment = setup
    db.execute(text('UPDATE users SET quota_jobs=1 WHERE id=1'))
    db.execute(text('INSERT INTO quota_usage(user_id,period_key,jobs_used) VALUES(1,:period,1)'),
               {'period': _get_period_key('monthly')})
    db.execute(text("UPDATE education_assignments SET auto_printer_ids='[9,10]',"
                    "auto_print_seconds=600,auto_material_grams=20 WHERE id=:id"), {'id': assignment['id']})
    db.commit()
    with patch.object(education_review_service, 'approve_submission',
                      wraps=education_review_service.approve_submission) as attempt:
        assert attach(db, assignment)['auto_approval'] == 'manual_review'
        attempt.assert_called_once()
    assert state(db, 14) == ('submitted', 'submitted', 2)
    assert db.execute(text('SELECT assignment_id,first_approved_at FROM education_assignment_submissions '
                           'WHERE submission_id=14')).one() == (assignment['id'], None)
    assert usage(db) == [(1, 1, 0, 0)]


def test_explicit_ams_refusal_and_replay(review_db, mapped_case):
    client, headers, body, _, _, uploaded = mapped_case
    path = f"/education/submissions/{uploaded['id']}/approve"
    review_db.execute(text('UPDATE users SET quota_jobs=1 WHERE id=1'))
    review_db.execute(text('INSERT INTO quota_usage(user_id,period_key,jobs_used) VALUES(1,:period,1)'),
                      {'period': _get_period_key('monthly')})
    review_db.commit()
    assert_refused(client.post(path, headers=headers, json=body))
    assert state(review_db, uploaded['id']) == ('submitted', 'submitted', 1)
    assert review_db.execute(text('SELECT ams_mapping FROM jobs WHERE id=:job'),
                             {'job': uploaded['job_id']}).scalar_one() is None
    assert usage(review_db) == [(1, 1, 0, 0)]
    review_db.execute(text('UPDATE users SET quota_jobs=2 WHERE id=1'))
    review_db.commit()
    approved = client.post(path, headers=headers, json=body)
    assert approved.status_code == 200 and approved.json()['ams_mapping'] == [0]
    assert client.post(path, headers=headers, json=body).json() == approved.json()
    assert usage(review_db) == [(1, 2, 0, 0)]
