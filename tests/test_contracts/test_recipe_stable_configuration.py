"""Stable recipes keep full dispatch fences and fail closed on legacy history."""
import json
import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy import text
from core.dispatch_identity import build_identity, stable_configuration_key
from tests.test_contracts.test_dispatch_identity_behavior import creds, KEY
from tests.test_contracts.test_batch_acceptance import batch, shop, review_db, make_recipe, body


@pytest.mark.parametrize('change', [
    {'active_materials':['PLA','PETG'], 'filament_slots':[[1,'PLA'],[4,'PETG']]},
    {'plug_configuration':{'plug_type':'mqtt','plug_host':'changed.test'}},
])
def test_only_recipe_key_ignores_unused_slots_and_power(change):
    old = creds(active_materials=['PLA'], filament_slots=[[1,'PLA'],[4,'PLA']])
    new = {**old, **change}
    assert build_identity(old,KEY).configuration_key != build_identity(new,KEY).configuration_key
    assert stable_configuration_key(old,KEY) == stable_configuration_key(new,KEY)
    assert stable_configuration_key(old,KEY) != build_identity(old,KEY).configuration_key


@pytest.mark.parametrize('change', [{'access_code':'changed'}, {'nozzle_diameter':.6},
    {'bed_x_mm':255}, {'bed_y_mm':255}, {'api_key':'changed'}, {'org_id':2}, {'ip':'different.test'}])
def test_used_configuration_changes_refuse(change):
    assert stable_configuration_key(creds(),KEY) != stable_configuration_key(creds(**change),KEY)


@pytest.fixture
def legacy(batch, monkeypatch):
    from core.registry import registry
    from modules.printers.binding import PrinterBindingService
    from core import crypto
    monkeypatch.setattr(crypto,'decrypt',lambda value:value)
    monkeypatch.setitem(registry._providers,'PrinterBindingProvider',PrinterBindingService())
    db = batch[3]
    db.execute(text("""UPDATE printers SET api_host='printer.test',api_key='SYNTHETIC|test-code',
        is_active=TRUE,bed_x_mm=256,bed_y_mm=256,nozzle_diameter=0.4 WHERE id=301"""))
    db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(301,1,'PLA'),(301,4,'PLA')"))
    from modules.printers.dispatch import _get_printer_info
    from core.auth import SECRET_KEY
    full = build_identity(_get_printer_info(301,connection=db),SECRET_KEY.encode()).configuration_key
    stable = stable_configuration_key(_get_printer_info(301,connection=db),SECRET_KEY.encode())
    db.execute(text("""UPDATE dispatch_attempt_receipts SET configuration_key=:full,
        stable_configuration_key=:stable,stable_key_source='dispatch',created_at='2026-10-26 12:00:00'
        WHERE printer_id=301"""),{'full':full,'stable':stable})
    db.commit()
    recipe = make_recipe(batch)
    old = {k:v for k,v in recipe.items() if k!='id'}
    old.update(version=1);old['payload']={**old['payload'],'configuration_key':full}
    db.execute(text('UPDATE production_recipes SET definition=:d WHERE id=:id'),{'d':json.dumps(old),'id':recipe['id']})
    db.execute(text("UPDATE dispatch_attempt_receipts SET stable_configuration_key=NULL,stable_key_source=NULL WHERE printer_id=301"))
    db.commit()
    return batch,recipe,old


def test_legacy_unused_slot_change_matches_without_rewriting_recipe(legacy):
    batch,recipe,old=legacy;db=batch[3]
    db.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=301 AND slot_number=4"))
    from modules.printers.dispatch import _get_printer_info
    from core.auth import SECRET_KEY
    new = build_identity(_get_printer_info(301,connection=db),SECRET_KEY.encode()).configuration_key
    assert new != old['payload']['configuration_key']
    db.execute(text("UPDATE dispatch_attempt_receipts SET configuration_key=:key WHERE job_id=302"),{'key':new});db.commit()
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==200,response.text
    rows=db.execute(text('SELECT configuration_key,stable_configuration_key,stable_key_source FROM dispatch_attempt_receipts WHERE printer_id=301')).all()
    assert len({r[0] for r in rows})==2 and len({r[1] for r in rows})==1
    assert all(r[2]=='backfilled_no_printer_audit' for r in rows)
    assert json.loads(db.execute(text('SELECT definition FROM production_recipes WHERE id=:id'),{'id':recipe['id']}).scalar_one())==old


@pytest.mark.parametrize('action,stamp', [('printer.updated','2026-10-27 00:00:00'),
    ('printer.updated','2026-10-26 12:00:00'), ('printer.deleted','2026-10-27 00:00:00'),
    ('printer.created','2026-10-27 00:00:00'), ('update','2026-10-27 00:00:00'),
    ('printer.updated','malformed'), ('printer.updated',None),
    ('printer.updated','2026-10-26T08:00:01-04:00')])
def test_legacy_changed_or_unprovable_history_refuses_without_backfill(legacy,action,stamp):
    batch,recipe,_=legacy;db=batch[3]
    db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,timestamp) VALUES(:action,'printer',301,:stamp)"),{'action':action,'stamp':stamp});db.commit()
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==409 and 'Qualify a new recipe revision' in response.json()['detail']
    assert db.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts WHERE stable_configuration_key IS NOT NULL')).scalar_one()==0
    assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one()==0


@pytest.mark.parametrize('action,printer,stamp', [('sync',301,'2026-10-27 00:00:00'),
    ('printer.updated',302,'2026-10-27 00:00:00'), ('printer.updated',301,'2026-10-26T07:59:59-04:00')])
def test_legacy_prior_or_unrelated_audits_allow(legacy,action,printer,stamp):
    batch,recipe,_=legacy;db=batch[3]
    db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,timestamp) VALUES(:action,'printer',:printer,:stamp)"),locals());db.commit()
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==200,response.text


@pytest.mark.parametrize('column,value', [('api_key','SYNTHETIC|changed-code'),('nozzle_diameter',.6),('bed_x_mm',300)])
def test_new_receipt_changed_configuration_refuses(batch,monkeypatch,column,value):
    # Retained dispatch-time keys distinguish real changes even without an audit.
    from core.auth import SECRET_KEY
    recipe=make_recipe(batch);db=batch[3]
    original=creds(api_type='bambu',serial='SYNTHETIC',access_code='test-code')
    changed={**original, {'api_key':'access_code'}.get(column,column):value}
    first=stable_configuration_key(original,SECRET_KEY.encode())
    second=stable_configuration_key(changed,SECRET_KEY.encode())
    assert first!=second
    definition={k:v for k,v in recipe.items() if k!='id'}
    definition['payload']['configuration_key']=first
    db.execute(text('UPDATE production_recipes SET definition=:d WHERE id=:id'),{'d':json.dumps(definition),'id':recipe['id']})
    db.execute(text('UPDATE dispatch_attempt_receipts SET stable_configuration_key=:key WHERE job_id=302'),{'key':second});db.commit()
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==409 and response.json()['detail']=='Job printer configuration does not match this recipe revision'


@pytest.mark.parametrize('field,value,label', [('print_file_id',999,'file'),('payload_sha256','c'*64,'file'),
    ('plate_index',9,'plate'), ('model_id',999,'model'),('printer_id',999,'printer'),('configuration_key','c'*64,'printer configuration')])
def test_inspection_names_mismatching_field(batch,field,value,label):
    recipe=make_recipe(batch);db=batch[3]
    definition={k:v for k,v in recipe.items() if k!='id'}
    definition['payload'][field]=value
    db.execute(text('UPDATE production_recipes SET definition=:d WHERE id=:id'),{'d':json.dumps(definition),'id':recipe['id']});db.commit()
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==409 and response.json()['detail']==f'Job {label} does not match this recipe revision'


def test_legacy_reference_tampering_refused(legacy):
    batch,recipe,old=legacy;db=batch[3]
    old['reference_claim_token']='f'*32
    db.execute(text('UPDATE production_recipes SET definition=:d WHERE id=:id'),{'d':json.dumps(old),'id':recipe['id']});db.commit()
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==409 and 'reference evidence differs' in response.json()['detail']


def test_exact_latch_job85_matches_all_five_legacy_reference_receipts(batch,monkeypatch):
    from core.registry import registry
    from modules.printers.binding import PrinterBindingService
    from core import crypto
    from core.auth import SECRET_KEY
    from core.economic_identity import _capture_fresh, _allocate_created
    from core.economic_source_creation import _create_attempt
    from core.dispatch_receipts import filename_fingerprint
    from modules.jobs.models import Job
    from modules.printers.dispatch import _get_printer_info
    monkeypatch.setattr(crypto,'decrypt',lambda value:value)
    monkeypatch.setitem(registry._providers,'PrinterBindingProvider',PrinterBindingService())
    db=batch[3]
    db.execute(text("""INSERT INTO printers(id,name,api_type,api_host,api_key,is_active,org_id,
        model,bed_x_mm,bed_y_mm,nozzle_diameter) VALUES(1,'X1C','bambu','latch.test',
        'SYNTHETIC-LATCH|test-code',TRUE,1,'X1C',256,256,0.4)"""))
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(2,'Latch',1)"))
    db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(1,1,'PLA'),(1,4,'PLA')"))
    old_key=build_identity(_get_printer_info(1,connection=db),SECRET_KEY.encode()).configuration_key
    definitions=[]
    for jid in (63,71,72,78,81,85):
        if jid==85:
            db.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=1 AND slot_number=4"))
        full=build_identity(_get_printer_info(1,connection=db),SECRET_KEY.encode()).configuration_key
        job=Job(item_name='100 latches',status='completed',quantity=1,model_id=2,
            printer_id=1,print_file_id=5,payload_sha256='f851334c'+'a'*56,plate_index=0,charged_to_org_id=1)
        creation=_capture_fresh(job,'job',owner_org_id=1)
        job.id=jid
        db.add(job);db.flush();_allocate_created(db,creation)
        token=uuid.uuid4().hex;name='odin-dispatch-'+token+'.3mf'
        observed=_create_attempt(db,{'printer_id':1,'scheduled_job_id':jid,'status':'running',
            'started_at':datetime.now(timezone.utc),'filename':name,'job_name':name.removesuffix('.3mf')})
        db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"),{'id':observed.id})
        db.execute(text('''INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,
            prepared_power_key,observed_filename_key,observed_print_job_id,created_at)
            VALUES(:token,1,:jid,:full,:full,:fp,:observed,'2026-10-26 12:00:00')'''),
            {'token':token,'jid':jid,'full':full,'fp':filename_fingerprint(SECRET_KEY.encode(),token,name),'observed':observed.id})
        if jid!=85:
            definition={'version':1,'name':'100 latch plate','materials':'PLA','qualified':True,
                'reference_job_id':jid,'reference_claim_token':token,'reference_observation_uid':observed.birth_nonce,
                'payload':{'model_id':2,'print_file_id':5,'payload_sha256':job.payload_sha256,'plate_index':0,'printer_id':1,'configuration_key':full},
                'outputs':[{'product_id':101,'kind':'main','expected':100,'product_name':None,'bom_model_id':None}]}
            definitions.append(definition)
    assert full!=old_key
    db.commit()
    from modules.orders.batch_production import recipe,job_evidence,recipe_payload
    from modules.orders.recipe_matching import assert_payload_matches
    from modules.orders.models import Product
    for index,definition in enumerate(definitions):
        definition['outputs'][0]['product_name']=db.get(Product,101).name
        rid='ff5df69abfe441729d7bf9835e6e9141' if index==4 else uuid.uuid4().hex
        db.execute(text('INSERT INTO production_recipes(id,owner,definition) VALUES(:id,1,:d)'),{'id':rid,'d':json.dumps(definition)})
        assert_payload_matches(recipe_payload(job_evidence(db,1,85)),recipe(db,1,rid)['payload'])
    db.commit()
    response=batch[0].post('/products/production/inspections',json={'recipe_id':rid,'job_id':85,
        'square_revision':batch[-1]['revision'],'inspection_confirmed':True,
        'outputs':[{'product_id':101,'kind':'main','accepted':100,'rejected':0}]})
    assert response.status_code==200,response.text
    assert db.execute(text("SELECT COUNT(*) FROM dispatch_attempt_receipts WHERE printer_id=1 AND stable_key_source='backfilled_no_printer_audit'")).scalar_one()==6


def test_outside_binding_uses_stable_key_and_refuses_changed_nozzle(legacy):
    batch,recipe,_=legacy;db=batch[3]
    from core.economic_source_creation import _create_attempt
    db.begin()
    outside=_create_attempt(db,{'printer_id':301,'status':'running','started_at':datetime.now(timezone.utc),
        'filename':'latch.3mf','job_name':'latch'})
    db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"),{'id':outside.id})
    db.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=301 AND slot_number=4"));db.commit()
    response=batch[0].post(f'/products/production/outside-prints/{outside.id}/bind',json={'recipe_id':recipe['id'],
        'attestation':{'started_outside_odin':True,'file_revision_confirmed':True},'attested_by':'owner_confirmed_to_operator'})
    assert response.status_code==200,response.text
    from modules.orders.outside_prints import evidence
    assert evidence(db,1,outside.id)['stable_configuration_key']==response.json()['payload']['configuration_key']
    db.execute(text('UPDATE printers SET nozzle_diameter=0.6 WHERE id=301'));db.commit()
    from fastapi import HTTPException
    with pytest.raises(HTTPException,match='configuration changed'):evidence(db,1,outside.id)


@pytest.mark.parametrize('sliced,current,allowed',[(.4,.4,True),(.4,.6,False),(.6,.4,False),
    (None,.6,True),(.4,None,True)])
def test_legacy_backfill_requires_known_sliced_nozzle_match(legacy,sliced,current,allowed):
    batch,recipe,_=legacy;db=batch[3]
    db.execute(text("""INSERT INTO print_files(id,filename,original_filename,stored_path,nozzle_diameter)
        VALUES(301,'synthetic.3mf','synthetic.3mf','/synthetic/no-file-read.3mf',:nozzle)"""),{'nozzle':sliced})
    db.execute(text('UPDATE printers SET nozzle_diameter=:nozzle WHERE id=301'),{'nozzle':current});db.commit()
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE entity_type='printer' AND entity_id=301")).scalar_one()==0
    response=batch[0].post('/products/production/inspections',json=body(batch,recipe))
    if allowed:
        assert response.status_code==200,response.text
        assert set(db.execute(text('SELECT stable_key_source FROM dispatch_attempt_receipts WHERE printer_id=301')).scalars())=={'backfilled_no_printer_audit'}
    else:
        assert response.status_code==409 and 'Qualify a new recipe revision' in response.json()['detail']
        assert db.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts WHERE stable_configuration_key IS NOT NULL')).scalar_one()==0
        assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one()==0
