"""Explicit zero completed estimates must not erase recorded fees or revenue."""
import pytest
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client

@pytest.mark.parametrize('revenue,job_cost,expected_cost,expected_profit',[
    (30.0,0.0,7.0,23.0),(0.0,5.0,12.0,-12.0),(0.0,0.0,7.0,-7.0),
])
def test_explicit_zero_financial_values_preserve_known_fees_and_profit(bom_client,review_db,revenue,job_cost,expected_cost,expected_profit):
    from core.base import JobStatus
    from modules.jobs.models import Job
    response=bom_client.post('/orders',json={'order_number':'Synthetic zero-value finances','revenue':revenue,'platform_fees':2,'payment_fees':1,'shipping_cost':4,'items':[{'product_id':101,'quantity':1,'unit_price':revenue}]})
    assert response.status_code==200
    order=response.json();identifier=order['id'];item=order['items'][0]['id']
    review_db.add(Job(item_name='Synthetic completed known estimate',order_item_id=item,status=JobStatus.COMPLETED,estimated_cost=job_cost,quantity=1,quantity_on_bed=1,charged_to_org_id=1))
    review_db.commit()
    response=bom_client.get(f'/orders/{identifier}')
    assert response.status_code==200
    data=response.json()
    # This exercises the existing completed-job-estimate formula, not actual-usage COGS.
    assert data['actual_cost']==expected_cost
    assert data['profit']==expected_profit
    assert data['revenue']==revenue
    if revenue==0:assert data['margin_percent'] is None


@pytest.mark.parametrize('recorded', [False, True])
def test_fee_only_zero_is_distinct_from_missing_cost_data(bom_client, recorded):
    fee = 0 if recorded else None
    response = bom_client.post('/orders', json={'order_number': 'Synthetic zero fee presence', 'revenue': 10,
        'platform_fees': fee, 'payment_fees': fee, 'shipping_cost': fee,
        'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 10}]})
    assert response.status_code == 200, response.text
    result = bom_client.get(f"/orders/{response.json()['id']}").json()
    assert result['estimated_cost'] == (0 if recorded else None)
    assert result['actual_cost'] == (0 if recorded else None)
    assert result['profit'] == (10 if recorded else None)
