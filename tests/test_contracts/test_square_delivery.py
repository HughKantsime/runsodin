"""Local delivery/recovery proof. All Square HTTP is simulated; no real credentials."""
import copy
import json
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update

from tests.test_contracts.test_batch_acceptance import batch, make_recipe, body
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_education_review_workflow import review_db
from modules.orders import batch_production as batches, square_delivery_client as wire, stock_recovery as recovery


def prepared(batch):
    recipe=make_recipe(batch)
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==200,response.text
    return dict(batch[3].execute(select(batches.additions)).mappings().one())


def enable(batch):
    from modules.orders import square_store as store
    doc, old = store.load(batch[3], 1)
    doc['stock_writes_enabled'] = True
    store.save(batch[3], 1, doc, old)
    batch[3].commit()


def http(monkeypatch,payload=None,status=200,exception=None):
    response=Mock();response.status_code=status
    response.iter_content.return_value=[json.dumps(payload).encode()]
    response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
    session=Mock();session.__enter__=Mock(return_value=session);session.__exit__=Mock(return_value=False)
    session.request.return_value=response
    if exception:session.request.side_effect=exception
    factory=Mock(return_value=session);monkeypatch.setattr(wire.requests,'Session',factory)
    return factory,session


def counts(raw):
    return {'counts':[{'catalog_object_id':c['adjustment']['catalog_object_id'],
        'location_id':c['adjustment']['to_location_id'],'state':'IN_STOCK','quantity':'123.00000'}
        for c in json.loads(raw)['changes']]}


def history(raw):
    return {'changes':[{'type':'ADJUSTMENT','adjustment':{**c['adjustment'],'id':'remote-'+str(i),
        'quantity':str(Decimal(c['adjustment']['quantity'])),'occurred_at':c['adjustment']['occurred_at'].replace('Z','+00:00')}}
        for i,c in enumerate(json.loads(raw)['changes'])]}


def test_live_gate_refuses_before_any_credentials_or_network(batch,monkeypatch):
    row=prepared(batch)
    factory,_=http(monkeypatch)
    monkeypatch.setattr(recovery.store,'load',Mock(side_effect=AssertionError('credential read')))
    with pytest.raises(HTTPException) as e:recovery.execute_prepared(batch[3],batch[1],row['id'])
    assert e.value.status_code==503
    with pytest.raises(HTTPException):wire.SquareWriter('production','synthetic').send(row['request_body'],tenant_enabled=True)
    factory.assert_not_called()
    assert batch[3].execute(select(batches.additions.c.attempts)).scalar()==0


def test_fixed_endpoint_exact_bytes_and_supported_success_counts(batch,monkeypatch):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _,session=http(monkeypatch,counts(row['request_body']))
    result=recovery.execute_prepared(batch[3],batch[1],row['id'])
    assert result['state']=='confirmed' and result['attempts']==1
    args,kwargs=session.request.call_args
    assert args==('POST','https://connect.squareupsandbox.com/v2/inventory/changes/batch-create')
    assert kwargs['data']==row['request_body'].encode()
    assert kwargs['allow_redirects'] is False and kwargs['timeout']==(4,10) and session.trust_env is False
    assert kwargs['headers']['Square-Version']==wire.VERSION
    assert result['retry_allowed'] is False
    assert recovery.execute_prepared(batch[3],batch[1],row['id'])['state']=='confirmed'
    assert session.request.call_count==1


@pytest.mark.parametrize('payload,status',[(None,200),({},200),({'errors':[{'detail':'private'}]},200),({'counts':[]},200),({'counts':[{'state':'IN_STOCK','catalog_object_id':'OTHER','location_id':'L1','quantity':'7'}]},200),({},301),({},401),({},403),({},429),({},500)])
def test_invalid_or_unacknowledged_response_stays_unknown(batch,monkeypatch,payload,status):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _,session=http(monkeypatch,payload,status)
    answer=recovery.execute_prepared(batch[3],batch[1],row['id'])
    assert answer['state']=='unknown' and answer['attempts']==1 and 'private' not in str(answer)
    with pytest.raises(HTTPException):recovery.execute_prepared(batch[3],batch[1],row['id'])
    assert session.request.call_count==1


def test_timeout_then_exact_read_only_reconciliation_never_resends(batch,monkeypatch):
    import requests
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _,session=http(monkeypatch,exception=requests.Timeout('private credential text'))
    assert recovery.execute_prepared(batch[3],batch[1],row['id'])['state']=='unknown'
    assert batch[3].execute(select(batches.additions.c.request_body)).scalar()==row['request_body']
    _,session=http(monkeypatch,history(row['request_body']))
    answer=batch[0].post(f"/products/production/additions/{row['id']}/check")
    assert answer.status_code==200 and answer.json()['state']=='confirmed'
    assert session.request.call_args.args[1].endswith('/batch-retrieve')
    assert answer.json()['attempts']==1 and 'private' not in answer.text


@pytest.mark.parametrize('mutation',['missing','partial','wrong_quantity','wrong_reference','wrong_time','extra','duplicate_id','cursor'])
def test_ambiguous_history_stays_unknown(batch,monkeypatch,mutation):
    row=prepared(batch);db=batch[3]
    db.execute(update(batches.additions).values(state='unknown',attempts=1));db.commit()
    data=history(row['request_body'])
    if mutation in ('missing','partial'):data['changes']=[]
    if mutation=='wrong_quantity':data['changes'][0]['adjustment']['quantity']='999'
    if mutation=='wrong_reference':data['changes'][0]['adjustment']['reference_id']='different'
    if mutation=='wrong_time':data['changes'][0]['adjustment']['occurred_at']='2020-01-01T00:00:00Z'
    if mutation=='extra':
        extra=copy.deepcopy(data['changes'][0]);extra['adjustment']['id']='another';data['changes'].append(extra)
    if mutation=='duplicate_id':data['changes'].append(copy.deepcopy(data['changes'][0]))
    if mutation=='cursor':data['cursor']='repeated-cursor'
    http(monkeypatch,data)
    answer=batch[0].post(f"/products/production/additions/{row['id']}/check").json()
    assert answer['state']=='unknown' and answer['readiness']=='manual_review' and answer['attempts']==1


@pytest.mark.parametrize('mutation',['merchant','environment','mapping','product','body'])
def test_changed_local_destination_or_payload_blocks_without_network(batch,monkeypatch,mutation):
    row=prepared(batch);db=batch[3]
    from modules.orders import square_store as store
    doc,previous=store.load(db,1)
    if mutation=='revision':doc['revision']='changed'
    elif mutation=='merchant':doc['merchant_id']='OTHER'
    elif mutation=='environment':doc['environment']='production'
    elif mutation=='mapping':doc['mappings']['101']['variation_id']='OTHER'
    if mutation in ('revision','merchant','environment','mapping'):store.save(db,1,doc,previous)
    if mutation=='product':db.execute(text("UPDATE products SET name='changed' WHERE id=101"))
    if mutation=='body':
        data=json.loads(row['request_body']);data['changes'][0]['adjustment']['quantity']='999'
        db.execute(update(batches.additions).values(request_body=json.dumps(data)))
    db.commit();network=Mock(side_effect=AssertionError('no network'));monkeypatch.setattr(recovery,'live_check',network)
    answer=batch[0].post(f"/products/production/additions/{row['id']}/check").json()
    assert answer['readiness']=='blocked' and answer['state']=='prepared'
    network.assert_not_called()


def test_remote_location_tracking_merchant_and_revision_rechecks(batch,monkeypatch):
    row=prepared(batch)
    monkeypatch.setattr(wire.SquareInventoryProbe,'locations',lambda *_:[])
    answer=batch[0].post(f"/products/production/additions/{row['id']}/check").json()
    assert answer['readiness']=='blocked' and 'location' in answer['message']


def test_check_is_read_only_and_terminal_state_needs_no_connector(batch,monkeypatch):
    row=prepared(batch)
    factory,_=http(monkeypatch)
    answer=batch[0].post(f"/products/production/additions/{row['id']}/check").json()
    assert answer['readiness']=='checks_passed' and answer['square_writes_enabled'] is False
    factory.assert_not_called()  # Existing reader fixture handles reads only.
    db=batch[3];db.execute(update(batches.additions).values(state='confirmed',attempts=1));db.commit()
    monkeypatch.setattr(recovery.store,'load',Mock(side_effect=AssertionError('no connector access')))
    assert batch[0].post(f"/products/production/additions/{row['id']}/check").json()['state']=='confirmed'


def test_check_tenant_role_and_api_credential_denials(batch,monkeypatch):
    row=prepared(batch);client,user=batch[:2];url=f"/products/production/additions/{row['id']}/check"
    user['role']='viewer';assert client.post(url).status_code==403
    user['role']='admin';user['_auth_kind']='user_api_token';assert client.post(url).status_code==403
    user['_auth_kind']='jwt';user['group_id']=2;assert client.post(url).status_code==404
    for url in ('/products/production/deliver',f"/products/production/additions/{row['id']}/send",f"/products/production/additions/{row['id']}/retry"):
        assert client.post(url).status_code in (404,405)


def test_history_search_pagination_and_linked_addition_beyond_first100(batch):
    row=prepared(batch);db=batch[3];original=dict(db.execute(select(batches.receipts)).mappings().one())
    detail=json.loads(original['definition'])
    for i in range(105):
        data={**detail,'recipe_name':f'History {i:03}','occurred_at':f'2026-10-20T12:{i//60:02}:{i%60:02}Z'}
        db.execute(batches.receipts.insert().values(**{**original,'id':f'{i+1:032x}','job_uid':f'job-{i}','claim_token':f'{i+200:032x}','definition':json.dumps(data)}))
    db.commit();client=batch[0]
    page=client.get('/products/production/history').json();assert len(page['receipts'])==25 and page['next_offset']==25
    dates=[r['occurred_at'] for r in page['receipts']];assert dates==sorted(dates,reverse=True)
    found=client.get('/products/production/history',params={'query':'History 104'}).json();assert len(found['receipts'])==1
    found=client.get('/products/production/history',params={'state':'prepared'}).json();assert found['additions'][0]['id']==row['id'] and len(found['receipts'])==1
    assert client.get('/products/production/history',params={'query':'%'}).json()['receipts']==[]
    assert client.get('/products/production/history',params={'offset':-1}).status_code==422
    assert client.get('/products/production/history',params={'state':'made_up'}).status_code==422
    batch[1]['group_id']=2;assert client.get('/products/production/history').json()['receipts']==[]


def test_simultaneous_execution_reserves_only_one_network_send(batch,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    row=prepared(batch);enable(batch);engine=batch[3].get_bind();batch[3].rollback()
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _,session=http(monkeypatch,counts(row['request_body']))
    barrier=Barrier(2);original=recovery.live_check
    def preflight(*args,**kwargs):
        answer=original(*args,**kwargs);barrier.wait(timeout=10);return answer
    monkeypatch.setattr(recovery,'live_check',preflight)
    def run():
        with Session(engine) as db:
            try:return recovery.execute_prepared(db,dict(batch[1]),row['id'])['state']
            except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:answers=list(pool.map(lambda _:run(),range(2)))
    assert answers.count('confirmed')==1 and answers.count(409)==1
    assert session.request.call_count==1


def test_configuration_race_prevents_reservation_and_send(batch,monkeypatch):
    from sqlalchemy.orm import Session
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    factory,_=http(monkeypatch,counts(row['request_body']))
    original=recovery.live_check
    def race(doc,row,**kwargs):
        answer=original(doc,row,**kwargs)
        with Session(batch[3].get_bind()) as other:
            config,prior=recovery.store.load(other,1);recovery.store.save(other,1,config,prior);other.commit()
        return answer
    monkeypatch.setattr(recovery,'live_check',race)
    with pytest.raises(HTTPException):recovery.execute_prepared(batch[3],batch[1],row['id'])
    batch[3].rollback();factory.assert_not_called()
    assert batch[3].execute(select(batches.additions.c.attempts)).scalar()==0


def test_crash_after_remote_ack_keeps_durable_unknown_and_reconciles(batch,monkeypatch):
    class ProcessStopped(BaseException):pass
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    http(monkeypatch,counts(row['request_body']));original=wire.SquareWriter.send
    def crash(self,raw,**kwargs):assert original(self,raw,**kwargs);raise ProcessStopped()
    monkeypatch.setattr(wire.SquareWriter,'send',crash)
    with pytest.raises(ProcessStopped):recovery.execute_prepared(batch[3],batch[1],row['id'])
    batch[3].rollback();assert batch[3].execute(select(batches.additions.c.state)).scalar()=='unknown'
    _,session=http(monkeypatch,history(row['request_body']))
    assert recovery.check(batch[3],batch[1],row['id'])['state']=='confirmed'
    assert session.request.call_args.args[1].endswith('batch-retrieve')


@pytest.mark.parametrize('mutation',['physical_count','quantity','reference','duplicate','timestamp','extra_key'])
def test_invalid_saved_mutation_never_reaches_network(batch,monkeypatch,mutation):
    row=prepared(batch);data=json.loads(row['request_body']);a=data['changes'][0]['adjustment']
    if mutation=='physical_count':data['changes'][0]['type']='PHYSICAL_COUNT'
    elif mutation=='quantity':a['quantity']='1.5'
    elif mutation=='reference':a['reference_id']='different'
    elif mutation=='duplicate':data['changes'].append(copy.deepcopy(data['changes'][0]))
    elif mutation=='timestamp':a['occurred_at']='bad'
    elif mutation=='extra_key':data['extra']='unwanted'
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True);factory,_=http(monkeypatch,{})
    with pytest.raises(HTTPException):wire.SquareWriter('sandbox','synthetic').send(json.dumps(data),tenant_enabled=True)
    factory.assert_not_called()


@pytest.mark.parametrize('mutation',['tracking','merchant'])
def test_remote_identity_or_tracking_changes_block(batch,monkeypatch,mutation):
    row=prepared(batch)
    if mutation=='tracking':monkeypatch.setattr(wire.SquareInventoryProbe,'variation',lambda *_:{'tracked':False})
    else:monkeypatch.setattr(wire.SquareInventoryProbe,'merchant',lambda *_:'OTHER')
    answer=batch[0].post(f"/products/production/additions/{row['id']}/check").json()
    assert answer['readiness']=='blocked' and answer['attempts']==0


def test_pre_revision_additions_derive_identity_from_saved_request(batch,monkeypatch):
    row=prepared(batch);dest=json.loads(row['destination']);dest['connection_revision']='legacy-revision-no-longer-current'
    batch[3].execute(update(batches.additions).values(destination=json.dumps(dest)));batch[3].commit()
    answer=recovery.check(batch[3],batch[1],row['id'])
    assert answer['readiness']=='checks_passed' and answer['state']=='prepared'


@pytest.mark.parametrize('mutation',['malformed','oversized','duplicate_count','nonfinite_count'])
def test_response_integrity_limits_leave_durable_unknown(batch,monkeypatch,mutation):
    row=prepared(batch);enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    data=counts(row['request_body'])
    if mutation=='duplicate_count':data['counts'].append(copy.deepcopy(data['counts'][0]))
    if mutation=='nonfinite_count':data['counts'][0]['quantity']='NaN'
    _,session=http(monkeypatch,data)
    response=session.request.return_value
    if mutation=='malformed':response.iter_content.return_value=[b'private invalid json']
    if mutation=='oversized':response.iter_content.return_value=[b'x'*(4*1024*1024+1)]
    answer=recovery.execute_prepared(batch[3],batch[1],row['id'])
    assert answer['state']=='unknown' and 'private' not in str(answer)


def test_lowest_wire_boundary_rejects_other_endpoints_and_payloads(monkeypatch):
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True);factory,_=http(monkeypatch,{})
    for environment,path,raw in [('production','https://other.invalid/v2/inventory/changes/batch-create','{}'),('other','/v2/inventory/changes/batch-create','{}'),('production','/v2/payments','{}'),('production','/v2/inventory/changes/batch-create','{}')]:
        with pytest.raises(HTTPException):wire.exchange(environment,'synthetic',path,raw)
    factory.assert_not_called()


def test_withdrawn_baseline_send_has_distinct_terminal_conflict(batch,monkeypatch):
    row=prepared(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    batch[3].execute(update(batches.additions).values(state='withdrawn_baseline',attempts=0));batch[3].commit()
    factory,_=http(monkeypatch)
    monkeypatch.setattr(recovery.store,'load',Mock(side_effect=AssertionError('no connector access')))
    response=batch[0].post(f"/products/batches/additions/{row['id']}/send")
    assert response.status_code==409
    assert response.json()['detail']=='This addition was withdrawn into a baseline and is never sent.'
    factory.assert_not_called()
    saved=batch[3].execute(select(batches.additions)).mappings().one()
    assert (saved['state'],saved['attempts'])==('withdrawn_baseline',0)


def legacy_body(raw):
    data=json.loads(raw)
    for c in data['changes']:
        a=c['adjustment']
        a['location_id']=a.pop('to_location_id')
        assert a.pop('from_location_id')==a['location_id']
    return json.dumps(data)


def test_new_adjustment_shape_and_count_location(batch):
    row=prepared(batch)
    request=wire.validate_request(row['request_body'])
    for c in request['changes']:
        a=c['adjustment']
        assert a['from_location_id']==a['to_location_id']=='L1'
        assert 'location_id' not in a
    assert wire.valid_counts(counts(row['request_body']),request)


@pytest.mark.parametrize('mutation',['different_locations','retired','mixed'])
def test_location_shape_rejected_before_wire(batch,monkeypatch,mutation):
    row=prepared(batch);data=json.loads(row['request_body']);a=data['changes'][0]['adjustment']
    if mutation=='different_locations':a['from_location_id']='L2'
    elif mutation=='retired':data=json.loads(legacy_body(row['request_body']))
    else:a['location_id']='L1'
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True);factory,_=http(monkeypatch,{})
    with pytest.raises(HTTPException) as exc:
        wire.SquareWriter('sandbox','synthetic').send(json.dumps(data),tenant_enabled=True)
    assert exc.value.status_code==409
    factory.assert_not_called()


@pytest.mark.parametrize('state',['prepared','unknown'])
def test_legacy_evidence_readable_and_never_sent(batch,monkeypatch,state):
    row=prepared(batch);raw=legacy_body(row['request_body'])
    batch[3].execute(update(batches.additions).values(request_body=raw,state=state,attempts=int(state=='unknown')))
    batch[3].commit()
    factory,_=http(monkeypatch,{'changes':[]})
    page=batch[0].get('/products/production/history',params={'state':state})
    assert page.status_code==200 and page.json()['additions'][0]['id']==row['id']
    answer=recovery.check(batch[3],batch[1],row['id'])
    if state=='prepared':
        assert answer['readiness']=='checks_passed'
        assert answer['preview'][0]['location_id']=='L1'
        enable(batch);monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
        with pytest.raises(HTTPException) as exc:recovery.execute_prepared(batch[3],batch[1],row['id'])
        assert exc.value.status_code==409
        assert exc.value.detail=='This addition was prepared for a retired Square API shape; review it'
        factory.assert_not_called()
    else:assert answer['readiness']=='manual_review'
    saved=batch[3].execute(select(batches.additions)).mappings().one()
    assert saved['request_body']==raw and saved['attempts']==int(state=='unknown')


@pytest.mark.parametrize('legacy',[False,True])
def test_reconcile_from_to_adjustments(batch,monkeypatch,legacy):
    row=prepared(batch)
    raw=legacy_body(row['request_body']) if legacy else row['request_body']
    _,session=http(monkeypatch,history(row['request_body']))
    assert wire.SquareInventoryProbe('sandbox','synthetic').reconciled(raw)
    sent=json.loads(session.request.call_args.kwargs['data'])
    assert sent['location_ids']==['L1']
    wrong=history(row['request_body']);wrong['changes'][0]['adjustment']['from_location_id']='L2'
    http(monkeypatch,wrong)
    assert not wire.SquareInventoryProbe('sandbox','synthetic').reconciled(raw)
