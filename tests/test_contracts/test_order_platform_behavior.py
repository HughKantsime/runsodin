"""Advertised platform values persist and filter through actual order HTTP routes."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_product_bom_integrity import bom_client, review_db


@pytest.mark.parametrize('platform', ['etsy', 'amazon', 'wholesale', 'direct'])
def test_order_platform_create_update_filter_and_reload(bom_client, review_db, platform):
    from core.dependencies import get_current_user
    bom_client.app.dependency_overrides[get_current_user] = lambda: {'id':3, 'role':'admin', 'group_id':1, 'is_active':True}
    response = bom_client.post('/orders', json={'order_number':'Synthetic platform order', 'platform':platform,
        'customer_name':'Synthetic purchaser', 'items':[{'product_id':101, 'quantity':2, 'unit_price':3.5}]})
    assert response.status_code == 200
    identifier = response.json()['id']
    assert response.json()['platform'] == platform
    detail = bom_client.get(f'/orders/{identifier}')
    assert detail.status_code == 200 and detail.json()['platform'] == platform
    assert detail.json()['items'][0]['quantity'] == 2
    review_db.execute(text("INSERT INTO orders(order_number,org_id,status,platform) VALUES('Synthetic foreign platform',2,'pending',:platform)"), {'platform':platform})
    review_db.commit()
    filtered = bom_client.get('/orders', params={'platform':platform})
    assert filtered.status_code == 200
    assert {row['id'] for row in filtered.json()} == {identifier}
    assert filtered.json()[0]['platform'] == platform
    changed = 'direct' if platform != 'direct' else 'wholesale'
    response = bom_client.patch(f'/orders/{identifier}', json={'platform':changed})
    assert response.status_code == 200 and response.json()['platform'] == changed
    assert bom_client.get('/orders', params={'platform':platform}).json() == []
    result = bom_client.get('/orders', params={'platform':changed})
    assert result.status_code == 200 and {row['id'] for row in result.json()} == {identifier}
    detail = bom_client.get(f'/orders/{identifier}').json()
    assert detail['platform'] == changed and detail['customer_name'] == 'Synthetic purchaser'
    assert detail['items'][0]['quantity'] == 2 and detail['items'][0]['unit_price'] == 3.5
    assert review_db.execute(text('SELECT platform FROM orders WHERE id=:id'), {'id':identifier}).scalar_one() == changed
