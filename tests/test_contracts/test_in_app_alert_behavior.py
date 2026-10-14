"""Real alert HTTP persistence, ownership and token-scope acceptance."""
from datetime import datetime, timedelta
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def alert_client(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.errors import OdinError
    from core.base import AlertType, AlertSeverity
    from modules.notifications.models import Alert
    from modules.notifications.routes import alerts
    app=FastAPI();app.include_router(alerts.router)
    @app.exception_handler(OdinError)
    async def error(request,exc):return JSONResponse(status_code=exc.status,content=exc.to_envelope())
    def db_session():
        with Session(review_db.get_bind()) as db:yield db
    user={'id':3,'role':'admin','group_id':1,'is_active':True}
    app.dependency_overrides[get_db]=db_session
    app.dependency_overrides[get_current_user]=lambda:user
    now=datetime(2026,10,11,12)
    for id,uid,kind,severity,read in [
        (701,3,AlertType.PRINT_COMPLETE,AlertSeverity.INFO,False),
        (702,3,AlertType.PRINT_FAILED,AlertSeverity.CRITICAL,False),
        (703,3,AlertType.SPOOL_LOW,AlertSeverity.WARNING,True),
        (704,5,AlertType.PRINT_FAILED,AlertSeverity.CRITICAL,False),
    ]:
        review_db.add(Alert(id=id,user_id=uid,alert_type=kind,severity=severity,title=f'Synthetic alert {id}',is_read=read,created_at=now+timedelta(seconds=id)))
    review_db.commit()
    with TestClient(app) as client:yield client,user

@pytest.mark.parametrize('query,ids',[
    ('',[703,702,701]),('?severity=critical',[702]),('?alert_type=print_complete',[701]),
    ('?is_read=false',[702,701]),('?is_read=true',[703]),('?limit=1&offset=1',[702]),
    ('?severity=critical&is_read=true',[]),
])
def test_list_filters_pagination_order_and_ownership(alert_client,query,ids):
    client,user=alert_client
    response=client.get('/alerts'+query)
    assert response.status_code==200
    assert [row['id'] for row in response.json()]==ids


def test_mark_read_is_persisted_idempotent_and_updates_count(alert_client,review_db):
    client,user=alert_client
    assert client.get('/alerts/unread-count').json()=={'unread_count':2}
    for _ in range(2):assert client.patch('/alerts/701/read').status_code==200
    assert review_db.execute(text('SELECT is_read FROM alerts WHERE id=701')).scalar_one()==1
    assert client.get('/alerts/unread-count').json()=={'unread_count':1}
    assert client.patch('/alerts/704/read').status_code==404
    assert review_db.execute(text('SELECT is_read FROM alerts WHERE id=704')).scalar_one()==0


def test_mark_all_read_changes_only_current_user(alert_client,review_db):
    client,user=alert_client
    assert client.post('/alerts/mark-all-read').status_code==200
    assert client.get('/alerts/unread-count').json()=={'unread_count':0}
    assert review_db.execute(text('SELECT is_read FROM alerts WHERE id=704')).scalar_one()==0

@pytest.mark.parametrize('operation',['single','all'])
def test_read_only_token_cannot_mark_alerts_read(alert_client,review_db,operation):
    client,user=alert_client;user['_token_scopes']=['agent:read']
    response=client.patch('/alerts/701/read') if operation=='single' else client.post('/alerts/mark-all-read')
    assert response.status_code==403
    assert review_db.execute(text('SELECT is_read FROM alerts WHERE id=701')).scalar_one()==0


def test_operator_can_delete_own_alert_without_deleting_foreign_alert(alert_client,review_db):
    client,user=alert_client;user['role']='operator'
    assert client.delete('/alerts/701').status_code==200
    assert review_db.execute(text('SELECT COUNT(*) FROM alerts WHERE id=701')).scalar_one()==0
    assert client.delete('/alerts/704').status_code==404
    assert review_db.execute(text('SELECT COUNT(*) FROM alerts WHERE id=704')).scalar_one()==1

@pytest.mark.parametrize('role,scopes,allowed',[
    ('operator',None,True),('admin',None,True),('viewer',None,False),
    ('operator',['agent:write'],True),('admin',['admin'],True),
    ('admin',['agent:read'],False),('admin',['read'],False),('admin',['printers:write'],False),
])
def test_delete_enforces_role_and_scope_and_repeat_is_not_found(alert_client,review_db,role,scopes,allowed):
    client,user=alert_client;user['role']=role
    if scopes is not None:user['_token_scopes']=scopes
    response=client.delete('/alerts/701')
    assert response.status_code==(200 if allowed else 403)
    assert review_db.execute(text('SELECT COUNT(*) FROM alerts WHERE id=701')).scalar_one()==int(not allowed)
    if allowed:assert client.delete('/alerts/701').status_code==404

@pytest.mark.parametrize('scopes',[['read'],['printers:write'],['agent:read']])
def test_mark_all_read_denies_unrelated_scopes_without_mutation(alert_client,review_db,scopes):
    client,user=alert_client;user['_token_scopes']=scopes
    assert client.post('/alerts/mark-all-read').status_code==403
    assert review_db.execute(text('SELECT is_read FROM alerts WHERE id=701')).scalar_one()==0
    assert review_db.execute(text('SELECT is_read FROM alerts WHERE id=702')).scalar_one()==0
