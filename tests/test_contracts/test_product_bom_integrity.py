"""Product BOM behavior, costing and tenant-safe references via actual HTTP routes."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def bom_client(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.orders.routes import products, orders_crud
    review_db.execute(text("INSERT INTO models(id,name,org_id,cost_per_item) VALUES(101,'Own model',1,2.5),(102,'Foreign private model',2,19),(103,'Shared model',NULL,1)"))
    review_db.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(101,'Own product',1,20),(102,'Foreign product',2,30)"))
    review_db.commit()
    app=FastAPI();app.include_router(products.router);app.include_router(orders_crud.router)
    def request_db():
        with Session(review_db.get_bind()) as s:yield s
    app.dependency_overrides[get_db]=request_db
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'operator','group_id':1,'is_active':True}
    return TestClient(app)

@pytest.mark.parametrize('model_id',[102,99999])
@pytest.mark.parametrize('path',['/products','/products/101/components'])
def test_bom_rejects_foreign_or_missing_model_atomically(bom_client,review_db,model_id,path):
    payload={'model_id':model_id,'quantity_needed':2}
    if path=='/products':payload={'name':'Must not persist','components':[{'model_id':101,'quantity_needed':1},payload]}
    before=review_db.execute(text('SELECT count(*) FROM products')).scalar_one()
    response=bom_client.post(path,json=payload)
    assert response.status_code==404,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT count(*) FROM products')).scalar_one()==before
        assert fresh.execute(text('SELECT count(*) FROM product_components')).scalar_one()==0

@pytest.mark.parametrize('model_id,expected',[ (101,7.5),(103,3.0)])
def test_bom_costing_add_remove_roundtrip(bom_client,model_id,expected):
    response=bom_client.post('/products/101/components',json={'model_id':model_id,'quantity_needed':3})
    assert response.status_code==200,response.text
    component=response.json()['id']
    detail=bom_client.get('/products/101').json()
    assert detail['component_count']==1
    assert detail['estimated_cogs']==expected
    assert detail['components'][0]['model_name']==('Own model' if model_id==101 else 'Shared model')
    assert bom_client.delete(f'/products/101/components/{component}').status_code==204
    assert bom_client.get('/products/101').json()['components']==[]
    assert bom_client.get('/products/102').status_code==404

@pytest.mark.parametrize('product_id',[102,99999])
@pytest.mark.parametrize('path',['/orders','/orders/101/items'])
def test_order_rejects_foreign_or_missing_product_atomically(bom_client,review_db,product_id,path):
    review_db.execute(text("INSERT INTO orders(id,order_number,org_id,status) VALUES(101,'Own order',1,'pending')"));review_db.commit()
    item={'product_id':product_id,'quantity':2}
    payload={'order_number':'Must not persist','items':[{'product_id':101,'quantity':1},item]} if path=='/orders' else item
    response=bom_client.post(path,json=payload)
    assert response.status_code==404,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT count(*) FROM orders')).scalar_one()==1
        assert fresh.execute(text('SELECT count(*) FROM order_items')).scalar_one()==0

def test_order_zero_price_and_bom_schedule_roundtrip(bom_client,review_db):
    review_db.execute(text("UPDATE models SET quantity_per_bed=4,build_time_hours=1,default_filament_type='PLA' WHERE id=101"));review_db.commit()
    assert bom_client.post('/products/101/components',json={'model_id':101,'quantity_needed':3}).status_code==200
    response=bom_client.post('/orders',json={'order_number':'Fixture order','revenue':0,'items':[{'product_id':101,'quantity':3,'unit_price':0}]})
    assert response.status_code==200,response.text
    order=response.json();assert order['items'][0]['unit_price']==0
    scheduled=bom_client.post(f"/orders/{order['id']}/schedule")
    assert scheduled.status_code==200,scheduled.text
    assert scheduled.json()['jobs_created']==3
    assert [row['quantity_on_bed'] for row in scheduled.json()['details']]==[4,4,1]
    assert bom_client.get(f"/orders/{order['id']}").json()['status']=='in_progress'
    shipped=bom_client.patch(f"/orders/{order['id']}/ship",json={'tracking_number':'SYNTHETIC-TRACKING'})
    assert shipped.status_code==200,shipped.text
    assert shipped.json()['status']=='shipped'
    assert shipped.json()['tracking_number']=='SYNTHETIC-TRACKING'

@pytest.mark.parametrize('foreign_link',['product','model'])
def test_legacy_foreign_order_graph_cannot_schedule(bom_client,review_db,foreign_link):
    product_id=102 if foreign_link=='product' else 101
    model_id=102 if foreign_link=='model' else 101
    review_db.execute(text("INSERT INTO orders(id,order_number,org_id,status) VALUES(101,'Legacy order',1,'pending')"))
    review_db.execute(text('INSERT INTO order_items(order_id,product_id,quantity,fulfilled_quantity) VALUES(101,:product,2,0)'),{'product':product_id})
    review_db.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(:product,:model,1)'),{'product':product_id,'model':model_id})
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,status) VALUES(101,'Synthetic packaging',50,'active')"))
    review_db.execute(text('INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(:product,101,2)'),{'product':product_id})
    review_db.commit()
    before=review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    response=bom_client.post('/orders/101/schedule')
    assert response.status_code==404,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT count(*) FROM jobs')).scalar_one()==before
        assert fresh.execute(text('SELECT status FROM orders WHERE id=101')).scalar_one()=='pending'
        assert fresh.execute(text('SELECT count(*) FROM consumable_usage')).scalar_one()==0
        assert fresh.execute(text('SELECT current_stock FROM consumables WHERE id=101')).scalar_one()==50


def test_superadmin_can_reference_other_org_model(bom_client):
    from core.dependencies import get_current_user
    bom_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'is_active':True}
    response=bom_client.post('/products',json={'name':'Superadmin product','components':[{'model_id':102,'quantity_needed':1}]})
    assert response.status_code==200,response.text
    assert response.json()['components'][0]['model_id']==102
