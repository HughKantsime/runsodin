"""Explicit local assortment policy with simulated Square reads, no stock/queue writes."""
import json
import pytest
from sqlalchemy import select,text,update
from tests.test_contracts.test_batch_acceptance import batch,make_recipe,body
from tests.test_contracts.test_square_readonly_workflow import shop,mapped
from tests.test_contracts.test_education_review_workflow import review_db
from modules.orders import assortment,batch_production as batches

BASE='/products/production/assortment'

def policy(batch,assortment='active',kind='multicolor_character',pid=101,revision=None):
    if revision is None:revision=batch[0].get(BASE).json()['revision']
    return batch[0].put(f'{BASE}/{pid}',json={'revision':revision,'assortment':assortment,'kind':kind})


def ready(batch,yield_count=5):
    # Both completed fixture plates must be accounted for, never silently discarded.
    recipe=make_recipe(batch,outputs=[{'product_id':101,'kind':'main','expected':yield_count}])
    for jid in (301,302):
        request=body(batch,recipe);request['job_id']=jid;request['outputs'][0].update(accepted=yield_count-1,rejected=1)
        assert batch[0].post('/products/production/inspections',json=request).status_code==200
    batch[3].execute(update(batches.additions).values(state='confirmed',attempts=1));batch[3].commit()
    return recipe


def stock(monkeypatch,quantity='2',state='known'):
    from modules.orders.square_client import SquareReader
    monkeypatch.setattr(SquareReader,'count',lambda *_:{'quantity':quantity,'state':state,'calculated_at':'2026-10-20T12:00:00Z'})


def test_policy_unconfigured_explicit_inactive_trial_and_no_remote_reads(batch):
    client,_,calls,*_=batch;count=len(calls)
    state=client.get(BASE).json();assert {p['id'] for p in state['products']}=={101,104}
    assert all(p['policy'] is None and p['rules']['routine_replenishment'] is False for p in state['products'])
    assert client.post(f'{BASE}/101/preview').json()['status']=='unconfigured'
    assert policy(batch,assortment='inactive').status_code==200
    assert client.post(f'{BASE}/101/preview').json()['status']=='inactive'
    assert policy(batch,assortment='trial').status_code==200
    assert client.post(f'{BASE}/101/preview').json()['status']=='trial'
    assert len(calls)==count


@pytest.mark.parametrize('kind,floor,minimum',[('multicolor_character',5,5),('dragon_jumbo',15,1),('dragon_half',15,1),('dragon_other',5,1),('other',5,1)])
def test_rule_threshold_is_separate_from_batch_minimum(batch,kind,floor,minimum):
    answer=policy(batch,kind=kind).json();entry=next(p for p in answer['products'] if p['id']==101)
    assert entry['rules']=={'trigger_below':floor,'minimum_main_batch':minimum,'routine_replenishment':True}


def test_stale_edit_refuses_and_preserves_other_products(batch):
    first=policy(batch).json();revision=first['revision'];policy(batch,pid=104,kind='other')
    r=policy(batch,assortment='inactive',revision=revision);assert r.status_code==409
    saved=batch[0].get(BASE).json()['products'];assert saved[0]['policy']['assortment']=='active' and saved[1]['policy']['kind']=='other'


@pytest.mark.parametrize('role,auth,group,status',[('viewer','jwt',1,403),('operator','jwt',1,403),('admin','user_api_token',1,403),('admin','jwt',2,403)])
def test_policy_edit_tenant_role_and_credential_guards(batch,role,auth,group,status):
    batch[1].update(role=role,_auth_kind=auth,group_id=group)
    answer=batch[0].put(f'{BASE}/101',json={'revision':None,'assortment':'active','kind':'other'})
    assert answer.status_code==status


def test_viewer_read_only_and_preview_denials(batch):
    policy(batch);client,user=batch[:2]
    user['role']='viewer';assert client.get(BASE).status_code==200;assert client.post(f'{BASE}/101/preview').status_code==403
    user.update(role='operator',_auth_kind='user_api_token');assert client.post(f'{BASE}/101/preview').status_code==403
    user.update(role='admin',_auth_kind='jwt');assert client.post(f'{BASE}/102/preview').status_code==404
    assert client.post(f'{BASE}/103/preview').status_code==404


def test_multicolor_five_qualification_but_drafts_and_actual_inspection_preserved(batch):
    # Historical3piecequalified recipe remains immutable and its real output may be recorded.
    recipe=make_recipe(batch,outputs=[{'product_id':101,'kind':'main','expected':3}]);assert policy(batch).status_code==200
    data={'reference_job_id':301,'name':'Three','materials':'PLA','qualification_confirmed':True,
          'outputs':[{'product_id':101,'kind':'main','expected':3}]}
    assert batch[0].post('/products/production/recipes',json=data).status_code==409
    data['qualification_confirmed']=False;assert batch[0].post('/products/production/recipes',json=data).status_code==200
    request=body(batch,recipe);request['outputs'][0].update(accepted=2,rejected=1)
    assert batch[0].post('/products/production/inspections',json=request).status_code==200
    data['outputs'][0]['expected']=5;data['qualification_confirmed']=True
    assert batch[0].post('/products/production/recipes',json=data).status_code==200


def test_small_flush_character_is_not_a_main_batch_violation(batch):
    policy(batch,pid=104)
    data={'reference_job_id':301,'name':'Mixed','materials':'PLA','qualification_confirmed':True,
          'outputs':[{'product_id':101,'kind':'main','expected':1},{'product_id':104,'kind':'flush','expected':1}]}
    assert batch[0].post('/products/production/recipes',json=data).status_code==200


def test_low_two_uses_fixed_five_yield_not_three_deficit(batch,monkeypatch):
    recipe=ready(batch);policy(batch);stock(monkeypatch,'2')
    answer=batch[0].post(f'{BASE}/101/preview').json()
    assert answer['status']=='candidate' and answer['stock']['quantity']=='2'
    assert answer['recipes'][0]['id']==recipe['id'] and answer['recipes'][0]['main_yield']==5
    assert answer['advisory_only'] is True
    assert len(batch[0].get('/products/production/jobs').json()['jobs'])==2


@pytest.mark.parametrize('value,state',[('5','known'),('15','known'),('99','known')])
def test_stock_at_or_above_floor_is_quiet(batch,monkeypatch,value,state):
    ready(batch);policy(batch);stock(monkeypatch,value,state)
    answer=batch[0].post(f'{BASE}/101/preview').json();assert answer['status']=='enough' and answer['recipes']==[]


@pytest.mark.parametrize('value,state',[(None,'unknown'),('-1','known'),('1.5','known'),('NaN','known')])
def test_unknown_negative_or_fractional_stock_never_infers_shortage(batch,monkeypatch,value,state):
    ready(batch);policy(batch);stock(monkeypatch,value,state)
    answer=batch[0].post(f'{BASE}/101/preview').json();assert answer['status']=='stock_review' and not answer['recipes']


def test_dragon_below_fifteen_and_fixed_yield_not_target(batch,monkeypatch):
    ready(batch,yield_count=2);policy(batch,kind='dragon_jumbo');stock(monkeypatch,'14')
    answer=batch[0].post(f'{BASE}/101/preview').json();assert answer['status']=='candidate' and answer['recipes'][0]['main_yield']==2


@pytest.mark.parametrize('state',['prepared','unknown'])
def test_pending_square_addition_suppresses_duplicate_plate(batch,monkeypatch,state):
    recipe=make_recipe(batch);batch[0].post('/products/production/inspections',json=body(batch,recipe))
    batch[3].execute(update(batches.additions).values(state=state));batch[3].commit();policy(batch);stock(monkeypatch)
    answer=batch[0].post(f'{BASE}/101/preview').json();assert answer['status']=='pending_review' and not answer['recipes']


@pytest.mark.parametrize('state,unassigned',[('pending',True),('scheduled',False),('printing',False),('paused',False),('completed',False)])
def test_existing_jobs_and_unassigned_work_block_duplicate_suggestions(batch,monkeypatch,state,unassigned):
    recipe=make_recipe(batch);policy(batch);stock(monkeypatch)
    batch[3].execute(text('UPDATE jobs SET status=:status,printer_id=:printer WHERE id=302'),{'status':state,'printer':None if unassigned else 301});batch[3].commit()
    answer=batch[0].post(f'{BASE}/101/preview').json();assert answer['status']=='pending_review'


def test_old_three_piece_recipe_not_suggested_under_character_policy(batch,monkeypatch):
    ready(batch,yield_count=3);policy(batch);stock(monkeypatch)
    answer=batch[0].post(f'{BASE}/101/preview').json();assert answer['status']=='recipe_needed' and not answer['recipes']


def test_configuration_edit_during_remote_reads_refuses_stale_preview(batch,monkeypatch):
    ready(batch);policy(batch)
    from modules.orders.square_client import SquareReader
    from sqlalchemy.orm import Session
    def race(*_):
        with Session(batch[3].get_bind()) as db:
            doc,prior=assortment.load(db,1);doc['products']['101']['assortment']='inactive';assortment.save(db,1,doc,prior);db.commit()
        return {'quantity':'2','state':'known','calculated_at':None}
    monkeypatch.setattr(SquareReader,'count',race)
    assert batch[0].post(f'{BASE}/101/preview').status_code==409


def test_inactive_retains_stock_files_orders_and_policy_delete_cleanup(batch):
    policy(batch,pid=104);assert policy(batch,assortment='inactive',pid=104).status_code==200
    assert batch[0].get('/products/104').status_code==200
    assert batch[0].delete('/products/104').status_code==204
    doc,_=assortment.load(batch[3],1);assert '104' not in doc['products']
    batch[3].execute(text("INSERT INTO products(id,name,org_id) VALUES(104,'New identity',1)"));batch[3].commit()
    entry=next(p for p in batch[0].get(BASE).json()['products'] if p['id']==104);assert entry['policy'] is None


@pytest.mark.parametrize('missing',['print_file_id','payload_sha256','plate_index'])
def test_missing_preparation_evidence_is_pending_uncertainty(batch,monkeypatch,missing):
    make_recipe(batch);policy(batch);stock(monkeypatch)
    from modules.jobs.models import Job
    batch[3].execute(update(Job).where(Job.id==302).values(**{missing:None},status='pending',printer_id=None));batch[3].commit()
    assert batch[0].post(f'{BASE}/101/preview').json()['status']=='pending_review'


def test_pending_job_on_another_machine_does_not_create_a_duplicate_plate(batch,monkeypatch):
    make_recipe(batch);policy(batch);stock(monkeypatch)
    batch[3].execute(text("INSERT INTO printers(id,name,model,org_id) VALUES(399,'Other','X1',1)"))
    batch[3].execute(text("UPDATE jobs SET status='pending',printer_id=399 WHERE id=302"));batch[3].commit()
    assert batch[0].post(f'{BASE}/101/preview').json()['status']=='pending_review'


def test_truncated_job_scan_is_not_assumed_empty(batch,monkeypatch):
    ready(batch);policy(batch);stock(monkeypatch)
    for i in range(201):batch[3].execute(text("INSERT INTO jobs(id,item_name,model_id,status,charged_to_org_id) VALUES(:id,'uncertain',301,'pending',1)"),{'id':500+i})
    batch[3].commit();answer=batch[0].post(f'{BASE}/101/preview').json()
    assert answer['status']=='pending_review' and 'incomplete' in answer['message']


def test_square_untracked_and_missing_connection_do_not_invent_stock(batch,monkeypatch):
    ready(batch);policy(batch)
    from modules.orders.square_client import SquareReader
    monkeypatch.setattr(SquareReader,'variation',lambda *_:{'tracked':False})
    assert batch[0].post(f'{BASE}/101/preview').json()['status']=='blocked'
    from modules.orders import square_store
    batch[3].execute(text('DELETE FROM system_config WHERE key=:key'),{'key':square_store.key(1)});batch[3].commit()
    assert batch[0].post(f'{BASE}/101/preview').json()['status']=='blocked'


def test_policy_tenant_cleanup_occurs_inside_existing_transaction(batch):
    policy(batch,pid=104)
    from modules.orders.square_store import forget_tenant
    forget_tenant(batch[3],1)
    assert assortment.load(batch[3],1)[1] is None
    batch[3].rollback()
    assert '104' in assortment.load(batch[3],1)[0]['products']


def test_retained_recipe_blocks_product_delete_without_losing_policy(batch):
    make_recipe(batch);policy(batch)
    assert batch[0].delete('/products/101').status_code==409
    assert assortment.load(batch[3],1)[0]['products']['101']['assortment']=='active'


@pytest.mark.parametrize('other_kind,expected_status',[('main','recipe_needed'),('flush','candidate')])
def test_mixed_existing_recipe_checks_all_current_main_policies_not_flush(batch,monkeypatch,other_kind,expected_status):
    ready(batch)
    make_recipe(batch,outputs=[{'product_id':101,'kind':other_kind,'expected':1},{'product_id':104,'kind':'main','expected':5}])
    policy(batch,pid=101,kind='multicolor_character');policy(batch,pid=104,kind='other')
    from modules.orders import square_store
    doc,prior=square_store.load(batch[3],1)
    doc['mappings']['104']={'variation_id':'V2','location_id':'L1','item_name':'Second','variation_name':'Default'}
    square_store.save(batch[3],1,doc,prior);batch[3].commit()
    from modules.orders.square_client import SquareReader
    monkeypatch.setattr(SquareReader,'variation',lambda *_:{'tracked':True});stock(monkeypatch,'2')
    answer=batch[0].post(f'{BASE}/104/preview').json()
    assert answer['status']==expected_status,answer
