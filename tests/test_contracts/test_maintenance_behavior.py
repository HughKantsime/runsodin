"""Actual maintenance HTTP authorization and service-hour lifecycle."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def maintenance_client(review_db):
    from sqlalchemy import event
    def enforce_fk(connection, record, proxy):
        cursor=connection.cursor();cursor.execute("PRAGMA foreign_keys=ON");cursor.close()
    engine=review_db.get_bind()
    event.listen(engine,"checkout",enforce_fk)
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import routes_maintenance
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,is_active,total_print_hours) VALUES(11,'Foreign maintenance','bambu',2,1,100),(12,'Shared maintenance','bambu',2,1,20)"))
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=12'))
    review_db.execute(text('UPDATE printers SET total_print_hours=100 WHERE id=9'))
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,interval_print_hours,is_active) VALUES(101,'Synthetic service',10,1)"))
    review_db.execute(text("INSERT INTO maintenance_logs(id,printer_id,task_id,task_name,notes,print_hours_at_service) VALUES(101,9,101,'Own service','own',90),(102,11,101,'Foreign service','foreign',90),(103,12,101,'Shared service','shared',10)"));review_db.commit()
    app=FastAPI();app.include_router(routes_maintenance.router,prefix="/api/v1")
    from core.middleware.dry_run import dry_run_middleware
    app.middleware("http")(dry_run_middleware)
    from core.errors import OdinError
    from fastapi.responses import JSONResponse
    @app.exception_handler(OdinError)
    async def structured_error(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())
    def request_db():
        with Session(review_db.get_bind()) as db:yield db
    user={'id':3,'group_id':1,'role':'admin','is_active':True}
    app.dependency_overrides[get_db]=request_db
    app.dependency_overrides[get_current_user]=lambda:user
    try:
        yield TestClient(app,raise_server_exceptions=False),user
    finally:
        event.remove(engine,"checkout",enforce_fk)


def test_maintenance_lists_and_status_scope(maintenance_client):
    client,user=maintenance_client
    logs=client.get('/api/v1/maintenance/logs');assert logs.status_code==200,logs.text
    assert {r['id'] for r in logs.json()}=={101,103}
    response=client.get('/api/v1/maintenance/status');assert response.status_code==200,response.text
    identifiers={r['printer_id'] for r in response.json()}
    assert 9 in identifiers and 12 in identifiers and 11 not in identifiers
    response=client.get('/api/v1/maintenance/logs?printer_id=11')
    assert response.status_code==200 and response.json()==[]
    user.update(role='admin',group_id=None)
    assert {r['id'] for r in client.get('/api/v1/maintenance/logs').json()}=={101,102,103}


@pytest.mark.parametrize('dry',[False,True])
def test_foreign_maintenance_create_denied_before_preview_or_write(dry,maintenance_client,review_db):
    client,_=maintenance_client
    before=review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':11,'task_id':101,'task_name':'Denied service'},headers={'X-Dry-Run':'true'} if dry else {})
    assert response.status_code==404,response.text
    assert 'Foreign maintenance' not in response.text
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()==before


def test_foreign_maintenance_delete_denied(maintenance_client,review_db):
    client,_=maintenance_client
    response=client.delete('/api/v1/maintenance/logs/102')
    assert response.status_code==404,response.text
    assert review_db.execute(text('SELECT notes FROM maintenance_logs WHERE id=102')).scalar_one()=='foreign'


def test_service_uses_printer_counter_and_resets_status(maintenance_client,review_db):
    client,_=maintenance_client
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':9,'task_id':101,'task_name':'Synthetic service'})
    assert response.status_code==200,response.text
    assert response.json()['print_hours_at_service']==100
    state=next(p for p in client.get('/api/v1/maintenance/status').json() if p['printer_id']==9)
    task=next(t for t in state['tasks'] if t['task_id']==101)
    assert task['hours_since_service']==0 and task['status']=='ok'
    review_db.execute(text('UPDATE printers SET total_print_hours=105 WHERE id=9'));review_db.commit()
    state=next(p for p in client.get('/api/v1/maintenance/status').json() if p['printer_id']==9)
    task=next(t for t in state['tasks'] if t['task_id']==101)
    assert task['hours_since_service']==5 and task['progress_percent']==50


def test_maintenance_shared_and_unassigned_access(maintenance_client,review_db):
    client,user=maintenance_client
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':12,'task_id':101,'task_name':'Shared service'})
    assert response.status_code==200,response.text
    user.update(group_id=None,role='operator')
    assert {r['printer_id'] for r in client.get('/api/v1/maintenance/logs').json()}=={12}
    before=review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':9,'task_id':101,'task_name':'Denied'})
    assert response.status_code==404,response.text
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()==before


def test_maintenance_own_dry_run_zero_writes(maintenance_client,review_db):
    client,_=maintenance_client
    before=review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':9,'task_id':101,'task_name':'Preview'},headers={'X-Dry-Run':'true'})
    assert response.status_code==200,response.text
    assert response.json()['dry_run'] is True
    assert response.json()['would_execute']['print_hours_at_service']==100
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()==before


def test_invalid_task_and_history_preservation(maintenance_client,review_db):
    client,_=maintenance_client
    before=review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':9,'task_id':999,'task_name':'Invalid'})
    assert response.status_code==404,response.text
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()==before
    assert client.delete('/api/v1/maintenance/tasks/101').status_code==204
    rows=review_db.execute(text('SELECT task_id,task_name FROM maintenance_logs ORDER BY id')).all()
    assert [r.task_id for r in rows]==[None,None,None]
    assert [r.task_name for r in rows]==['Own service','Foreign service','Shared service']


def test_maintenance_role_and_token_write_denials(maintenance_client,review_db):
    client,user=maintenance_client
    user['role']='viewer'
    assert client.post('/api/v1/maintenance/logs',json={'printer_id':9,'task_name':'Denied'}).status_code==403
    user.update(role='admin',_token_scopes=['read'])
    assert client.delete('/api/v1/maintenance/logs/101').status_code==403
    assert client.delete('/api/v1/maintenance/tasks/101').status_code==403
    assert client.post('/api/v1/maintenance/tasks',json={'name':'Denied'}).status_code==403
    assert client.patch('/api/v1/maintenance/tasks/101',json={'name':'Denied'}).status_code==403
    assert review_db.execute(text('SELECT name FROM maintenance_tasks WHERE id=101')).scalar_one()=='Synthetic service'
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()==3


def test_counter_reset_and_same_timestamp_service_tie(maintenance_client,review_db):
    client,_=maintenance_client
    review_db.execute(text("INSERT INTO maintenance_logs(id,printer_id,task_id,task_name,performed_at,print_hours_at_service) VALUES(104,9,101,'Later service','2026-10-10 00:00:00',100),(105,9,101,'Newest same second','2026-10-10 00:00:00',110)"))
    review_db.execute(text("UPDATE maintenance_logs SET performed_at='2026-10-09 00:00:00' WHERE id=101"))
    review_db.execute(text('UPDATE printers SET total_print_hours=115 WHERE id=9'));review_db.commit()
    task=next(t for p in client.get('/api/v1/maintenance/status').json() if p['printer_id']==9 for t in p['tasks'] if t['task_id']==101)
    assert task['hours_since_service']==5
    review_db.execute(text('UPDATE printers SET total_print_hours=5 WHERE id=9'));review_db.commit()
    task=next(t for p in client.get('/api/v1/maintenance/status').json() if p['printer_id']==9 for t in p['tasks'] if t['task_id']==101)
    assert task['hours_since_service']==0 and task['progress_percent']==0


@pytest.mark.parametrize('printer_id',[11,999])
def test_maintenance_agent_error_envelope(printer_id,maintenance_client,review_db):
    client,_=maintenance_client
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':printer_id,'task_name':'Denied'})
    assert response.status_code==404,response.text
    assert response.json()['error']=={'code':'printer_not_found','detail':'Printer not found','retriable':False}
    assert 'Foreign maintenance' not in response.text
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()==3


def test_global_template_crud_and_owned_log_delete(maintenance_client,review_db):
    client,_=maintenance_client
    response=client.post('/api/v1/maintenance/tasks',json={'name':'Extra synthetic service','interval_days':30,'estimated_cost':12})
    assert response.status_code==200,response.text
    identifier=response.json()['id']
    response=client.patch(f'/api/v1/maintenance/tasks/{identifier}',json={'interval_days':14,'description':'Synthetic description'})
    assert response.status_code==200,response.text
    task=next(t for t in client.get('/api/v1/maintenance/tasks').json() if t['id']==identifier)
    assert task['interval_days']==14 and task['estimated_cost']==12 and task['description']=='Synthetic description'
    assert client.delete('/api/v1/maintenance/logs/101').status_code==204
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs WHERE id=101')).scalar_one()==0
    assert client.delete(f'/api/v1/maintenance/tasks/{identifier}').status_code==204
