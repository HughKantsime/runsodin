"""Advertised original-student correction through the generic jobs API."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_user_limits import signed_license
import pytest


@pytest.mark.parametrize('mode,expected', [('own',200),('foreign',403),('foreign_org',404),('pending',403),('unknown_field',403),('printer',403),('null_name',422),('null_quantity',422),('changed_state',409),('claimed',409),('api_read',403),('api_write',200),('correct_and_resubmit',200),('resubmit_read',403),('resubmit_write',200)])
def test_original_submitter_can_correct_own_rejected_job(review_db, signed_license, monkeypatch, mode, expected):
    from core import auth
    from core.db import get_db
    from modules.jobs.routes import jobs_crud
    from modules.jobs.routes import jobs_lifecycle
    from modules.jobs.models import Job
    from core.base import JobStatus
    from core.economic_identity import _add_created
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    signed_license(tier='education', features=[])
    from modules.notifications import alert_dispatcher
    monkeypatch.setattr(alert_dispatcher, 'dispatch_alert', lambda **kwargs: None)

    monkeypatch.setattr(auth, 'SECRET_KEY', 'owned-rejected-edit-claim-key-only')
    job = Job(item_name='Owned rejected fixture', status=JobStatus.PENDING if mode == 'pending' else JobStatus.REJECTED,
              submitted_by=4 if mode == 'foreign' else 1, charged_to_org_id=2 if mode == 'foreign_org' else 1, quantity=1)
    _add_created(review_db, job, 'job', owner_org_id=job.charged_to_org_id)
    review_db.commit()
    if mode == 'claimed':
        from sqlalchemy import insert
        from core.dispatch_claim_schema import PrinterDispatchClaim
        review_db.execute(insert(PrinterDispatchClaim.__table__).values(printer_id=9,job_id=job.id,physical_key='a'*64,configuration_key='b'*64,payload_sha256='c'*64,owner_org_id=1,state='preparing',token='d'*32))
        review_db.commit()
    assert review_db.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'), {'id':job.id}).scalar_one()
    app = FastAPI()
    app.include_router(jobs_crud.router)
    app.include_router(jobs_lifecycle.router)

    def database():
        with Session(review_db.get_bind()) as db:
            yield db

    app.dependency_overrides[get_db] = database
    token = auth.create_access_token({'sub':'student','role':'viewer'})
    headers = {'Authorization':'Bearer '+token}
    if mode in ('api_read', 'api_write', 'resubmit_read', 'resubmit_write'):
        import json
        monkeypatch.delenv('API_KEY', raising=False)
        raw = 'odin_owned_rejected_correction_fixture'
        review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(1,'Owned correction key',:hash,:prefix,:scopes)"),
                          {'hash':auth.hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(['read:jobs' if mode in ('api_read','resubmit_read') else 'write:jobs'])})
        review_db.commit()
        headers = {'X-API-Key':raw}
    body = {'notes':'Corrected before resubmitting'}
    if mode == 'unknown_field': body['invented_privileged_field'] = True
    if mode == 'printer': body['printer_id'] = 1
    if mode == 'null_name': body['item_name'] = None
    if mode == 'null_quantity': body['quantity'] = None
    if mode == 'changed_state':
        original = jobs_crud.prepare_job_write
        def change_before_lock(db, ids, changes):
            result = original(db, ids, changes)
            db.execute(text("UPDATE jobs SET status='scheduled' WHERE id=:id"), {'id':job.id})
            return result
        monkeypatch.setattr(jobs_crud, 'prepare_job_write', change_before_lock)
    with TestClient(app) as client:
        response = client.post(f'/jobs/{job.id}/resubmit', headers=headers) if mode.startswith('resubmit_') else client.patch(f'/jobs/{job.id}', json=body, headers=headers)
        assert response.status_code == expected
        if mode == 'correct_and_resubmit':
            with Session(review_db.get_bind()) as observer:
                assert observer.get(Job, job.id).status == JobStatus.REJECTED
            assert client.post(f'/jobs/{job.id}/resubmit', headers=headers).status_code == 200
    with Session(review_db.get_bind()) as fresh:
        persisted = fresh.query(Job).filter(Job.id == job.id).one()
        expected_status = JobStatus.SUBMITTED if mode in ('correct_and_resubmit','resubmit_write') else JobStatus.PENDING if mode == 'pending' else JobStatus.REJECTED
        assert persisted.status == expected_status
        assert persisted.notes == ('Corrected before resubmitting' if expected == 200 and not mode.startswith('resubmit_') else None)
