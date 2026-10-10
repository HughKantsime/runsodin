"""Actual route boundaries keep generic operations out of EDU authority."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def client(review_db, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_agent, jobs_crud, jobs_lifecycle
    monkeypatch.setattr(registry, '_providers', registry.providers | {'EducationPolicyProvider': EducationPolicyService()})
    monkeypatch.setattr(jobs_agent, 'require_feature', lambda _: None)
    monkeypatch.setattr(jobs_lifecycle, 'require_feature', lambda _: None)
    app = FastAPI()
    for module in (jobs_agent, jobs_crud, jobs_lifecycle):
        app.include_router(module.router)
    def request_db():
        with Session(review_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {'id':3,'username':'admin','role':'admin','group_id':1,'is_active':True,'_auth_kind':'session_jwt'}
    return TestClient(app)


CASES = [('POST', '/jobs/13/'+action, {'reason':'test denial'}) for action in ('start','complete','fail','reset','cancel','approve','reject','resubmit','repeat')]
CASES += [('POST','/jobs/13/link-print?print_job_id=1',{}), ('PATCH','/jobs/13',{'priority':1}),
    ('PATCH','/jobs/13/move',{'printer_id':9,'scheduled_start':'2026-10-12T10:00:00Z'}),
    ('PATCH','/jobs/13/failure',{'failure_reason':'other'}), ('DELETE','/jobs/13',None),
    ('PATCH','/jobs/reorder',{'job_ids':[13]})]
CASES += [('POST','/jobs/bulk-update',{'job_ids':[13],'action':action}) for action in ('cancel','delete','hold','unhold','set_priority')]


@pytest.mark.parametrize('method,path,body', CASES)
def test_education_generic_mutation_denied_without_state_change(client, review_db, method, path, body):
    before = review_db.execute(text('SELECT * FROM jobs WHERE id=13')).one()
    submission = review_db.execute(text('SELECT * FROM education_submissions WHERE job_id=13')).one()
    response = client.request(method, path, json=body)
    assert response.status_code == 409, response.text
    assert 'Education' in response.json()['detail']
    review_db.expire_all()
    assert review_db.execute(text('SELECT * FROM jobs WHERE id=13')).one() == before
    assert review_db.execute(text('SELECT * FROM education_submissions WHERE job_id=13')).one() == submission
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == 1


def test_generic_reset_retains_behavior(client, review_db):
    review_db.execute(text("INSERT INTO jobs (id,item_name,status,charged_to_org_id,quantity,hold) VALUES (20,'Generic','scheduled',1,1,0)"))
    review_db.commit()
    response = client.post('/jobs/20/reset')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'pending'


def test_missing_policy_fails_closed(client, monkeypatch):
    from core.registry import registry
    monkeypatch.setattr(registry, '_providers', {})
    assert client.post('/jobs/13/reset').status_code == 503


@pytest.mark.parametrize('path,body', [('/jobs',{'item_name':'Bypass','model_id':12}),
    ('/jobs/bulk',[{'item_name':'Bypass','model_id':12}]),
    ('/jobs/batch',{'item_name':'Bypass','model_id':12,'printer_ids':[9]})])
def test_education_model_cannot_create_generic_job(client, review_db, path, body):
    response = client.post(path, json=body)
    assert response.status_code == 409, response.text
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == 1


def test_generic_job_cannot_attach_education_model(client, review_db):
    review_db.execute(text("INSERT INTO jobs (id,item_name,status,charged_to_org_id,quantity,hold) VALUES (20,'Generic','pending',1,1,0)"))
    review_db.commit()
    assert client.patch('/jobs/20', json={'model_id':12}).status_code == 409
    assert review_db.execute(text('SELECT model_id FROM jobs WHERE id=20')).scalar_one() is None


@pytest.mark.parametrize('path,body', [('/jobs/bulk-update',{'job_ids':[20,13],'action':'set_priority','priority':1}), ('/jobs/reorder',{'job_ids':[20,13]})])
def test_mixed_batch_rolls_back_preceding_generic_write(client, review_db, path, body):
    review_db.execute(text("INSERT INTO jobs (id,item_name,status,charged_to_org_id,quantity,hold,priority,queue_position) VALUES (20,'Generic','pending',1,1,0,3,7)"))
    review_db.commit()
    response = client.request('PATCH' if path.endswith('reorder') else 'POST', path, json=body)
    assert response.status_code == 409
    with Session(review_db.get_bind()) as fresh:
        assert tuple(fresh.execute(text('SELECT priority,queue_position FROM jobs WHERE id=20')).one()) == (3,7)


def test_alias_of_education_print_file_is_also_guarded(client, review_db):
    review_db.execute(text("INSERT INTO models (id,name,print_file_id,category,org_id) VALUES (25,'Alias',11,'Custom',1)"))
    review_db.commit()
    assert client.post('/jobs',json={'item_name':'Alias reuse','model_id':25}).status_code == 409


def test_preexisting_generic_reference_cannot_be_repeated(client, review_db):
    review_db.execute(text("INSERT INTO jobs (id,item_name,status,charged_to_org_id,quantity,hold,model_id) VALUES (20,'Legacy bypass','pending',1,1,0,12)"))
    review_db.commit()
    assert client.post('/jobs/20/repeat').status_code == 409
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one() == 2


def test_preexisting_generic_reference_cannot_dispatch(review_db, monkeypatch):
    from core import db as core_db
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers import dispatch
    monkeypatch.setattr(core_db,'engine',review_db.get_bind())
    monkeypatch.setattr(registry,'_providers',registry.providers | {'EducationPolicyProvider':EducationPolicyService()})
    allowed = dispatch._authorize_education_hardware_action({'model_id':12,'education_submission_id':None}, {})
    assert allowed[0] is False
    assert 'no hardware command' in allowed[1]
