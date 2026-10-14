"""Owned actual existing-field persistence and label sheet regression checks."""
import csv,io
import pytest
from PIL import Image
from tests.test_contracts.test_inventory_claim_acceptance import inventory_client
from tests.test_contracts.test_education_review_workflow import review_db

def test_profile_create_patch_clear_and_csv_use_actual_stored_values(inventory_client):
    created=inventory_client.post('/api/spools',json={'filament_id':990,'pa_profile':'K0.042','low_stock_threshold_g':0,'initial_weight_g':0})
    assert created.status_code==200
    sid=created.json()['id']
    row=inventory_client.get(f'/api/spools/{sid}').json()
    assert row['pa_profile']=='K0.042' and row['low_stock_threshold_g']==0 and row['is_low_stock'] is False
    assert sid not in {x['id'] for x in inventory_client.get('/api/spools/low-stock').json()}
    exported=next(x for x in csv.DictReader(io.StringIO(inventory_client.get('/api/spools/export').text)) if x['ID']==str(sid))
    assert exported['PA Profile']=='K0.042'
    assert inventory_client.patch(f'/api/spools/{sid}',json={'notes':'Owned note'}).status_code==200
    assert inventory_client.get(f'/api/spools/{sid}').json()['pa_profile']=='K0.042'
    assert inventory_client.patch(f'/api/spools/{sid}',json={'pa_profile':None,'low_stock_threshold_g':100}).status_code==200
    row=inventory_client.get(f'/api/spools/{sid}').json()
    assert row['pa_profile'] is None and row['low_stock_threshold_g']==100 and row['is_low_stock'] is True

@pytest.mark.parametrize('body',[{'pa_profile':'x'*51},{'low_stock_threshold_g':-1},{'low_stock_threshold_g':.5},{'low_stock_threshold_g':True},{'low_stock_threshold_g':2147483648},{'low_stock_threshold_g':9223372036854775808}])
def test_invalid_profile_fields_refuse_without_changed_stored_value(inventory_client,body):
    sid=inventory_client.post('/api/spools',json={'filament_id':990,'pa_profile':'Owned unchanged','low_stock_threshold_g':50}).json()['id']
    assert inventory_client.patch(f'/api/spools/{sid}',json=body).status_code==422
    row=inventory_client.get(f'/api/spools/{sid}').json()
    assert row['pa_profile']=='Owned unchanged' and row['low_stock_threshold_g']==50

def test_batch_label_sheet_renders_every_selected_owned_spool(inventory_client,monkeypatch):
    from modules.inventory.routes import spool_ops
    ids=[inventory_client.post('/api/spools',json={'filament_id':990}).json()['id'] for _ in range(2)]
    original=spool_ops.generate_single_label;seen=[]
    def renderer(spool,*args):
        seen.append(spool.id)
        return original(spool,*args)
    monkeypatch.setattr(spool_ops,'generate_single_label',renderer)
    response=inventory_client.get('/api/spools/labels/batch',params={'spool_ids':','.join(map(str,ids)),'size':'small'})
    assert response.status_code==200 and response.headers['content-type']=='image/png'
    assert set(seen)==set(ids) and len(seen)==2
    image=Image.open(io.BytesIO(response.content));image.load()
    assert image.size==(2550,3300) and image.getbbox() is not None

@pytest.mark.parametrize('threshold',[2147483648,9223372036854775808])
def test_create_rejects_database_incompatible_threshold(inventory_client,threshold):
    before=inventory_client.get('/api/spools').json()
    response=inventory_client.post('/api/spools',json={'filament_id':990,'low_stock_threshold_g':threshold})
    assert response.status_code==422
    assert inventory_client.get('/api/spools').json()==before

def test_storage_boundary_threshold_survives_create(inventory_client):
    response=inventory_client.post('/api/spools',json={'filament_id':990,'low_stock_threshold_g':2147483647})
    assert response.status_code==200
    assert inventory_client.get('/api/spools/'+str(response.json()['id'])).json()['low_stock_threshold_g']==2147483647
