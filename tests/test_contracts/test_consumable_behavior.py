"""Global consumable inventory lifecycle, adjustment history and real role denial."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def consumable_client(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.orders.routes import consumables
    app=FastAPI();app.include_router(consumables.router)
    def request_db():
        with Session(review_db.get_bind()) as session: yield session
    app.dependency_overrides[get_db]=request_db
    identity={"id":3,"role":"operator","group_id":1,"is_active":True}
    app.dependency_overrides[get_current_user]=lambda:identity
    return TestClient(app), identity


def test_consumable_create_adjust_history_low_stock_delete(consumable_client,review_db):
    client, identity=consumable_client
    created=client.post('/consumables',json={"name":"Synthetic packaging","current_stock":10,"min_stock":5,"cost_per_unit":0.25})
    assert created.status_code==200,created.text
    body=created.json();identifier=body['id'];assert not body['is_low_stock']
    assert client.post(f'/consumables/{identifier}/adjust',json={"type":"deduct","quantity":8}).json()['new_stock']==2
    low=client.get('/consumables/low-stock');assert low.status_code==200
    assert [r['id'] for r in low.json()]==[identifier]
    assert client.post(f'/consumables/{identifier}/adjust',json={"type":"restock","quantity":3.5}).json()['new_stock']==5.5
    detail=client.get(f'/consumables/{identifier}').json()
    assert detail['current_stock']==5.5
    assert sorted(r['quantity_used'] for r in detail['usage_history'])==[-3.5,8]
    assert not detail['is_low_stock']
    assert client.patch(f'/consumables/{identifier}',json={"status":"inactive"}).status_code==200
    assert client.get('/consumables?status=inactive').json()[0]['id']==identifier
    assert client.get('/consumables?status=active').json()==[]
    assert client.delete(f'/consumables/{identifier}').status_code==403
    identity['role']='admin'
    assert client.delete(f'/consumables/{identifier}').status_code==200
    assert client.get(f'/consumables/{identifier}').status_code==404
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT count(*) FROM consumables WHERE id=:id'),{'id':identifier}).scalar_one()==0

@pytest.mark.parametrize('quantity',[0,-1])
def test_adjust_rejects_nonpositive_without_stock_mutation(consumable_client,quantity):
    client, _=consumable_client
    created=client.post('/consumables',json={"name":"Synthetic","current_stock":10}).json()
    identifier=created['id']
    response=client.post(f'/consumables/{identifier}/adjust',json={"type":"deduct","quantity":quantity})
    assert response.status_code==422,response.text
    detail=client.get(f'/consumables/{identifier}').json()
    assert detail['current_stock']==10 and detail['usage_history']==[]


def test_viewer_cannot_create_or_adjust(consumable_client):
    client, identity=consumable_client
    identifier=client.post('/consumables',json={"name":"Synthetic","current_stock":10}).json()['id']
    identity['role']='viewer'
    assert client.post('/consumables',json={"name":"Denied"}).status_code==403
    assert client.post(f'/consumables/{identifier}/adjust',json={"type":"deduct","quantity":1}).status_code==403
    assert client.get(f'/consumables/{identifier}').json()['current_stock']==10



def test_consumable_unknown_cost_can_be_saved_without_false_zero_or_server_error(consumable_client):
    client,_=consumable_client
    created=client.post('/consumables',json={'name':'Synthetic unknown cost','current_stock':5,'cost_per_unit':2}).json()
    response=client.patch(f"/consumables/{created['id']}",json={'cost_per_unit':None})
    assert response.status_code==200
    assert response.json()['cost_per_unit'] is None
    detail=client.get(f"/consumables/{created['id']}")
    assert detail.status_code==200 and detail.json()['cost_per_unit'] is None
