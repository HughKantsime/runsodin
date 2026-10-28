"""File 8 source evidence: XML id 4, plate filament_ids [3], gcode T3."""
import hashlib
import json
import zipfile
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from core.print_payload import source_filaments, validate_mapping
from modules.printers.dispatch_ams_gate import preflight_ams_source
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_schedule_ams_mapping import mapping_client, schedule
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.cert_D_capture import publish

FILAMENTS = json.dumps([{'slot':4,'type':'PETG','color':'#FFFFFF'}])
MAPPING = [{'source_slot':4,'target_slot':4}]
VECTOR = [-1,-1,-1,3]


def source_file(path, *, xml_id=4, plate_id=3, tool=3, switch=3):
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('Metadata/slice_info.config', f'<config><plate><metadata key="index" value="1"/><filament id="{xml_id}" type="PETG"/></plate></config>')
        z.writestr('Metadata/plate_1.json',json.dumps({'filament_ids':[plate_id]}))
        z.writestr('Metadata/project_settings.config',json.dumps({'filament_type':['PLA','PLA','PLA','PETG']}))
        z.writestr('Metadata/plate_1.gcode',f'; filament_type = PLA;PLA;PLA;PETG\nM620 S{switch}A\nM621 S{switch}A\nT{tool}\n')


def preflight(path, vector=VECTOR):
    return preflight_ams_source({'plate_index':0,'ams_mapping':json.dumps(vector)}, mapping=vector,
        staged_path=str(path),payload_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_file_four_to_tray_four_is_sparse_and_passes_preflight(tmp_path):
    path=tmp_path/'file-eight.3mf';source_file(path)
    vector=validate_mapping(MAPPING,FILAMENTS,{4:'PETG'},'bambu')
    assert vector==VECTOR
    assert preflight(path,vector)['source_index']==3


@pytest.mark.parametrize('change', [{'xml_id':1},{'plate_id':0},{'tool':0},{'switch':0},{'plate_id':True}])
def test_conflicting_source_facts_refuse(tmp_path,change):
    path=tmp_path/'conflict.3mf';source_file(path,**change)
    with pytest.raises(ValueError): preflight(path)


@pytest.mark.parametrize('vector', [[3],[-1,-1,-1,-1],[3,-1,-1,3],[-1,-1,-1,True]])
def test_wrong_source_or_multiple_mapped_sources_refuse(tmp_path, vector):
    path=tmp_path/'source.3mf';source_file(path)
    with pytest.raises(ValueError): preflight(path,vector)


@pytest.mark.parametrize('filaments', [[{'type':'PETG'}],[{'slot':0,'type':'PETG'}],
    [{'slot':True,'type':'PETG'}],[{'slot':17,'type':'PETG'}],[{'slot':4,'type':'PETG'}]*2])
def test_missing_or_invalid_ids_refuse(filaments):
    with pytest.raises(HTTPException): validate_mapping(MAPPING,json.dumps(filaments),{4:'PETG'},'bambu')


@pytest.mark.parametrize('count',[1,2,16])
def test_wholly_legacy_metadata_uses_position_plus_one(count):
    legacy=[{'type':'PETG','color':'#FFFFFF'} for _ in range(count)]
    encoded=json.dumps(legacy)
    assert source_filaments(encoded)==[{**f,'slot':i+1} for i,f in enumerate(legacy)]
    slots={i+1:'PETG' for i in range(count)}
    for offset in (0,1):
        mapping=[{'source_slot':i+offset,'target_slot':i+1} for i in range(count)]
        assert validate_mapping(mapping,encoded,slots,'bambu')==list(range(count))
    with pytest.raises(HTTPException) as error:
        validate_mapping(mapping,encoded,{**slots,1:'PLA'},'bambu')
    assert error.value.status_code==400


@pytest.mark.parametrize('filaments',[
    [{'type':'PETG'},{'slot':2,'type':'PETG'}],
    [{'slot':1,'type':'PETG'},{'type':'PETG'}],
    [{'slot':None,'type':'PETG'}], [{'slot':'1','type':'PETG'}],
    [{'slot':True,'type':'PETG'}], [{'slot':1.0,'type':'PETG'}],
    [{'slot':0,'type':'PETG'}], [{'slot':17,'type':'PETG'}],
    [{'slot':1,'type':'PETG'}]*2, [], [{'type':'PETG'}]*17,
    [None], ['PETG'], [[]], [1], [True], [{'type':'PETG'},None], {}, None,
])
def test_mixed_or_invalid_metadata_still_returns_400(filaments):
    with pytest.raises(HTTPException) as error:source_filaments(json.dumps(filaments))
    assert error.value.status_code==400


def test_legacy_positions_only_when_the_entire_file_agrees():
    aligned=json.dumps([{'slot':1,'type':'PLA'},{'slot':2,'type':'PETG'}])
    assert validate_mapping([{'source_slot':0,'target_slot':2},{'source_slot':1,'target_slot':4}],aligned,{2:'PLA',4:'PETG'},'bambu')==[1,3]
    assert validate_mapping([{'source_slot':1,'target_slot':2},{'source_slot':2,'target_slot':4}],aligned,{2:'PLA',4:'PETG'},'bambu')==[1,3]
    with pytest.raises(HTTPException): validate_mapping([{'source_slot':0,'target_slot':4}],FILAMENTS,{4:'PETG'},'bambu')
    reordered=json.dumps([{'slot':4,'type':'PETG'},{'slot':1,'type':'PLA'}])
    assert validate_mapping([{'source_slot':4,'target_slot':4},{'source_slot':1,'target_slot':2}],reordered,{2:'PLA',4:'PETG'},'bambu')==[1,-1,-1,3]
    with pytest.raises(HTTPException):
        validate_mapping([{'source_slot':0,'target_slot':4},{'source_slot':1,'target_slot':2}],reordered,{2:'PLA',4:'PETG'},'bambu')


def test_reprint_persists_real_file_id_and_suggests_it(reprint_client,review_db):
    client,_,path=reprint_client;source_file(path)
    review_db.execute(text('UPDATE print_files SET filaments_json=:f WHERE id=101'),{'f':FILAMENTS})
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex) VALUES(9,4,'PETG','FFFFFF')"));review_db.commit()
    response=client.post('/archives/101/reprint',json={'printer_id':9,'ams_mapping':MAPPING})
    assert response.status_code==200,response.text
    vector=json.loads(review_db.execute(text('SELECT ams_mapping FROM jobs WHERE id=:id'),{'id':response.json()['job_id']}).scalar_one())
    assert vector==VECTOR
    assert preflight(path,vector)['source_index']==3
    preview=client.get('/archives/101/ams-preview?printer_id=9').json()
    assert preview['required'][0]['slot']==4
    assert preview['suggested_mapping']==[{'source_slot':4,'target_slot':4,'confidence':'exact'}]
    publish('frontend/src/test-fixtures/ams-file-source-wire.json',{'preview':preview,'stored':vector})


@pytest.mark.parametrize('kind',['models','print-files'])
def test_schedule_and_patch_use_file_id(mapping_client,review_db,kind):
    client,auth=mapping_client
    review_db.execute(text('UPDATE print_files SET filaments_json=:f WHERE id=501'),{'f':FILAMENTS});review_db.commit()
    response=schedule(mapping_client,kind,mapping=MAPPING)
    assert response.status_code==200,response.text
    job=response.json()['job_id']
    detail=client.get(f'/api/jobs/{job}',headers=auth()).json()
    assert detail['ams_mapping']==VECTOR
    assert detail['ams_preview']['required'][0]['slot']==4
    changed=client.patch(f'/api/jobs/{job}',headers=auth(),json={'ams_mapping':[{'source_slot':4,'target_slot':3}]})
    assert changed.status_code==200,changed.text
    assert changed.json()['ams_mapping']==[-1,-1,-1,2]
    assert client.patch(f'/api/jobs/{job}',headers=auth(),json={'ams_mapping':[{'source_slot':0,'target_slot':4}]}).status_code==400
