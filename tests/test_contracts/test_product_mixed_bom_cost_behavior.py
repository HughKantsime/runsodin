"""Whole mixed-BOM estimates preserve explicit zero and unknown input costs."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_product_bom_integrity import bom_client, review_db


@pytest.mark.parametrize('mode,expected', [('mixed_known',10.5),('all_zero',0.0),('unknown_printed',None),('unknown_consumable',None)])
@pytest.mark.parametrize('endpoint',['detail','list'])
def test_mixed_bom_estimate_accounts_for_every_component_cost(bom_client,review_db,mode,expected,endpoint):
 review_db.execute(text("INSERT INTO models(id,name,org_id,cost_per_item) VALUES(104,'Synthetic second printed component',1,0)"))
 review_db.execute(text("INSERT INTO consumables(id,name,current_stock,cost_per_unit) VALUES(201,'Synthetic purchased component',100,0.75),(202,'Synthetic known zero component',100,0)"))
 if mode=='all_zero':
  review_db.execute(text('UPDATE models SET cost_per_item=0 WHERE id=101'))
  review_db.execute(text('UPDATE consumables SET cost_per_unit=0 WHERE id=201'))
 elif mode=='unknown_printed':review_db.execute(text('UPDATE models SET cost_per_item=NULL WHERE id=101'))
 elif mode=='unknown_consumable':review_db.execute(text('UPDATE consumables SET cost_per_unit=NULL WHERE id=201'))
 review_db.commit()
 for identifier,quantity in [(101,3),(104,2)]:
  response=bom_client.post('/products/101/components',json={'model_id':identifier,'quantity_needed':quantity})
  assert response.status_code==200,response.text
 for identifier,quantity in [(201,4),(202,5)]:
  response=bom_client.post('/products/101/consumables',json={'consumable_id':identifier,'quantity_per_product':quantity})
  assert response.status_code==200,response.text
 response=bom_client.get('/products/101' if endpoint=='detail' else '/products')
 assert response.status_code==200,response.text
 product=response.json() if endpoint=='detail' else next(row for row in response.json() if row['id']==101)
 assert product['estimated_cogs']==expected
 assert product['component_count']==2 and len(product['consumables'])==2
 assert review_db.execute(text('SELECT SUM(quantity_needed) FROM product_components WHERE product_id=101')).scalar_one()==5
 assert review_db.execute(text('SELECT SUM(quantity_per_product) FROM product_consumables WHERE product_id=101')).scalar_one()==9


@pytest.mark.parametrize('endpoint',['detail','list'])
def test_empty_bom_has_no_cost_estimate(bom_client,endpoint):
 response=bom_client.get('/products/101' if endpoint=='detail' else '/products')
 assert response.status_code==200
 product=response.json() if endpoint=='detail' else next(row for row in response.json() if row['id']==101)
 assert product['estimated_cogs'] is None


@pytest.mark.parametrize('change',[
 'dangling_model','dangling_supply','foreign_model','global_product_private_model',
 'invalid_print_quantity','invalid_supply_quantity','negative_cost','nonfinite_cost',
 'multiply_overflow','sum_overflow',
])
@pytest.mark.parametrize('endpoint',['detail','list'])
def test_unresolved_legacy_bom_cost_is_unknown_without_side_effects(bom_client,review_db,change,endpoint):
 test_mixed_bom_estimate_accounts_for_every_component_cost(bom_client,review_db,'mixed_known',10.5,'detail')
 statements={
 'dangling_model':'UPDATE product_components SET model_id=99999 WHERE product_id=101 AND model_id=104',
 'dangling_supply':'UPDATE product_consumables SET consumable_id=99999 WHERE product_id=101 AND consumable_id=202',
 'foreign_model':'UPDATE models SET org_id=2 WHERE id=104',
 'global_product_private_model':'UPDATE products SET org_id=NULL WHERE id=101',
 'invalid_print_quantity':'UPDATE product_components SET quantity_needed=0 WHERE product_id=101 AND model_id=104',
 'invalid_supply_quantity':'UPDATE product_consumables SET quantity_per_product=-2 WHERE product_id=101 AND consumable_id=202',
 'negative_cost':'UPDATE consumables SET cost_per_unit=-1 WHERE id=201',
 }
 if change.startswith('dangling_'):
  # Owned legacy-data fixture only: restore enforcement before exercising HTTP.
  review_db.commit();review_db.execute(text('PRAGMA foreign_keys=OFF'))
  assert review_db.execute(text('PRAGMA foreign_keys')).scalar_one()==0
 if change in statements:review_db.execute(text(statements[change]))
 elif change=='nonfinite_cost':review_db.execute(text('UPDATE consumables SET cost_per_unit=:value WHERE id=201'),{'value':float('inf')})
 elif change=='multiply_overflow':review_db.execute(text('UPDATE models SET cost_per_item=:value WHERE id=101'),{'value':1e308})
 else:
  review_db.execute(text('UPDATE models SET cost_per_item=:value WHERE id IN (101,104)'),{'value':1e308})
  review_db.execute(text('UPDATE product_components SET quantity_needed=1 WHERE product_id=101'))
 review_db.commit()
 if change.startswith('dangling_'):
  review_db.execute(text('PRAGMA foreign_keys=ON'))
  assert review_db.execute(text('PRAGMA foreign_keys')).scalar_one()==1
 stock_before=review_db.execute(text('SELECT id,current_stock FROM consumables ORDER BY id')).all()
 jobs_before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
 response=bom_client.get('/products/101' if endpoint=='detail' else '/products')
 assert response.status_code==200,response.text
 product=response.json() if endpoint=='detail' else next(row for row in response.json() if row['id']==101)
 assert product['estimated_cogs'] is None
 assert review_db.execute(text('SELECT id,current_stock FROM consumables ORDER BY id')).all()==stock_before
 assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==jobs_before


@pytest.mark.parametrize('quantity',[float('inf'),float('nan'),None,0,-1])
def test_nonfinite_or_invalid_supply_quantity_cannot_generate_estimate(quantity):
 from types import SimpleNamespace as S
 from modules.orders.product_costing import estimated_product_cogs
 product=S(org_id=1,components=[],consumable_links=[S(consumable=S(cost_per_unit=1),quantity_per_product=quantity)])
 assert estimated_product_cogs(product) is None


def test_fractional_consumable_amount_is_valid_in_product_estimate():
 from types import SimpleNamespace as S
 from modules.orders.product_costing import estimated_product_cogs
 product=S(org_id=1,components=[],consumable_links=[S(consumable=S(cost_per_unit=4),quantity_per_product=0.25)])
 assert estimated_product_cogs(product)==1


def test_delete_product_with_mixed_bom_preserves_models_supplies_and_stock(bom_client,review_db):
 from core.dependencies import get_current_user
 test_mixed_bom_estimate_accounts_for_every_component_cost(bom_client,review_db,'mixed_known',10.5,'detail')
 bom_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':1,'is_active':True}
 review_db.execute(text('INSERT INTO product_consumables(product_id,consumable_id,quantity_per_product) VALUES(102,201,7)'))
 review_db.execute(text("INSERT INTO consumable_usage(consumable_id,quantity_used,notes) VALUES(201,2,'Synthetic retained usage')"))
 review_db.commit()
 usage_before=review_db.execute(text('SELECT id,consumable_id,quantity_used,notes FROM consumable_usage ORDER BY id')).all()
 before=review_db.execute(text('SELECT id,current_stock,cost_per_unit FROM consumables ORDER BY id')).all()
 response=bom_client.delete('/products/101')
 assert response.status_code==204,response.text
 assert bom_client.get('/products/101').status_code==404
 with review_db.get_bind().connect() as fresh:
  assert fresh.execute(text('SELECT count(*) FROM product_components WHERE product_id=101')).scalar_one()==0
  assert fresh.execute(text('SELECT count(*) FROM product_consumables WHERE product_id=101')).scalar_one()==0
  assert fresh.execute(text('SELECT id,current_stock,cost_per_unit FROM consumables ORDER BY id')).all()==before
  assert fresh.execute(text('SELECT count(*) FROM models WHERE id IN (101,104)')).scalar_one()==2
  assert fresh.execute(text('SELECT id,consumable_id,quantity_used,notes FROM consumable_usage ORDER BY id')).all()==usage_before
  assert fresh.execute(text('SELECT quantity_per_product FROM product_consumables WHERE product_id=102 AND consumable_id=201')).scalar_one()==7
