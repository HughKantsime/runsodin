"""Order production acceptance through HTTP and fresh persisted job/stock records."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client

@pytest.fixture
def production_order(bom_client,review_db):
    from core.dependencies import get_current_user
    from modules.jobs.routes import jobs_crud
    identity={'id':3,'role':'operator','group_id':1,'is_active':True}
    bom_client.app.dependency_overrides[get_current_user]=lambda:identity
    bom_client.app.include_router(jobs_crud.router)
    review_db.execute(text("UPDATE models SET quantity_per_bed=4,build_time_hours=1,default_filament_type='PLA' WHERE id=101"))
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,status,cost_per_unit) VALUES(101,'Synthetic packaging',20,'active',0.5)"))
    review_db.execute(text('INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(101,101,2)'))
    review_db.commit()
    response=bom_client.post('/products/101/components',json={'model_id':101,'quantity_needed':3})
    assert response.status_code==200,response.text
    response=bom_client.post('/orders',json={'order_number':'Synthetic production','items':[{'product_id':101,'quantity':3,'unit_price':20}]})
    assert response.status_code==200,response.text
    return bom_client,identity,response.json()['id']


def snapshot(review_db,order_id):
    with Session(review_db.get_bind()) as fresh:
        return {
            'jobs':[dict(row) for row in fresh.execute(text('SELECT id,charged_to_org_id,charged_to_user_id,submitted_by,quantity_on_bed FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:order_id) ORDER BY id'),{'order_id':order_id}).mappings()],
            'stock':fresh.execute(text('SELECT current_stock FROM consumables WHERE id=101')).scalar_one(),
            'usage':[dict(row) for row in fresh.execute(text('SELECT quantity_used FROM consumable_usage WHERE order_id=:order_id ORDER BY id'),{'order_id':order_id}).mappings()],
            'status':fresh.execute(text('SELECT status FROM orders WHERE id=:id'),{'id':order_id}).scalar_one(),
        }


def test_order_generated_jobs_preserve_org_and_actor(production_order,review_db):
    client,identity,order_id=production_order
    response=client.post(f'/orders/{order_id}/schedule');assert response.status_code==200,response.text
    state=snapshot(review_db,order_id)
    assert len(state['jobs'])==3 and [j['quantity_on_bed'] for j in state['jobs']]==[4,4,1]
    assert {(j['charged_to_org_id'],j['charged_to_user_id'],j['submitted_by']) for j in state['jobs']}=={(1,3,3)}
    assert state['stock']==14 and state['usage']==[{'quantity_used':6}] and state['status']=='in_progress'


def test_other_org_cannot_read_or_mutate_order_generated_jobs(production_order,review_db):
    client,identity,order_id=production_order
    response=client.post(f'/orders/{order_id}/schedule');assert response.status_code==200,response.text
    state=snapshot(review_db,order_id);job_id=state['jobs'][0]['id']
    identity.update(id=5,group_id=2)
    response=client.get(f'/jobs/{job_id}');assert response.status_code==404,response.text
    response=client.patch(f'/jobs/{job_id}',json={'item_name':'Unauthorized'});assert response.status_code==404,response.text
    assert snapshot(review_db,order_id)==state


def test_repeated_order_schedule_does_not_duplicate_jobs_or_consume_stock(production_order,review_db):
    client,identity,order_id=production_order
    response=client.post(f'/orders/{order_id}/schedule');assert response.status_code==200,response.text
    before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code in (200,409),response.text
    assert snapshot(review_db,order_id)==before


def test_shortage_rejects_entire_mixed_bom_schedule(production_order,review_db):
    client,identity,order_id=production_order
    review_db.execute(text('UPDATE consumables SET current_stock=3 WHERE id=101'));review_db.commit()
    before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==409,response.text
    assert snapshot(review_db,order_id)==before

@pytest.mark.parametrize('status',['in_progress','partial','fulfilled','shipped','cancelled'])
def test_non_pending_order_cannot_generate_production(production_order,review_db,status):
    client,identity,order_id=production_order
    review_db.execute(text('UPDATE orders SET status=:status WHERE id=:id'),{'status':status,'id':order_id});review_db.commit()
    before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==409,response.text
    assert snapshot(review_db,order_id)==before


def test_late_consumable_shortage_rolls_back_all_earlier_changes(production_order,review_db):
    client,identity,order_id=production_order
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,status,cost_per_unit) VALUES(102,'Second synthetic consumable',1,'active',1)"))
    review_db.execute(text('INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(101,102,2)'));review_db.commit()
    before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==409,response.text
    assert snapshot(review_db,order_id)==before
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT current_stock FROM consumables WHERE id=102')).scalar_one()==1
        assert fresh.execute(text('SELECT COUNT(*) FROM consumable_usage WHERE consumable_id=102')).scalar_one()==0


def test_two_scheduling_sessions_cannot_duplicate_production(production_order,review_db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client,identity,order_id=production_order
    barrier=Barrier(2)
    original=Session.execute
    def synchronized(session,statement,*args,**kwargs):
        if str(statement).lstrip().upper().startswith('UPDATE ORDERS') and not session.info.get('schedule_race_arrived'):
            session.info['schedule_race_arrived'] = True
            barrier.wait(timeout=10)
        return original(session,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post(f'/orders/{order_id}/schedule'),range(2)))
    assert sorted(response.status_code for response in responses)==[200,409]
    state=snapshot(review_db,order_id)
    assert len(state['jobs'])==3 and state['stock']==14 and state['usage']==[{'quantity_used':6}]


@pytest.mark.parametrize('historical',['job','usage'])
def test_pending_order_with_historical_production_does_not_regenerate(production_order,review_db,historical):
    client,identity,order_id=production_order
    if historical=='job':
        review_db.execute(text("INSERT INTO jobs(item_name,status,order_item_id,charged_to_org_id) SELECT 'Historical cancelled','cancelled',id,1 FROM order_items WHERE order_id=:id"),{'id':order_id})
    else:
        review_db.execute(text("INSERT INTO consumable_usage(consumable_id,order_id,quantity_used,notes) VALUES(101,:id,1,'Historical')"),{'id':order_id})
    review_db.commit();before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==409,response.text
    assert snapshot(review_db,order_id)==before


def test_inactive_required_consumable_rejects_order_without_omission(production_order,review_db):
    client,identity,order_id=production_order
    review_db.execute(text("UPDATE consumables SET status='inactive' WHERE id=101"));review_db.commit()
    before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==409,response.text
    assert snapshot(review_db,order_id)==before


def test_no_work_order_remains_pending_and_does_not_consume_stock(production_order,review_db):
    client,identity,order_id=production_order
    review_db.execute(text('DELETE FROM product_components WHERE product_id=101'))
    review_db.execute(text('DELETE FROM product_consumables WHERE product_id=101'));review_db.commit()
    before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==200,response.text
    assert response.json()['jobs_created']==0 and response.json()['consumables_deducted']==[]
    assert snapshot(review_db,order_id)==before


def test_shared_order_keeps_shared_job_ownership(production_order,review_db):
    client,identity,order_id=production_order
    review_db.execute(text('UPDATE orders SET org_id=NULL WHERE id=:id'),{'id':order_id});review_db.commit()
    response=client.post(f'/orders/{order_id}/schedule');assert response.status_code==200,response.text
    state=snapshot(review_db,order_id)
    assert {j['charged_to_org_id'] for j in state['jobs']}=={None}
    assert {j['submitted_by'] for j in state['jobs']}=={3}


def test_shared_consumable_demand_is_aggregated_across_items(production_order,review_db):
    client,identity,order_id=production_order
    review_db.execute(text('UPDATE product_consumables SET quantity_per_product=0.25 WHERE product_id=101'))
    review_db.execute(text('UPDATE consumables SET current_stock=1.25 WHERE id=101'))
    review_db.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(103,'Second own product',1,1)"))
    review_db.execute(text('INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(103,101,0.125)'))
    review_db.commit()
    response=client.post(f'/orders/{order_id}/items',json={'product_id':103,'quantity':2,'unit_price':1})
    assert response.status_code==200,response.text
    response=client.post(f'/orders/{order_id}/schedule');assert response.status_code==200,response.text
    state=snapshot(review_db,order_id)
    assert state['stock']==0.25 and state['usage']==[{'quantity_used':1}]
    assert response.json()['consumables_deducted']==[{'consumable':'Synthetic packaging','quantity_deducted':1,'remaining_stock':0.25}]


def test_nonfinite_stored_stock_rejects_schedule_without_writes(production_order,review_db):
    client,identity,order_id=production_order
    review_db.execute(text('UPDATE consumables SET current_stock=:stock WHERE id=101'),{'stock':float('inf')});review_db.commit()
    before=snapshot(review_db,order_id)
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==409,response.text
    assert snapshot(review_db,order_id)==before
