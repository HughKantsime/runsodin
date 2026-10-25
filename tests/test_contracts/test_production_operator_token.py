"""Real token authentication and production persistence; synthetic Square only."""
import json
import secrets

import pytest
from sqlalchemy import text, select, update

from tests.test_contracts.test_batch_acceptance import batch, make_recipe, body
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_square_delivery import enable, counts, http, history
from modules.orders import batch_production as batches, square_delivery_client as wire


def authenticate(batch, scopes=('write:production',), role='operator'):
    from core.auth import hash_password
    from core.dependencies import get_current_user
    raw = 'odin_' + secrets.token_urlsafe(32)
    db = batch[3]
    db.execute(text('UPDATE users SET role=:role WHERE id=3'), {'role':role})
    db.execute(text('INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,\'Synthetic production operator\',:hash,:prefix,:scopes)'),
               {'hash':hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(list(scopes))})
    db.commit()
    batch[4].dependency_overrides.pop(get_current_user)
    batch[0].headers['X-API-Key'] = raw


def prepare(batch):
    recipe=make_recipe(batch)
    enable(batch)
    batch[-1].update(batch[0].get("/products/square").json())
    authenticate(batch)
    request=body(batch,recipe)
    response=batch[0].post('/products/production/inspections',json=request)
    assert response.status_code==200, response.text
    row=dict(batch[3].execute(select(batches.additions)).mappings().one())
    return request,response.json(),row


def test_token_records_checks_sends_and_retries_without_double_stock(batch,monkeypatch):
    request,receipt,row=prepare(batch)
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _,transport=http(monkeypatch,counts(row['request_body']))
    client=batch[0]
    assert client.post('/products/production/inspections',json=request).json()['id']==receipt['id']
    assert client.post(f"/products/production/additions/{row['id']}/check").json()['readiness']=='checks_passed'
    url=f"/products/batches/additions/{row['id']}/send"
    assert client.post(url).json()['state']=='confirmed'
    assert client.post(url).json()['state']=='confirmed'
    assert transport.request.call_count==1
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar()==1
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar()==1


@pytest.mark.parametrize('scopes', [('write',),('admin',),('agent:write',),('read',),('write:jobs',),()])
def test_other_tokens_never_reach_inspection_or_stock_transport(batch,monkeypatch,scopes):
    recipe=make_recipe(batch); authenticate(batch,scopes)
    factory,_=http(monkeypatch)
    for path,data in [('/products/production/inspections',body(batch,recipe)),('/products/production/additions/absent/check',{}),('/products/batches/additions/absent/send',{})]:
        assert batch[0].post(path,json=data).status_code in (401,403)
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar()==0
    factory.assert_not_called()


@pytest.mark.parametrize('path,method',[
    ('/products/production/recipes','post'),('/products/square','put'),('/products/square','delete'),
    ('/products/101/square','put'),('/products/square/activation','put'),('/products/101/square/baseline','put'),
    ('/products/batches/additions/absent/baseline-review','post'),('/products/production/components','post'),
    ('/products/production/finishing','post'),('/products/production/assortment/101','put'),
    ('/products/production/assortment/101/preview','post'),
])
def test_production_permission_cannot_change_setup_or_unrelated_workflows(batch,path,method):
    authenticate(batch)
    assert batch[0].request(method,path,json={}).status_code==403


def test_viewer_and_owner_demotion_rejected(batch,monkeypatch):
    request,_,row=prepare(batch)
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    factory,_=http(monkeypatch)
    batch[3].execute(text("UPDATE users SET role='viewer' WHERE id=3"));batch[3].commit()
    assert batch[0].post('/products/production/inspections',json=request).status_code==403
    assert batch[0].post(f"/products/batches/additions/{row['id']}/send").status_code==403
    factory.assert_not_called()


def test_cross_tenant_and_missing_confirmation_refused(batch,monkeypatch):
    recipe=make_recipe(batch); authenticate(batch)
    request=body(batch,recipe);request['inspection_confirmed']=False
    assert batch[0].post('/products/production/inspections',json=request).status_code==409
    batch[3].execute(text('UPDATE users SET group_id=2 WHERE id=3'));batch[3].commit()
    request['inspection_confirmed']=True
    assert batch[0].post('/products/production/inspections',json=request).status_code==404
    assert batch[0].post('/products/batches/additions/absent/send').status_code in (404,503)
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar()==0


@pytest.mark.parametrize('server,tenant',[(False,True),(True,False)])
def test_operator_cannot_bypass_either_activation_key(batch,monkeypatch,server,tenant):
    _,_,row=prepare(batch)
    from modules.orders import square_store
    doc,old=square_store.load(batch[3],1);doc['stock_writes_enabled']=tenant
    square_store.save(batch[3],1,doc,old);batch[3].commit()
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',server)
    factory,_=http(monkeypatch)
    assert batch[0].post(f"/products/batches/additions/{row['id']}/send").status_code==503
    assert batch[3].execute(select(batches.additions.c.attempts)).scalar()==0
    factory.assert_not_called()


def test_unknown_outcome_reconciles_without_resending(batch,monkeypatch):
    _,_,row=prepare(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    import requests
    _,transport=http(monkeypatch,exception=requests.Timeout())
    url=f"/products/batches/additions/{row['id']}/send"
    assert batch[0].post(url).json()['state']=='unknown'
    assert batch[0].post(url).status_code==409
    transport.request.side_effect=None
    transport.request.return_value.iter_content.return_value=[json.dumps(history(row['request_body'])).encode()]
    assert batch[0].post(f"/products/production/additions/{row['id']}/check").json()['state']=='confirmed'
    # Exactly one stock send; the second external request reads adjustment history.
    assert transport.request.call_count==2


def test_withdrawn_and_foreign_additions_cannot_be_sent(batch,monkeypatch):
    _,_,row=prepare(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    factory,_=http(monkeypatch)
    batch[3].execute(update(batches.additions).values(state='withdrawn_baseline'));batch[3].commit()
    assert batch[0].post(f"/products/batches/additions/{row['id']}/send").status_code==409
    batch[3].execute(text('UPDATE users SET group_id=2 WHERE id=3'));batch[3].commit()
    assert batch[0].post(f"/products/batches/additions/{row['id']}/send").status_code==404
    factory.assert_not_called()


from tests.test_contracts.test_api_token_delegation_behavior import token_client, interactive_headers


@pytest.mark.parametrize('role,status',[('admin',200),('operator',200),('viewer',403)])
def test_explicit_production_scope_mint_role_floor(token_client,review_db,role,status):
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'),{'role':role});review_db.commit()
    client,_=token_client
    response=client.post('/tokens',headers=interactive_headers(),json={'name':'Synthetic production token','scopes':['write:production']})
    assert response.status_code==status
    if status==200:
        assert response.json()['scopes']==['write:production']


def test_production_token_cannot_mint_credentials(token_client):
    client,issue=token_client
    response=client.post('/tokens',headers=issue(['write:production']),json={'name':'No delegation','scopes':['admin']})
    assert response.status_code==403


@pytest.mark.parametrize('kind,scopes',[
    ('legacy_global_api_key', ['write:production']), ('api_key', []),
    ('jwt',['write:production']), (None,['write:production']),
])
def test_forged_or_legacy_provenance_does_not_grant_production(kind,scopes):
    from fastapi import HTTPException
    from modules.orders.production_auth import assert_production_operator
    with pytest.raises(HTTPException) as exc:
        assert_production_operator({'role':'admin','_auth_kind':kind,'_token_scopes':scopes})
    assert exc.value.status_code==403


def test_configured_action_removal_and_viewer_widening_do_not_bypass_floor(batch,monkeypatch):
    recipe=make_recipe(batch);authenticate(batch)
    from core.models import SystemConfig
    config=SystemConfig(key='rbac_permissions', value={'action_access':{'products.inspections':[]}})
    batch[3].add(config);batch[3].commit()
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code==403
    config.value={'action_access':{'products.inspections':['viewer']}}
    batch[3].commit()
    batch[3].execute(text("UPDATE users SET role='viewer' WHERE id=3"));batch[3].commit()
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code==403


def test_concurrent_token_sends_reserve_one_adjustment(batch,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from modules.orders import stock_recovery
    _,_,row=prepare(batch);batch[3].rollback()
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _,transport=http(monkeypatch,counts(row['request_body']))
    barrier=Barrier(2);original=stock_recovery.live_check
    def preflight(*args,**kwargs):
        answer=original(*args,**kwargs);barrier.wait(timeout=10);return answer
    monkeypatch.setattr(stock_recovery,'live_check',preflight)
    url=f"/products/batches/additions/{row['id']}/send"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:batch[0].post(url),range(2)))
    assert sorted(r.status_code for r in responses)==[200,409]
    assert transport.request.call_count==1


def test_audit_identifies_operator_and_does_not_duplicate_on_retry(batch,monkeypatch):
    request,_,row=prepare(batch)
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    http(monkeypatch,counts(row['request_body']))
    assert batch[0].post('/products/production/inspections',json=request).status_code==200
    url=f"/products/batches/additions/{row['id']}/send"
    assert batch[0].post(url).status_code==200
    assert batch[0].post(url).status_code==200
    from core.models import AuditLog
    records=batch[3].execute(select(AuditLog).where(AuditLog.action.in_(['production.inspected','production.stock_send_reserved']))).scalars().all()
    assert len(records)==2
    assert all(r.details['operator_id']==3 and r.details['auth_kind']=='user_api_token' for r in records)


@pytest.mark.parametrize('operation',['inspection','send'])
def test_audit_flush_failure_rolls_back_business_intent(batch,monkeypatch,operation):
    from core.models import AuditLog
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    if operation=='send':
        _,_,row=prepare(batch)
        monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
        factory,_=http(monkeypatch,counts(row['request_body']))
    else:
        recipe=make_recipe(batch);authenticate(batch)
    def fail_audit(*args):raise RuntimeError('synthetic audit persistence failure')
    event.listen(AuditLog,'before_insert',fail_audit)
    try:
        with pytest.raises(RuntimeError,match='synthetic audit persistence failure'):
            if operation=='send':batch[0].post(f"/products/batches/additions/{row['id']}/send")
            else:batch[0].post('/products/production/inspections',json=body(batch,recipe))
    finally:event.remove(AuditLog,'before_insert',fail_audit)
    with Session(batch[3].get_bind()) as fresh:
        if operation=='send':
            saved=fresh.execute(select(batches.additions)).mappings().one()
            assert saved['state']=='prepared' and saved['attempts']==0
            factory.assert_not_called()
        else:
            assert fresh.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar()==0
            assert fresh.execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar()==0
