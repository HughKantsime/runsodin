"""Inventory claim assertions through actual isolated HTTP/ORM handlers."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from core.db import get_db
from core.dependencies import get_current_user
from modules.inventory.routes import router as inventory_router
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def inventory_client(review_db):
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES(990,'Synthetic','Test PETG','PETG',0.025)"))
    review_db.commit()
    from modules.reporting.routes import exports
    app=FastAPI();app.include_router(inventory_router,prefix='/api');app.include_router(exports.router,prefix='/api')
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'operator','group_id':1}
    app.dependency_overrides[get_db]=lambda:review_db
    with TestClient(app) as client:yield client


def test_spool_creation_generates_distinct_qr_and_preserves_inventory_fields(inventory_client):
    body={'filament_id':990,'initial_weight_g':750,'price':18.75,'lot_number':'synthetic-batch','vendor':'synthetic vendor'}
    first=inventory_client.post('/api/spools',json=body);second=inventory_client.post('/api/spools',json=body)
    assert first.status_code==second.status_code==200
    assert first.json()['qr_code']!=second.json()['qr_code']
    rows=inventory_client.get('/api/spools').json()
    row=next(x for x in rows if x['id']==first.json()['id'])
    assert row['remaining_weight_g']==750 and row['lot_number']=='synthetic-batch'
    assert row['filament_material']=='PETG' and row['filament_brand']=='Synthetic'
    assert row['status']=='active'
    assert inventory_client.patch('/api/spools/'+str(row['id']),json={'remaining_weight_g':20}).status_code==200
    assert inventory_client.delete('/api/spools/'+str(row['id'])).status_code==200
    assert inventory_client.get('/api/spools/'+str(row['id'])).json()['status']=='archived'


def test_low_stock_boundary_active_scope_and_list_flags(inventory_client,review_db):
    for identity,weight,threshold,status,org in [(991,49,50,'active',1),(992,50,50,'active',1),(993,75,100,'active',1),(994,0,50,'archived',1),(995,0,50,'active',2)]:
        review_db.execute(text('INSERT INTO spools(id,filament_id,remaining_weight_g,low_stock_threshold_g,status,org_id) VALUES(:id,990,:weight,:threshold,:status,:org)'),dict(id=identity,weight=weight,threshold=threshold,status=status,org=org))
    review_db.commit()
    low=inventory_client.get('/api/spools/low-stock')
    assert low.status_code==200 and {x['id'] for x in low.json()}=={991,993}
    rows={x['id']:x for x in inventory_client.get('/api/spools').json()}
    assert rows[991]['is_low_stock'] is True and rows[992]['is_low_stock'] is False
    assert rows[993]['low_stock_threshold_g']==100 and 995 not in rows


@pytest.mark.parametrize('field,value',[('low_stock_threshold_g',100),('pa_profile','synthetic K0.025')])
def test_advertised_spool_profile_fields_can_be_saved(inventory_client,field,value):
    identity=inventory_client.post('/api/spools',json={'filament_id':990}).json()['id']
    response=inventory_client.patch(f'/api/spools/{identity}',json={field:value})
    assert response.status_code==200
    rows=inventory_client.get('/api/spools').json()
    assert next(row for row in rows if row['id']==identity)[field]==value


def test_drying_history_retains_multiple_conditions(inventory_client):
    identity=inventory_client.post('/api/spools',json={'filament_id':990}).json()['id']
    for duration,temp,note in [(4,55,'synthetic first'),(6,60,'synthetic second')]:
        response=inventory_client.post(f'/api/spools/{identity}/dry',params={'duration_hours':duration,'temp_c':temp,'notes':note})
        assert response.status_code==200 and response.json()['dried_at']
    history=inventory_client.get(f'/api/spools/{identity}/drying-history').json()
    assert {(x['duration_hours'],x['temp_c'],x['notes']) for x in history}=={(4,55,'synthetic first'),(6,60,'synthetic second')}


def test_advertised_spool_export_path_downloads_csv(inventory_client):
    response=inventory_client.get('/api/spools/export')
    assert response.status_code==200
    assert response.headers['content-type'].startswith('text/csv')


def test_spool_csv_contains_advertised_material_brand_and_pa(inventory_client):
    import csv,io
    response=inventory_client.get('/api/spools/export')
    assert response.status_code==200
    fields=set(csv.DictReader(io.StringIO(response.text)).fieldnames)
    assert {'Material','Brand','PA Profile'} <= fields


@pytest.mark.parametrize('role,can_write',[('viewer',False),('operator',True),('admin',True)])
def test_role_floor_on_real_inventory_read_and_create(inventory_client,role,can_write):
    inventory_client.app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':role,'group_id':1}
    assert inventory_client.get('/api/spools').status_code==200
    response=inventory_client.post('/api/spools',json={'filament_id':990})
    assert response.status_code==(200 if can_write else 403)
