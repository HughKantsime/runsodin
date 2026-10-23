"""Optional preparation state does not schedule work or claim capacity."""
from datetime import date,timedelta,datetime,timezone
import pytest
from sqlalchemy import text
from tests.test_contracts.test_batch_acceptance import batch,make_recipe
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_education_review_workflow import review_db

BASE='/products/production/operations'
@pytest.fixture
def ops(batch,monkeypatch):
    from modules.orders.routes.operations import router
    batch[4].include_router(router)
    from modules.orders.routes.orders_crud import router as order_router
    from modules.orders.production import OrderFulfillmentService
    from core.registry import registry
    monkeypatch.setitem(registry._providers,'OrderFulfillmentProvider',OrderFulfillmentService())
    batch[4].include_router(order_router)
    batch[4].router.routes.sort(key=lambda r:0 if getattr(r,'path','').startswith(BASE) else 1)
    return batch

def kit(ops,**changes):
    state=ops[0].get(BASE).json();body={'revision':state['revision'],'name':'Table cover','available':1,'condition':'ready','issue':'','resolution':'','active':True};body.update(changes)
    return ops[0].put(BASE+'/kit',json=body)

def test_damage_persists_repair_explanation_and_packing_invalidates(ops):
    state=kit(ops).json();item=state['kit'][0];revision=state['revision']
    body={'revision':revision,'title':'Synthetic future show','starts_on':'2027-10-16','status':'planned','picks':[{'item_id':item['id'],'quantity':1}]}
    state=ops[0].put(BASE+'/events',json=body).json();event=state['events'][0]
    assert not event['all_selected_packed'] and not event['ready_to_leave']
    url=f"{BASE}/events/{event['id']}/packing/{item['id']}"
    response=ops[0].put(url,json={'revision':state['revision'],'confirmed':True});assert response.status_code==200,response.text
    assert response.json()['events'][0]['all_selected_packed']
    state=kit(ops,item_id=item['id'],condition='damaged',issue='Torn corner').json()
    assert not state['events'][0]['all_selected_packed'] and state['kit'][0]['issue']=='Torn corner'
    assert ops[0].put(url,json={'revision':state['revision'],'confirmed':True}).status_code==409
    assert kit(ops,item_id=item['id']).status_code==409
    repaired=kit(ops,item_id=item['id'],resolution='Repaired seam').json()
    assert not repaired['events'][0]['all_selected_packed']
    assert kit(ops,item_id=item['id'],revision=revision).status_code==409

def test_kit_errors_private_and_tenant_guard(ops):
    response=kit(ops,condition='damaged');assert response.status_code==409
    bad=kit(ops,available=True,name='PRIVATE-SYNTHETIC')
    assert bad.status_code==422 and 'PRIVATE-SYNTHETIC' not in bad.text
    assert 'no-store' in bad.headers['cache-control']
    assert kit(ops).status_code==200
    ops[1]['group_id']=2;assert ops[0].get(BASE).json()['kit']==[]
    ops[1]['role']='viewer';assert ops[0].get(BASE).status_code==403

def test_conflict_inputs_missing_then_overbooked_then_changed_order_unknown(ops):
    r=ops[0].post('/orders',json={'order_number':'SYNTHETIC-WHOLESALE','platform':'wholesale','revenue':40,'shipping_charged':0,'items':[{'product_id':101,'quantity':2,'unit_price':20}]});assert r.status_code==200,r.text
    oid=r.json()['id'];initial=ops[0].get(BASE+'/conflicts').json();assert initial['state']=='unknown' and not initial['capacity_verified']
    today=date.today().isoformat();payload={'revision':initial['revision'],'starts_on':today,'ends_on':today,'available_minutes':30,'estimates':[{'order_id':oid,'minutes':60,'promised_by':today}]}
    response=ops[0].put(BASE+'/capacity',json=payload);assert response.status_code==200,response.text
    assert response.json()['state']=='conflict' and response.json()['known_minutes_in_window']==60 and not response.json()['queue_changed']
    payload.update(revision=response.json()['revision'],available_minutes=90)
    clear=ops[0].put(BASE+'/capacity',json=payload);assert clear.json()['state']=='within_entered_total'
    ops[3].execute(text('UPDATE order_items SET quantity=3 WHERE order_id=:id'),{'id':oid});ops[3].commit()
    latest=ops[0].get(BASE+'/conflicts').json();assert latest['state']=='unknown' and latest['orders'][0]['entered_minutes'] is None

@pytest.mark.parametrize('change',[{'available_minutes':-1},{'available_minutes':1441},{'ends_on':'2020-01-01'},{'available_minutes':True}])
def test_invalid_capacity_never_schedules(ops,change):
    today=date.today().isoformat();body={'revision':None,'starts_on':today,'ends_on':today,'available_minutes':10,'estimates':[]};body.update(change)
    assert ops[0].put(BASE+'/capacity',json=body).status_code==422
    assert ops[0].get(BASE).json()['capacity'] is None

def test_file_name_or_saved_ids_do_not_prove_file_exists_or_dispatch_readiness(ops):
    recipe=make_recipe(ops)
    response=ops[0].get(BASE+'/jobs/301/readiness');assert response.status_code==200,response.text
    facts=response.json();assert facts['retained_physical_evidence'] and len(facts['matching_recipes'])==1
    assert not facts['saved_preparation_complete'] and not facts['dispatch_ready'] and not facts['file_bytes_checked']
    assert any('file record' in r for r in facts['reasons'])
    ops[3].execute(text('UPDATE jobs SET payload_sha256=NULL WHERE id=301'));ops[3].commit()
    assert not ops[0].get(BASE+'/jobs/301/readiness').json()['saved_preparation_complete']
    ops[1]['group_id']=2;assert ops[0].get(BASE+'/jobs/301/readiness').status_code==404
