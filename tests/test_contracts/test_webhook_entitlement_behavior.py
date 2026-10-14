"""Real signed entitlement and HTTP enforcement, without remote provider requests."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_user_limits import signed_license

@pytest.mark.parametrize('tier,allowed',[('community',False),('pro',True),('education',True),('enterprise',True)])
def test_webhook_create_requires_signed_entitlement(review_db,signed_license,monkeypatch,tier,allowed):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications.routes import webhooks
    signed_license(tier=tier,features=[])
    app=FastAPI();app.include_router(webhooks.router)
    app.dependency_overrides[get_db]=lambda:review_db
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'username':'Synthetic admin','role':'admin','group_id':None}
    monkeypatch.setattr(webhooks,'_validate_webhook_url',lambda url:None)
    monkeypatch.setattr(webhooks,'_encrypt_webhook_url',lambda url:'synthetic-encrypted-value')
    before=review_db.execute(text('SELECT COUNT(*) FROM webhooks')).scalar_one()
    with TestClient(app) as client:
        response=client.post('/webhooks',json={'name':'Synthetic hook','url':'https://synthetic.invalid/hook','alert_types':['print_complete']})
    assert response.status_code==(200 if allowed else 403),response.text
    assert review_db.execute(text('SELECT COUNT(*) FROM webhooks')).scalar_one()==before+int(allowed)
    if allowed:
        row=review_db.execute(text("SELECT name,url,alert_types FROM webhooks WHERE name='Synthetic hook'")).one()
        assert row.name=='Synthetic hook' and row.url=='synthetic-encrypted-value'
        assert row.alert_types=='["print_complete"]'

@pytest.fixture
def webhook_client(review_db,monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications.routes import webhooks
    identity={'id':3,'username':'Synthetic admin','role':'admin','group_id':None}
    review_db.execute(text("INSERT INTO webhooks(id,name,url,webhook_type,is_enabled) VALUES(501,'Synthetic hook','https://synthetic.invalid/hook','generic',1)"));review_db.commit()
    app=FastAPI();app.include_router(webhooks.router)
    app.dependency_overrides[get_db]=lambda:review_db
    app.dependency_overrides[get_current_user]=lambda:identity
    calls=[]
    monkeypatch.setattr(webhooks,'_validate_webhook_url',lambda url:None)
    monkeypatch.setattr(webhooks,'resolve_and_check_webhook_url',lambda url:url)
    monkeypatch.setattr(webhooks,'_decrypt_webhook_url',lambda url:url)
    class Response:status_code=200
    monkeypatch.setattr(webhooks,'safe_post',lambda *args,**kwargs:calls.append((args,kwargs)) or Response())
    return TestClient(app),identity,calls

@pytest.mark.parametrize('operation',['patch','test'])
@pytest.mark.parametrize('license_case',['community','expired'])
def test_downgrade_denies_existing_hook_changes_and_test_fire(review_db,signed_license,webhook_client,operation,license_case):
    signed_license(tier='community' if license_case=='community' else 'pro',features=[],expires_at='2000-01-01' if license_case=='expired' else '2099-01-01')
    client,identity,calls=webhook_client
    before=dict(review_db.execute(text('SELECT * FROM webhooks WHERE id=501')).one()._mapping)
    response=client.patch('/webhooks/501',json={'name':'Changed'}) if operation=='patch' else client.post('/webhooks/501/test')
    assert response.status_code==403,response.text
    assert calls==[]
    assert dict(review_db.execute(text('SELECT * FROM webhooks WHERE id=501')).one()._mapping)==before

@pytest.mark.parametrize('operation,identity_case',[(op,identity) for op in ['list','create','patch','delete','test'] for identity in ['grouped_admin','viewer','read_token'] if (op,identity)!=('list','read_token')])
def test_global_hook_credentials_require_system_admin_and_write_scope(review_db,signed_license,webhook_client,operation,identity_case):
    signed_license(tier='pro',features=[])
    client,identity,calls=webhook_client
    if identity_case=='grouped_admin':identity['group_id']=1
    elif identity_case=='viewer':identity['role']='viewer'
    else:identity['_token_scopes']=['read']
    before=dict(review_db.execute(text('SELECT * FROM webhooks WHERE id=501')).one()._mapping)
    if operation=='list':response=client.get('/webhooks')
    elif operation=='create':response=client.post('/webhooks',json={'name':'Second','url':'https://synthetic.invalid/second'})
    elif operation=='patch':response=client.patch('/webhooks/501',json={'name':'Changed'})
    elif operation=='delete':response=client.delete('/webhooks/501')
    else:response=client.post('/webhooks/501/test')
    assert response.status_code==403,response.text
    assert calls==[]
    assert dict(review_db.execute(text('SELECT * FROM webhooks WHERE id=501')).one()._mapping)==before
    assert review_db.execute(text('SELECT COUNT(*) FROM webhooks')).scalar_one()==1


def test_downgrade_allows_system_admin_to_inspect_and_remove_stored_hook(review_db,signed_license,webhook_client):
    signed_license(tier='community',features=[])
    client,identity,calls=webhook_client
    response=client.get('/webhooks');assert response.status_code==200
    assert any(row['id']==501 for row in response.json())
    response=client.delete('/webhooks/501');assert response.status_code==200
    assert review_db.execute(text('SELECT COUNT(*) FROM webhooks WHERE id=501')).scalar_one()==0
    assert calls==[]

@pytest.mark.parametrize('path',['channel','route'])
@pytest.mark.parametrize('license_case',['community','expired'])
def test_stored_hooks_do_not_dispatch_without_current_entitlement(review_db,signed_license,webhook_client,monkeypatch,path,license_case):
    from modules.notifications import channels
    from modules.notifications.routes import webhooks
    signed_license(tier='community' if license_case=='community' else 'pro',features=[],expires_at='2000-01-01' if license_case=='expired' else '2099-01-01')
    def prohibited(*args,**kwargs):pytest.fail('Unlicensed provider/database work started')
    if path=='channel':
        monkeypatch.setattr(channels,'get_db',prohibited)
        channels.send_webhook('print_complete','Synthetic','Synthetic')
    else:
        monkeypatch.setattr(webhooks.threading,'Thread',prohibited)
        webhooks._dispatch_to_webhooks(review_db,'print_complete','Synthetic','Synthetic','info')
    assert review_db.execute(text('SELECT is_enabled FROM webhooks WHERE id=501')).scalar_one()==1


def test_paid_system_admin_can_update_and_test_existing_hook(review_db,signed_license,webhook_client):
    signed_license(tier='pro',features=[])
    client,identity,calls=webhook_client
    response=client.patch('/webhooks/501',json={'name':'Updated synthetic hook'})
    assert response.status_code==200,response.text
    assert review_db.execute(text('SELECT name FROM webhooks WHERE id=501')).scalar_one()=='Updated synthetic hook'
    response=client.post('/webhooks/501/test')
    assert response.status_code==200 and response.json()['success'] is True,response.text
    assert len(calls)==1
    assert calls[0][0]==('https://synthetic.invalid/hook',)
    assert calls[0][1]['json']['event']=='test'

@pytest.mark.parametrize('operation',['create','patch'])
@pytest.mark.parametrize('invalid',['{broken','"all"',{'all':True},123,['all',123],['all',''],['all',None],'['*1200+'0'+']'*1200])
def test_invalid_event_selection_is_rejected_without_partial_mutation(
    review_db,signed_license,webhook_client,operation,invalid
):
    signed_license(tier='pro',features=[])
    client,identity,calls=webhook_client
    before=[dict(row._mapping) for row in review_db.execute(text('SELECT * FROM webhooks')).all()]
    data={'name':'Changed','url':'https://synthetic.invalid/hook','alert_types':invalid}
    response=client.post('/webhooks',json=data) if operation=='create' else client.patch('/webhooks/501',json=data)
    assert response.status_code==400
    assert [dict(row._mapping) for row in review_db.execute(text('SELECT * FROM webhooks')).all()]==before
    assert calls==[]

@pytest.mark.parametrize('operation',['create','patch'])
@pytest.mark.parametrize('selection,stored',[
    (None,None),('',None),([], '[]'),(['print_complete'],'["print_complete"]'),
    ('["all"]','["all"]'),
])
def test_valid_event_selections_persist_canonically(review_db,signed_license,webhook_client,monkeypatch,operation,selection,stored):
    from modules.notifications.routes import webhooks
    signed_license(tier='pro',features=[])
    client,identity,calls=webhook_client
    monkeypatch.setattr(webhooks,'_encrypt_webhook_url',lambda value:'synthetic-encrypted-value')
    data={'name':'Valid selection','url':'https://synthetic.invalid/hook','alert_types':selection}
    response=client.post('/webhooks',json=data) if operation=='create' else client.patch('/webhooks/501',json=data)
    assert response.status_code==200
    assert review_db.execute(text("SELECT alert_types FROM webhooks WHERE name='Valid selection'")).scalar_one()==stored
    assert calls==[]


def test_rejected_selection_update_preserves_original_dispatch(review_db,signed_license,webhook_client,monkeypatch):
    from modules.notifications.routes import webhooks
    signed_license(tier='pro',features=[])
    client,identity,calls=webhook_client
    review_db.execute(text('UPDATE webhooks SET alert_types=:events WHERE id=501'),{'events':'["print_complete"]'});review_db.commit()
    class ImmediateThread:
        def __init__(self,target,daemon):self.target=target
        def start(self):self.target()
    monkeypatch.setattr(webhooks.threading,'Thread',ImmediateThread)
    response=client.patch('/webhooks/501',json={'alert_types':['all',123],'name':'Changed'})
    assert response.status_code==400
    webhooks._dispatch_to_webhooks(review_db,'print_failed','Synthetic','Synthetic','info')
    assert calls==[]
    webhooks._dispatch_to_webhooks(review_db,'print_complete','Synthetic','Synthetic','info')
    assert len(calls)==1
    assert calls[0][1]['json']['event']=='print_complete'
