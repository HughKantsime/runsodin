"""Synthetic physical components cannot become finished goods without assembly."""
import json,uuid
import pytest
from sqlalchemy import text,select
from tests.test_contracts.test_batch_acceptance import batch,make_recipe,body
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_education_review_workflow import review_db

def component(batch,job=301,good=3):
    return batch[0].post('/products/production/components',json={'job_id':job,'accepted':good,'rejected':0,'unit_description':'One complete handle, not nested sections','inspection_confirmed':True,'single_component_type_confirmed':True})
def bom(batch):
    response=batch[0].post('/products/101/components',json={'model_id':301,'quantity_needed':1})
    assert response.status_code==200,response.text
    r=batch[0].get('/products/production/finishing/101/requirements');assert r.status_code==200,r.text
    return r.json()
def finish_body(batch,receipt,preview,good=2,bad=0):
    return {'request_id':uuid.uuid4().hex,'product_id':101,'bom_hash':preview['bom_hash'],'accepted':good,'rejected':bad,
            'allocations':[{'receipt_id':receipt['id'],'quantity':good+bad}],
            'assembly_confirmed':True,'hardware_confirmed':True,'square_revision':batch[-1]['revision']}

def test_components_then_finished_receipt_and_prepared_addition_are_distinct(batch):
    preview=bom(batch);r=component(batch);assert r.status_code==200,r.text;receipt=r.json()
    assert receipt['stock_status']=='components_only'
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar()==0
    payload=finish_body(batch,receipt,preview,2,1)
    done=batch[0].post('/products/production/finishing',json=payload);assert done.status_code==200,done.text
    assert done.json()['outputs'][0]['accepted']==2
    assert batch[0].post('/products/production/finishing',json=payload).json()['id']==done.json()['id']
    state=batch[0].get('/products/production/finishing').json()
    assert state['components'][0]['available']==0 and len(state['finishes'])==1
    addition=batch[3].execute(text('SELECT receipt_id,request_body FROM production_stock_additions')).one()
    adjustment=json.loads(addition.request_body)['changes'][0]['adjustment']
    assert adjustment['from_location_id']==adjustment['to_location_id']=='L1'
    assert 'location_id' not in adjustment
    assert addition.receipt_id==done.json()['id'] and json.loads(addition.request_body)['changes'][0]['adjustment']['quantity']=='2'
    from modules.orders.stock_recovery import snapshot,operation
    aid=batch[3].execute(text('SELECT id FROM production_stock_additions')).scalar_one()
    snapshot(batch[3],1,operation(batch[3],1,aid))

@pytest.mark.parametrize('change',[{'assembly_confirmed':False},{'hardware_confirmed':False},{'accepted':4},{'bom_hash':'f'*64},{'square_revision':'stale'},{'allocations':[]}])
def test_invalid_finishing_never_consumes_or_prepares(batch,change):
    preview=bom(batch);receipt=component(batch).json();payload=finish_body(batch,receipt,preview);payload.update(change)
    assert batch[0].post('/products/production/finishing',json=payload).status_code in (409,422)
    assert batch[0].get('/products/production/finishing').json()['components'][0]['available']==3
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar()==0

def test_double_spend_changed_request_and_component_duplicate_refused(batch):
    preview=bom(batch);receipt=component(batch).json();assert component(batch).json()['id']==receipt['id']
    assert component(batch,good=4).status_code==409
    payload=finish_body(batch,receipt,preview);assert batch[0].post('/products/production/finishing',json=payload).status_code==200
    changed={**payload,'accepted':1};assert batch[0].post('/products/production/finishing',json=changed).status_code==409
    changed={**payload,'request_id':uuid.uuid4().hex};assert batch[0].post('/products/production/finishing',json=changed).status_code==409

def test_component_and_single_print_acceptance_mutually_exclusive(batch):
    recipe=make_recipe(batch);receipt=component(batch,302);assert receipt.status_code==200
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code==409

def test_reverse_legacy_acceptance_fence(batch):
    recipe=make_recipe(batch);assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code==200
    assert component(batch,302).status_code==409

def test_rejected_assembly_retains_consumption_without_square(batch):
    preview=bom(batch);receipt=component(batch).json();payload=finish_body(batch,receipt,preview,0,1);payload['square_revision']=None
    assert batch[0].post('/products/production/finishing',json=payload).status_code==200
    assert batch[0].get('/products/production/finishing').json()['components'][0]['available']==2
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar()==0

def test_changed_bom_source_or_organization_cannot_reuse_evidence(batch):
    preview=bom(batch);receipt=component(batch).json();payload=finish_body(batch,receipt,preview)
    batch[3].execute(text("UPDATE models SET name='Changed geometry label' WHERE id=301"));batch[3].commit()
    assert batch[0].post('/products/production/finishing',json=payload).status_code==409
    batch[1]['group_id']=2
    assert batch[0].get('/products/production/finishing').json()['components']==[]
    assert batch[0].post('/products/production/finishing',json=payload).status_code in (403,404,409)

def test_retained_model_and_tenant_cannot_be_deleted(batch):
    component(batch)
    from modules.orders.finishing import guard_model_delete,guard_tenant
    from fastapi import HTTPException
    for fn,args in ((guard_model_delete,(batch[3],1,301)),(guard_tenant,(batch[3],1))):
        with pytest.raises(HTTPException) as err:fn(*args)
        assert err.value.status_code==409

@pytest.mark.parametrize('role,kind',[('viewer','jwt'),('admin','user_api_token')])
def test_inspection_role_and_interactive_guards(batch,role,kind):
    batch[1]['role']=role;batch[1]['_auth_kind']=kind
    assert component(batch).status_code==403

def test_linked_hardware_cannot_disappear_via_consumable_delete(batch):
    from modules.orders.routes.consumables import router
    batch[4].include_router(router)
    batch[3].execute(text("INSERT INTO consumables(id,name,unit) VALUES(501,'Synthetic screw','piece')"));batch[3].commit()
    assert batch[0].post('/products/101/consumables',json={'consumable_id':501,'quantity_per_product':4}).status_code==200
    preview=bom(batch)
    assert preview['bom']['hardware'][0]['name']=='Synthetic screw' and preview['bom']['hardware'][0]['unit']=='piece'
    assert batch[0].delete('/consumables/501').status_code==409
    assert batch[3].execute(text('SELECT count(*) FROM product_consumables WHERE consumable_id=501')).scalar_one()==1

def test_finished_receipt_history_and_deletion_retention(batch):
    preview=bom(batch);receipt=component(batch).json();payload=finish_body(batch,receipt,preview)
    done=batch[0].post('/products/production/finishing',json=payload);assert done.status_code==200
    history=batch[0].get('/products/production/history').json()
    assert history['receipts'][0]['kind']=='assembly' and history['receipts'][0]['id']==done.json()['id']
    assert history['additions'][0]['receipt_id']==done.json()['id']
    assert batch[0].delete('/products/101').status_code==409

def test_real_model_delete_route_retains_inspected_source(batch):
    from modules.models_library.routes.models_crud import router
    batch[4].include_router(router)
    assert component(batch).status_code==200
    response=batch[0].delete('/models/301')
    assert response.status_code==409,response.text
    assert batch[3].execute(text('SELECT count(*) FROM models WHERE id=301')).scalar_one()==1


def test_assembly_addition_keeps_baseline_review_and_withdrawal_guards(batch):
    from tests.test_contracts.test_square_stock_release import baseline, review
    from modules.orders import stock_recovery as recovery
    preview=bom(batch);receipt=component(batch).json()
    done=batch[0].post('/products/production/finishing',json=finish_body(batch,receipt,preview))
    assert done.status_code==200,done.text
    row=dict(batch[3].execute(select(recovery.batches.additions)).mappings().one())
    assert baseline(batch).status_code==200
    with pytest.raises(__import__('fastapi').HTTPException) as err:
        recovery.snapshot(batch[3],1,row)
    assert err.value.detail=='baseline_review'
    assert review(batch,row).status_code==200
    recovery.snapshot(batch[3],1,row)
    assert review(batch,row,{'101':'counted'}).status_code==200
    assert recovery.operation(batch[3],1,row['id'])['state']=='withdrawn_baseline'
    history=batch[0].get('/products/production/history',params={'state':'withdrawn_baseline'}).json()
    assert history['receipts'][0]['id']==done.json()['id']
    assert batch[0].get('/products/production/history',params={'state':'in_baseline'}).json()['receipts']==[]
