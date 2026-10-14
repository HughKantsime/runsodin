"""Measured actual totals must not certify catalog estimates or partial inputs."""
import pytest
from tests.test_contracts.test_product_bom_integrity import bom_client, review_db


@pytest.mark.parametrize('endpoint', ['detail', 'list'])
@pytest.mark.parametrize('case', ['completed_estimate', 'partial_estimates', 'fees_only'])
def test_unmeasured_or_incomplete_order_cost_is_not_reported_as_actual(bom_client, review_db, endpoint, case):
    from core.base import JobStatus
    from modules.jobs.models import Job
    response = bom_client.post('/orders', json={
        'order_number': 'Synthetic measured-cost completeness', 'revenue': 30,
        'platform_fees': 0, 'payment_fees': 0, 'shipping_cost': 0,
        'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 30}],
    })
    assert response.status_code == 200
    identifier = response.json()['id']
    item = response.json()['items'][0]['id']
    if case != 'fees_only':
        review_db.add(Job(item_name='Synthetic unmeasured completed estimate', order_item_id=item,
                          status=JobStatus.COMPLETED, estimated_cost=12.99,
                          quantity=1, quantity_on_bed=1, charged_to_org_id=1))
    if case == 'partial_estimates':
        review_db.add(Job(item_name='Synthetic unknown completed cost', order_item_id=item,
                          status=JobStatus.COMPLETED, estimated_cost=None,
                          quantity=1, quantity_on_bed=1, charged_to_org_id=1))
    review_db.commit()
    if endpoint == 'detail':
        result = bom_client.get(f'/orders/{identifier}')
        assert result.status_code == 200
        data = result.json()
    else:
        result = bom_client.get('/orders')
        assert result.status_code == 200
        data = next(row for row in result.json() if row['id'] == identifier)
    if endpoint == 'list':
        assert all(data.get(field) is None for field in ('actual_cost', 'profit', 'margin_percent'))
        detail = bom_client.get(f'/orders/{identifier}')
        assert detail.status_code == 200
        data = detail.json()
    assert data['actual_cost'] is None, 'No measured purchase-valued production evidence exists'
    assert data['profit'] is None
    assert data['margin_percent'] is None
    if case == 'completed_estimate':
        assert data['estimated_cost'] == 12.99


def test_bootstrap_preserves_recorded_order_and_does_not_backfill_estimates_as_cost_evidence(bom_client,review_db):
    from sqlalchemy import text
    from core.schema import bootstrap_database
    from core.base import JobStatus
    from modules.jobs.models import Job
    response=bom_client.post('/orders',json={'order_number':'Synthetic retained order','revenue':30,
        'platform_fees':0,'payment_fees':0,'shipping_cost':0,
        'items':[{'product_id':101,'quantity':1,'unit_price':30}]})
    assert response.status_code==200
    identifier=response.json()['id'];item=response.json()['items'][0]['id']
    review_db.add(Job(item_name='Synthetic failed estimate',order_item_id=item,status=JobStatus.FAILED,
        estimated_cost=12.99,quantity=1,quantity_on_bed=1,charged_to_org_id=1))
    review_db.commit()
    tables=('cost_identities','cost_order_memberships','cost_job_bindings','cost_attempt_bindings',
        'cost_obligations','cost_attempt_participation','finance_currency_settings')
    with review_db.get_bind().connect() as conn:
        counts={table:conn.execute(text('SELECT COUNT(*) FROM '+table)).scalar_one() for table in tables}
        before=conn.execute(text('SELECT order_number,revenue,platform_fees,payment_fees,shipping_cost FROM orders WHERE id=:id'),{'id':identifier}).one()
    assert counts["cost_identities"]==2 and counts["cost_order_memberships"]==1
    assert all(count==0 for table,count in counts.items() if table not in ("cost_identities","cost_order_memberships"))
    bootstrap_database(review_db.get_bind())
    with review_db.get_bind().connect() as conn:
        assert conn.execute(text('SELECT order_number,revenue,platform_fees,payment_fees,shipping_cost FROM orders WHERE id=:id'),{'id':identifier}).one()==before
        assert {table:conn.execute(text('SELECT COUNT(*) FROM '+table)).scalar_one() for table in tables}==counts
    result=bom_client.get(f'/orders/{identifier}')
    assert result.status_code==200
    assert all(result.json()[field] is None for field in ('actual_cost','profit','margin_percent'))
