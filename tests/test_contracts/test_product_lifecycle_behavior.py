"""Product lifecycle uses actual opaque credentials and persistent database state."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def product_client(token_client,review_db):
    from modules.orders.routes.products import router
    client,issue=token_client;client.app.include_router(router)
    review_db.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(199,'Synthetic existing',1,0)"))
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(199,'Synthetic existing BOM model',1)"))
    review_db.execute(text("INSERT INTO consumables(id,name,current_stock) VALUES(199,'Synthetic existing BOM supply',10)"))
    review_db.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(199,199,1)'))
    review_db.execute(text('INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(199,199,2)'))
    review_db.commit()
    return client,issue

@pytest.mark.parametrize('action',['create','update','delete'])
@pytest.mark.parametrize('scope',['read','write:jobs'])
def test_narrow_credentials_cannot_mutate_products(product_client,review_db,action,scope):
    client,issue=product_client;headers=issue([scope])
    before=review_db.execute(text('SELECT id,name,price FROM products ORDER BY id')).all()
    if action=='create':response=client.post('/products',headers=headers,json={'name':'Synthetic forbidden'})
    elif action=='update':response=client.patch('/products/199',headers=headers,json={'name':'Synthetic forbidden'})
    else:response=client.delete('/products/199',headers=headers)
    assert response.status_code==403
    assert review_db.execute(text('SELECT id,name,price FROM products ORDER BY id')).all()==before


def test_operator_cannot_delete_product(product_client,review_db):
    client,issue=product_client
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"));review_db.commit()
    response=client.delete('/products/199',headers=issue(['write']))
    assert response.status_code==403
    assert review_db.execute(text('SELECT COUNT(*) FROM products WHERE id=199')).scalar_one()==1
    assert review_db.execute(text('SELECT COUNT(*) FROM product_components WHERE product_id=199')).scalar_one()==1
    assert review_db.execute(text('SELECT COUNT(*) FROM product_consumables WHERE product_id=199')).scalar_one()==1


def test_authorized_product_create_edit_list_delete_preserves_fields(product_client):
    client,issue=product_client;headers=issue(['admin'])
    fields={'name':'Synthetic gear','sku':'SYNTHETIC-001','price':0,'description':'Synthetic product description'}
    response=client.post('/products',headers=headers,json=fields)
    assert response.status_code==200
    identifier=response.json()['id']
    for key,value in fields.items():assert response.json()[key]==value
    response=client.patch(f'/products/{identifier}',headers=headers,json={'name':'Synthetic revised','price':12.5})
    assert response.status_code==200 and response.json()['sku']==fields['sku']
    assert response.json()['description']==fields['description']
    listing=client.get('/products',headers=headers)
    assert listing.status_code==200
    saved=next(r for r in listing.json() if r['id']==identifier)
    assert saved['name']=='Synthetic revised' and saved['price']==12.5
    assert client.delete(f'/products/{identifier}',headers=headers).status_code==204
    assert client.get(f'/products/{identifier}',headers=headers).status_code==404


@pytest.mark.parametrize('path',['/products','/products/199'])
def test_unrelated_credentials_cannot_read_products(product_client,path):
    client,issue=product_client
    assert client.get(path,headers=issue(['write:jobs'])).status_code==403


@pytest.mark.parametrize('scope',['read','admin','interactive'])
def test_product_own_shared_and_foreign_visibility(product_client,review_db,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue=product_client
    review_db.execute(text("INSERT INTO products(id,name,org_id) VALUES(200,'Synthetic foreign product',2),(201,'Synthetic shared product',NULL)"));review_db.commit()
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    response=client.get('/products',headers=headers)
    assert response.status_code==200
    assert {r['id'] for r in response.json()}=={199,201}
    assert client.get('/products/199',headers=headers).status_code==200
    assert client.get('/products/201',headers=headers).status_code==200
    assert client.get('/products/200',headers=headers).status_code==404


@pytest.mark.parametrize('kind',['component_add','component_remove','supply_add','supply_remove'])
@pytest.mark.parametrize('scope',['read','write:jobs'])
def test_narrow_credentials_cannot_change_bom_links(product_client,review_db,kind,scope):
    client,issue=product_client;headers=issue([scope])
    before_components=review_db.execute(text('SELECT id,product_id,model_id,quantity_needed FROM product_components ORDER BY id')).all()
    before_supplies=review_db.execute(text('SELECT id,product_id,consumable_id,quantity_per_product FROM product_consumables ORDER BY id')).all()
    if kind=='component_add':response=client.post('/products/199/components',headers=headers,json={'model_id':199,'quantity_needed':2})
    elif kind=='component_remove':response=client.delete(f'/products/199/components/{before_components[0][0]}',headers=headers)
    elif kind=='supply_add':response=client.post('/products/199/consumables',headers=headers,json={'consumable_id':199,'quantity_per_product':2})
    else:response=client.delete(f'/products/199/consumables/{before_supplies[0][0]}',headers=headers)
    assert response.status_code==403
    assert review_db.execute(text('SELECT id,product_id,model_id,quantity_needed FROM product_components ORDER BY id')).all()==before_components
    assert review_db.execute(text('SELECT id,product_id,consumable_id,quantity_per_product FROM product_consumables ORDER BY id')).all()==before_supplies


def test_operator_write_scope_can_add_and_remove_both_bom_link_types(product_client,review_db):
    client,issue=product_client
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"));review_db.commit()
    headers=issue(['write'])
    component=client.post('/products/199/components',headers=headers,json={'model_id':199,'quantity_needed':3})
    supply=client.post('/products/199/consumables',headers=headers,json={'consumable_id':199,'quantity_per_product':4})
    assert component.status_code==200 and supply.status_code==200
    assert component.json()['quantity_needed']==3 and supply.json()['quantity_per_product']==4
    assert client.delete(f"/products/199/components/{component.json()['id']}",headers=headers).status_code==204
    assert client.delete(f"/products/199/consumables/{supply.json()['id']}",headers=headers).status_code==200
    assert review_db.execute(text('SELECT COUNT(*) FROM product_components WHERE product_id=199')).scalar_one()==1
    assert review_db.execute(text('SELECT COUNT(*) FROM product_consumables WHERE product_id=199')).scalar_one()==1
