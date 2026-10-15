"""cert-D orders: F09 order management, lifecycle, per-order P&L and shipping behavior.

Real orders/products/jobs/scheduler routers on a bootstrapped SQLite schema; only the
identity dependency is substituted. No source or existing test is modified.
"""
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401
from tests.test_contracts.test_order_production_behavior import production_order  # noqa: F401


def _as(client, role, group=1, user=3):
    from core.dependencies import get_current_user
    client.app.dependency_overrides[get_current_user] = lambda: {'id': user, 'role': role, 'group_id': group, 'is_active': True}


def _fresh(review_db, sql, **params):
    with Session(review_db.get_bind()) as fresh:
        return fresh.execute(text(sql), params).mappings().all()


# ---------------------------------------------------------------- F09-9.1-02 / F09-9.3-01

def test_order_persists_customer_reference_and_product_line_items(bom_client, review_db):
    review_db.execute(text("INSERT INTO products(id,name,sku,org_id,price) VALUES(103,'Second product','SP-3',1,7.25)"))
    review_db.commit()
    response = bom_client.post('/orders', json={'order_number': 'REF-1001', 'customer_name': 'Ada Customer', 'platform': 'etsy',
                                                'items': [{'product_id': 101, 'quantity': 3}, {'product_id': 103, 'quantity': 4, 'unit_price': 6.5}]})
    assert response.status_code == 200, response.text
    identifier = response.json()['id']
    detail = bom_client.get(f'/orders/{identifier}').json()
    assert detail['order_number'] == 'REF-1001' and detail['customer_name'] == 'Ada Customer'
    lines = {i['product_id']: i for i in detail['items']}
    assert lines[101]['product_name'] == 'Own product' and lines[103]['product_name'] == 'Second product' and lines[103]['product_sku'] == 'SP-3'
    # Omitted unit price falls back to the Product's sale price.
    assert lines[101]['unit_price'] == 20 and lines[103]['unit_price'] == 6.5
    rows = _fresh(review_db, 'SELECT product_id,quantity,unit_price FROM order_items WHERE order_id=:o ORDER BY product_id', o=identifier)
    assert [dict(r) for r in rows] == [{'product_id': 101, 'quantity': 3, 'unit_price': 20.0}, {'product_id': 103, 'quantity': 4, 'unit_price': 6.5}]
    summary = next(o for o in bom_client.get('/orders').json() if o['id'] == identifier)
    assert summary['customer_name'] == 'Ada Customer' and summary['order_number'] == 'REF-1001' and summary['item_count'] == 2
    # A line item must reference a Product visible to the caller.
    assert bom_client.post('/orders', json={'items': [{'product_id': 102, 'quantity': 1}]}).status_code == 404
    assert bom_client.post('/orders', json={'items': [{'product_id': 99999, 'quantity': 1}]}).status_code == 404


def test_line_subtotal_is_sale_price_times_quantity_but_order_revenue_is_entered_separately(bom_client, review_db):
    review_db.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(103,'Second product',1,7.25)"))
    review_db.commit()
    response = bom_client.post('/orders', json={'order_number': 'REV-1', 'items': [{'product_id': 101, 'quantity': 3}, {'product_id': 103, 'quantity': 4}]})
    assert response.status_code == 200, response.text
    identifier = response.json()['id']
    detail = bom_client.get(f'/orders/{identifier}').json()
    assert sorted(i['subtotal'] for i in detail['items']) == [29.0, 60.0]   # 4 x 7.25, 3 x 20
    # Order revenue (used by P&L, stat cards and invoice total) is NOT derived from the lines.
    assert detail['revenue'] is None and detail['estimated_profit'] is None
    assert next(o for o in bom_client.get('/orders').json() if o['id'] == identifier)['revenue'] is None
    _as(bom_client, 'admin')
    assert bom_client.patch(f'/orders/{identifier}', json={'revenue': 50}).json()['revenue'] == 50
    detail = bom_client.get(f'/orders/{identifier}').json()
    assert detail['revenue'] == 50 and sorted(i['subtotal'] for i in detail['items']) == [29.0, 60.0]


# ---------------------------------------------------------------- F09-9.3-02 / F09-9.3-03

def _bom(review_db):
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,status,cost_per_unit) VALUES(301,'Box',50,'active',0.5)"))
    review_db.execute(text("INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(101,101,3),(101,103,2)"))
    review_db.execute(text("INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(101,301,2)"))
    review_db.commit()


def test_estimated_cogs_sums_every_bom_component_cost_and_consumable_cost(bom_client, review_db):
    _bom(review_db)
    response = bom_client.post('/orders', json={'order_number': 'COGS-1', 'revenue': 50, 'platform_fees': 2, 'payment_fees': 1,
                                                'shipping_cost': 3, 'items': [{'product_id': 101, 'quantity': 2}]})
    assert response.status_code == 200, response.text
    identifier = response.json()['id']
    detail = bom_client.get(f'/orders/{identifier}').json()
    # 2 x (model101 2.5 x 3 + model103 1.0 x 2 + consumable 0.5 x 2) = 2 x 10.5
    assert detail['estimated_cogs'] == 21.0
    assert detail['estimated_profit'] == 23.0            # 50 - 21 - (2+1+3)
    assert detail['estimated_margin_percent'] == 46.0
    assert detail['actual_cost'] is None and detail['profit'] is None and detail['margin_percent'] is None


def test_printed_component_cost_is_the_entered_model_cost_not_a_filament_computation(bom_client, review_db):
    _bom(review_db)
    identifier = bom_client.post('/orders', json={'revenue': 50, 'items': [{'product_id': 101, 'quantity': 1}]}).json()['id']
    assert bom_client.get(f'/orders/{identifier}').json()['estimated_cogs'] == 10.5
    # Changing the model's filament usage leaves COGS unchanged ...
    review_db.execute(text("UPDATE models SET color_requirements=:c WHERE id=101"), {'c': json.dumps({'1': {'color': 'red', 'grams': 900}})})
    review_db.commit()
    assert bom_client.get(f'/orders/{identifier}').json()['estimated_cogs'] == 10.5
    # ... while the free-entry Model "cost per item" drives it.
    review_db.execute(text('UPDATE models SET cost_per_item=4.5 WHERE id=101'))
    review_db.commit()
    assert bom_client.get(f'/orders/{identifier}').json()['estimated_cogs'] == 16.5   # 4.5x3 + 1x2 + 0.5x2


def test_profit_and_margin_are_estimated_independently_per_order(bom_client, review_db):
    _bom(review_db)
    first = bom_client.post('/orders', json={'revenue': 100, 'items': [{'product_id': 101, 'quantity': 1}]}).json()['id']
    second = bom_client.post('/orders', json={'revenue': 21, 'platform_fees': 1.5, 'items': [{'product_id': 101, 'quantity': 2}]}).json()['id']
    a, b = bom_client.get(f'/orders/{first}').json(), bom_client.get(f'/orders/{second}').json()
    assert (a['estimated_cogs'], a['estimated_profit'], a['estimated_margin_percent']) == (10.5, 89.5, 89.5)
    assert (b['estimated_cogs'], b['estimated_profit'], b['estimated_margin_percent']) == (21.0, -1.5, -7.1)
    listing = {o['id']: o for o in bom_client.get('/orders').json()}
    assert listing[first]['estimated_profit'] == 89.5 and listing[second]['estimated_margin_percent'] == -7.1
    # Non-estimated profit/margin are never calculated.
    assert a['profit'] is None and a['margin_percent'] is None and b['profit'] is None


# ---------------------------------------------------------------- F09-9.2-01 lifecycle end-to-end

def test_lifecycle_pending_in_progress_fulfilled_shipped_over_http(production_order, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, identity, order_id = production_order
    client.app.include_router(jobs_lifecycle.router)
    status = lambda: _fresh(review_db, 'SELECT status,tracking_number,shipped_date FROM orders WHERE id=:id', id=order_id)[0]
    assert status()['status'] == 'pending'
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    assert status()['status'] == 'in_progress'
    jobs = [r['id'] for r in _fresh(review_db, 'SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:o) ORDER BY id', o=order_id)]
    assert len(jobs) == 3
    for job in jobs[:-1]:
        assert client.post(f'/jobs/{job}/complete').status_code == 200
    assert status()['status'] == 'in_progress'
    assert client.post(f'/jobs/{jobs[-1]}/complete').status_code == 200
    assert status()['status'] == 'fulfilled'
    shipped = client.patch(f'/orders/{order_id}/ship', json={'tracking_number': '1Z999AA10123456784'})
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()['status'] == 'shipped' and shipped.json()['tracking_number'] == '1Z999AA10123456784'
    row = status()
    assert row['status'] == 'shipped' and row['tracking_number'] == '1Z999AA10123456784' and row['shipped_date'] is not None


def test_fulfilled_cannot_be_set_manually_before_jobs_complete(production_order, review_db):
    client, identity, order_id = production_order
    assert client.post(f'/orders/{order_id}/schedule').status_code == 200
    identity['role'] = 'admin'
    response = client.patch(f'/orders/{order_id}', json={'status': 'fulfilled'})
    assert response.status_code == 409, response.text
    assert _fresh(review_db, 'SELECT status FROM orders WHERE id=:id', id=order_id)[0]['status'] == 'in_progress'


# ---------------------------------------------------------------- F09-9.2-02 scheduler color-match via HTTP

@pytest.mark.parametrize('red_printer,white_printer', [(10, 9), (9, 10)])
def test_scheduler_run_endpoint_assigns_order_jobs_to_color_matched_printer(production_order, review_db, red_printer, white_printer):
    from modules.jobs import scheduler_routes
    client, identity, order_id = production_order
    client.app.include_router(scheduler_routes.router)
    review_db.execute(text("UPDATE models SET color_requirements=:c WHERE id=101"), {'c': json.dumps({'1': {'color': 'red'}})})
    review_db.execute(text("UPDATE filament_slots SET color='white' WHERE printer_id=:p"), {'p': white_printer})
    review_db.execute(text("UPDATE filament_slots SET color='red' WHERE printer_id=:p"), {'p': red_printer})
    review_db.commit()
    scheduled = client.post(f'/orders/{order_id}/schedule')
    assert scheduled.status_code == 200 and scheduled.json()['jobs_created'] == 3
    stock = _fresh(review_db, 'SELECT current_stock FROM consumables WHERE id=101')[0]['current_stock']
    assert stock == 14 and scheduled.json()['consumables_deducted'] == [{'consumable': 'Synthetic packaging', 'quantity_deducted': 6, 'remaining_stock': 14}]
    jobs_sql = 'SELECT printer_id,match_score,status FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:o)'
    assert all(r['printer_id'] is None for r in _fresh(review_db, jobs_sql, o=order_id))
    run = client.post('/scheduler/run')
    assert run.status_code == 200, run.text
    rows = _fresh(review_db, jobs_sql, o=order_id)
    assigned = [r for r in rows if r['printer_id'] is not None]
    assert assigned and {r['printer_id'] for r in assigned} == {red_printer}
    assert {r['match_score'] for r in assigned} == {100} and {r['status'] for r in assigned} == {'scheduled'}


# ---------------------------------------------------------------- F09-9.5-01 shipping roles

def _order_with_status(review_db, status, identifier=701):
    review_db.execute(text("INSERT INTO orders(id,order_number,status,org_id) VALUES(:id,:n,:s,1)"), {'id': identifier, 'n': f'SHIP-{identifier}', 's': status})
    review_db.execute(text("INSERT INTO order_items(order_id,product_id,quantity,unit_price,fulfilled_quantity) VALUES(:id,101,1,20,1)"), {'id': identifier})
    review_db.commit()
    return identifier


@pytest.mark.parametrize('role', ['operator', 'admin'])
def test_operator_and_admin_mark_shipped_with_persisted_tracking(bom_client, review_db, role):
    identifier = _order_with_status(review_db, 'fulfilled')
    _as(bom_client, role)
    response = bom_client.patch(f'/orders/{identifier}/ship', json={'tracking_number': f'TRACK-{role}', 'shipped_date': '2026-10-01T12:00:00Z'})
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'shipped' and response.json()['tracking_number'] == f'TRACK-{role}'
    row = _fresh(review_db, 'SELECT status,tracking_number,shipped_date FROM orders WHERE id=:id', id=identifier)[0]
    assert row['status'] == 'shipped' and row['tracking_number'] == f'TRACK-{role}' and str(row['shipped_date']).startswith('2026-10-01 12:00:00')
    assert bom_client.get(f'/orders/{identifier}').json()['tracking_number'] == f'TRACK-{role}'


def test_viewer_and_foreign_org_cannot_mark_shipped(bom_client, review_db):
    identifier = _order_with_status(review_db, 'fulfilled')
    _as(bom_client, 'viewer')
    assert bom_client.patch(f'/orders/{identifier}/ship', json={'tracking_number': 'NOPE'}).status_code == 403
    _as(bom_client, 'admin', group=2, user=5)
    assert bom_client.patch(f'/orders/{identifier}/ship', json={'tracking_number': 'NOPE'}).status_code == 404
    row = _fresh(review_db, 'SELECT status,tracking_number FROM orders WHERE id=:id', id=identifier)[0]
    assert dict(row) == {'status': 'fulfilled', 'tracking_number': None}


@pytest.mark.xfail(strict=True, reason="DEFECT: backend/modules/orders/routes/orders_crud.py:559 ship_order sets status=shipped without checking current status; PATCH /orders/{id}/ship on a cancelled order returns 200 and turns it into shipped (UI only offers Mark Shipped for fulfilled orders)")
def test_cancelled_order_cannot_be_marked_shipped(bom_client, review_db):
    identifier = _order_with_status(review_db, 'cancelled')
    response = bom_client.patch(f'/orders/{identifier}/ship', json={'tracking_number': 'TRACK-CANCELLED'})
    assert response.status_code == 409, response.text
    assert _fresh(review_db, 'SELECT status FROM orders WHERE id=:id', id=identifier)[0]['status'] == 'cancelled'


# ---------------------------------------------------------------- F09-9.1-03 edit modal backend contract

def test_edit_modal_operations_persist_metadata_and_line_item_changes(bom_client, review_db):
    review_db.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(103,'Second product',1,7.25)"))
    review_db.commit()
    identifier = bom_client.post('/orders', json={'order_number': 'EDIT-1', 'customer_name': 'Before', 'platform': 'etsy',
                                                  'items': [{'product_id': 101, 'quantity': 1}, {'product_id': 103, 'quantity': 1}]}).json()['id']
    items = {i['product_id']: i['id'] for i in bom_client.get(f'/orders/{identifier}').json()['items']}
    # Operators may not edit order metadata/items in place (UI shows Edit Order to admin only).
    assert bom_client.patch(f'/orders/{identifier}', json={'customer_name': 'Operator'}).status_code == 403
    _as(bom_client, 'admin')
    response = bom_client.patch(f'/orders/{identifier}', json={'customer_name': 'After', 'order_number': 'EDIT-1B', 'platform': 'direct',
                                                              'customer_email': 'after@example.test', 'notes': 'gift', 'revenue': 42.5})
    assert response.status_code == 200, response.text
    assert bom_client.patch(f'/orders/{identifier}/items/{items[101]}', json={'quantity': 5, 'unit_price': 18}).status_code == 200
    assert bom_client.delete(f'/orders/{identifier}/items/{items[103]}').status_code == 204
    added = bom_client.post(f'/orders/{identifier}/items', json={'product_id': 103, 'quantity': 2, 'unit_price': 7})
    assert added.status_code == 200 and added.json()['subtotal'] == 14
    detail = bom_client.get(f'/orders/{identifier}').json()
    assert (detail['customer_name'], detail['order_number'], detail['platform'], detail['customer_email'], detail['notes'], detail['revenue']) == \
        ('After', 'EDIT-1B', 'direct', 'after@example.test', 'gift', 42.5)
    assert sorted((i['product_id'], i['quantity'], i['unit_price'], i['subtotal']) for i in detail['items']) == [(101, 5, 18.0, 90.0), (103, 2, 7.0, 14.0)]
    rows = _fresh(review_db, 'SELECT product_id,quantity,unit_price FROM order_items WHERE order_id=:o ORDER BY product_id', o=identifier)
    assert [tuple(r.values()) for r in rows] == [(101, 5, 18.0), (103, 2, 7.0)]
