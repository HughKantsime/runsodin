"""Real Education dispatch admission and authoritative observation receipt."""
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import text,event
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_education_dispatch_policy import actual_education_dispatch

@pytest.fixture
def admitted_education(actual_education_dispatch,monkeypatch,request):
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch
    from modules.notifications import job_events
    from modules.printers.monitors import mqtt_job_lifecycle,moonraker_monitor
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    monkeypatch.setitem(registry._providers,'OrderFulfillmentProvider',OrderFulfillmentService())
    db,uploaded=actual_education_dispatch
    names=[]
    def adapter(*args,**kwargs):names.append(args[2]);return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_bambu',adapter)
    reserved=getattr(request,'param','confirmed')=='reserved'
    if reserved:monkeypatch.setattr(dispatch,'_confirm_education_dispatch',lambda *args:{'transitioned':False})
    ok,_=dispatch.dispatch_job(9,uploaded['job_id'])
    assert ok is not reserved
    engine=db.get_bind();db.rollback()
    @contextmanager
    def owned_db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(job_events,'get_db',owned_db)
    monkeypatch.setattr(mqtt_job_lifecycle,'get_db',owned_db)
    monkeypatch.setattr(moonraker_monitor,'engine',engine)
    def forbidden(*args,**kwargs):raise AssertionError('Education must not use generic event fanout')
    monkeypatch.setattr(job_events,'get_event_bus',lambda:SimpleNamespace(publish=forbidden))
    def start(source='job_event', *, reported_name=None):
        name=names[0] if reported_name is None else reported_name
        if source=='job_event':return job_events.job_started(9,name)
        if source=='mqtt':return mqtt_job_lifecycle.record_job_started(9,'Synthetic',{'subtask_name':name,'gcode_file':'Metadata/plate_1.gcode'},forbidden,forbidden)[0]
        monitor=moonraker_monitor.MoonrakerMonitor(9,'Synthetic','127.0.0.1')
        monitor._job_started(SimpleNamespace(filename=name,total_layers=10,bed_target=60,nozzle_target=200))
        return monitor._current_job_db_id
    return engine,uploaded,names[0],start


def bound(engine,job):
    with engine.connect() as conn:return conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:job'),{'job':job}).scalar_one()

@pytest.mark.parametrize('admitted_education',['confirmed','reserved'],indirect=True)
@pytest.mark.parametrize('source',['job_event','mqtt','moonraker'])
def test_actual_education_start_binds_its_admitted_universal_receipt(admitted_education,source):
    engine,uploaded,_,start=admitted_education
    identifier=start(source)
    assert identifier is not None
    with engine.connect() as conn:
        if source=='moonraker':
            # This fixture is an actual Bambu P1S dispatch. A fixed Moonraker
            # adapter callback cannot supply its transport identity.
            assert conn.execute(text('SELECT scheduled_job_id,economic_uid FROM print_jobs WHERE id=:id'),{'id':identifier}).one()==(None,None)
            assert bound(engine,uploaded['job_id']) is None
            return
        assert conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==uploaded['job_id']
        assert conn.execute(text('SELECT state FROM education_monitor_claims WHERE job_id=:job'),{'job':uploaded['job_id']}).scalar_one()=='running'
    assert bound(engine,uploaded['job_id'])==identifier


def test_same_observation_resume_keeps_old_received_time_and_binding(admitted_education):
    from modules.organizations.education_policy import claim_monitor_observation
    engine,uploaded,name,start=admitted_education
    identifier=start()
    with engine.begin() as conn:
        conn.execute(text("UPDATE print_jobs SET observed_received_at='2001-01-01 00:00:00' WHERE id=:id"),{'id':identifier})
        result=claim_monitor_observation(conn,print_job_id=identifier,printer_id=9,observed_filename=name)
        assert result['authorized'] and result['dispatch_receipt_binding']=='matched'
    assert bound(engine,uploaded['job_id'])==identifier

@pytest.mark.parametrize('obstacle',['configuration','missing_receipt','legacy_no_claim','old_receipt','different_observation','token','revision'])
def test_uncertain_binding_preserves_authoritative_education_observation(admitted_education,obstacle):
    from modules.organizations.education_policy import claim_monitor_observation
    engine,uploaded,name,start=admitted_education
    with engine.begin() as conn:
        if obstacle=='configuration':conn.execute(text("UPDATE printers SET plug_auth_token='synthetic replacement' WHERE id=9"))
        elif obstacle=='missing_receipt':conn.execute(text('DELETE FROM dispatch_attempt_receipts'))
        elif obstacle=='legacy_no_claim':conn.execute(text('DELETE FROM printer_dispatch_claims'))
        elif obstacle=='old_receipt':conn.execute(text('UPDATE dispatch_attempt_receipts SET created_at=:stamp'),{'stamp':datetime.now(timezone.utc)+timedelta(minutes=2)})
    identifier=start();assert identifier is not None
    if obstacle in ('different_observation','token','revision'):
        assert bound(engine,uploaded['job_id'])==identifier
        with engine.begin() as conn:
            if obstacle=='different_observation':
                second=conn.execute(text("INSERT INTO print_jobs(printer_id,job_name,filename,scheduled_job_id,status,started_at,observed_received_at) VALUES(9,:name,:name,:job,'running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) RETURNING id"),{'name':name,'job':uploaded['job_id']}).scalar_one()
                result=claim_monitor_observation(conn,print_job_id=second,printer_id=9,observed_filename=name)
                assert result['dispatch_receipt_binding']=='refused'
            elif obstacle=='revision':
                conn.execute(text('UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1 WHERE id=:id'),{'id':uploaded['id']})
                result=claim_monitor_observation(conn,print_job_id=identifier,printer_id=9,observed_filename=name)
                assert result['dispatch_receipt_binding']=='refused'
            else:
                conn.execute(text("UPDATE print_jobs SET job_name='odin-ffffffffffffffffffffffffffffffff.3mf' WHERE id=:id"),{'id':identifier})
                result=claim_monitor_observation(conn,print_job_id=identifier,printer_id=9,observed_filename=name)
                assert result['dispatch_receipt_binding']=='refused'
        assert bound(engine,uploaded['job_id'])==identifier
    elif obstacle!='missing_receipt':assert bound(engine,uploaded['job_id']) is None
    with engine.connect() as conn:
        if obstacle in ('configuration','missing_receipt','legacy_no_claim','old_receipt'):
            # New prospective births cannot be attributed from partial teacher
            # authority when durable universal dispatch evidence is missing/bad.
            row=conn.execute(text('SELECT scheduled_job_id,economic_uid,economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
            assert row[0] is None and row[1] is None and row[2]
            assert conn.execute(text('SELECT state FROM cost_source_lifetimes WHERE birth_nonce=:nonce'),{'nonce':row[2]}).scalar_one()=='unresolved'
        else:
            assert conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==uploaded['job_id']
        assert conn.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':uploaded['job_id']}).scalar_one()=='printing'


@pytest.mark.parametrize('admitted_education',['confirmed','reserved'],indirect=True)
def test_receipt_write_failure_rolls_back_actual_observation_and_education_transition(admitted_education):
    engine,uploaded,_,start=admitted_education
    with engine.connect() as conn:
        before=conn.execute(text('SELECT state,authority_revision FROM education_monitor_claims')).one()
        events=conn.execute(text('SELECT COUNT(*) FROM education_audit_events')).scalar_one()
    from core.dispatch_observation_executor import TransactionView
    original=TransactionView.execute
    def fault(self,statement,parameters=None):
        if str(statement).lstrip().startswith('UPDATE dispatch_attempt_receipts SET observed_print_job_id'):
            raise RuntimeError('Synthetic receipt write failure')
        return original(self,statement,parameters)
    monkeypatch=__import__('pytest').MonkeyPatch()
    monkeypatch.setattr(TransactionView,'execute',fault)
    try:assert start() is None
    finally:monkeypatch.undo()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM print_jobs')).scalar_one()==0
        assert conn.execute(text('SELECT state,authority_revision FROM education_monitor_claims')).one()==before
        assert conn.execute(text('SELECT COUNT(*) FROM education_audit_events')).scalar_one()==events
    assert bound(engine,uploaded['job_id']) is None



def test_late_first_observation_aborts_prepared_authority_and_outer_transaction(admitted_education,monkeypatch):
    from modules.organizations.services import EducationPolicyService
    from core.dispatch_observation_executor import TransactionView
    original=EducationPolicyService.finalize_start_authority
    def late(self,executor,**kwargs):
        TransactionView(executor).execute(text("UPDATE print_jobs SET observed_received_at='2001-01-01 00:00:00' WHERE id=:id"),{'id':kwargs['print_job_id']})
        return original(self,executor,**kwargs)
    monkeypatch.setattr(EducationPolicyService,'finalize_start_authority',late)
    engine,uploaded,_,start=admitted_education
    with engine.connect() as c:
        before=tuple(c.execute(text('SELECT state,authority_revision FROM education_monitor_claims')).one())
        counts=tuple(c.execute(text('SELECT count(*) FROM '+table)).scalar_one() for table in ('print_jobs','cost_source_lifetimes','education_audit_events'))
    assert start() is None
    assert bound(engine,uploaded['job_id']) is None
    with engine.connect() as c:
        assert tuple(c.execute(text('SELECT state,authority_revision FROM education_monitor_claims')).one())==before
        assert tuple(c.execute(text('SELECT count(*) FROM '+table)).scalar_one() for table in ('print_jobs','cost_source_lifetimes','education_audit_events'))==counts


@pytest.mark.parametrize('universal',[True,False])
def test_resume_revalidates_authority_changed_between_hint_and_lock(admitted_education,monkeypatch,universal):
    from core.dispatch_observation_executor import TransactionView
    from modules.organizations.education_policy import claim_monitor_observation
    engine,uploaded,name,start=admitted_education
    identifier=start();assert identifier is not None
    if not universal:
        with engine.begin() as conn:conn.execute(text('DELETE FROM printer_dispatch_claims'))
    original=TransactionView.execute
    changed=[]
    def drift(self,statement,parameters=None):
        if not changed and str(statement).startswith('UPDATE education_submissions SET updated_at=updated_at'):
            changed.append(True)
            original(self,text('UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1 WHERE id=:id'),{'id':uploaded['id']})
        return original(self,statement,parameters)
    monkeypatch.setattr(TransactionView,'execute',drift)
    with engine.begin() as conn:
        result=claim_monitor_observation(conn,print_job_id=identifier,printer_id=9,observed_filename=name)
        assert changed==[True]
        assert result['authorized'] is False and result['dispatch_receipt_binding']=='refused'
    assert bound(engine,uploaded['job_id'])==identifier


@pytest.mark.parametrize('admitted_education',['confirmed','reserved'],indirect=True)
def test_actual_bambu_extensionless_education_wire_name_binds_receipt(admitted_education):
    engine,uploaded,name,start=admitted_education
    identifier=start('mqtt',reported_name=name.removesuffix('.3mf'))
    assert identifier is not None
    assert bound(engine,uploaded['job_id'])==identifier


@pytest.mark.parametrize('terminal',['completed','cancelled','failed'])
def test_extensionless_education_terminal_allows_next_real_classroom_dispatch(admitted_education,review_db,tmp_path,monkeypatch,terminal):
    from modules.printers.monitors import mqtt_job_lifecycle
    from modules.printers import dispatch
    from tests.test_contracts.test_education_upload_scheduler_integration import _upload_and_approve,_schedule
    engine,uploaded,name,start=admitted_education
    wire=name.removesuffix('.3mf')
    identifier=start('mqtt',reported_name=wire)
    assert identifier is not None and bound(engine,uploaded['job_id'])==identifier
    def forbidden(**kw):pytest.fail('Education terminal must not send generic alerts')
    mqtt_job_lifecycle.record_job_ended(printer_id=9,printer_name='Owned fixture',current_job_id=identifier,
        linked_job_id=uploaded['job_id'],status=terminal,
        state={'subtask_name':wire,'gcode_file':'Metadata/plate_1.gcode','print_error':0},
        start_spool_weights={},dispatch_alert_fn=forbidden)
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).scalar_one()==terminal
    second=_upload_and_approve(review_db,tmp_path,'P1S')
    from modules.jobs.scheduler import Scheduler
    assert Scheduler().run(review_db,start_date=datetime.now(timezone.utc)+timedelta(minutes=1)).scheduled_count==1
    review_db.rollback()
    ok,reason=dispatch.dispatch_job(9,second['job_id'])
    assert ok,reason
    with engine.connect() as c:
        assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:job'),{'job':uploaded['job_id']}).scalar_one()==identifier
        assert c.execute(text('SELECT job_id FROM printer_dispatch_claims WHERE printer_id=9')).scalar_one()==second['job_id']
