"""Organization list/detail parity for real routes and disposable persisted objects."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def scope_client(review_db,monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.base import FilamentType
    from modules.jobs.models import Job
    from modules.models_library.models import Model
    from modules.inventory.models import Spool,FilamentLibrary
    from modules.orders.models import Order,Product
    from modules.printers.models import Printer
    from modules.jobs.routes import jobs_crud
    from modules.models_library.routes import models_crud
    from modules.inventory.routes import spools,spool_ops
    from modules.orders.routes import orders_crud,products
    from modules.printers import routes_crud
    from modules.archives.routes import archives_crud,tags
    from modules.reporting.routes import analytics
    from modules.jobs.routes import presets
    from modules.printers import camera_routes
    from modules.vision.routes import detections
    from modules.system import routes_admin
    monkeypatch.setattr(analytics.settings,'spoolman_url','')
    # Authorization assertions only: config synchronization has its own isolated IO gate.
    monkeypatch.setattr(camera_routes,'sync_go2rtc_config',lambda db:None)
    for identifier,org,name in [(101,1,'Own'),(102,2,'Foreign private'),(103,None,'Shared')]:
        review_db.add(Model(id=identifier,name=name,org_id=org,default_filament_type=FilamentType.PLA))
        review_db.add(Job(id=identifier,item_name=name,charged_to_org_id=org))
        review_db.add(Order(id=identifier,order_number=name,org_id=org))
        review_db.add(Product(id=identifier,name=name,org_id=org,price=5))
        review_db.add(Printer(id=identifier,name=name,org_id=org,api_type='bambu'))
    review_db.add(FilamentLibrary(id=101,brand='Synthetic',name='PLA',material='PLA'));review_db.flush()
    for identifier,org in [(101,1),(102,2),(103,None)]:
        review_db.add(Spool(id=identifier,filament_id=101,org_id=org))
    review_db.commit()
    for identifier in (101,102,103):
        review_db.execute(text("INSERT INTO print_archives(id,printer_id,print_name,status) VALUES(:id,:id,'Synthetic archive','completed')"),{'id':identifier})
    review_db.execute(text("UPDATE jobs SET quantity=COALESCE(quantity,1),hold=COALESCE(hold,0),is_locked=COALESCE(is_locked,0)"))
    review_db.execute(text("UPDATE models SET default_filament_type=COALESCE(default_filament_type,'PLA')"))
    review_db.execute(text("UPDATE printers SET slot_count=COALESCE(slot_count,4),tags=COALESCE(tags,'[]'),timelapse_enabled=COALESCE(timelapse_enabled,0),shared=COALESCE(shared,0)"))
    review_db.execute(text("UPDATE printers SET camera_enabled=1,camera_url='http://synthetic.invalid/camera' WHERE id>=101"))
    for identifier in (101,102,103):
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(:id,:id,'Synthetic telemetry print','completed','2026-10-10 12:00:00')"),{'id':identifier})
        review_db.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status) VALUES(:id,:id,'spaghetti',0.9,'pending')"),{'id':identifier})
        review_db.execute(text("UPDATE print_archives SET tags=:tag WHERE id=:id"),{'id':identifier,'tag':f'tag-{identifier}'})
    review_db.commit()
    app=FastAPI()
    for module in (jobs_crud,models_crud,spool_ops,spools,orders_crud,products,routes_crud,archives_crud,tags,analytics,presets,camera_routes,detections,routes_admin):app.include_router(module.router)
    def request_db():
        with Session(review_db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=request_db
    identity={'id':3,'role':'operator','group_id':1,'is_active':True}
    app.dependency_overrides[get_current_user]=lambda:identity
    return TestClient(app),identity

@pytest.mark.parametrize('path',['/jobs','/models','/spools','/orders','/products','/printers','/archives'])
@pytest.mark.parametrize('role,org,expected',[('operator',1,{101,103}),('operator',None,{103}),('admin',1,{101,103}),('admin',None,{101,102,103})])
def test_list_visibility_matches_scope(scope_client,path,role,org,expected):
    client,identity=scope_client;identity.update(role=role,group_id=org)
    response=client.get(path)
    assert response.status_code==200,response.text
    body=response.json();rows=body['items'] if isinstance(body,dict) else body
    assert {r['id'] for r in rows if r['id'] in {101,102,103}}==expected
    if path=='/archives':assert body['total']==len(expected)

@pytest.mark.parametrize('path',['/jobs','/models','/spools','/printers'])
def test_grouped_admin_cannot_select_foreign_organization(scope_client,path):
    client,identity=scope_client;identity.update(role='admin',group_id=1)
    response=client.get(path+'?org_id=2')
    assert response.status_code==200,response.text
    assert {r['id'] for r in response.json() if r['id'] in {101,102,103}}=={101,103}

@pytest.mark.parametrize('path',['/jobs','/models','/spools','/orders','/products','/printers','/archives'])
def test_unassigned_foreign_detail_denied(scope_client,path):
    client,identity=scope_client;identity.update(group_id=None)
    response=client.get(path+'/102')
    assert response.status_code==404,response.text


@pytest.mark.parametrize('path',['/cameras','/vision/detections','/print-jobs','/print-jobs/unlinked'])
@pytest.mark.parametrize('role,org,expected',[('operator',None,{103}),('operator',1,{101,103}),('admin',None,{101,102,103})])
def test_media_and_telemetry_lists_are_scoped(scope_client,path,role,org,expected):
    client,identity=scope_client;identity.update(role=role,group_id=org)
    response=client.get(path)
    assert response.status_code==200,response.text
    body=response.json()
    rows=body.get('items',body.get('detections',body.get('jobs',[]))) if isinstance(body,dict) else body
    assert {r['id'] for r in rows if r['id'] in {101,102,103}}==expected
    if path=='/vision/detections':assert body['total']==len(expected)


def test_unassigned_dashboard_and_search_hide_private_resources(scope_client):
    client,identity=scope_client;identity.update(group_id=None)
    response=client.get('/stats');assert response.status_code==200,response.text
    assert response.json()['printers']['total']==1
    assert [p['id'] for p in response.json()['printer_stats']]==[103]
    response=client.get('/search?q=Foreign')
    assert response.status_code==200,response.text
    assert 'Foreign private' not in response.text


def test_shared_printer_and_archive_remain_visible(scope_client,review_db):
    client,identity=scope_client;identity.update(group_id=None)
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=102'));review_db.commit()
    response=client.get('/printers');assert response.status_code==200,response.text
    assert {r['id'] for r in response.json() if r['id']>=101}=={102,103}
    response=client.get('/archives?per_page=1');assert response.status_code==200,response.text
    assert response.json()['total']==2
    assert len(response.json()['items'])==1
    assert client.get('/archives/102').status_code==200


def test_foreign_archive_write_denied_and_tags_scoped(scope_client,review_db):
    client,identity=scope_client;identity.update(group_id=None)
    response=client.patch('/archives/102',json={'notes':'Denied overwrite'})
    assert response.status_code==404,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT notes FROM print_archives WHERE id=102')).scalar_one() is None
    response=client.get('/tags');assert response.status_code==200,response.text
    assert 'tag-103' in response.text and 'tag-102' not in response.text and 'tag-101' not in response.text


def test_orphan_archive_visibility_is_not_broadened(scope_client,review_db):
    client,identity=scope_client;identity.update(group_id=None)
    review_db.execute(text("INSERT INTO print_archives(id,printer_id,print_name,status) VALUES(104,NULL,'Orphan','completed')"));review_db.commit()
    response=client.get('/archives');assert response.status_code==200,response.text
    assert {r['id'] for r in response.json()['items']}=={103,104}
    assert response.json()['total']==2


def test_alternate_export_and_printer_tags_hide_foreign(scope_client,review_db):
    import csv,io
    client,identity=scope_client;identity.update(group_id=None)
    review_db.execute(text("UPDATE printers SET tags='[\"foreign-only-tag\"]' WHERE id=102"));review_db.commit()
    response=client.get('/spools/export');assert response.status_code==200,response.text
    assert {int(r['ID']) for r in csv.DictReader(io.StringIO(response.text))}=={103}
    response=client.get('/printers/tags');assert response.status_code==200,response.text
    assert 'foreign-only-tag' not in response.json()


def test_mixed_bulk_spool_update_denied_atomically(scope_client,review_db):
    client,identity=scope_client
    response=client.post('/spools/bulk-update',json={'spool_ids':[101,102],'action':'archive'})
    assert response.status_code==404,response.text
    with Session(review_db.get_bind()) as fresh:
        assert list(fresh.execute(text('SELECT status FROM spools WHERE id IN (101,102) ORDER BY id')).scalars())==['active','active']

@pytest.mark.parametrize('path',['/spools/102/qr','/spools/102/label','/spools/labels/batch?spool_ids=101,102'])
def test_foreign_spool_labels_denied(scope_client,path):
    client,_=scope_client
    assert client.get(path).status_code==404


@pytest.mark.parametrize('ids',[[True],["101"],[0],[-1],[101,99999]])
def test_bulk_invalid_or_missing_ids_never_change_stock(scope_client,review_db,ids):
    client,_=scope_client
    response=client.post('/spools/bulk-update',json={'spool_ids':ids,'action':'archive'})
    assert response.status_code==(404 if ids==[101,99999] else 400),response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT status FROM spools WHERE id=101')).scalar_one()=='active'


def test_bulk_deduplicates_and_enforces_write_scope(scope_client,review_db):
    client,identity=scope_client
    identity['_token_scopes']=['read:spools']
    assert client.post('/spools/bulk-update',json={'spool_ids':[101],'action':'archive'}).status_code==403
    identity.pop('_token_scopes')
    response=client.post('/spools/bulk-update',json={'spool_ids':[101,101],'action':'archive'})
    assert response.status_code==200,response.text
    assert response.json()['affected']==1


@pytest.mark.parametrize('prior_id,expected',[(102,404),(101,200)])
def test_scan_move_authorizes_previous_printer_atomically(scope_client,review_db,prior_id,expected):
    client,_=scope_client
    review_db.execute(text("UPDATE spools SET qr_code='SYNTHETIC-OWN',location_printer_id=:pid,location_slot=1 WHERE id=101"),{'pid':prior_id})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id,filament_type) VALUES(:pid,1,101,'PLA')"),{'pid':prior_id})
    review_db.commit()
    response=client.post('/spools/scan-assign',json={'qr_code':'SYNTHETIC-OWN','printer_id':103,'slot':1})
    assert response.status_code==expected,response.text
    with Session(review_db.get_bind()) as fresh:
        old=fresh.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=:pid AND slot_number=1'),{'pid':prior_id}).scalar_one()
        location=fresh.execute(text('SELECT location_printer_id,location_slot FROM spools WHERE id=101')).one()
        if expected==404:
            assert old==101 and tuple(location)==(prior_id,1)
            assert fresh.execute(text('SELECT count(*) FROM filament_slots WHERE printer_id=103')).scalar_one()==0
        else:
            assert old is None and tuple(location)==(103,1)
            assert fresh.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=103 AND slot_number=1')).scalar_one()==101


def test_scan_rejects_displaced_foreign_spool_without_changes(scope_client,review_db):
    client,_=scope_client
    review_db.execute(text("UPDATE spools SET qr_code='SYNTHETIC-OWN' WHERE id=101"))
    review_db.execute(text("UPDATE spools SET location_printer_id=103,location_slot=1 WHERE id=102"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id,filament_type) VALUES(103,1,102,'PLA')"))
    review_db.commit()
    response=client.post('/spools/scan-assign',json={'qr_code':'SYNTHETIC-OWN','printer_id':103,'slot':1})
    assert response.status_code==404,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=103')).scalar_one()==102
        assert fresh.execute(text('SELECT location_printer_id FROM spools WHERE id=101')).scalar_one() is None


def test_shared_target_scan_is_allowed_and_private_target_denied(scope_client,review_db):
    client,_=scope_client
    review_db.execute(text("UPDATE spools SET qr_code='SYNTHETIC-OWN' WHERE id=101"));review_db.commit()
    response=client.post('/spools/scan-assign',json={'qr_code':'SYNTHETIC-OWN','printer_id':102,'slot':1})
    assert response.status_code==200 and response.json()['success'] is False
    review_db.execute(text('UPDATE printers SET shared=1 WHERE id=102'));review_db.commit()
    response=client.post('/spools/scan-assign',json={'qr_code':'SYNTHETIC-OWN','printer_id':102,'slot':1})
    assert response.status_code==200 and response.json()['success'] is True,response.text


def test_scan_move_between_slots_clears_previous_same_printer(scope_client,review_db):
    client,_=scope_client
    review_db.execute(text("UPDATE spools SET qr_code='SYNTHETIC-OWN',location_printer_id=101,location_slot=1 WHERE id=101"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id,filament_type) VALUES(101,1,101,'PLA')"));review_db.commit()
    response=client.post('/spools/scan-assign',json={'qr_code':'SYNTHETIC-OWN','printer_id':101,'slot':2})
    assert response.status_code==200 and response.json()['success'] is True,response.text
    with Session(review_db.get_bind()) as fresh:
        assert list(fresh.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=101 ORDER BY slot_number')).scalars())==[None,101]


def test_scan_displaces_owned_spool_and_clears_its_location(scope_client,review_db):
    client,_=scope_client
    review_db.execute(text("UPDATE spools SET qr_code='SYNTHETIC-OWN' WHERE id=101"))
    review_db.execute(text("UPDATE spools SET location_printer_id=103,location_slot=1 WHERE id=103"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id,filament_type) VALUES(103,1,103,'PLA')"));review_db.commit()
    response=client.post('/spools/scan-assign',json={'qr_code':'SYNTHETIC-OWN','printer_id':103,'slot':1})
    assert response.status_code==200 and response.json()['success'] is True,response.text
    with Session(review_db.get_bind()) as fresh:
        assert tuple(fresh.execute(text('SELECT location_printer_id,location_slot FROM spools WHERE id=103')).one())==(None,None)
        assert fresh.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=103')).scalar_one()==101
