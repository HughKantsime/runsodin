"""Actual final-admission transactions on an owned complete application DB."""
import secrets
import pytest
from sqlalchemy import text
from core import dispatch_claims
from core.dispatch_identity import build_identity
from modules.printers import dispatch
from modules.printers.dispatch_admission import admit
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def setup(review_db,monkeypatch):
    from core import db
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    engine=review_db.get_bind()
    monkeypatch.setattr(db,'engine',engine)
    provider=EducationPolicyService()
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',provider)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO printers(id,name,api_type,is_active,org_id,bed_x_mm,bed_y_mm,nozzle_diameter) VALUES(59,'Synthetic gate printer','moonraker',1,1,256,256,.4)"))
        conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(59,1,'PLA')"))
        conn.execute(text("UPDATE printers SET api_type='moonraker',api_host='printer.example.test',api_key='',gcode_state='IDLE' WHERE id=59"))
        conn.execute(text("INSERT INTO models(id,name,org_id) VALUES(51,'Synthetic admission',1)"))
        conn.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,model_id,org_id) VALUES(52,'synthetic.gcode','synthetic.gcode','/owned/synthetic.gcode',51,1)"))
        conn.execute(text("INSERT INTO jobs(id,model_id,item_name,status,printer_id,charged_to_org_id,hold) VALUES(53,51,'Synthetic admission','scheduled',59,1,0)"))
    job=dispatch._load_job(53);creds=dispatch._get_printer_info(59)
    key=secrets.token_bytes(32);identity=build_identity(creds,key)
    claim=dispatch_claims.acquire(engine,59,53,identity.target_key,identity.configuration_key,'a'*64,1)
    assert claim.status=='acquired'
    return engine,provider,dict(token=claim.token,job=job,creds=creds,key=key,payload_sha256='a'*64,transport_filename=f'odin-dispatch-{claim.token}.gcode',power_accepted=True)


def state(engine):
    with engine.connect() as conn:return conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one()


def test_admission_commits_exact_claim_after_current_authority_checks(setup):
    engine,provider,inputs=setup
    result=admit(engine,**inputs)
    assert result.admitted, result.reason
    assert state(engine)=='adapter_entered'
    assert not dispatch_claims.release_preparing(engine,inputs['token'])
    assert not admit(engine,**inputs).admitted


@pytest.mark.parametrize('statement',[
    "UPDATE jobs SET hold=1 WHERE id=53",
    "UPDATE jobs SET status='cancelled' WHERE id=53",
    "UPDATE jobs SET printer_id=10 WHERE id=53",
    "UPDATE jobs SET charged_to_org_id=2 WHERE id=53",
    "UPDATE jobs SET plate_index=2 WHERE id=53",
    "UPDATE jobs SET ams_mapping='[1]' WHERE id=53",
    "UPDATE jobs SET model_id=12 WHERE id=53",
    "UPDATE jobs SET print_file_id=11 WHERE id=53",
    "UPDATE print_files SET stored_path='/changed.gcode' WHERE id=52",
    "UPDATE print_files SET original_filename='changed.gcode' WHERE id=52",
    "UPDATE print_files SET org_id=2 WHERE id=52",
    "UPDATE models SET org_id=2 WHERE id=51",
    "UPDATE printers SET api_host='changed.example.test' WHERE id=59",
    "UPDATE printers SET api_key='changed synthetic input' WHERE id=59",
    "UPDATE printers SET is_active=0 WHERE id=59",
    "UPDATE printers SET org_id=2 WHERE id=59",
    "UPDATE printers SET bed_x_mm=128 WHERE id=59",
    "UPDATE printers SET nozzle_diameter=.6 WHERE id=59",
    "UPDATE printers SET plug_type='tasmota',plug_host='changed.example.test' WHERE id=59",
    "UPDATE filament_slots SET filament_type='ABS' WHERE printer_id=59",
])
def test_intervening_dispatch_fact_change_refuses_without_advancing_claim(setup,statement):
    engine,provider,inputs=setup
    with engine.begin() as conn:conn.execute(text(statement))
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


@pytest.mark.parametrize('statement',[
    "UPDATE printers SET gcode_state='RUNNING' WHERE id=59",
    "INSERT INTO jobs(id,model_id,item_name,status,printer_id) VALUES(54,51,'Active','paused',59)",
    "INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) VALUES(70,59,'Active','running',CURRENT_TIMESTAMP)",
])
def test_active_attempts_refuse_even_when_snapshot_identity_unchanged(setup,statement):
    engine,provider,inputs=setup
    with engine.begin() as conn:conn.execute(text(statement))
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


@pytest.mark.parametrize('change',[{'token':'f'*32},{'payload_sha256':'b'*64},{'key':b'x'*32}])
def test_exact_claim_binding_required(setup,change):
    engine,provider,inputs=setup
    assert not admit(engine,**{**inputs,**change}).admitted
    assert state(engine)=='preparing'


def test_loader_uses_caller_transaction_and_keeps_it_open(setup):
    engine,provider,inputs=setup
    with engine.connect() as conn:
        tx=conn.begin()
        conn.execute(text("UPDATE jobs SET hold=1 WHERE id=53"))
        conn.execute(text("UPDATE printers SET api_host='transaction.example.test' WHERE id=59"))
        assert dispatch._load_job(53,connection=conn)['hold']==1
        assert dispatch._get_printer_info(59,connection=conn)['ip']=='transaction.example.test'
        assert tx.is_active and not conn.closed
        tx.rollback()
    assert dispatch._load_job(53)['hold']==0
    assert dispatch._get_printer_info(59)['ip']=='printer.example.test'


def test_admission_commit_uncertainty_never_authorizes_and_retains_fence(setup):
    from contextlib import contextmanager
    engine,provider,inputs=setup
    class Uncertain:
        @contextmanager
        def begin(self):
            with engine.begin() as conn:yield conn
            raise RuntimeError('synthetic lost commit receipt')
    assert not admit(Uncertain(),**inputs).admitted
    assert state(engine)=='adapter_entered'
    assert not dispatch_claims.release_preparing(engine,inputs['token'])


def education_inputs(setup,monkeypatch):
    from core import crypto
    engine,provider,inputs=setup
    monkeypatch.setenv('ENCRYPTION_KEY',__import__('cryptography.fernet',fromlist=['Fernet']).Fernet.generate_key().decode())
    with engine.begin() as conn:
        conn.execute(text('DELETE FROM printer_dispatch_claims'))
        conn.execute(text("UPDATE printers SET api_type='bambu',api_host='printer.example.test',api_key=:key WHERE id=9"),{'key':crypto.encrypt('SYNTHETICPRINTER|'+secrets.token_hex(16))})
        conn.execute(text("UPDATE jobs SET status='scheduled',printer_id=9 WHERE id=13"))
        conn.execute(text("UPDATE education_submissions SET status='scheduled',approved_printer_id=9 WHERE id=14"))
        reservation=provider.reserve_dispatch(conn,job_id=13,printer_id=9,expected_revision=1,extension='.3mf')
        assert reservation
    job=dispatch._load_job(13);creds=dispatch._get_printer_info(9)
    identity=build_identity(creds,inputs['key'])
    claim=dispatch_claims.acquire(engine,9,13,identity.target_key,identity.configuration_key,'a'*64,1)
    assert claim.status=='acquired'
    return dict(token=claim.token,job=job,creds=creds,key=inputs['key'],payload_sha256='a'*64,
                education_provider=provider,education_reservation=reservation,transport_filename=reservation['remote_filename'],power_accepted=True)


def test_existing_education_reservation_admits_on_same_transaction(setup,monkeypatch):
    engine,provider,_=setup
    inputs=education_inputs(setup,monkeypatch)
    result=admit(engine,**inputs)
    assert result.admitted,result.reason
    assert state(engine)=='adapter_entered'
    with engine.connect() as conn:
        assert conn.execute(text('SELECT state FROM education_monitor_claims')).scalar_one()=='reserved'


@pytest.mark.parametrize('change',['token','revoked','center','revision','claim_state'])
def test_education_authority_or_exact_reservation_drift_refuses(setup,monkeypatch,change):
    engine,provider,_=setup
    inputs=education_inputs(setup,monkeypatch)
    if change=='token':inputs['education_reservation']={**inputs['education_reservation'],'token':'f'*32}
    else:
        sql={'revoked':"UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9",
             'center':"UPDATE education_cost_centers SET state='archived' WHERE id=7",
             'revision':"UPDATE education_submissions SET lifecycle_revision=2 WHERE id=14",
             'claim_state':"UPDATE education_monitor_claims SET state='awaiting'"}[change]
        with engine.begin() as conn:conn.execute(text(sql))
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


@pytest.mark.parametrize('primary_alias',[False,True])
def test_operation_owned_file_cannot_be_admitted_as_generic(setup,primary_alias):
    engine,provider,inputs=setup
    with engine.begin() as conn:
        if primary_alias:
            conn.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(55,'primary.gcode','primary.gcode','/operation-only/primary.gcode',1)"))
            conn.execute(text('UPDATE models SET print_file_id=55 WHERE id=51'))
            conn.execute(text('UPDATE jobs SET print_file_id=52 WHERE id=53'))
            conn.execute(text("UPDATE education_upload_operations SET final_path='/operation-only/primary.gcode' WHERE operation_id='upload-1'"))
        else:
            conn.execute(text("UPDATE education_upload_operations SET final_path='/owned/synthetic.gcode' WHERE operation_id='upload-1'"))
    inputs['job']=dispatch._load_job(53)
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


def refresh_claim_inputs(engine,inputs):
    assert dispatch_claims.release_preparing(engine,inputs['token'])
    job=dispatch._load_job(inputs['job']['id'])
    creds=dispatch._get_printer_info(job['printer_id'])
    identity=build_identity(creds,inputs['key'])
    claim=dispatch_claims.acquire(engine,job['printer_id'],job['id'],identity.target_key,identity.configuration_key,'a'*64,creds.get('org_id'))
    assert claim.status=='acquired'
    return {**inputs,'job':job,'creds':creds,'token':claim.token,'transport_filename':f'odin-dispatch-{claim.token}.gcode'}


@pytest.mark.parametrize('sql',[
    'UPDATE models SET org_id=2 WHERE id=51',
    'UPDATE print_files SET org_id=2 WHERE id=52',
    'UPDATE printers SET org_id=2 WHERE id=59',
    'UPDATE jobs SET charged_to_org_id=NULL WHERE id=53',
])
def test_initial_generic_private_resource_ownership_must_match_job_tenant(setup,sql):
    engine,provider,inputs=setup
    with engine.begin() as conn:conn.execute(text(sql))
    inputs=refresh_claim_inputs(engine,inputs)
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


@pytest.mark.parametrize('scenario',['own','global-payload','global-all','foreign-shared-printer'])
def test_valid_explicit_tenant_global_resources_and_shared_printer_admit(setup,scenario):
    engine,provider,inputs=setup
    with engine.begin() as conn:
        if scenario in ('global-payload','global-all'):
            conn.execute(text('UPDATE models SET org_id=NULL WHERE id=51'))
            conn.execute(text('UPDATE print_files SET org_id=NULL WHERE id=52'))
        if scenario=='global-all':
            conn.execute(text('UPDATE printers SET org_id=NULL WHERE id=59'))
            conn.execute(text('UPDATE jobs SET charged_to_org_id=NULL WHERE id=53'))
        if scenario=='foreign-shared-printer':
            conn.execute(text('UPDATE printers SET org_id=2,shared=1 WHERE id=59'))
    inputs=refresh_claim_inputs(engine,inputs)
    result=admit(engine,**inputs)
    assert result.admitted,result.reason


def test_sharing_revocation_after_capture_refuses(setup):
    engine,provider,inputs=setup
    with engine.begin() as conn:conn.execute(text('UPDATE printers SET org_id=2,shared=1 WHERE id=59'))
    inputs=refresh_claim_inputs(engine,inputs)
    with engine.begin() as conn:conn.execute(text('UPDATE printers SET shared=0 WHERE id=59'))
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


def test_shared_printer_does_not_authorize_foreign_private_payload(setup):
    engine,provider,inputs=setup
    with engine.begin() as conn:
        conn.execute(text('UPDATE printers SET org_id=2,shared=1 WHERE id=59'))
        conn.execute(text('UPDATE print_files SET org_id=2 WHERE id=52'))
    inputs=refresh_claim_inputs(engine,inputs)
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


@pytest.mark.parametrize('column,value',[('shared',2),('shared','false'),('is_active',2),('is_active','false')])
def test_malformed_database_permission_flags_never_become_authorized_credentials(setup,column,value):
    engine,provider,inputs=setup
    with engine.begin() as conn:
        if column=='shared':conn.execute(text('UPDATE printers SET org_id=2 WHERE id=59'))
        conn.execute(text(f'UPDATE printers SET {column}=:value WHERE id=59'),{'value':value})
    assert dispatch._get_printer_info(59) is None
    assert not admit(engine,**inputs).admitted
    assert state(engine)=='preparing'


def test_final_admission_refuses_printer_changed_to_kobra(setup):
    engine, _, inputs = setup
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET machine_type='Kobra S1 Max' WHERE id=59"))
    result = admit(engine, **inputs)
    from modules.printers.dispatch_support import KOBRA_DISPATCH_DETAIL
    assert not result.admitted
    assert result.reason == 'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL
    assert state(engine) == 'preparing'
