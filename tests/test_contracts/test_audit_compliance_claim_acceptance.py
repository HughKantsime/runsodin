"""Owned HTTP assertions for audit filters and advertised bulk actions; no devices."""
import csv
import io
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_audit_actor_requests import client as actor_client

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("enterprise_license")


@pytest.fixture
def audit_client(review_db, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.reporting.routes import exports
    from modules.jobs.routes import jobs_crud
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setattr(registry, '_providers', registry.providers | {'EducationPolicyProvider': EducationPolicyService()})
    app = FastAPI()
    app.include_router(exports.router)
    app.include_router(jobs_crud.router)
    principal = dict(id=3, username='owned-audit-review', role='admin', group_id=None,
                     is_active=True, _auth_kind='session_jwt')
    app.dependency_overrides[get_current_user] = lambda: principal
    def db():
        with Session(review_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = db
    with review_db.get_bind().begin() as conn:
        conn.execute(text('DELETE FROM audit_logs'))
        for identifier, action, kind, when in [(9101,'create','job','2026-10-10 10:00:00'),
                                              (9102,'update','job','2026-10-11 10:00:00'),
                                              (9103,'create','printer','2026-10-12 10:00:00')]:
            conn.execute(text('INSERT INTO audit_logs(id,timestamp,action,entity_type,entity_id,details,ip_address) '
                              'VALUES(:id,:when,:action,:kind,:id,:details,:ip)'),
                         dict(id=identifier, when=when, action=action, kind=kind,
                              details='{"marker":"owned,quoted"}', ip='192.0.2.17'))
        for identifier in (9201,9202):
            conn.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,priority) "
                              "VALUES(:id,'Owned ordinary batch','pending',1,1,FALSE,3)"), {'id':identifier})
    with TestClient(app) as client:
        yield client, principal


def test_audit_list_filters_dates_and_paginates_owned_rows(audit_client):
    client,_ = audit_client
    response = client.get('/audit-logs', params={'entity_type':'job','date_from':'2026-10-10',
                                                'date_to':'2026-10-11','limit':1,'offset':1})
    assert response.status_code == 200
    body=response.json()
    assert body['total']==2 and body['limit']==1 and body['offset']==1
    assert [row['id'] for row in body['logs']]==[9101]
    assert body['logs'][0]['ip_address']=='192.0.2.17'
    assert body['logs'][0]['timestamp'].startswith('2026-10-10')


def test_audit_csv_filters_and_preserves_quoted_details(audit_client):
    client,_ = audit_client
    response=client.get('/export/audit-logs',params={'entity_type':'job','action':'update'})
    assert response.status_code==200 and 'text/csv' in response.headers['content-type']
    rows=list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows)==1 and rows[0]['ID']=='9102'
    assert rows[0]['Action']=='update' and rows[0]['IP Address']=='192.0.2.17'
    assert 'owned,quoted' in rows[0]['Details']


@pytest.mark.parametrize('role,org',[('viewer',None),('operator',None),('admin',1)])
def test_audit_cross_tenant_history_is_platform_admin_only(audit_client,role,org):
    client,principal=audit_client
    principal.update(role=role,group_id=org)
    assert client.get('/audit-logs').status_code==403
    assert client.get('/export/audit-logs').status_code==403


@pytest.mark.parametrize('action', ['set_priority','cancel','delete'])
def test_ordinary_bulk_mutation_commits_expected_targets_and_audit(audit_client,review_db,action):
    client,_=audit_client
    response=client.post('/jobs/bulk-update',json={'job_ids':[9201,9202],'action':action,'priority':1})
    assert response.status_code==200 and response.json()['affected']==2
    with Session(review_db.get_bind()) as db:
        rows=db.execute(text('SELECT status,priority FROM jobs WHERE id IN (9201,9202)')).all()
        assert (len(rows)==0 if action=='delete' else
                all(row[0]=='cancelled' for row in rows) if action=='cancel' else
                all(row[1]==1 for row in rows))
        assert db.execute(text('SELECT COUNT(*) FROM audit_logs WHERE action=:action'),
                          {'action':'bulk_'+action}).scalar_one()==1


def test_claim_audit_mutation_records_authenticated_user_and_request_ip(actor_client, review_db, monkeypatch):
    from modules.reporting.routes import exports
    from modules.jobs.routes import jobs_crud
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setattr(registry, '_providers', registry.providers | {'EducationPolicyProvider': EducationPolicyService()})
    client, bearer, _, _ = actor_client
    client.app.include_router(exports.router)
    client.app.include_router(jobs_crud.router)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,hold,priority) VALUES(9201,'Owned actor batch','pending',1,FALSE,3)"))
    review_db.commit()
    headers = bearer('admin')
    response = client.post('/jobs/bulk-update', headers=headers, json={'job_ids':[9201],'action':'set_priority','priority':1})
    assert response.status_code == 200
    history = client.get('/audit-logs', headers=headers, params={'action':'bulk_set_priority'})
    assert history.status_code == 200
    row = history.json()['logs'][0]
    assert (row['user_id'], row['username'], row['auth_kind']) == (3, 'admin', 'bearer_jwt')
    assert row['ip_address'] == '198.51.100.7'


@pytest.mark.parametrize('ui_action',['set_priority','reschedule'])
def test_claim_current_bulk_toolbar_actions_are_accepted(audit_client,ui_action):
    client,_=audit_client
    response=client.post('/jobs/bulk-update',json={'job_ids':[9201,9202],'action':ui_action,'priority':1})
    assert response.status_code==200
