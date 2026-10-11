"""Archive reprint persistence, original linkage and target authorization."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def reprint_client(review_db,tmp_path,monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    original_provider = registry.get_provider
    monkeypatch.setattr(registry,"get_provider",lambda name: EducationPolicyService() if name=="EducationPolicyProvider" else original_provider(name))
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.archives.routes import archives_crud
    source=tmp_path/'synthetic.gcode.3mf'
    import zipfile
    with zipfile.ZipFile(source,'w') as package:
        package.writestr('Metadata/plate_1.gcode','; Synthetic fixture, never send to hardware')
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(101,'Original model',1)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,model_id,printer_id,status,charged_to_org_id,quantity,hold) VALUES(101,'Original print',101,9,'completed',1,1,0)"))
    review_db.execute(text("INSERT INTO print_files(id,model_id,job_id,stored_path,original_filename,plate_count) VALUES(101,101,101,:path,'synthetic.gcode.3mf',1)"),{'path':str(source)})
    review_db.execute(text("INSERT INTO print_archives(id,job_id,printer_id,print_file_id,print_name,status,file_path,plate_count) VALUES(101,101,9,101,'Original print','completed',:path,1)"),{'path':str(source)})
    review_db.execute(text("UPDATE print_archives SET org_id=1,ownership_state='known',ownership_source='synthetic_job_file' WHERE id=101"))
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,is_active) VALUES(11,'Foreign private','bambu',2,1)"))
    review_db.commit()
    app=FastAPI();app.include_router(archives_crud.router)
    from modules.jobs.routes import jobs_crud
    app.include_router(jobs_crud.router)
    def request_db():
        with Session(review_db.get_bind()) as session: yield session
    app.dependency_overrides[get_db]=request_db
    identity={'id':3,'role':'operator','group_id':1,'is_active':True}
    app.dependency_overrides[get_current_user]=lambda:identity
    return TestClient(app,raise_server_exceptions=False),identity,source


def test_reprint_creates_persisted_dispatchable_original_model(reprint_client,review_db):
    client,identity,source=reprint_client
    response=client.post('/archives/101/reprint',json={'printer_id':9})
    assert response.status_code==200,response.text
    identifier=response.json()['job_id']
    with Session(review_db.get_bind()) as fresh:
        row=fresh.execute(text('SELECT model_id,printer_id,charged_to_org_id,submitted_by,status FROM jobs WHERE id=:id'),{'id':identifier}).one_or_none()
        assert row is not None
        assert tuple(row)==(101,9,1,3,'pending')
        linked=fresh.execute(text('SELECT pf.stored_path FROM jobs j JOIN print_files pf ON pf.model_id=j.model_id WHERE j.id=:id'),{'id':identifier}).scalar_one()
        assert linked==str(source)


def test_reprint_rejects_foreign_target_without_job(reprint_client,review_db):
    client,_,_=reprint_client
    before=review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    response=client.post('/archives/101/reprint',json={'printer_id':11})
    assert response.status_code==404,response.text
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT count(*) FROM jobs')).scalar_one()==before


def test_reprint_missing_file_and_viewer_denial(reprint_client,review_db):
    client,identity,source=reprint_client
    identity['role']='viewer'
    assert client.post('/archives/101/reprint',json={'printer_id':9}).status_code==403
    identity['role']='operator';source.unlink()
    response=client.post('/archives/101/reprint',json={'printer_id':9})
    assert response.status_code==400,response.text
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==2


def test_reprint_exact_file_and_second_plate(reprint_client,review_db,monkeypatch):
    import zipfile
    import core.db
    from modules.printers.dispatch import _load_job
    client,_,source=reprint_client
    with zipfile.ZipFile(source,'a') as package:
        package.writestr('Metadata/plate_2.gcode','; Second synthetic plate')
    review_db.execute(text("INSERT INTO print_files(id,model_id,stored_path,original_filename) VALUES(102,101,'/not-the-archive','other.gcode.3mf')"))
    review_db.commit()
    response=client.post('/archives/101/reprint',json={'printer_id':9,'plate_index':1})
    assert response.status_code==200,response.text
    monkeypatch.setattr(core.db,'engine',review_db.get_bind())
    job=_load_job(response.json()['job_id'])
    assert job['print_file_id']==101
    assert job['plate_index']==1
    assert job['stored_path']==str(source)
    review_db.execute(text('DELETE FROM print_files WHERE id=101'));review_db.commit()
    assert _load_job(response.json()['job_id']) is None


@pytest.mark.parametrize('plate',[-1,1,99])
def test_reprint_bad_plate_writes_nothing(reprint_client,review_db,plate):
    client,_,_=reprint_client
    before=review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    response=client.post('/archives/101/reprint',json={'printer_id':9,'plate_index':plate})
    assert response.status_code in (400,422),response.text
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==before


def test_reprint_shared_target_and_mapping(reprint_client,review_db):
    client,_,_=reprint_client
    review_db.execute(text("UPDATE printers SET shared=1 WHERE id=11"))
    review_db.execute(text("UPDATE print_files SET filaments_json='[{\"type\":\"PLA\"}]' WHERE id=101"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number) VALUES(11,4)"));review_db.commit()
    body={'printer_id':11,'ams_mapping':[{'source_slot':0,'target_slot':4}]}
    response=client.post('/archives/101/reprint',json=body)
    assert response.status_code==200,response.text
    import json
    row=review_db.execute(text('SELECT print_file_id,ams_mapping FROM jobs WHERE id=:id'),{'id':response.json()['job_id']}).one()
    assert row[0]==101 and json.loads(row[1])==[3]
    body['ams_mapping'][0]['target_slot']=16
    assert client.post('/archives/101/reprint',json=body).status_code==400


@pytest.mark.parametrize('api_type',['bambu','moonraker'])
def test_reprint_mapping_validation(api_type):
    from core.print_payload import validate_mapping
    from fastapi import HTTPException
    bad=[[],[{'source_slot':0,'target_slot':1}],
         [{'source_slot':0,'target_slot':1},{'source_slot':0,'target_slot':2}],
         [{'source_slot':0,'target_slot':1},{'source_slot':1,'target_slot':1}],
         [{'source_slot':False,'target_slot':1},{'source_slot':1,'target_slot':2}]]
    for mapping in bad:
        with pytest.raises(HTTPException):
            validate_mapping(mapping,'[{},{}]',{1,2},api_type)


def test_archive_invalid_zip_and_ambiguous_plate(reprint_client):
    import zipfile
    client,_,source=reprint_client
    source.write_bytes(b'not a printable zip')
    assert client.post('/archives/101/reprint',json={'printer_id':9}).status_code==400
    with zipfile.ZipFile(source,'w') as package:
        package.writestr('Metadata/plate_1.gcode','; One')
        package.writestr('Metadata/plate_1.gcode','; Ambiguous')
    assert client.post('/archives/101/reprint',json={'printer_id':9}).status_code==400


from tests.test_contracts.test_education_connected_print_workflow import tls_mqtt
from tests.test_contracts.test_bambu_ftps_transfer import ProtectedFTPServer, resumption_context


@pytest.mark.parametrize('path',['legacy','v2'])
def test_reprint_real_transport_selected_plate_mapping(path,reprint_client,review_db,
        monkeypatch,tls_mqtt,resumption_context):
    import time
    import zipfile
    from cryptography.fernet import Fernet
    from core import crypto, db as core_db, ws_hub
    from modules.printers import dispatch
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import broker_policy, ftp_upload
    from tests.test_contracts.test_education_connected_print_workflow import SYNTHETIC_ACCESS_CODE
    client,_,source=reprint_client
    port,commands=tls_mqtt
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    review_db.execute(text("UPDATE printers SET api_host='127.0.0.1',api_key=:key WHERE id=9"),
        {'key':crypto.encrypt('synthetic-printer|'+SYNTHETIC_ACCESS_CODE)})
    review_db.execute(text("UPDATE print_files SET filaments_json='[{\"type\":\"PLA\"}]' WHERE id=101"))
    review_db.execute(text('INSERT INTO filament_slots(printer_id,slot_number) VALUES(9,4)'))
    review_db.commit()
    with zipfile.ZipFile(source,'a') as package:
        package.writestr('Metadata/plate_2.gcode','; Second synthetic plate, no hardware')
    response=client.post('/archives/101/reprint',json={'printer_id':9,'plate_index':1,
        'ams_mapping':[{'source_slot':0,'target_slot':4}]})
    assert response.status_code==200,response.text
    job_id=response.json()['job_id']
    monkeypatch.setattr(core_db,'engine',review_db.get_bind())
    monkeypatch.setattr(ws_hub,'engine',review_db.get_bind())
    monkeypatch.setattr(feature_flag,'is_v2_enabled',lambda:path=='v2')
    monkeypatch.setattr(BambuPrinter,'MQTT_PORT',port)
    monkeypatch.setattr(broker_policy,'resolve_bambu_broker_config',
        lambda *a,**k:broker_policy.BrokerEndpoint('127.0.0.1',port,True))
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    original_provider=registry.get_provider
    monkeypatch.setattr(registry,'get_provider',lambda name: EducationPolicyService() if name=='EducationPolicyProvider' else original_provider(name))
    peer=ProtectedFTPServer(resumption_context,require_session_reuse=True,
        expected_credentials=('bblp',SYNTHETIC_ACCESS_CODE))
    monkeypatch.setattr(ftp_upload,'FTPS_PORT',peer.port)
    try: ok,reason=dispatch.dispatch_job(9,job_id)
    finally: peer.finish()
    assert ok,reason
    assert not peer.errors and peer.data_tls and peer.session_reused
    assert peer.received==source.read_bytes()
    deadline=time.monotonic()+3
    while not any(c.get('print',{}).get('command')=='project_file' for c in commands):
        assert time.monotonic()<deadline
        time.sleep(.02)
    starts=[c['print'] for c in commands if c.get('print',{}).get('command')=='project_file']
    assert len(starts)==1
    assert starts[0]['param']=='Metadata/plate_2.gcode'
    assert starts[0]['ams_mapping']==[3]
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':job_id}).scalar_one()=='printing'


@pytest.mark.parametrize('failure',['tamper','missing_hash','invalid_hash','missing_slot','wrong_count'])
def test_reprint_dispatch_revalidates_before_transport(failure,reprint_client,review_db,monkeypatch):
    import zipfile
    from core import db as core_db
    from modules.printers import dispatch
    client,_,source=reprint_client
    review_db.execute(text("UPDATE print_files SET filaments_json='[{\"type\":\"PLA\"}]' WHERE id=101"))
    review_db.execute(text('INSERT INTO filament_slots(printer_id,slot_number) VALUES(9,4)'));review_db.commit()
    response=client.post('/archives/101/reprint',json={'printer_id':9,
        'ams_mapping':[{'source_slot':0,'target_slot':4}]})
    assert response.status_code==200,response.text
    job_id=response.json()['job_id']
    if failure=='tamper':
        with zipfile.ZipFile(source,'w') as package:
            package.writestr('Metadata/plate_1.gcode','; Changed synthetic payload')
    elif failure in ('missing_hash','invalid_hash'):
        review_db.execute(text('UPDATE jobs SET payload_sha256=:hash WHERE id=:id'),
            {'id':job_id,'hash':None if failure=='missing_hash' else 'invalid'})
    elif failure=='missing_slot': review_db.execute(text('DELETE FROM filament_slots WHERE printer_id=9'))
    else: review_db.execute(text("UPDATE print_files SET filaments_json='[{},{}]' WHERE id=101"))
    review_db.commit()
    monkeypatch.setattr(core_db,'engine',review_db.get_bind())
    monkeypatch.setattr(dispatch,'_get_printer_info',lambda _: {'api_type':'bambu','ip':'127.0.0.1'})
    monkeypatch.setattr(dispatch,'_dispatch_bambu',lambda *a,**k:pytest.fail('Denied payload touched transport'))
    ok,reason=dispatch.dispatch_job(9,job_id)
    assert not ok and reason
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':job_id}).scalar_one()=='pending'


def test_ams_preview_foreign_target_denied(reprint_client):
    client,_,_=reprint_client
    assert client.get('/archives/101/ams-preview?printer_id=11').status_code==404


def test_repeat_preserves_archived_payload(reprint_client,review_db,monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    client,_,source=reprint_client
    monkeypatch.setattr(registry,'get_provider',lambda name:EducationPolicyService() if name=='EducationPolicyProvider' else None)
    response=client.post('/archives/101/reprint',json={'printer_id':9})
    assert response.status_code==200,response.text
    original=response.json()['job_id']
    response=client.post(f'/jobs/{original}/repeat')
    assert response.status_code==200,response.text
    query=text('SELECT model_id,print_file_id,payload_sha256,plate_index,ams_mapping,charged_to_org_id,submitted_by FROM jobs WHERE id=:id')
    first=review_db.execute(query,{'id':original}).one()
    repeated=review_db.execute(query,{'id':response.json()['new_job_id']}).one()
    assert tuple(first)==tuple(repeated)
    review_db.execute(text('DELETE FROM print_files WHERE id=101'));review_db.commit()
    assert client.post(f'/jobs/{original}/repeat').status_code==400


@pytest.mark.parametrize('problem',['model_foreign','file_foreign','hash_changed','model_deleted','file_deleted'])
def test_reprint_source_denials_without_job(problem,reprint_client,review_db):
    client,_,_=reprint_client
    if problem=='model_foreign': statement="UPDATE models SET org_id=2 WHERE id=101"
    elif problem=='file_foreign': statement="UPDATE print_files SET org_id=2 WHERE id=101"
    elif problem=='hash_changed': statement="UPDATE print_files SET file_hash='invalid' WHERE id=101"
    elif problem=='model_deleted': statement='DELETE FROM models WHERE id=101'
    else: statement='DELETE FROM print_files WHERE id=101'
    review_db.execute(text(statement));review_db.commit()
    before=review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    assert client.post('/archives/101/reprint',json={'printer_id':9}).status_code==400
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==before


def test_staged_file_becomes_education_owned(reprint_client,review_db,monkeypatch):
    from core import db as core_db
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers import dispatch
    client,_,_=reprint_client
    response=client.post('/archives/101/reprint',json={'printer_id':9})
    assert response.status_code==200,response.text
    identifier=response.json()['job_id']
    review_db.execute(text('UPDATE print_files SET org_id=1 WHERE id=101'))
    review_db.execute(text('UPDATE education_submissions SET print_file_id=101 WHERE id=14'));review_db.commit()
    monkeypatch.setattr(registry,'get_provider',lambda name:EducationPolicyService() if name=='EducationPolicyProvider' else None)
    monkeypatch.setattr(core_db,'engine',review_db.get_bind())
    monkeypatch.setattr(dispatch,'_get_printer_info',lambda _: {'api_type':'bambu','ip':'127.0.0.1'})
    monkeypatch.setattr(dispatch,'_dispatch_bambu',lambda *a,**k:pytest.fail('Education alias reached transport'))
    before=review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()
    assert client.post(f'/jobs/{identifier}/repeat').status_code==409
    ok,reason=dispatch.dispatch_job(9,identifier)
    assert not ok and 'Education' in reason
    assert review_db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==before


@pytest.mark.parametrize('change',['model','file','model_swap'])
def test_repeat_rechecks_tenant_and_rejects_model_swap(change,reprint_client,review_db,monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    client,_,_=reprint_client
    monkeypatch.setattr(registry,'get_provider',lambda name:EducationPolicyService() if name=='EducationPolicyProvider' else None)
    response=client.post('/archives/101/reprint',json={'printer_id':9})
    assert response.status_code==200,response.text
    identifier=response.json()['job_id']
    if change=='model_swap':
        response=client.patch(f'/jobs/{identifier}',json={'model_id':None})
        assert response.status_code==409,response.text
        assert review_db.execute(text('SELECT model_id FROM jobs WHERE id=:id'),{'id':identifier}).scalar_one()==101
    else:
        table='models' if change=='model' else 'print_files'
        review_db.execute(text(f'UPDATE {table} SET org_id=2 WHERE id=101'));review_db.commit()
        assert client.post(f'/jobs/{identifier}/repeat').status_code==404
