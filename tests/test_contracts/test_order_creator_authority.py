"""Actual JWT order birth cannot infer platform authority from NULL organization."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize("name,scope,key",[("admin","org",1),("platform","global",0),("unassigned",None,None)])
def test_order_birth_attests_actual_actor_authority(creators,review_db,name,scope,key):
    from modules.orders.routes.orders_crud import router
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    response=client.post("/api/orders",headers=auth(name),json={"order_number":"OWNED-AUTH-"+name,"platform":"direct","customer_name":"Fixture"})
    assert response.status_code == 200, response.text
    identifier=response.json()["id"]
    review_db.expire_all()
    row=review_db.execute(text("SELECT o.economic_uid,i.owner_scope,i.owner_key FROM orders o LEFT JOIN cost_identities i ON i.identity_uid=o.economic_uid WHERE o.id=:id"),{"id":identifier}).one()
    assert tuple(row[1:]) == (scope,key)
    assert bool(row[0]) == (scope is not None)


@pytest.mark.parametrize("name,scope,key",[("admin","org",1),("platform","global",0),("unassigned",None,None)])
def test_inline_and_added_items_keep_actual_owner_authority(creators,review_db,monkeypatch,name,scope,key):
    from modules.orders.routes.orders_crud import router
    from modules.orders.models import Product
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    monkeypatch.setitem(registry._providers,"OrderFulfillmentProvider",OrderFulfillmentService())
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    product=Product(name="Owned product",price=12,org_id=1 if name=="admin" else None)
    review_db.add(product);review_db.commit()
    response=client.post("/api/orders",headers=auth(name),json={"order_number":"OWNED-ITEM-"+name,"items":[{"product_id":product.id,"quantity":2}]})
    assert response.status_code == 200,response.text
    oid=response.json()["id"]
    added=client.post(f"/api/orders/{oid}/items",headers=auth(name),json={"product_id":product.id,"quantity":3})
    assert added.status_code == 200,added.text
    review_db.expire_all()
    rows=review_db.execute(text("SELECT i.quantity,i.unit_price,i.economic_uid,c.owner_scope,c.owner_key FROM order_items i LEFT JOIN cost_identities c ON c.identity_uid=i.economic_uid WHERE i.order_id=:id ORDER BY i.id"),{"id":oid}).all()
    assert len(rows)==2
    assert [(r[0],r[1]) for r in rows]==[(2,12),(3,12)]
    for row in rows:
        assert tuple(row[3:])==(scope,key)
        assert bool(row[2])==(scope is not None)


def test_inline_item_registry_failure_rolls_back_whole_order_graph(creators,review_db):
    from fastapi.testclient import TestClient
    from modules.orders.routes.orders_crud import router
    from modules.orders.models import Product
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    product=Product(name="Rollback product",price=12,org_id=1)
    review_db.add(product);review_db.commit()
    def counts():
        return tuple(review_db.execute(text("SELECT count(*) FROM "+table)).scalar_one() for table in ("orders","order_items","cost_identities","cost_order_memberships"))
    before=counts();review_db.commit()
    review_db.execute(text("CREATE TRIGGER owned_fail_item_registry BEFORE INSERT ON cost_identities WHEN NEW.resource_kind='item' BEGIN SELECT RAISE(ABORT, 'owned item registry failure'); END"));review_db.commit()
    failing=TestClient(client.app,raise_server_exceptions=False)
    response=failing.post("/api/orders",headers=auth(),json={"order_number":"OWNED-ROLLBACK","items":[{"product_id":product.id,"quantity":2}]})
    assert response.status_code==500
    review_db.expire_all()
    assert counts()==before


@pytest.mark.parametrize("name,scope,key",[("admin","org",1),("platform","global",0),("unassigned",None,None)])
def test_order_schedule_jobs_keep_actor_authority(creators,review_db,monkeypatch,name,scope,key):
    from modules.orders.routes.orders_crud import router
    from modules.orders.models import Product,ProductComponent
    from modules.models_library.models import Model
    from modules.orders.production import OrderFulfillmentService
    from core.registry import registry
    monkeypatch.setitem(registry._providers,"OrderFulfillmentProvider",OrderFulfillmentService())
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    owner=1 if name=="admin" else None
    model=Model(name="Owned schedule model",org_id=owner,quantity_per_bed=1,build_time_hours=1)
    product=Product(name="Owned schedule product",price=12,org_id=owner)
    review_db.add_all([model,product]);review_db.flush()
    review_db.add(ProductComponent(product_id=product.id,model_id=model.id,quantity_needed=1));review_db.commit()
    created=client.post("/api/orders",headers=auth(name),json={"order_number":"OWNED-SCHEDULE-"+name,"items":[{"product_id":product.id,"quantity":1}]})
    assert created.status_code==200,created.text
    oid=created.json()["id"]
    response=client.post(f"/api/orders/{oid}/schedule",headers=auth(name))
    assert response.status_code==200,response.text
    review_db.expire_all()
    rows=review_db.execute(text("SELECT j.economic_uid,c.owner_scope,c.owner_key FROM jobs j JOIN order_items i ON i.id=j.order_item_id LEFT JOIN cost_identities c ON c.identity_uid=j.economic_uid WHERE i.order_id=:id"),{"id":oid}).all()
    assert len(rows)==1
    assert tuple(rows[0][1:])==(scope,key)
    assert bool(rows[0][0])==(scope is not None)


@pytest.mark.parametrize("scopes,expected",[(["read"],403),(["write"],200)])
def test_order_creation_obeys_real_api_key_write_scope(creators,review_db,scopes,expected):
    from modules.orders.routes.orders_crud import router
    client,_,token,_=creators
    client.app.include_router(router,prefix="/api")
    before=review_db.execute(text("SELECT count(*) FROM orders")).scalar_one()
    response=client.post("/api/orders",headers=token(scopes),json={"order_number":"OWNED-KEY-SCOPE"})
    assert response.status_code==expected,response.text
    review_db.expire_all()
    assert review_db.execute(text("SELECT count(*) FROM orders")).scalar_one()==before+(expected==200)


@pytest.mark.parametrize("operation",["create","update","delete","add_item","update_item","delete_item","ship"])
@pytest.mark.parametrize("scope",["read","write"])
def test_every_order_mutation_has_actual_scoped_key_boundary(creators,review_db,monkeypatch,operation,scope):
    from modules.orders.routes.orders_crud import router
    from modules.orders.models import Product
    from modules.orders.production import OrderFulfillmentService
    from core.registry import registry
    monkeypatch.setitem(registry._providers,"OrderFulfillmentProvider",OrderFulfillmentService())
    client,auth,token,_=creators
    client.app.include_router(router,prefix="/api")
    product=Product(name="Scope fixture",price=12,org_id=1)
    review_db.add(product);review_db.commit()
    created=client.post("/api/orders",headers=auth(),json={"order_number":"OWNED-MUTATION","items":[{"product_id":product.id,"quantity":2}]})
    assert created.status_code==200,created.text
    oid=created.json()["id"]
    iid=review_db.execute(text("SELECT id FROM order_items WHERE order_id=:id"),{"id":oid}).scalar_one()
    if operation=="ship":
        # Shipping is only reachable from the production lifecycle (pending orders are refused).
        review_db.execute(text("UPDATE orders SET status='fulfilled' WHERE id=:id"),{"id":oid});review_db.commit()
    def snapshot():
        return (tuple(review_db.execute(text("SELECT id,notes,status,tracking_number FROM orders ORDER BY id")).all()),tuple(review_db.execute(text("SELECT id,order_id,quantity,unit_price FROM order_items ORDER BY id")).all()))
    before=snapshot();review_db.commit()
    routes={
      "create":("POST","/api/orders",{"order_number":"OWNED-NEW"},200),
      "update":("PATCH",f"/api/orders/{oid}",{"notes":"changed"},200),
      "delete":("DELETE",f"/api/orders/{oid}",None,204),
      "add_item":("POST",f"/api/orders/{oid}/items",{"product_id":product.id,"quantity":3},200),
      "update_item":("PATCH",f"/api/orders/{oid}/items/{iid}",{"quantity":3},200),
      "delete_item":("DELETE",f"/api/orders/{oid}/items/{iid}",None,204),
      "ship":("PATCH",f"/api/orders/{oid}/ship",{"tracking_number":"owned tracking"},200)}
    method,path,body,status=routes[operation]
    response=client.request(method,path,headers=token([scope]),**({"json":body} if body is not None else {}))
    assert response.status_code==(403 if scope=="read" else status),response.text
    review_db.expire_all()
    if scope=="read":assert snapshot()==before
    else:assert snapshot()!=before
