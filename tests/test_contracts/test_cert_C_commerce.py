"""Certification group C — Pricing (F06-6.4 occurrence 2), Consumables (F07), Products & BOM (F08).

Real products/orders/consumables/pricing routers and real schema."""
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_product_bom_integrity import bom_client, review_db
from tests.test_contracts.test_pricing_behavior import pricing_client, token_client
from tests.test_contracts.test_consumable_behavior import consumable_client

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------- F06-6.4-01/02-occurrence-2

def test_model_cost_uses_cost_per_gram_of_the_models_material(pricing_client, review_db):
    client, issue = pricing_client
    # Rates are seeded in the filament library directly: no API can write them (see inventory cert defect).
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(301,'Cert','PLA rate','PLA',0.02),(302,'Cert','PETG rate','PETG',0.05),(303,'Cert','ASA rate','ASA',0.04)"))
    review_db.commit()
    headers = issue(['read'])
    for material, rate in [('PETG', 0.05), ('PLA', 0.02), ('ASA', 0.04)]:
        review_db.execute(text('UPDATE models SET default_filament_type=:m WHERE id=199'), {'m': material})
        review_db.commit()
        body = client.get('/models/199/cost', headers=headers).json()
        assert body['cost_per_gram'] == rate and body['costs']['material'] == pytest.approx(100 * rate)


def test_pricing_configuration_feeds_cost_calculator_estimate(pricing_client):
    client, issue = pricing_client
    before = client.get('/models/199/cost', headers=issue(['read'])).json()
    assert client.put('/pricing-config', headers=issue(['admin']), json={'spool_cost': 50, 'spool_weight': 1000, 'hourly_rate': 0}).status_code == 200
    after = client.get('/models/199/cost', headers=issue(['read'])).json()
    assert after['cost_per_gram'] == 0.05 and after['costs']['material'] == pytest.approx(5.0)
    assert after['costs']['labor'] == 0 and before['costs']['labor'] > 0
    assert client.put('/pricing-config', headers=issue(['write']), json={'spool_cost': 1}).status_code == 403


def test_product_and_order_cogs_derive_from_filament_usage_and_consumables(bom_client, review_db):
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(302,'Cert','PETG rate','PETG',0.05)"))
    review_db.execute(text("UPDATE models SET cost_per_item=NULL, default_filament_type='PETG', color_requirements=:c WHERE id=101"), {'c': json.dumps({'slot1': {'color': '#000000', 'grams': 100}})})
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,cost_per_unit) VALUES(201,'Cert insert',100,0.75)"))
    review_db.commit()
    assert bom_client.post('/products/101/components', json={'model_id': 101, 'quantity_needed': 1}).status_code == 200
    assert bom_client.post('/products/101/consumables', json={'consumable_id': 201, 'quantity_per_product': 4}).status_code == 200
    assert bom_client.get('/products/101').json()['estimated_cogs'] == pytest.approx(8.0)
    order = bom_client.post('/orders', json={'order_number': 'CERT-PL', 'revenue': 30, 'platform_fees': 0, 'payment_fees': 0, 'shipping_cost': 0,
                                             'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 30}]}).json()
    assert bom_client.get(f"/orders/{order['id']}").json()['estimated_profit'] == pytest.approx(22.0)


def test_order_pl_uses_bom_estimate_when_component_costs_are_entered(bom_client, review_db):
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,cost_per_unit) VALUES(201,'Cert insert',100,0.75)"))
    review_db.commit()
    bom_client.post('/products/101/components', json={'model_id': 101, 'quantity_needed': 2})
    bom_client.post('/products/101/consumables', json={'consumable_id': 201, 'quantity_per_product': 4})
    order = bom_client.post('/orders', json={'order_number': 'CERT-PL2', 'revenue': 30, 'platform_fees': 1, 'payment_fees': 0, 'shipping_cost': 0,
                                             'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 30}]})
    assert order.status_code == 200, order.text
    detail = bom_client.get(f"/orders/{order.json()['id']}").json()
    assert detail['estimated_cogs'] == pytest.approx(8.0)  # 2 x 2.5 model cost_per_item + 4 x 0.75
    assert detail['estimated_profit'] == pytest.approx(21.0)


# ---------------------------------------------------------- F08-8.1-01/03

def test_operator_creates_and_edits_but_cannot_delete_product(bom_client, review_db):
    from core.dependencies import get_current_user
    created = bom_client.post('/products', json={'name': 'Cert lamp', 'sku': 'CERT-LAMP', 'price': 49.0, 'description': 'Cert description'})
    assert created.status_code == 200, created.text
    identity = created.json()['id']
    assert bom_client.patch(f'/products/{identity}', json={'price': 0, 'sku': 'CERT-LAMP-2'}).status_code == 200
    detail = bom_client.get(f'/products/{identity}').json()
    assert (detail['name'], detail['sku'], detail['price'], detail['description']) == ('Cert lamp', 'CERT-LAMP-2', 0, 'Cert description')
    assert bom_client.delete(f'/products/{identity}').status_code == 403
    assert bom_client.get(f'/products/{identity}').status_code == 200
    bom_client.app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'viewer', 'group_id': 1, 'is_active': True}
    assert bom_client.post('/products', json={'name': 'Denied'}).status_code == 403
    assert bom_client.patch(f'/products/{identity}', json={'name': 'Denied'}).status_code == 403
    bom_client.app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'admin', 'group_id': 1, 'is_active': True}
    assert bom_client.delete(f'/products/{identity}').status_code == 204
    assert bom_client.get(f'/products/{identity}').status_code == 404


def test_capture_wire_fixture_for_products_ui(bom_client, review_db):
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,cost_per_unit) VALUES(201,'Cert insert',100,0.75)"))
    review_db.commit()
    bom_client.post('/products/101/components', json={'model_id': 101, 'quantity_needed': 2})
    bom_client.post('/products/101/consumables', json={'consumable_id': 201, 'quantity_per_product': 4})
    bom_client.patch('/products/101', json={'sku': 'OWN-1'})
    bom_client.post('/products', json={'name': 'Cert empty', 'sku': None, 'price': 12})
    rows = bom_client.get('/products').json()
    for row in rows:
        for key in ('created_at', 'updated_at'):
            if row.get(key):
                row[key] = '2026-10-14T12:00:00'
    (REPO / 'frontend/src/test-fixtures/cert-C-products-wire.json').write_text(json.dumps({'scope': 'Owned real GET /products response (cert C)', 'products': rows}, indent=2) + '\n')
    assert {(r['name'], r['estimated_cogs']) for r in rows} == {('Own product', 8.0), ('Cert empty', None)}


# ---------------------------------------------------------- F07-area

def test_consumable_crud_low_stock_and_bom_deduction_wire(consumable_client):
    client, identity = consumable_client
    a = client.post('/consumables', json={'name': 'Cert magnet', 'sku': 'MAG-6x3', 'unit': 'piece', 'cost_per_unit': 0.12, 'current_stock': 3, 'min_stock': 10})
    b = client.post('/consumables', json={'name': 'Cert box', 'unit': 'piece', 'cost_per_unit': 0.4, 'current_stock': 50, 'min_stock': 5})
    assert a.status_code == b.status_code == 200
    assert client.patch(f"/consumables/{a.json()['id']}", json={'vendor': 'Cert vendor'}).status_code == 200
    rows = client.get('/consumables').json()
    assert {r['name']: r['is_low_stock'] for r in rows} == {'Cert magnet': True, 'Cert box': False}
    assert [r['name'] for r in client.get('/consumables/low-stock').json()] == ['Cert magnet']
    for row in rows:
        for key in ('created_at', 'updated_at'):
            row[key] = '2026-10-14T12:00:00'
    (REPO / 'frontend/src/test-fixtures/cert-C-consumables-wire.json').write_text(json.dumps({'scope': 'Owned real GET /consumables response (cert C)', 'consumables': rows}, indent=2) + '\n')


# ------------------------------------------- regression: cert-fix-C filament-derived COGS

def _bom(bom_client, model_id=101):
    assert bom_client.post('/products/101/components', json={'model_id': model_id, 'quantity_needed': 2}).status_code == 200
    return bom_client.get('/products/101').json()['estimated_cogs']


def test_filament_cogs_falls_back_to_pricing_config_spool_rate(bom_client, review_db):
    from core.models import SystemConfig
    review_db.add(SystemConfig(key='pricing_config', value={'spool_cost': 30, 'spool_weight': 1000}))
    review_db.execute(text("UPDATE models SET cost_per_item=NULL, default_filament_type='TPU', color_requirements=:c WHERE id=101"),
                      {'c': json.dumps({'slot1': {'color': '#000000', 'grams': 40}, 'slot2': {'color': '#ffffff', 'grams': 10}})})
    review_db.commit()
    assert _bom(bom_client) == pytest.approx(2 * 50 * 0.03)


def test_filament_cogs_unknown_without_grams_and_entered_cost_still_wins(bom_client, review_db):
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(302,'Cert','PETG rate','PETG',0.05)"))
    review_db.execute(text("UPDATE models SET cost_per_item=NULL, default_filament_type='PETG', color_requirements=NULL WHERE id=101"))
    review_db.commit()
    assert _bom(bom_client) is None
    review_db.execute(text("UPDATE models SET cost_per_item=1.25, color_requirements=:c WHERE id=101"), {'c': json.dumps({'slot1': {'color': '#000000', 'grams': 100}})})
    review_db.commit()
    assert bom_client.get('/products/101').json()['estimated_cogs'] == pytest.approx(2.5)


def test_filament_cogs_is_per_sellable_piece_not_per_plate(bom_client, review_db):
    """100 g plate holding 4 sellable pieces at $0.05/g costs $1.25 per item (review HIGH-1)."""
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(302,'Cert','PETG rate','PETG',0.05)"))
    review_db.execute(text("UPDATE models SET cost_per_item=NULL, quantity_per_bed=4, default_filament_type='PETG', color_requirements=:c WHERE id=101"),
                      {'c': json.dumps({'slot1': {'color': '#000000', 'grams': 100}})})
    review_db.commit()
    assert bom_client.post('/products/101/components', json={'model_id': 101, 'quantity_needed': 1}).status_code == 200
    assert bom_client.get('/products/101').json()['estimated_cogs'] == pytest.approx(1.25)
    for stored in (0, None):
        review_db.execute(text('UPDATE models SET quantity_per_bed=:q WHERE id=101'), {'q': stored})
        review_db.commit()
        assert bom_client.get('/products/101').json()['estimated_cogs'] == pytest.approx(5.0)



def test_material_rate_is_lowest_id_valid_row_and_corrupt_rows_never_500(pricing_client, review_db):
    """Deterministic rate (lowest id with a finite non-negative rate); bad rows skipped (review HIGH-2/3)."""
    client, issue = pricing_client
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES"
                           "(310,'Cert','Later PETG','PETG',0.09),(306,'Cert','Corrupt PETG','PETG',:inf),"
                           "(305,'Cert','Negative PETG','PETG',-1),(308,'Cert','First valid PETG','PETG',0.04)"), {'inf': float('inf')})
    review_db.execute(text("UPDATE models SET default_filament_type='PETG' WHERE id=199"))
    review_db.commit()
    body = client.get('/models/199/cost', headers=issue(['read'])).json()
    assert body['cost_per_gram'] == 0.04 and body['costs']['material'] == pytest.approx(4.0)
    from modules.models_library.services import calculate_job_cost
    assert calculate_job_cost(review_db, model_id=199)[0] > 0


def test_model_list_reports_unknown_estimate_instead_of_500_on_overflow(pricing_client, review_db):
    from modules.models_library.routes import models_crud
    client, issue = pricing_client
    client.app.include_router(models_crud.router)
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(311,'Cert','PLA','PLA',100)"))
    review_db.execute(text("UPDATE models SET default_filament_type='PLA', color_requirements=:c WHERE id=199"), {'c': '{"slot":{"grams":1e308}}'})
    review_db.commit()
    response = client.get('/models-with-pricing', headers=issue(['read:models']))
    assert response.status_code == 200, response.text
    row = next(r for r in response.json() if r['id'] == 199)
    assert (row['estimated_cost'], row['suggested_price']) == (None, None)


SEARCH_FIXTURE = REPO / 'frontend/src/test-fixtures/cert-C-consumables-search-wire.json'


def test_capture_consumables_search_and_status_filter_wire(consumable_client):
    """F07-7.1-02 producer: real GET /consumables responses the Consumables page searches
    (client side, name or SKU) and filters (server side, ?status=), for every status the
    page offers. The UI test (Consumables.search.cert.test.tsx) consumes this fixture."""
    client, _identity = consumable_client
    for body in (
        {'name': 'Neodymium magnet 6x3', 'sku': 'MAG-6X3', 'unit': 'piece', 'cost_per_unit': 0.12, 'current_stock': 40, 'min_stock': 10, 'status': 'active'},
        {'name': 'Shipping box small', 'sku': 'BOX-S', 'unit': 'piece', 'cost_per_unit': 0.4, 'current_stock': 0, 'min_stock': 5, 'status': 'depleted'},
        {'name': 'Heat-set insert M3', 'sku': 'INS-M3', 'unit': 'piece', 'cost_per_unit': 0.05, 'current_stock': 200, 'min_stock': 50, 'status': 'active'},
        {'name': 'Old label roll', 'sku': None, 'unit': 'roll', 'cost_per_unit': 3.5, 'current_stock': 2, 'min_stock': 0, 'status': 'discontinued'},
    ):
        assert client.post('/consumables', json=body).status_code == 200
    responses = {}
    for status in ('', 'active', 'depleted', 'discontinued'):
        response = client.get('/consumables', params={'status': status} if status else None)
        assert response.status_code == 200
        rows = response.json()
        for row in rows:
            for key in ('created_at', 'updated_at'):
                row[key] = '2026-10-14T12:00:00'
        responses[status or 'all'] = rows
    # The server filter is exact: each status response holds only that status.
    assert {r['name'] for r in responses['active']} == {'Neodymium magnet 6x3', 'Heat-set insert M3'}
    assert [r['name'] for r in responses['depleted']] == ['Shipping box small']
    assert [r['name'] for r in responses['discontinued']] == ['Old label roll']
    assert len(responses['all']) == 4
    fixture = {'scope': 'Owned real GET /consumables and GET /consumables?status= responses (F07-7.1-02)',
               'responses': responses}
    if SEARCH_FIXTURE.exists():
        assert json.loads(SEARCH_FIXTURE.read_text()) == fixture, 'Producer contract changed; reconcile the consumer explicitly'
    else:
        SEARCH_FIXTURE.write_text(json.dumps(fixture, indent=2) + '\n')
