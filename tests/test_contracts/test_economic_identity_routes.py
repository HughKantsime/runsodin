"""Registered migration and sanctioned order creation through actual HTTP routes."""
from sqlalchemy import text
from tests.test_contracts.test_order_production_behavior import production_order
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_education_review_workflow import review_db


def test_registered_bootstrap_actual_order_item_job_graph(production_order,review_db):
    from core.schema import bootstrap_database
    client,identity,order_id=production_order
    response=client.post(f'/orders/{order_id}/schedule')
    assert response.status_code==200,response.text
    rows=review_db.execute(text('''SELECT o.economic_uid,i.economic_uid,j.economic_uid,b.job_uid FROM orders o
        JOIN order_items i ON i.order_id=o.id JOIN jobs j ON j.order_item_id=i.id
        JOIN cost_job_bindings b ON b.job_uid=j.economic_uid WHERE o.id=:id'''),{'id':order_id}).all()
    assert len(rows)==3 and all(all(row) for row in rows)
    before=review_db.execute(text('SELECT identity_uid FROM cost_identities ORDER BY identity_uid')).scalars().all()
    bootstrap_database(review_db.get_bind())
    assert review_db.execute(text('SELECT identity_uid FROM cost_identities ORDER BY identity_uid')).scalars().all()==before
    assert review_db.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id='python:021-economic-identity'")).scalar_one()==1
    detail=client.get(f'/orders/{order_id}')
    assert detail.status_code==200
    assert all(detail.json()[field] is None for field in ('actual_cost','profit','margin_percent'))


def test_ordinary_add_item_preserves_legacy_null_root_and_job(production_order,review_db):
    from modules.orders.models import Order
    client,identity,_=production_order
    legacy=Order(org_id=1,order_number='Synthetic legacy no financial identity')
    review_db.add(legacy);review_db.commit();identifier=legacy.id
    response=client.post(f'/orders/{identifier}/items',json={'product_id':101,'quantity':1,'unit_price':20})
    assert response.status_code==200,response.text
    response=client.post(f'/orders/{identifier}/schedule')
    assert response.status_code==200,response.text
    rows=review_db.execute(text('''SELECT o.economic_uid,i.economic_uid,j.economic_uid FROM orders o
        JOIN order_items i ON i.order_id=o.id JOIN jobs j ON j.order_item_id=i.id WHERE o.id=:id'''),{'id':identifier}).all()
    assert rows and all(tuple(row)==(None,None,None) for row in rows)
    detail=client.get(f'/orders/{identifier}').json()
    assert all(detail[field] is None for field in ('actual_cost','profit','margin_percent'))
