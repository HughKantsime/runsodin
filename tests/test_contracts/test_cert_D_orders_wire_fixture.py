"""cert-D orders: capture the real Orders HTTP producer shapes for the certD UI contract test.

The fixture is written on first run and asserted unchanged afterwards (same pattern as
test_chargeback_wire_fixture.py). Volatile timestamps are pinned to one constant; all
other values are the genuine router output of a real BOM/order/job/scheduling scenario.
"""
import base64
import json
from pathlib import Path

from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401
from tests.test_contracts.cert_D_capture import capturing

FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/pages/orders/certDorders.wire.json'
PINNED = '2026-10-01T12:00:00'


def _pin(value):
    if isinstance(value, list):
        return [_pin(v) for v in value]
    if isinstance(value, dict):
        return {k: (PINNED if (k.endswith('_at') or k.endswith('_date')) and v is not None else _pin(v)) for k, v in value.items()}
    return value


def test_capture_orders_wire_from_real_http(bom_client, review_db, monkeypatch):
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.jobs.routes import jobs_crud, jobs_lifecycle
    from modules.organizations import routes_permissions
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client = bom_client
    for router in (jobs_crud.router, jobs_lifecycle.router, routes_permissions.router):
        client.app.include_router(router)
    identity = {'id': 3, 'role': 'admin', 'group_id': 1, 'is_active': True}
    client.app.dependency_overrides[get_current_user] = lambda: identity
    review_db.execute(text("UPDATE models SET quantity_per_bed=4,build_time_hours=1 WHERE id=101"))
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock,status,cost_per_unit) VALUES(301,'Box',50,'active',0.5)"))
    review_db.execute(text("INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(101,301,2)"))
    review_db.commit()
    assert client.post('/products/101/components', json={'model_id': 101, 'quantity_needed': 3}).status_code == 200
    # unit COGS = 2.5 x 3 + 0.5 x 2 = 8.5
    made = {}
    for key, body in {
        'fulfilled': {'order_number': 'WIRE-ETSY-1', 'platform': 'etsy', 'customer_name': 'Wire Customer', 'revenue': 60, 'platform_fees': 3,
                      'items': [{'product_id': 101, 'quantity': 2, 'unit_price': 30}]},
        'pending': {'order_number': 'WIRE-AMZ-2', 'platform': 'amazon', 'customer_name': 'Second Buyer', 'revenue': 40,
                    'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 40}]},
        'wholesale': {'order_number': 'WIRE-WHL-3', 'platform': 'wholesale', 'customer_name': 'Shop Partner', 'revenue': 100,
                      'items': [{'product_id': 101, 'quantity': 4, 'unit_price': 25}]},
    }.items():
        response = client.post('/orders', json=body)
        assert response.status_code == 200, response.text
        made[key] = response.json()['id']
    # Drive the first order to fulfilled through real schedule + job completions.
    assert client.post(f"/orders/{made['fulfilled']}/schedule").status_code == 200
    jobs = list(review_db.execute(text('SELECT job_id FROM order_production_jobs ORDER BY job_id')).scalars())
    assert len(jobs) == 2   # 2 x 3 pieces at 4 per bed
    for job in jobs:
        assert client.post(f'/jobs/{job}/complete').status_code == 200
    # Wholesale order is cancelled (excluded from margin eligibility).
    assert client.patch(f"/orders/{made['wholesale']}", json={'status': 'cancelled'}).status_code == 200

    capture = {'scope': 'Owned genuine HTTP router output (orders/products/permissions); timestamps pinned', 'ids': made}
    capture['permissions'] = client.get('/permissions').json()
    capture['products'] = client.get('/products').json()
    capture['list_initial'] = client.get('/orders').json()
    capture['detail_initial'] = {k: client.get(f'/orders/{v}').json() for k, v in made.items()}
    assert capture['detail_initial']['fulfilled']['status'] == 'fulfilled'
    assert capture['detail_initial']['fulfilled']['estimated_profit'] == 40.0      # 60 - 17 - 3
    assert capture['detail_initial']['pending']['estimated_profit'] == 31.5        # 40 - 8.5

    scheduled = client.post(f"/orders/{made['pending']}/schedule")
    assert scheduled.status_code == 200 and scheduled.json()['jobs_created'] == 1
    capture['schedule_pending'] = scheduled.json()
    capture['list_after_schedule'] = client.get('/orders').json()
    capture['detail_pending_after_schedule'] = client.get(f"/orders/{made['pending']}").json()

    identity['role'] = 'operator'
    shipped = client.patch(f"/orders/{made['fulfilled']}/ship", json={'tracking_number': '1ZWIRE0001', 'shipped_date': '2026-10-01T12:00:00Z'})
    assert shipped.status_code == 200 and shipped.json()['status'] == 'shipped'
    capture['ship_fulfilled'] = shipped.json()
    capture['list_after_ship'] = client.get('/orders').json()
    capture['detail_fulfilled_after_ship'] = client.get(f"/orders/{made['fulfilled']}").json()

    identity['role'] = 'admin'
    edited = client.patch(f"/orders/{made['wholesale']}", json={'customer_name': 'Edited Partner'})
    assert edited.status_code == 200 and edited.json()['customer_name'] == 'Edited Partner'
    capture['edit_wholesale'] = edited.json()

    invoice = client.get(f"/orders/{made['fulfilled']}/invoice.pdf")
    assert invoice.status_code == 200 and invoice.content.startswith(b'%PDF-')
    capture['invoice_headers'] = {'content-type': invoice.headers['content-type'], 'content-disposition': invoice.headers['content-disposition']}
    capture['invoice_magic_b64'] = base64.b64encode(invoice.content[:5]).decode()
    capture = _pin(capture)
    if FIXTURE.exists() and not capturing():
        assert json.loads(FIXTURE.read_text()) == capture, 'Producer contract changed; reconcile the consumer explicitly'
    else:  # first capture, or an explicit CAPTURE=1 re-capture
        assert capturing(), f'{FIXTURE.name} is missing; capture it with CAPTURE=1'
        FIXTURE.write_text(json.dumps(capture, indent=1, sort_keys=True) + '\n')
