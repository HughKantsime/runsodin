"""E4 behavior on the real bootstrapped SQLite schema, with isolated HTTP routing."""
import base64
import hashlib
import importlib
import io
import json
import zipfile
from datetime import datetime
from unittest.mock import Mock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from core.db import get_db
from core.dependencies import get_current_user
from core.registry import registry
from core import traceability as service
from core.traceability_privacy import export_personal, erase_personal
from modules.jobs.routes.traceability import router

M = importlib.import_module('core.schema.migrations.052_traceability')


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///'+str(tmp_path/'e4.db'), connect_args={'check_same_thread':False})
    bootstrap_database(engine)
    user = {'id':1,'role':'admin','group_id':None,'is_active':True}
    license = Mock()
    license.has_feature.return_value = True
    license.tier = 'enterprise'
    monkeypatch.setattr('license_manager.get_license', lambda:license)
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    import license_manager
    monkeypatch.setattr(license_manager, 'LICENSE_DIR', str(tmp_path))
    (tmp_path/license_manager.DEVICE_PRIVATE_KEY_FILENAME).write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    policy = Mock(spec=['assert_generic_job_mutation_allowed', 'assert_file_preview_allowed'])
    from modules.orders.production import OrderFulfillmentService
    fulfillment = OrderFulfillmentService()
    monkeypatch.setattr(registry, 'get_provider', lambda name:policy if name=='EducationPolicyProvider' else fulfillment if name=='OrderFulfillmentProvider' else None)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'operator','x','admin',TRUE),(2,'other','x','operator',TRUE)"))
        c.execute(text("INSERT INTO groups(id,name) VALUES(1,'Acme Manufacturing'),(2,'Other')"))
        c.execute(text("INSERT INTO printers(id,name,tags,org_id,nozzle_type) VALUES(1,'P1','[\"QA\"]',1,'hardened')"))
        c.execute(text("INSERT INTO print_files(id,original_filename,file_hash,layer_height,nozzle_diameter) VALUES(1,'widget.gcode',:hash,0.2,0.4)"), {'hash':'a'*64})
        c.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,print_file_id,charged_to_org_id,actual_end) VALUES(1,'Widget','completed',1,1,1,'2026-11-01 12:00:00'),(2,'Other','completed',1,1,2,'2026-11-01 12:00:00')"))
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.include_router(router, prefix='/api/v1')
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda:user
    with TestClient(app) as client:
        yield client,engine,user,license,policy
    engine.dispose()


def test_record_is_frozen_unknowns_explicit_and_reads_audited(world):
    client,engine,*_ = world
    response = client.get('/api/jobs/1/traceability')
    assert response.status_code == 200, response.text
    record = response.json()
    assert record['file']['sha256'] == 'a'*64
    assert record['model_revision']['id'] is None
    assert record['printer_serial'] is None and record['filament']==[]
    assert record['nozzle']['diameter'] == .4
    with engine.begin() as c:
        c.execute(text("UPDATE print_files SET original_filename='changed' WHERE id=1"))
    assert client.get('/api/v1/jobs/1/traceability').json()['file']['name']=='widget.gcode'
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='traceability.read'")).scalar()==2


@pytest.mark.parametrize('path,method,body', [
    ('/1/traceability','get',None),('/1/certificate','get',None),
    ('/1/inspection','put',{'result':'pass'}),('/1/deliver','post',None),
    ('/traceability/policies?org_id=1','get',None),
    ('/traceability/policies','put',{'org_id':1,'target_kind':'printer_tag','target_key':'QA','required':True}),
    ('/traceability/export?start=2026-11-01T00:00:00Z&end=2026-11-02T00:00:00Z','get',None),
])
def test_all_routes_refuse_unlicensed(world,path,method,body):
    client,_,_,license,_=world
    license.has_feature.return_value=False
    response = getattr(client,method)('/api/jobs'+path, **({'json':body} if body else {}))
    assert response.status_code==403 and 'traceability' in response.text


def test_tenant_roles_and_tokens(world):
    client,_,user,_,_=world
    user.update(role='operator',group_id=1)
    assert client.get('/api/jobs/1/traceability').status_code==200
    assert client.get('/api/jobs/2/traceability').status_code==404
    assert client.get('/api/jobs/traceability/policies?org_id=1').status_code==403
    user['role']='viewer'
    assert client.get('/api/jobs/1/traceability').status_code==403
    user.update(role='operator',_auth_kind='user_api_token',_token_scopes=['read:jobs'])
    assert client.put('/api/jobs/1/inspection',json={'result':'pass'}).status_code==403


def test_required_inspection_and_failed_inspection_refuse_delivery(world):
    client,engine,*_=world
    body={'org_id':1,'target_kind':'printer_tag','target_key':'QA','required':True}
    assert client.put('/api/jobs/traceability/policies',json=body).status_code==200
    assert client.post('/api/jobs/1/deliver').status_code==409
    assert client.put('/api/jobs/1/inspection',json={'result':'fail','notes':'dimensional mismatch'}).status_code==200
    assert client.post('/api/jobs/1/deliver').status_code==409
    assert client.put('/api/jobs/1/inspection',json={'result':'pass','notes':'verified'}).status_code==200
    first=client.post('/api/jobs/1/deliver')
    assert first.status_code==200 and first.json()['delivery']['delivered_at']
    assert client.post('/api/jobs/1/deliver').json()['delivery']==first.json()['delivery']
    assert client.put('/api/jobs/1/inspection',json={'result':'fail'}).status_code==409
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='traceability.delivered'")).scalar()==1
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='traceability.delivery_refused'")).scalar()==2


def test_optional_delivery_and_reset_does_not_reuse_inspection(world):
    client,engine,*_=world
    assert client.post('/api/jobs/1/deliver').status_code==200
    with Session(engine) as db:
        from core.print_attempts import bump_print_attempt, sqlalchemy_runner
        bump_print_attempt(sqlalchemy_runner(db),1)
        db.commit()
    record=client.get('/api/jobs/1/traceability').json()
    assert record['attempt']==1 and record['delivery']['delivered_at'] is None


def test_not_completed_and_education_refused(world):
    client,engine,_,_,policy=world
    with engine.begin() as c:c.execute(text("UPDATE jobs SET status='printing' WHERE id=1"))
    assert client.get('/api/jobs/1/traceability').status_code==409
    from fastapi import HTTPException
    policy.assert_generic_job_mutation_allowed.side_effect=HTTPException(409,'Education workflow required')
    assert client.get('/api/jobs/2/traceability').status_code==409


def verify_pdf(pdf,signature):
    assert pdf.startswith(b'%PDF')
    Ed25519PublicKey.from_public_bytes(base64.b64decode(signature['public_key'])).verify(base64.b64decode(signature['signature']),b'odin-traceability-certificate-v1\n'+hashlib.sha256(pdf).digest())
    from cryptography.exceptions import InvalidSignature
    with pytest.raises(InvalidSignature):
        Ed25519PublicKey.from_public_bytes(base64.b64decode(signature['public_key'])).verify(base64.b64decode(signature['signature']),b'odin-traceability-certificate-v1\n'+hashlib.sha256(pdf+b'x').digest())


def test_pdf_signature_and_bundle(world):
    client,*_=world
    response=client.get('/api/jobs/1/certificate?bundle=true')
    assert response.status_code==200,response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        verify_pdf(z.read('certificate.pdf'),json.loads(z.read('signature.json')))
        assert json.loads(z.read('traceability.json'))['job_id']==1
    pdf=client.get('/api/jobs/1/certificate')
    verify_pdf(pdf.content,{'public_key':pdf.headers['X-ODIN-Public-Key'],'signature':pdf.headers['X-ODIN-Signature']})


def test_batch_is_scoped_and_date_bounded(world):
    client,_,user,*_=world
    user.update(role='operator',group_id=1)
    response=client.get('/api/jobs/traceability/export',params={'start':'2026-11-01T00:00:00Z','end':'2026-11-02T00:00:00Z'})
    assert response.status_code==200,response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        assert json.loads(z.read('manifest.json'))['job_ids']==[1]
        verify_pdf(z.read('job-1.pdf'),json.loads(z.read('job-1.signature.json')))
    for start,end in [('2026-11-02','2026-11-01'),('2026-11-02T00:00:00Z','2026-11-01T00:00:00Z')]:
        assert client.get('/api/jobs/traceability/export',params={'start':start,'end':end}).status_code==422


def test_privacy_export_and_erasure(world):
    client,engine,*_=world
    client.put('/api/jobs/1/inspection',json={'result':'pass','notes':'operator@example.test inspected'})
    client.post('/api/jobs/1/deliver')
    with Session(engine) as db:
        assert export_personal(db,1)[0]['inspection_notes']=='operator@example.test inspected'
        erase_personal(db,1,{'operator@example.test'})
        db.commit()
        assert export_personal(db,1)==[]
        row=db.execute(text('SELECT * FROM traceability_records')).mappings().one()
        assert row['inspected_by'] is None and row['delivered_by'] is None and row['inspection_notes']=='[deleted-user] inspected'
        assert row['inspection_result']=='pass'


def test_migration_repeat_inventory_and_bad_shape(world):
    _,engine,*_=world
    assert bootstrap_database(engine)['applied']==[]
    from modules.system.postgres_backup_service import canonical_tables
    assert set(M.DDL)<=set(canonical_tables())
    with engine.begin() as c:
        M.apply(c)
        c.exec_driver_sql('ALTER TABLE traceability_records ADD COLUMN bad TEXT')
    with pytest.raises(MigrationError):bootstrap_database(engine)


def test_feature_tiers():
    from license_manager import TIERS
    assert 'traceability' in TIERS['enterprise']['features']
    for tier in ('community','pro','education'):
        assert 'traceability' not in TIERS[tier]['features']


def test_snapshot_binding_changes_warn_without_refusing_passed_inspection(world):
    client,engine,*_=world
    assert client.put('/api/jobs/1/inspection',json={'result':'pass'}).status_code==200
    with engine.begin() as c:c.execute(text("UPDATE jobs SET payload_sha256=:hash WHERE id=1"),{'hash':'b'*64})
    response = client.post('/api/jobs/1/deliver')
    assert response.status_code == 200
    assert response.json()['warnings']


def test_cross_tenant_file_and_erased_principal_refused(world):
    client,engine,user,*_=world
    user.update(role='operator',group_id=1)
    with engine.begin() as c:c.execute(text('UPDATE print_files SET org_id=2 WHERE id=1'))
    assert client.get('/api/jobs/1/traceability').status_code==404
    with engine.begin() as c:c.execute(text('UPDATE users SET is_active=FALSE WHERE id=1'))
    assert client.get('/api/jobs/1/traceability').status_code==401


def test_signature_failure_rolls_back_capture_and_export_audit(world,monkeypatch):
    client,engine,*_=world
    import license_manager
    from pathlib import Path
    Path(license_manager.LICENSE_DIR, license_manager.DEVICE_PRIVATE_KEY_FILENAME).unlink()
    response = client.get('/api/jobs/1/certificate')
    assert response.status_code == 503 and 'device signing key' in response.text
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM traceability_records')).scalar()==0
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='traceability.certificate_exported'")).scalar()==0


def test_product_requirement_and_tenant_policy_boundary(world):
    client,engine,user,*_=world
    with engine.begin() as c:
        c.execute(text("INSERT INTO products(id,name,org_id) VALUES(1,'Widget',1),(2,'Other',2)"))
        c.execute(text("INSERT INTO orders(id,order_number,customer_name,org_id) VALUES(1,'E4-1','Example',1)"))
        c.execute(text("INSERT INTO order_items(id,order_id,product_id,quantity) VALUES(1,1,1,1)"))
        c.execute(text('UPDATE jobs SET order_item_id=1 WHERE id=1'))
    user.update(role='admin',group_id=1)
    body={'org_id':1,'target_kind':'product','target_key':'1','required':True}
    assert client.put('/api/jobs/traceability/policies',json=body).status_code==200
    assert client.post('/api/jobs/1/deliver').status_code==409
    assert client.put('/api/jobs/traceability/policies',json={**body,'target_key':'2'}).status_code==404
    assert client.put('/api/jobs/traceability/policies',json={**body,'org_id':2}).status_code==404
    assert client.get('/api/jobs/traceability/policies?org_id=2').status_code==404
    assert client.get('/api/jobs/traceability/policies?org_id=1').json()[0]['required'] is True


def test_materials_come_from_usage_not_current_slots(world):
    client,engine,*_=world
    with engine.begin() as c:
        c.execute(text("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(1,'Test','PLA','PLA','ffffff')"))
        c.execute(text("INSERT INTO spools(id,filament_id,lot_number,vendor,org_id) VALUES(1,1,'LOT-1','Vendor',1),(2,1,'WRONG','Current',1)"))
        c.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,used_at) VALUES(1,1,20,'2026-11-01 12:00:00')"))
        c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id) VALUES(1,1,2)"))
    material=client.get('/api/jobs/1/traceability').json()['filament']
    assert len(material)==1
    assert material[0]['lot_number']=='LOT-1' and material[0]['vendor']=='Vendor' and material[0]['material']=='PLA'


def test_notes_validation_and_gdpr_scrubs_other_authors(world):
    client,engine,*_=world
    assert client.put('/api/jobs/1/inspection',json={'result':'maybe'}).status_code==422
    assert client.put('/api/jobs/1/inspection',json={'result':'pass','notes':'x'*4001}).status_code==422
    client.put('/api/jobs/1/inspection',json={'result':'pass','notes':'other@example.test helped'})
    with Session(engine) as db:
        erase_personal(db,2,{'other@example.test'})
        db.commit()
    assert 'other@example.test' not in client.get('/api/jobs/1/traceability').text


def test_audit_failure_rolls_back_inspection(world,monkeypatch):
    client,engine,*_=world
    def fail(*args,**kwargs):raise RuntimeError('audit unavailable')
    monkeypatch.setattr('modules.jobs.routes.traceability.log_audit',fail)
    with pytest.raises(RuntimeError):client.put('/api/jobs/1/inspection',json={'result':'pass'})
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM traceability_records')).scalar()==0


def test_signing_uses_existing_installation_key(tmp_path,monkeypatch):
    import license_manager
    from modules.jobs.traceability_certificate import signed
    monkeypatch.setattr(license_manager,'LICENSE_DIR',str(tmp_path))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as refused:
        signed(b'%PDF-test')
    assert refused.value.status_code == 503 and list(tmp_path.iterdir()) == []
    license_manager.get_device_keypair()  # Installation owns provisioning, not export.
    first=signed(b'%PDF-test')
    second=signed(b'%PDF-test')
    assert first==second
    verify_pdf(b'%PDF-test',first)
    keyfile=tmp_path/license_manager.DEVICE_PRIVATE_KEY_FILENAME
    assert keyfile.exists() and keyfile.stat().st_mode & 0o777 == 0o600


def test_job_owner_personal_export(world):
    client,engine,*_=world
    client.get('/api/jobs/1/traceability')
    with Session(engine) as db:
        db.execute(text('UPDATE jobs SET charged_to_user_id=2 WHERE id=1'))
        db.commit()
        exported=export_personal(db,2)
        assert exported[0]['job_id']==1 and 'widget.gcode' in exported[0]['snapshot']


def test_concurrent_delivery_records_one_receipt(world):
    from concurrent.futures import ThreadPoolExecutor
    from modules.jobs.routes.traceability import deliver_job
    _,engine,user,*_=world
    def deliver():
        with Session(engine) as db:return deliver_job(1,user=user,db=db)['delivery']
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b=list(pool.map(lambda _:deliver(),range(2)))
    assert a==b and a['delivered_at']
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='traceability.delivered'")).scalar()==1


def test_platform_policy_covers_unscoped_jobs(world):
    client,engine,user,*_=world
    with engine.begin() as c:c.execute(text('UPDATE jobs SET charged_to_org_id=NULL WHERE id=1'))
    body={'org_id':0,'target_kind':'printer_tag','target_key':'QA','required':True}
    assert client.put('/api/jobs/traceability/policies',json=body).status_code==200
    assert client.post('/api/jobs/1/deliver').status_code==409
    user.update(role='admin',group_id=1)
    assert client.get('/api/jobs/traceability/policies?org_id=0').status_code==404
    assert client.put('/api/jobs/traceability/policies',json=body).status_code==404


def test_operation_owned_file_cannot_escape_through_generic_job(world):
    client,engine,*_=world
    with engine.begin() as c:
        c.execute(text("UPDATE print_files SET stored_path='/education/retained.gcode' WHERE id=1"))
        c.execute(text("INSERT INTO education_memberships(user_id,org_id) VALUES(1,1)"))
        c.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,final_path) VALUES('e4-private',1,1,'committed','/education/retained.gcode')"))
    assert client.get('/api/jobs/1/traceability').status_code==409


@pytest.mark.parametrize('result,expected',[(None,'Not inspected. Conformance is not established.'),('fail','Operator inspection: FAIL.'),('pass','Operator inspection: PASS.')])
def test_certificate_wording_and_branding(world,result,expected):
    from pypdf import PdfReader
    client,*_=world
    if result:assert client.put('/api/jobs/1/inspection',json={'result':result}).status_code==200
    response=client.get('/api/jobs/1/certificate')
    assert response.status_code==200,response.text
    text_content=''.join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)
    assert expected in text_content and 'Acme Manufacturing' in text_content


def test_serial_excludes_transport_secret(world,monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    from core.crypto import encrypt
    client,engine,*_=world
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET api_type='bambu',api_key=:key WHERE id=1"),{'key':encrypt('SERIAL-E4|SYNTHETIC-ACCESS-CODE')})
    response=client.get('/api/jobs/1/traceability')
    assert response.status_code==200,response.text
    assert response.json()['printer_serial']=='SERIAL-E4'
    assert 'SYNTHETIC-ACCESS-CODE' not in response.text and 'api_key' not in response.text



def test_certificate_uses_existing_org_branding_settings(world):
    from pypdf import PdfReader
    client,engine,*_=world
    with engine.begin() as c:
        c.execute(text('UPDATE groups SET settings_json=:settings WHERE id=1'),{'settings':json.dumps({'branding_app_name':'ACME Quality'})})
    response=client.get('/api/jobs/1/certificate')
    assert response.status_code==200,response.text
    assert 'ACME Quality' in PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
