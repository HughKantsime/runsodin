"""Sensitive system APIs enforce scope and superadmin through actual auth."""
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db,interactive_headers

@pytest.fixture
def system_client(token_client,review_db,monkeypatch,tmp_path):
    from modules.system import routes_backup,routes_config
    client,issue=token_client
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'));review_db.commit()
    for router in (routes_backup.router,routes_config.router):client.app.include_router(router)
    calls=[];dummy=tmp_path/'synthetic-backup.db';dummy.write_bytes(b'Owned synthetic boundary fixture')
    def create(*args,**kwargs):calls.append('backup_create');return dummy,{'size_bytes':dummy.stat().st_size}
    def stage(*args,**kwargs):calls.append('backup_stage');return {'candidate_sha256':'synthetic'}
    def path(*args,**kwargs):calls.append('backup_path');return dummy
    monkeypatch.setattr(routes_backup,'create_online_backup',create)
    monkeypatch.setattr(routes_backup,'stage_restore',stage)
    monkeypatch.setattr(routes_backup,'_safe_backup_path',path)
    def paths():calls.append('backup_list');return SimpleNamespace(backups=tmp_path)
    monkeypatch.setattr(routes_backup,'_backup_paths',paths)
    monkeypatch.setattr(routes_backup,'sql',SimpleNamespace(is_postgres=False))
    def invalidate():calls.append('mqtt_invalidate')
    def connect(**kwargs):calls.append('mqtt_test');return {'success':True}
    monkeypatch.setattr(routes_config,'mqtt_republish',SimpleNamespace(invalidate_cache=invalidate,test_connection=connect))
    return client,issue,calls,dummy


def operation(client,headers,kind):
    if kind=='backup_list':return client.get('/backups',headers=headers)
    if kind=='mqtt_read':return client.get('/config/mqtt-republish',headers=headers)
    if kind=='backup_create':return client.post('/backups',headers=headers)
    if kind=='backup_download':return client.get('/backups/synthetic-backup.db',headers=headers)
    if kind=='backup_delete':return client.delete('/backups/synthetic-backup.db',headers=headers)
    if kind=='backup_restore':return client.post('/backups/restore',headers=headers,files={'file':('synthetic.db',b'Synthetic uploaded boundary fixture','application/octet-stream')})
    if kind=='mqtt_update':return client.put('/config/mqtt-republish',headers=headers,json={'enabled':True,'host':'broker.example.test'})
    return client.post('/config/mqtt-republish/test',headers=headers,json={'host':'broker.example.test'})


def db_state(db):
    with Session(db.get_bind()) as fresh:
        return (tuple(fresh.execute(text('SELECT key,value FROM system_config ORDER BY key')).all()),fresh.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one())

@pytest.mark.parametrize('kind',['backup_list','backup_create','backup_download','backup_delete','backup_restore','mqtt_read','mqtt_update','mqtt_test'])
@pytest.mark.parametrize('scope',['read:printers','write:jobs','read','write','agent:write'])
def test_narrow_superadmin_token_cannot_access_sensitive_system_operation(system_client,review_db,kind,scope):
    client,issue,calls,dummy=system_client;headers=issue([scope]);before=db_state(review_db)
    response=operation(client,headers,kind)
    assert response.status_code==403
    assert calls==[]
    assert dummy.exists()
    unchanged=db_state(review_db)==before
    assert unchanged

@pytest.mark.parametrize('kind',['backup_list','backup_create','backup_download','backup_delete','backup_restore','mqtt_read','mqtt_update','mqtt_test'])
@pytest.mark.parametrize('credential',['admin_token','interactive'])
def test_authorized_superadmin_reaches_substituted_system_boundary(system_client,review_db,kind,credential):
    client,issue,calls,dummy=system_client
    headers=issue(['admin']) if credential=='admin_token' else interactive_headers()
    response=operation(client,headers,kind)
    assert response.status_code==(204 if kind=='backup_delete' else 200)
    assert len(calls)==(0 if kind=='mqtt_read' else 1)
    if kind=='mqtt_update':
        with Session(review_db.get_bind()) as fresh:assert fresh.execute(text("SELECT value FROM system_config WHERE key='mqtt_republish_host'")).scalar_one()=='broker.example.test'

@pytest.mark.parametrize('kind',['backup_list','backup_create','backup_download','backup_delete','backup_restore','mqtt_read','mqtt_update','mqtt_test'])
def test_admin_scope_does_not_bypass_superadmin_tenant_floor(system_client,review_db,kind):
    client,issue,calls,dummy=system_client
    review_db.execute(text('UPDATE users SET group_id=1 WHERE id=3'));review_db.commit()
    response=operation(client,issue(['admin']),kind)
    assert response.status_code==403
    assert calls==[]
    assert dummy.exists()

def test_parameterless_superadmin_needs_the_generic_method_scope(system_client):
    # F11-11.6-01/02: a superadmin route that names no scope needs the method's generic
    # scope ("read" for GET); a token scoped to an unrelated resource is refused.
    from fastapi import Depends
    from core.rbac import require_superadmin
    client,issue,_,_=system_client
    @client.app.get('/legacy-superadmin')
    def legacy(current_user=Depends(require_superadmin())):return {'accepted':True}
    assert client.get('/legacy-superadmin',headers=issue(['read:printers'])).status_code==403
    assert client.get('/legacy-superadmin',headers=issue(['read'])).status_code==200
