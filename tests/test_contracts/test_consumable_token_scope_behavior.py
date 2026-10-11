"""Real opaque API token authentication must bound global consumable writes."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def consumable_token_client(token_client,review_db):
    from modules.orders.routes import consumables
    client,issue=token_client;client.app.include_router(consumables.router)
    review_db.execute(text("INSERT INTO consumables(id,name,unit,cost_per_unit,current_stock,min_stock,status) VALUES(199,'Synthetic scope stock','piece',0,5,1,'active')"));review_db.commit()
    return client,issue

@pytest.mark.parametrize('scope',['read','read:spools','write:jobs','write:printers'])
@pytest.mark.parametrize('operation',['create','update','adjust','delete'])
def test_unrelated_token_cannot_mutate_consumables(consumable_token_client,review_db,scope,operation):
    client,issue=consumable_token_client;headers=issue([scope])
    before=tuple(review_db.execute(text('SELECT id,name,current_stock FROM consumables ORDER BY id')).all())
    if operation=='create':response=client.post('/consumables',headers=headers,json={'name':'Denied synthetic','current_stock':10})
    elif operation=='update':response=client.patch('/consumables/199',headers=headers,json={'current_stock':999})
    elif operation=='adjust':response=client.post('/consumables/199/adjust',headers=headers,json={'type':'deduct','quantity':2})
    else:response=client.delete('/consumables/199',headers=headers)
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        assert tuple(fresh.execute(text('SELECT id,name,current_stock FROM consumables ORDER BY id')).all())==before
        assert fresh.execute(text('SELECT COUNT(*) FROM consumable_usage')).scalar_one()==0


@pytest.mark.parametrize('scope',['write','admin'])
@pytest.mark.parametrize('operation',['create','update','adjust','delete'])
def test_authorized_global_token_can_mutate_consumables(consumable_token_client,review_db,scope,operation):
    client,issue=consumable_token_client;headers=issue([scope])
    if operation=='create':response=client.post('/consumables',headers=headers,json={'name':'Authorized synthetic','current_stock':10})
    elif operation=='update':response=client.patch('/consumables/199',headers=headers,json={'current_stock':7})
    elif operation=='adjust':response=client.post('/consumables/199/adjust',headers=headers,json={'type':'deduct','quantity':2})
    else:response=client.delete('/consumables/199',headers=headers)
    assert response.status_code==200
    with Session(review_db.get_bind()) as fresh:
        if operation=='create':assert fresh.execute(text('SELECT COUNT(*) FROM consumables')).scalar_one()==2
        elif operation=='update':assert fresh.execute(text('SELECT current_stock FROM consumables WHERE id=199')).scalar_one()==7
        elif operation=='adjust':
            assert fresh.execute(text('SELECT current_stock FROM consumables WHERE id=199')).scalar_one()==3
            assert list(fresh.execute(text('SELECT quantity_used FROM consumable_usage')).scalars())==[2]
        else:assert fresh.execute(text('SELECT COUNT(*) FROM consumables')).scalar_one()==0
