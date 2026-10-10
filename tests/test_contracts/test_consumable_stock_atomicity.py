"""Stock deductions cannot consume nonexistent units or conceal competing writes."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_consumable_behavior import consumable_client


def test_shortage_does_not_write_phantom_usage(consumable_client,review_db):
    client,_=consumable_client
    created=client.post('/consumables',json={'name':'Synthetic short stock','current_stock':3}).json()
    response=client.post(f"/consumables/{created['id']}/adjust",json={'type':'deduct','quantity':8})
    assert response.status_code==409,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT current_stock FROM consumables WHERE id=:id'),{'id':created['id']}).scalar_one()==3
        assert fresh.execute(text('SELECT COUNT(*) FROM consumable_usage WHERE consumable_id=:id'),{'id':created['id']}).scalar_one()==0


def test_two_manual_deductions_compete_for_actual_stock(consumable_client,review_db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client,_=consumable_client
    created=client.post('/consumables',json={'name':'Synthetic shared stock','current_stock':3}).json()
    barrier=Barrier(2);original=Session.execute
    def synchronized(session,statement,*args,**kwargs):
        if str(statement).lstrip().upper().startswith('UPDATE CONSUMABLES'):
            barrier.wait(timeout=10)
        return original(session,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post(f"/consumables/{created['id']}/adjust",json={'type':'deduct','quantity':2}),range(2)))
    assert sorted(response.status_code for response in responses)==[200,409]
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT current_stock FROM consumables WHERE id=:id'),{'id':created['id']}).scalar_one()==1
        assert list(fresh.execute(text('SELECT quantity_used FROM consumable_usage WHERE consumable_id=:id'),{'id':created['id']}).scalars())==[2]

@pytest.mark.parametrize('stock,quantity,kind',[(1e308,1e308,'restock'),(float('inf'),1,'deduct'),(float('inf'),1,'restock')])
def test_invalid_or_overflowing_stock_does_not_commit_usage(consumable_client,review_db,stock,quantity,kind):
    client,_=consumable_client
    identifier=client.post('/consumables',json={'name':'Synthetic balance','current_stock':1}).json()['id']
    review_db.execute(text('UPDATE consumables SET current_stock=:stock WHERE id=:id'),{'stock':stock,'id':identifier});review_db.commit()
    response=client.post(f'/consumables/{identifier}/adjust',json={'type':kind,'quantity':quantity})
    assert response.status_code==409,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT current_stock FROM consumables WHERE id=:id'),{'id':identifier}).scalar_one()==stock
        assert fresh.execute(text('SELECT COUNT(*) FROM consumable_usage WHERE consumable_id=:id'),{'id':identifier}).scalar_one()==0


def test_competing_finite_restocks_cannot_overflow_balance(consumable_client,review_db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    import math
    client,_=consumable_client
    identifier=client.post('/consumables',json={'name':'Synthetic overflow race','current_stock':0}).json()['id']
    barrier=Barrier(2);original=Session.execute
    def synchronized(session,statement,*args,**kwargs):
        if str(statement).lstrip().upper().startswith('UPDATE CONSUMABLES'):barrier.wait(timeout=10)
        return original(session,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',synchronized)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:client.post(f'/consumables/{identifier}/adjust',json={'type':'restock','quantity':1e308}),range(2)))
    assert sorted(response.status_code for response in responses)==[200,409]
    with Session(review_db.get_bind()) as fresh:
        stock=fresh.execute(text('SELECT current_stock FROM consumables WHERE id=:id'),{'id':identifier}).scalar_one()
        assert math.isfinite(stock) and stock==1e308
        assert list(fresh.execute(text('SELECT quantity_used FROM consumable_usage WHERE consumable_id=:id'),{'id':identifier}).scalars())==[-1e308]
