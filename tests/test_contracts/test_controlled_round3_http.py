"""Real app response projection for order items and distinct nested identities."""
import pytest
from tests.test_contracts.test_cert_G_harness import run_probe


@pytest.mark.parametrize('surface', ['order', 'components', 'timelapse'])
def test_response_resource_identity(tmp_path, surface):
    result=run_probe('controlled-round3-'+surface,r'''
install_license('enterprise', features=['controlled_parts'])
uid=seed_user('member','operator');seed_user('super','admin')
member=login('member');outsider=login('super')
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')")
sql(f"INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,{uid})")
sql("INSERT INTO models(id,name) VALUES(1,'Secret model'),(2,'Public model')")
sql("INSERT INTO print_files(id,filename,model_id) VALUES(1,'secret.3mf',1)")
sql("INSERT INTO jobs(id,item_name,print_file_id,status) VALUES(1,'Secret job',1,'completed'),(2,'Public job',NULL,'completed')")
assert member.put('/api/controlled-parts/files/1',json=dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='fixture')).status_code==200
sql("INSERT INTO products(id,name,sku) VALUES(1,'Secret product','Secret SKU'),(2,'Public product','Public SKU')")
# Component 1 names public model 2; model 1 is controlled. IDs are not interchangeable.
sql("INSERT INTO product_components(id,product_id,model_id,quantity_needed) VALUES(1,2,2,1),(2,1,1,1)")
sql("INSERT INTO orders(id,order_number,status) VALUES(1,'Public order','pending')")
sql("INSERT INTO order_items(id,order_id,product_id,quantity,fulfilled_quantity,unit_price) VALUES(1,1,1,1,0,12)")
sql("INSERT INTO printers(id,name) VALUES(1,'Public printer')")
sql("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(1,1,2,'Public print','completed',CURRENT_TIMESTAMP)")
''' + {
    'order': r'''
r=outsider.get('/api/orders/1');assert r.status_code==200,r.text
item=r.json()['items'][0];assert item['product_name']==item['product_sku']=='[controlled]',item
r=member.get('/api/orders/1');assert r.json()['items'][0]['product_sku']=='Secret SKU',r.text
''',
    'components': r'''
r=outsider.get('/api/products/2');assert r.status_code==200,r.text
assert r.json()['components'][0]['model_name']=='Public model',r.text
r=outsider.get('/api/products/1');assert r.status_code==200,r.text
assert r.json()['components'][0]['model_name']=='[controlled]',r.text
''',
    'timelapse': r'''
# Exercise the actual middleware on the historical timelapse wire shape. The
# normal list SQL can filter this row first; replayed projections still need it.
from fastapi import Depends
from core.dependencies import get_current_user
@app.get('/api/round3/timelapses')
def projection(actor=Depends(get_current_user)):
 return [{'id':70,'print_job_id':1,'job_name':'Secret job'}]
r=outsider.get('/api/round3/timelapses');assert r.status_code==200,r.text
assert r.json()[0]['job_name']=='[controlled]',r.text
r=member.get('/api/round3/timelapses');assert r.json()[0]['job_name']=='Secret job',r.text
'''}[surface] + "\nOUT['checked']=True\n",tmp_path)
    assert result['checked'] and result['_net_attempts']==[]
