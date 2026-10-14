"""Actual owned HTTP acceptance for returning queued work to the scheduler."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_audit_compliance_claim_acceptance import audit_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client


def test_reschedule_clears_assignment_without_erasing_history(audit_client, review_db):
    client, _ = audit_client
    review_db.execute(text("""UPDATE jobs SET status='scheduled',printer_id=9,
        scheduled_start='2026-10-20 10:00:00',scheduled_end='2026-10-20 11:00:00',
        actual_start='2026-10-01 10:00:00',actual_end='2026-10-01 11:00:00',
        match_score=99,is_locked=TRUE,hold=TRUE,quantity=7,priority=2,
        due_date='2026-10-25 10:00:00' WHERE id=9201"""))
    review_db.commit()
    response = client.post('/jobs/bulk-update', json={'job_ids': [9201, 9201], 'action': 'reschedule'})
    assert response.status_code == 200, response.text
    assert response.json()['affected'] == 1
    review_db.expire_all()
    row = review_db.execute(text("""SELECT status,printer_id,scheduled_start,scheduled_end,
        match_score,is_locked,hold,quantity,priority,actual_start,actual_end,due_date
        FROM jobs WHERE id=9201""")).one()
    assert tuple(row[:9]) == ('pending', None, None, None, None, 0, 1, 7, 2)
    assert str(row.actual_start).startswith('2026-10-01 10:00:00')
    assert str(row.actual_end).startswith('2026-10-01 11:00:00')
    assert str(row.due_date).startswith('2026-10-25 10:00:00')


@pytest.mark.parametrize('state', ['printing', 'paused', 'submitted', 'rejected', 'completed', 'failed', 'cancelled'])
def test_reschedule_never_resets_nonqueued_work(audit_client, review_db, state):
    client, _ = audit_client
    review_db.execute(text('UPDATE jobs SET status=:state,printer_id=9 WHERE id=9201'), {'state': state})
    review_db.commit()
    response = client.post('/jobs/bulk-update', json={'job_ids': [9201], 'action': 'reschedule'})
    assert response.status_code == 200, response.text
    assert response.json()['affected'] == 0
    assert tuple(review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=9201')).one()) == (state, 9)


def test_reschedule_noop_does_not_count_as_a_change(audit_client):
    client, _ = audit_client
    response = client.post('/jobs/bulk-update', json={'job_ids': [9201], 'action': 'reschedule'})
    assert response.status_code == 200, response.text
    assert response.json()['affected'] == 0


def test_reschedule_retains_preparing_dispatch_fence(audit_client, review_db):
    from core import dispatch_claims
    client, _ = audit_client
    review_db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9 WHERE id=9201"))
    review_db.commit()
    claim = dispatch_claims.acquire(review_db.get_bind(), 9, 9201, 'a' * 64, 'b' * 64, 'c' * 64)
    assert claim.status == 'acquired'
    response = client.post('/jobs/bulk-update', json={'job_ids': [9201], 'action': 'reschedule'})
    assert response.status_code == 200 and response.json()['affected'] == 0
    assert response.json()['skipped'] == 1
    assert tuple(review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=9201')).one()) == ('scheduled', 9)
    assert review_db.execute(text('SELECT token FROM printer_dispatch_claims WHERE job_id=9201')).scalar_one() == claim.token


def test_mixed_education_batch_leaves_generic_assignment_unchanged(audit_client, review_db):
    client, _ = audit_client
    review_db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9 WHERE id=9201"))
    review_db.commit()
    response = client.post('/jobs/bulk-update', json={'job_ids': [9201, 13], 'action': 'reschedule'})
    assert response.status_code == 409
    assert tuple(review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=9201')).one()) == ('scheduled', 9)


def test_foreign_and_missing_ids_are_unchanged_without_disclosing_which(audit_client, review_db):
    client, principal = audit_client
    principal.update(group_id=1)
    review_db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9,charged_to_org_id=2 WHERE id=9201"))
    review_db.commit()
    response = client.post('/jobs/bulk-update', json={'job_ids': [9201, 999999], 'action': 'reschedule'})
    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'affected': 0, 'skipped': 2}
    assert tuple(review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=9201')).one()) == ('scheduled', 9)


def test_reschedule_rolls_back_if_audit_cannot_be_written(audit_client, review_db, monkeypatch):
    from modules.jobs.routes import jobs_crud
    client, _ = audit_client
    review_db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9 WHERE id=9201"))
    review_db.commit()
    def fail(*args, **kwargs):
        raise RuntimeError('Owned injected audit failure')
    monkeypatch.setattr(jobs_crud, 'log_audit', fail)
    with pytest.raises(RuntimeError, match='Owned injected audit failure'):
        client.post('/jobs/bulk-update', json={'job_ids': [9201], 'action': 'reschedule'})
    assert tuple(review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=9201')).one()) == ('scheduled', 9)


@pytest.mark.parametrize('scopes,expected', [(['read:jobs'], 403), (['write:models'], 403), (['write:jobs'], 200)])
def test_reschedule_uses_actual_hashed_token_job_write_authority(token_client, review_db, monkeypatch, scopes, expected):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, issue = token_client
    client.app.include_router(jobs_crud.router)
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,priority,printer_id) "
                           "VALUES(9201,'Owned scoped reschedule','scheduled',1,1,FALSE,3,9)"))
    review_db.commit()
    response = client.post('/jobs/bulk-update', headers=issue(scopes),
                           json={'job_ids': [9201], 'action': 'reschedule'})
    assert response.status_code == expected
    row = review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=9201')).one()
    assert tuple(row) == (('pending', None) if expected == 200 else ('scheduled', 9))
