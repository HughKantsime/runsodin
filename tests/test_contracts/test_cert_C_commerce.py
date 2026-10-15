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
