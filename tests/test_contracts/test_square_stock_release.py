"""Release gates and cutover barriers. Every upstream operation is synthetic."""
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from tests.test_contracts.test_batch_acceptance import batch, make_recipe, body
from tests.test_contracts.test_square_readonly_workflow import shop, connect
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_square_delivery import prepared, enable, counts, http, history
from modules.orders import square_store as store, stock_recovery as recovery, square_delivery_client as wire
from core.models import AuditLog


def baseline(batch, at='2099-01-01T00:00:00Z', quantity=300, product=101):
    revision=batch[0].get('/products/square').json()['revision']
    response=batch[0].put(f'/products/{product}/square/baseline',json={'revision':revision,'at':at,'quantity':quantity})
    if response.status_code==200:batch[-1].update(response.json()['connection'])
    return response


def review(batch, row, answers=None):
    revision=batch[0].get('/products/square').json()['revision']
    return batch[0].post(f"/products/batches/additions/{row['id']}/baseline-review",json={
        'revision':revision,'answers':answers or {'101':'not_counted'}})


def send(batch, row):
    return batch[0].post(f"/products/batches/additions/{row['id']}/send")


@pytest.mark.parametrize('value,expected',[('1',True),('0',False),('true',False),('',False),(None,False)])
def test_fresh_process_switch(value,expected):
    env={**os.environ,'PYTHONPATH':'backend:.'}
    if value is None:env.pop('ODIN_SQUARE_STOCK_WRITES',None)
    else:env['ODIN_SQUARE_STOCK_WRITES']=value
    script="from modules.orders.square_delivery_client import LIVE_WRITES_ENABLED; import os; os.environ['ODIN_SQUARE_STOCK_WRITES']='different'; print(LIVE_WRITES_ENABLED)"
    result=subprocess.run([sys.executable,'-c',script],env=env,capture_output=True,text=True,check=True)
    assert result.stdout.strip()==str(expected)


@pytest.mark.parametrize('server,tenant',[(False,True),(True,False),(False,False),(True,1),(True,'true'),(True,None)])
def test_both_mutation_boundaries_require_strict_keys(batch,monkeypatch,server,tenant):
    row=prepared(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',server)
    factory,_=http(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        wire.SquareWriter('sandbox','synthetic').send(row['request_body'],tenant_enabled=tenant)
    assert exc.value.status_code==503
    with pytest.raises(HTTPException) as exc:
        wire.exchange('sandbox','synthetic','/v2/inventory/changes/batch-create',row['request_body'],tenant_enabled=tenant)
    assert exc.value.status_code==503
    factory.assert_not_called()


def test_activation_strict_cas_audit_and_rotation(batch,monkeypatch):
    client,user,_,db,*_=batch
    revision=batch[-1]['revision']
    url='/products/square/activation'
    assert client.put(url,json={'revision':revision,'stock_writes_enabled':'true'}).status_code==422
    assert client.put(url,json={'revision':'stale','stock_writes_enabled':True}).status_code==409
    result=client.put(url,json={'revision':revision,'stock_writes_enabled':True})
    assert result.status_code==200 and result.json()['stock_writes_enabled'] is True
    assert db.execute(select(AuditLog).where(AuditLog.action=='square.stock_activation')).scalar_one().details['attested_by']==user['id']
    rotated,_=connect(client)
    assert rotated['stock_writes_enabled'] is True
    changed,_=connect(client,'other')
    assert changed['stock_writes_enabled'] is False and changed['mappings']==[]
    revision=changed['revision']
    activated=client.put(url,json={'revision':revision,'stock_writes_enabled':True}).json()
    env=client.put('/products/square',json={'access_token':'synthetic-other','environment':'production','expected_revision':activated['revision']}).json()
    assert env['stock_writes_enabled'] is False
    assert client.delete('/products/square',params={'revision':env['revision']}).json()['stock_writes_enabled'] is False
    assert connect(client)[0]['stock_writes_enabled'] is False


@pytest.mark.parametrize('role,kind',[('viewer','jwt'),('operator','jwt'),('admin','user_api_token'),('admin','api_key'),('admin',None)])
def test_activation_and_baseline_interactive_admin_only(batch,role,kind):
    batch[1].update(role=role,_auth_kind=kind)
    assert batch[0].put('/products/square/activation',json={'revision':batch[-1]['revision'],'stock_writes_enabled':True}).status_code==403
    assert batch[0].put('/products/101/square/baseline',json={'revision':batch[-1]['revision'],'at':'2099-01-01T00:00:00Z','quantity':300}).status_code==403


@pytest.mark.parametrize('role,kind',[('viewer','jwt'),('admin','user_api_token'),('operator','api_key'),('operator',None)])
def test_send_interactive_operator_only(batch,role,kind):
    row=prepared(batch);batch[1].update(role=role,_auth_kind=kind)
    assert send(batch,row).status_code==403


def test_legacy_flag_and_route_disabled_no_remote_reads(batch,monkeypatch):
    row=prepared(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    network=__import__('unittest.mock',fromlist=['Mock']).Mock(side_effect=AssertionError('network'))
    monkeypatch.setattr(recovery,'live_check',network)
    assert send(batch,row).status_code==503
    network.assert_not_called()


def test_disable_during_preflight_prevents_reservation(batch,monkeypatch):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    factory,_=http(monkeypatch)
    original=recovery.live_check
    def race(doc,row,**kwargs):
        original(doc,row,**kwargs)
        with Session(batch[3].get_bind()) as other:
            current,old=store.load(other,1);current['stock_writes_enabled']=False
            store.save(other,1,current,old);other.commit()
    monkeypatch.setattr(recovery,'live_check',race)
    assert send(batch,row).status_code==409
    factory.assert_not_called()
    assert batch[3].execute(select(recovery.batches.additions.c.attempts)).scalar_one()==0


@pytest.mark.parametrize('edit',['unrelated','rotation','activation'])
def test_unrelated_revision_changes_leave_destination_sendable(batch,monkeypatch,edit):
    row=prepared(batch);doc,old=store.load(batch[3],1)
    if edit=='unrelated':doc['mappings']['104']={'location_id':'L2','variation_id':'V2','item_name':'Other','variation_name':'Other'}
    if edit=='rotation':doc['token']='rotated-synthetic'
    if edit=='activation':doc['stock_writes_enabled']=True
    store.save(batch[3],1,doc,old);batch[3].commit()
    assert recovery.check(batch[3],batch[1],row['id'])['readiness']=='checks_passed'


@pytest.mark.parametrize('field',['location_id','variation_id'])
def test_own_destination_cannot_be_retargeted(batch,monkeypatch,field):
    row=prepared(batch);doc,old=store.load(batch[3],1);doc['mappings']['101'][field]='OTHER'
    store.save(batch[3],1,doc,old);batch[3].commit()
    monkeypatch.setattr(recovery,'live_check',lambda *_:pytest.fail('remote read'))
    assert recovery.check(batch[3],batch[1],row['id'])['readiness']=='blocked'


@pytest.mark.parametrize('scopes',[[],['INVENTORY_READ'],'INVENTORY_WRITE',None,[1],['INVENTORY_WRITE',None]])
def test_scope_preflight_inside_executor_and_check(batch,monkeypatch,scopes):
    from modules.orders.square_client import SquareReader
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    original=SquareReader._read
    def read(self,method,path,**kwargs):
        if path=='/oauth2/token/status':return {'scopes':scopes}
        return original(self,method,path,**kwargs)
    monkeypatch.setattr(SquareReader,'_read',read);factory,_=http(monkeypatch)
    assert recovery.check(batch[3],batch[1],row['id'])['readiness']=='blocked'
    assert send(batch,row).status_code==409
    assert batch[3].execute(select(recovery.batches.additions.c.attempts)).scalar_one()==0
    factory.assert_not_called()


def test_scope_read_failure_blocks_and_reconciliation_needs_no_write_scope(batch,monkeypatch):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    def fail(*_):raise HTTPException(502,'Scope read unavailable')
    monkeypatch.setattr(wire.SquareInventoryProbe,'require_write_scope',fail)
    assert recovery.check(batch[3],batch[1],row['id'])['readiness']=='blocked'
    assert send(batch,row).status_code==502
    db=batch[3];db.execute(update(recovery.batches.additions).values(state='unknown',attempts=1));db.commit()
    http(monkeypatch,history(row['request_body']))
    assert recovery.check(db,batch[1],row['id'])['state']=='confirmed'


@pytest.mark.parametrize('at',['2020-01-01T00:00:00','2020-01-01T03:00:00+03:00'])
def test_post_baseline_output_frozen_with_utc_evidence(batch,at):
    assert baseline(batch,at).status_code==200
    recipe=make_recipe(batch);response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==200,response.text
    output=response.json()['outputs'][0]
    assert output['disposition']=='send' and output['send_quantity']==4
    assert output['observation_ended_at'].endswith('+00:00')
    assert output['stock_baseline']['at']=='2020-01-01T00:00:00+00:00'
    assert output['baseline_version']==batch[-1]['mappings'][0]['stock_baseline']['version']
    assert output['stock_baseline']['quantity']==300 and output['stock_baseline']['attested_by']==3


def test_pre_baseline_requires_choice_and_identical_replay_precedes_policy(batch):
    assert baseline(batch).status_code==200
    recipe=make_recipe(batch);request=body(batch,recipe)
    assert batch[0].post('/products/production/inspections',json=request).status_code==409
    request['outputs'][0]['baseline_disposition']='in_baseline'
    first=batch[0].post('/products/production/inspections',json=request)
    assert first.status_code==200,first.text
    assert first.json()['stock_status']=='in_baseline'
    assert batch[3].execute(select(recovery.batches.additions)).first() is None
    assert baseline(batch,quantity=400).status_code==200
    assert batch[0].post('/products/production/inspections',json=request).json()==first.json()
    page=batch[0].get('/products/production/history',params={'state':'in_baseline'}).json()
    assert len(page['receipts'])==1 and page['additions']==[]


def test_mixed_dispositions_recovery_uses_send_quantity(batch):
    assert baseline(batch).status_code==200
    recipe=make_recipe(batch,outputs=[{'product_id':101,'kind':'main','expected':5},{'product_id':101,'kind':'flush','expected':2}])
    request=body(batch,recipe);request['outputs'][0]['baseline_disposition']='in_baseline'
    request['outputs'].append({'product_id':101,'kind':'flush','accepted':1,'rejected':1,'baseline_disposition':'send'})
    result=batch[0].post('/products/production/inspections',json=request)
    assert result.status_code==200,result.text
    row=dict(batch[3].execute(select(recovery.batches.additions)).mappings().one())
    assert json.loads(row['request_body'])['changes'][0]['adjustment']['quantity']=='1'
    assert recovery.check(batch[3],batch[1],row['id'])['readiness']=='checks_passed'


def test_baseline_barrier_versions_review_binding_and_immutable_bytes(batch,monkeypatch):
    row=prepared(batch);original=row['request_body']
    first=baseline(batch);assert first.json()['pending_additions']==[row['id']]
    v1=first.json()['connection']['mappings'][0]['stock_baseline']['version']
    assert recovery.check(batch[3],batch[1],row['id'])['message']=='baseline_review'
    assert review(batch,row).status_code==200
    assert recovery.check(batch[3],batch[1],row['id'])['readiness']=='checks_passed'
    second=baseline(batch,quantity=400)
    v2=second.json()['connection']['mappings'][0]['stock_baseline']['version']
    assert v1!=v2
    enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True);factory,_=http(monkeypatch)
    assert send(batch,row).status_code==409
    factory.assert_not_called()
    assert batch[3].execute(select(recovery.batches.additions.c.request_body)).scalar_one()==original
    assert review(batch,row).status_code==200
    _,session=http(monkeypatch,counts(original))
    assert send(batch,row).json()['state']=='confirmed'
    assert session.request.call_count==1


def test_withdrawal_terminal_audited_and_cannot_revive(batch,monkeypatch):
    row=prepared(batch);baseline(batch)
    result=review(batch,row,{'101':'counted'})
    assert result.status_code==200 and result.json()['state']=='withdrawn_baseline'
    logs=batch[3].execute(select(AuditLog).where(AuditLog.action=='square.baseline_review')).scalars().all()
    assert len(logs)==1 and logs[0].details['answer']=='counted'
    assert review(batch,row).status_code==409
    monkeypatch.setattr(store,'load',lambda *_:pytest.fail('withdrawn must need no connector'))
    assert recovery.check(batch[3],batch[1],row['id'])['state']=='withdrawn_baseline'
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    assert send(batch,row).status_code==409


def test_unknown_blocks_baseline_change_and_withdrawal(batch):
    row=prepared(batch);db=batch[3]
    db.execute(update(recovery.batches.additions).values(state='unknown',attempts=1));db.commit()
    assert baseline(batch).status_code==409
    assert review(batch,row,{'101':'counted'}).status_code==409


def test_remap_clears_baseline_uuid_never_reused(batch):
    baseline(batch);old=batch[-1]['mappings'][0]['stock_baseline']['version']
    revision=batch[0].get('/products/square').json()['revision']
    # Removing a mapping clears the evidence; recreating the same destination is new.
    assert batch[0].delete('/products/101/square',params={'revision':revision}).status_code==200
    revision=batch[0].get('/products/square').json()['revision']
    remap=batch[0].put('/products/101/square',json={'revision':revision,'location_id':'L1','variation_id':'V1'})
    assert 'stock_baseline' not in remap.json()['mappings'][0]
    assert baseline(batch).json()['connection']['mappings'][0]['stock_baseline']['version']!=old


def globalize(batch):
    """Exercise the connector CAS without the group-row lock."""
    db=batch[3];doc,old=store.load(db,1);doc['tenant']=0
    store.save(db,0,doc,None)
    db.execute(text('UPDATE products SET org_id=NULL WHERE id IN (101,104)'))
    db.execute(update(recovery.batches.additions).values(owner=0))
    db.execute(update(recovery.batches.receipts).values(owner=0))
    db.commit();batch[1]['group_id']=None


@pytest.mark.parametrize('owner',[0,1])
@pytest.mark.parametrize('action',['baseline','withdrawal'])
def test_cutover_wins_against_paused_preflight(batch,monkeypatch,action,owner):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    if owner==0:globalize(batch)
    engine=batch[3].get_bind();batch[3].rollback()
    entered,release=Event(),Event();original=recovery.live_check
    def pause(*args,**kwargs):
        original(*args,**kwargs);entered.set();assert release.wait(10)
    monkeypatch.setattr(recovery,'live_check',pause);factory,_=http(monkeypatch)
    def executor():
        with Session(engine) as db:
            try:return recovery.execute_prepared(db,dict(batch[1]),row['id'])
            except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(executor);assert entered.wait(10)
        if action=='baseline':assert baseline(batch).status_code==200
        else:assert review(batch,row,{'101':'counted'}).status_code==200
        release.set();assert future.result(timeout=10)==409
    factory.assert_not_called()


@pytest.mark.parametrize('owner',[0,1])
@pytest.mark.parametrize('action',['baseline','withdrawal'])
def test_reservation_wins_against_cutover_while_network_inflight(batch,monkeypatch,action,owner):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    if owner==0:globalize(batch)
    engine=batch[3].get_bind();batch[3].rollback();entered,release=Event(),Event()
    def pause(self,raw,*,tenant_enabled):
        assert tenant_enabled is True;entered.set();assert release.wait(10);return False
    monkeypatch.setattr(wire.SquareWriter,'send',pause)
    def executor():
        with Session(engine) as db:return recovery.execute_prepared(db,dict(batch[1]),row['id'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(executor);assert entered.wait(10)
        if action=='baseline':assert baseline(batch).status_code==409
        else:assert review(batch,row,{'101':'counted'}).status_code==409
        release.set();assert future.result(timeout=10)['state']=='unknown'


def test_multi_product_review_requires_complete_unanimous_attestation(batch,monkeypatch):
    from modules.orders.square_client import SquareReader
    original=SquareReader._read
    def read(self,method,path,**kwargs):
        if path=='/v2/catalog/object/V2':
            data=original(self,method,'/v2/catalog/object/V1',**kwargs)
            data['object']['id']='V2'
            return data
        return original(self,method,path,**kwargs)
    monkeypatch.setattr(SquareReader,'_read',read)
    mapped=batch[0].put('/products/104/square',json={'revision':batch[-1]['revision'],'location_id':'L1','variation_id':'V2'})
    assert mapped.status_code==200;batch[-1].update(mapped.json())
    recipe=make_recipe(batch,outputs=[{'product_id':101,'kind':'main','expected':5},{'product_id':104,'kind':'flush','expected':2}])
    request=body(batch,recipe);request['outputs'].append({'product_id':104,'kind':'flush','accepted':1,'rejected':1})
    assert batch[0].post('/products/production/inspections',json=request).status_code==200
    row=dict(batch[3].execute(select(recovery.batches.additions)).mappings().one())
    baseline(batch);baseline(batch,product=104)
    assert review(batch,row,{'101':'not_counted'}).status_code==409
    assert review(batch,row,{'101':'not_counted','104':'counted'}).status_code==409
    assert recovery.check(batch[3],batch[1],row['id'])['message']=='baseline_review'
    assert review(batch,row,{'101':'not_counted','104':'not_counted'}).status_code==200
    assert recovery.check(batch[3],batch[1],row['id'])['readiness']=='checks_passed'
    baseline(batch,product=104,quantity=350)
    assert recovery.check(batch[3],batch[1],row['id'])['message']=='baseline_review'
    assert batch[3].execute(select(recovery.batches.additions.c.request_body)).scalar_one()==row['request_body']


def test_legacy_null_baseline_version_matches_only_without_baseline(batch):
    row=prepared(batch);db=batch[3]
    receipt=db.execute(select(recovery.batches.receipts)).mappings().one()
    detail=json.loads(receipt['definition'])
    for o in detail['outputs']:
        for key in ('baseline_version','stock_baseline','disposition','send_quantity','observation_ended_at'):o.pop(key,None)
    db.execute(update(recovery.batches.receipts).values(definition=json.dumps(detail)));db.commit()
    assert recovery.check(db,batch[1],row['id'])['readiness']=='checks_passed'
    baseline(batch)
    assert recovery.check(db,batch[1],row['id'])['message']=='baseline_review'


def test_scope_endpoint_fixed_host_read_allowlist_and_malformed_fail_closed(monkeypatch):
    from modules.orders.square_client import SquareReader
    # This test uses the real _read, never the workflow fixture override.
    factory,session=http(monkeypatch,{'scopes':['INVENTORY_WRITE']})
    SquareReader('production','synthetic').require_write_scope()
    assert session.request.call_args.args==('POST','https://connect.squareup.com/oauth2/token/status')
    assert session.request.call_args.kwargs['allow_redirects'] is False
    with pytest.raises(HTTPException):SquareReader('production','synthetic')._read('POST','/v2/inventory/changes/batch-create')
    assert session.request.call_count==1


def test_success_send_route_operator_exact_bytes_and_confirmed_no_resend(batch,monkeypatch):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _,session=http(monkeypatch,counts(row['request_body']));batch[1]['role']='operator'
    result=send(batch,row)
    assert result.status_code==200 and result.json()['state']=='confirmed'
    assert session.request.call_args.kwargs['data']==row['request_body'].encode()
    assert send(batch,row).json()['state']=='confirmed' and session.request.call_count==1


def test_tenant_flag_corrupt_nonboolean_fails_closed(batch):
    prepared(batch);doc,old=store.load(batch[3],1);doc['stock_writes_enabled']=1
    store.save(batch[3],1,doc,old);batch[3].commit()
    assert batch[0].get('/products/square').status_code==503


def test_disable_after_reservation_does_not_cancel_inflight_send(batch,monkeypatch):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    def inflight(self,raw,*,tenant_enabled):
        revision=batch[0].get('/products/square').json()['revision']
        disabled=batch[0].put('/products/square/activation',json={'revision':revision,'stock_writes_enabled':False})
        assert disabled.status_code==200
        assert tenant_enabled is True
        return True
    monkeypatch.setattr(wire.SquareWriter,'send',inflight)
    assert send(batch,row).json()['state']=='confirmed'


def test_withdrawal_audit_failure_rolls_back_state(batch,monkeypatch):
    from modules.orders import stock_baseline
    row=prepared(batch)
    def failure(*args,**kwargs):raise RuntimeError('simulated audit storage failure')
    monkeypatch.setattr(stock_baseline,'log_audit',failure)
    with pytest.raises(RuntimeError):review(batch,row,{'101':'counted'})
    assert batch[3].execute(select(recovery.batches.additions.c.state)).scalar_one()=='prepared'


def test_missing_and_conflicting_time_evidence_blocks_preparation(batch):
    baseline(batch,at='2020-01-01T00:00:00Z');recipe=make_recipe(batch);request=body(batch,recipe)
    request['outputs'][0]['baseline_disposition']='in_baseline'
    assert batch[0].post('/products/production/inspections',json=request).status_code==409
    batch[3].execute(text('UPDATE print_jobs SET ended_at=NULL WHERE scheduled_job_id=302'));batch[3].commit()
    assert batch[0].post('/products/production/inspections',json=request).status_code==409
    assert batch[3].execute(select(recovery.batches.receipts)).first() is None


def test_baseline_cleared_on_destination_change_but_same_destination_preserved(batch,monkeypatch):
    from modules.orders.square_client import SquareReader
    baseline(batch);original_version=batch[-1]['mappings'][0]['stock_baseline']['version']
    mapped=batch[0].put('/products/101/square',json={'revision':batch[-1]['revision'],'location_id':'L1','variation_id':'V1'})
    assert mapped.json()['mappings'][0]['stock_baseline']['version']==original_version
    original=SquareReader._read
    def read(self,method,path,**kwargs):
        if path=='/v2/catalog/object/V2':
            data=original(self,method,'/v2/catalog/object/V1',**kwargs);data['object']['id']='V2';return data
        return original(self,method,path,**kwargs)
    monkeypatch.setattr(SquareReader,'_read',read)
    mapped=batch[0].put('/products/101/square',json={'revision':mapped.json()['revision'],'location_id':'L1','variation_id':'V2'})
    assert mapped.status_code==200 and 'stock_baseline' not in mapped.json()['mappings'][0]


def test_sqlite_backup_restore_retains_baseline_and_review_without_receipt_edits(batch,tmp_path):
    import sqlite3
    row=prepared(batch);baseline(batch);assert review(batch,row).status_code==200
    db=batch[3];db.rollback();source=db.get_bind().raw_connection()
    backup_file=tmp_path/'stock-backup.db'
    try:
        with sqlite3.connect(backup_file) as target:source.driver_connection.backup(target)
    finally:source.close()
    with sqlite3.connect(backup_file) as restored:
        assert restored.execute('SELECT request_body FROM production_stock_additions').fetchone()[0]==row['request_body']
        original=db.execute(select(recovery.batches.receipts.c.definition)).scalar_one()
        assert restored.execute('SELECT definition FROM production_acceptances').fetchone()[0]==original
        audit=restored.execute("SELECT details FROM audit_logs WHERE action='square.baseline_review'").fetchone()[0]
        assert json.loads(audit)['addition_id']==row['id']
        assert restored.execute("SELECT value FROM system_config WHERE key='square_readonly:1'").fetchone()[0]
