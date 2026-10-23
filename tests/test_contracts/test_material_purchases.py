"""Synthetic order lines never count as received hardware; no external access."""
import uuid,json
from datetime import datetime,timezone,timedelta
import pytest
from sqlalchemy import text
from tests.test_contracts.test_batch_acceptance import batch
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_education_review_workflow import review_db
BASE='/consumables/purchases'
@pytest.fixture
def materials(batch):
    from modules.orders.routes.materials import router
    from modules.orders.routes.consumables import router as legacy
    batch[4].include_router(router);batch[4].include_router(legacy)
    batch[4].router.routes.sort(key=lambda r:0 if getattr(r,'path','').startswith(BASE) else 1)
    return batch

def create(m):
    r=m[0].post(BASE+'/materials',json={'name':'Synthetic magnet','specification':'Synthetic 6x3 mm','unit':'piece'});assert r.status_code==200,r.text
    cid=r.json()['materials'][-1]['consumable_id']
    mapping={'consumable_id':cid,'vendor':'Amazon','listing_id':'SYNTHETIC-ASIN','variant':'6x3 mm 100-pack','specification':'Synthetic 6x3 mm','units_per_pack':100,'verified':True}
    r=m[0].post(BASE+'/mappings',json=mapping);assert r.status_code==200,r.text
    return cid,mapping

def line(**changes):
    value={'vendor':'Amazon','order_reference':'SYNTHETIC-ORDER','line_reference':'1','listing_id':'SYNTHETIC-ASIN','variant':'6x3 mm 100-pack','specification':'Synthetic 6x3 mm','packs':2,'units_per_pack':100,'total_cost_minor':1299,'source_reference':'Books original pointer, no copied document'};value.update(changes);return value

def order(m,**changes):
    r=m[0].post(BASE+'/lines',json={'lines':[line(**changes)]});assert r.status_code==200,r.text;return r.json()['lines'][-1]
def receipt(m,lid,kind='receive_good',units=100,**changes):
    p={'source_event_reference':'SYNTHETIC-PACKAGE-'+uuid.uuid4().hex,'request_id':uuid.uuid4().hex,'line_id':lid,'kind':kind,'units':units,'occurred_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'confirmed':True,'note':'' if kind=='receive_good' else 'Synthetic physical disposition'};p.update(changes)
    return m[0].post(BASE+'/receipts',json=p),p

def test_purchase_incoming_receipt_damage_cancel_and_return_do_not_duplicate_stock(materials):
    cid,_=create(materials);o=order(materials)
    state=materials[0].get(BASE).json();assert state['materials'][0]['current_stock']==0 and o['totals']['incoming_units']==200
    r,p=receipt(materials,o['id'],units=120);assert r.status_code==200,r.text
    assert r.json()['materials'][0]['current_stock']==120
    assert materials[0].post(BASE+'/receipts',json=p).json()['materials'][0]['current_stock']==120
    p['units']=121;assert materials[0].post(BASE+'/receipts',json=p).status_code==409
    assert receipt(materials,o['id'],'receive_damaged',20)[0].status_code==200
    assert receipt(materials,o['id'],'cancel',60)[0].status_code==200
    r,_=receipt(materials,o['id'],'return_good',10);assert r.json()['materials'][0]['current_stock']==110
    assert receipt(materials,o['id'],'return_damaged',20)[0].status_code==200
    assert receipt(materials,o['id'],'receive_good',1)[0].status_code==409 # Returns never reopen incoming.
    assert receipt(materials,o['id'],'return_good',111)[0].status_code==409
    detail=materials[0].get(f'/consumables/{cid}').json();assert detail['managed_material'] and detail['current_stock']==110
    assert sorted(u['quantity_used'] for u in detail['usage_history'])==[-120,10]
    assert all('SYNTHETIC-ORDER' not in u['notes'] for u in detail['usage_history'])

def test_exact_line_dedup_pack_mapping_and_atomic_mixed_import(materials):
    create(materials);o=order(materials);assert order(materials)['id']==o['id']
    r=materials[0].post(BASE+'/lines',json={'lines':[line(line_reference='2'),line(packs=3)]});assert r.status_code==409
    assert len(materials[0].get(BASE).json()['lines'])==1
    for changes in ({'units_per_pack':50},{'variant':'other'},{'specification':'8x3 mm'},{'listing_id':'other'}):
        assert materials[0].post(BASE+'/lines',json={'lines':[line(line_reference=uuid.uuid4().hex,**changes)]}).status_code==409

@pytest.mark.parametrize('change',[{'confirmed':False},{'units':True},{'units':0},{'occurred_at':'2026-10-20T10:00:00'},{'occurred_at':'2099-01-01T00:00:00Z'}])
def test_invalid_receipt_keeps_zero_stock(materials,change):
    create(materials);o=order(materials);r,_=receipt(materials,o['id'],**change);assert r.status_code in (409,422)
    assert materials[0].get(BASE).json()['materials'][0]['current_stock']==0

def test_managed_legacy_mutation_and_foreign_reads_are_fenced(materials):
    cid,_=create(materials)
    assert materials[0].patch(f'/consumables/{cid}',json={'current_stock':999}).status_code==409
    assert materials[0].patch(f'/consumables/{cid}',json={'name':'Replacement'}).status_code==409
    assert materials[0].post(f'/consumables/{cid}/adjust',json={'quantity':2,'type':'restock'}).status_code==409
    assert materials[0].delete(f'/consumables/{cid}').status_code==409
    materials[1]['group_id']=2
    assert materials[0].get(BASE).json()['materials']==[]
    assert materials[0].get(f'/consumables/{cid}').status_code==404
    assert materials[0].get('/consumables').json()==[]
    assert materials[0].post('/products/102/consumables',json={'consumable_id':cid,'quantity_per_product':2}).status_code==404

def test_private_errors_encryption_and_export_exclusion(materials):
    create(materials);order(materials)
    raw=materials[3].execute(text("SELECT value FROM system_config WHERE key='material_purchases:1'")).scalar_one();assert 'SYNTHETIC-ORDER' not in raw and 'Synthetic magnet' not in raw
    r=materials[0].post(BASE+'/lines',json={'lines':[line(packs='PRIVATE-INVALID')]})
    assert r.status_code==422 and 'PRIVATE-INVALID' not in r.text and 'no-store' in r.headers['cache-control']
    from modules.orders.material_store import guard_tenant
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:guard_tenant(materials[3],1)
    assert e.value.status_code==409

def test_csv_preview_known_mapping_and_focused_exception_is_dry_run(materials):
    create(materials)
    csv='vendor,order_reference,line_reference,listing_id,variant,specification,packs,units_per_pack\nAmazon,SYNTHETIC-ORDER,1,SYNTHETIC-ASIN,6x3 mm 100-pack,Synthetic 6x3 mm,2,100\n'
    r=materials[0].post(BASE+'/preview',json={'csv_text':csv});assert r.status_code==200,r.text
    assert r.json()['can_import'] and r.json()['rows'][0]['ordered_units']==200 and not r.json()['stock_changed']
    assert materials[0].get(BASE).json()['lines']==[]
    r=materials[0].post(BASE+'/preview',json={'csv_text':csv.replace('2,100','2,50')});assert not r.json()['can_import']
    assert materials[0].post(BASE+'/preview',json={'csv_text':'bad,headers\n1,2'}).status_code==422

def test_adoption_requires_count_and_refuses_unscoped_legacy_history(materials):
    legacy=materials[0].post('/consumables',json={'name':'Legacy screw','unit':'piece','current_stock':3}).json();cid=legacy['id']
    body={'consumable_id':cid,'specification':'Synthetic screw','counted_units':3,'physically_counted':False}
    assert materials[0].post(BASE+'/adopt',json=body).status_code==409
    assert materials[0].post(f'/consumables/{cid}/adjust',json={'quantity':1,'type':'restock'}).status_code==200
    body['physically_counted']=True;assert materials[0].post(BASE+'/adopt',json=body).status_code==409
    fresh=materials[0].post('/consumables',json={'name':'Unused legacy screw','unit':'piece','current_stock':3}).json();body.update(consumable_id=fresh['id'],counted_units=4)
    r=materials[0].post(BASE+'/adopt',json=body);assert r.status_code==200,r.text
    assert r.json()['materials'][0]['current_stock']==4

@pytest.mark.parametrize('role,kind',[('viewer','jwt'),('admin','user_api_token')])
def test_interactive_floor(materials,role,kind):
    materials[1].update(role=role,_auth_kind=kind)
    assert materials[0].get(BASE).status_code==403

def assembly_setup(m,stock=10):
    from tests.test_contracts.test_finishing_workflow import bom,component,finish_body
    cid,_=create(m);o=order(m)
    if stock:assert receipt(m,o['id'],units=stock)[0].status_code==200
    assert m[0].post('/products/101/consumables',json={'consumable_id':cid,'quantity_per_product':4}).status_code==200
    preview=bom(m);r=component(m);assert r.status_code==200,r.text
    payload=finish_body(m,r.json(),preview)
    payload.update(hardware_usage=[{'consumable_id':cid,'units':8,'reason':''}],hardware_used_confirmed=True)
    return cid,payload

def test_finishing_consumes_actual_managed_hardware_once(materials):
    cid,payload=assembly_setup(materials)
    response=materials[0].post('/products/production/finishing',json=payload);assert response.status_code==200,response.text
    assert response.json()['material_use']['managed'][0]['used_units']==8
    assert materials[0].get(BASE).json()['materials'][0]['current_stock']==2
    assert materials[0].post('/products/production/finishing',json=payload).status_code==200
    assert materials[0].get(BASE).json()['materials'][0]['current_stock']==2
    assert materials[3].execute(text('SELECT count(*) FROM consumable_usage WHERE consumable_id=:id'),{'id':cid}).scalar_one()==2 # One physical receipt and one actual use.

@pytest.mark.parametrize('mode',['missing','confirmation','shortage','difference_reason'])
def test_finishing_bad_usage_or_shortage_rolls_back_all(materials,mode):
    cid,payload=assembly_setup(materials,5 if mode=='shortage' else 10)
    if mode=='missing':payload['hardware_usage']=[]
    elif mode=='confirmation':payload['hardware_used_confirmed']=False
    elif mode=='difference_reason':payload['hardware_usage'][0]['units']=0
    r=materials[0].post('/products/production/finishing',json=payload);assert r.status_code==409,r.text
    assert materials[0].get(BASE).json()['materials'][0]['current_stock']==(5 if mode=='shortage' else 10)
    assert materials[0].get('/products/production/finishing').json()['components'][0]['available']==3
    assert materials[3].execute(text('SELECT count(*) FROM production_stock_additions')).scalar_one()==0

def test_reused_hardware_needs_reason_and_never_adds_reclaimed_stock(materials):
    cid,payload=assembly_setup(materials)
    payload['hardware_usage'][0].update(units=0,reason='Previously fitted hardware reused; no new units taken')
    r=materials[0].post('/products/production/finishing',json=payload);assert r.status_code==200,r.text
    assert r.json()['material_use']['reclaimed_units']==0
    assert materials[0].get(BASE).json()['materials'][0]['current_stock']==10

def test_late_finishing_failure_rolls_back_hardware_and_components(materials,monkeypatch):
    cid,payload=assembly_setup(materials)
    from modules.orders import square_store
    from fastapi import HTTPException
    def unavailable(*args):raise HTTPException(409,'Synthetic late destination change')
    monkeypatch.setattr(square_store,'load',unavailable)
    assert materials[0].post('/products/production/finishing',json=payload).status_code==409
    assert materials[0].get(BASE).json()['materials'][0]['current_stock']==10
    assert materials[0].get('/products/production/finishing').json()['components'][0]['available']==3
    assert materials[3].execute(text('SELECT count(*) FROM consumable_usage WHERE consumable_id=:id'),{'id':cid}).scalar_one()==1

def test_scheduling_never_spends_managed_hardware(materials,monkeypatch):
    from modules.orders.routes.orders_crud import router
    from modules.orders.production import OrderFulfillmentService
    from core.registry import registry
    monkeypatch.setitem(registry._providers,'OrderFulfillmentProvider',OrderFulfillmentService())
    materials[4].include_router(router)
    cid,_=create(materials)
    assert materials[0].post('/products/101/components',json={'model_id':301,'quantity_needed':1}).status_code==200
    assert materials[0].post('/products/101/consumables',json={'consumable_id':cid,'quantity_per_product':4}).status_code==200
    r=materials[0].post('/orders',json={'order_number':'SYNTHETIC-MANAGED','items':[{'product_id':101,'quantity':2,'unit_price':20}]});assert r.status_code==200,r.text
    response=materials[0].post(f"/orders/{r.json()['id']}/schedule");assert response.status_code==200,response.text
    assert response.json()['consumables_deducted']==[]
    assert materials[0].get(BASE).json()['materials'][0]['current_stock']==0
    assert materials[3].execute(text('SELECT count(*) FROM consumable_usage WHERE consumable_id=:id'),{'id':cid}).scalar_one()==0

def test_same_physical_package_reference_under_new_request_does_not_receive_twice(materials):
    create(materials);o=order(materials)
    r,p=receipt(materials,o['id'],units=50);assert r.status_code==200
    p['request_id']=uuid.uuid4().hex
    r=materials[0].post(BASE+'/receipts',json=p);assert r.status_code==200,r.text
    assert r.json()['materials'][0]['current_stock']==50 and len(r.json()['events'])==1
    p.update(request_id=uuid.uuid4().hex,units=51)
    assert materials[0].post(BASE+'/receipts',json=p).status_code==409

def test_missing_encryption_rolls_back_material_creation(materials,monkeypatch):
    before=materials[3].execute(text('SELECT count(*) FROM consumables')).scalar_one()
    monkeypatch.delenv('ENCRYPTION_KEY',raising=False)
    r=materials[0].post(BASE+'/materials',json={'name':'Must not persist','specification':'Synthetic'})
    assert r.status_code==503
    assert materials[3].execute(text('SELECT count(*) FROM consumables')).scalar_one()==before

def test_response_validation_failure_is_before_stock_commit(materials,monkeypatch):
    create(materials);o=order(materials)
    from modules.orders import material_purchases
    from fastapi import HTTPException
    def failed_read(*args):raise HTTPException(409,'Synthetic unrelated identity review')
    monkeypatch.setattr(material_purchases,'read',failed_read)
    r,_=receipt(materials,o['id'],units=50);assert r.status_code==409
    cid=materials[3].execute(text('SELECT id FROM consumables')).scalar_one()
    assert materials[3].execute(text('SELECT current_stock FROM consumables WHERE id=:id'),{'id':cid}).scalar_one()==0
    assert materials[3].execute(text('SELECT count(*) FROM consumable_usage')).scalar_one()==0

def test_actual_support_bundle_omits_purchase_and_custody_records(materials,monkeypatch):
    import io,zipfile
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import diagnostics,routes_admin
    create(materials);order(materials)
    raw=materials[3].execute(text("SELECT value FROM system_config WHERE key='material_purchases:1'")).scalar_one()
    monkeypatch.setattr(diagnostics,'_read_tail',lambda path,n:[])
    app=FastAPI();app.include_router(routes_admin.router)
    app.dependency_overrides[get_db]=lambda:materials[3]
    app.dependency_overrides[get_current_user]=lambda:{**materials[1],'group_id':None}
    r=TestClient(app).get('/admin/support-bundle');assert r.status_code==200
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:everything=''.join(z.read(n).decode() for n in z.namelist())
    for secret in (raw,'SYNTHETIC-ORDER','material_purchases:','material_identity:','material_mutex:'):assert secret not in everything

def test_invalid_finishing_hardware_reason_not_echoed_or_cached(materials):
    cid,payload=assembly_setup(materials)
    payload['hardware_usage'][0]['reason']='PRIVATE-MATERIAL-DETAIL'*20
    r=materials[0].post('/products/production/finishing',json=payload)
    assert r.status_code==422 and 'PRIVATE-MATERIAL-DETAIL' not in r.text and 'no-store' in r.headers['cache-control']
