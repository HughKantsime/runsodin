"""Real schema and handler boundaries, with notification/archive effects substituted."""
from contextlib import contextmanager
from sqlalchemy import text
import pytest
from tests.test_contracts.test_education_monitor_policy import monitor_db

STOPPING_ERROR=[{'attr':0x0C000300,'code':0x00030006}]

@pytest.fixture
def hms_boundary(monitor_db,monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from core.registry import registry
    from modules.notifications import error_handling
    from modules.archives import archive
    monitor_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,filename,status,started_at) VALUES(201,9,'Synthetic current','current.3mf','running',CURRENT_TIMESTAMP)"))
    monitor_db.commit()
    engine=monitor_db.get_bind()
    @contextmanager
    def database():
        raw=engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw,False)
        finally:
            raw.close()
    monkeypatch.setattr(error_handling,'get_db',database)
    monkeypatch.setattr(registry,'get_provider',lambda name:None)
    outgoing=[]
    monkeypatch.setattr(error_handling,'dispatch_alert',lambda *args,**kwargs:outgoing.append(kwargs))
    monkeypatch.setattr(archive,'create_print_archive',lambda *args,**kwargs:None)
    return monitor_db,error_handling,registry,outgoing


def test_classification_failure_suppresses_external_effects(hms_boundary,monkeypatch):
    db,handler,registry,outgoing=hms_boundary
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=13 WHERE id=201'))
    db.commit()
    class UnavailablePolicy:
        def classify_monitor_observation(self,*args,**kwargs):
            raise RuntimeError('Synthetic classification unavailable')
        def terminal_monitor_observation(self,*args,**kwargs):
            raise RuntimeError('Synthetic terminal classification unavailable')
    monkeypatch.setattr(registry,'get_provider',lambda name:UnavailablePolicy() if name=='EducationPolicyProvider' else None)
    result=handler.process_hms_errors(9,STOPPING_ERROR,observed_filename='current.3mf')
    assert outgoing==[]
    assert result['education_suppressed'] is True
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'


def test_delayed_error_does_not_fail_different_current_filename(hms_boundary):
    db,handler,registry,outgoing=hms_boundary
    handler.process_hms_errors(9,STOPPING_ERROR,observed_filename='previous.3mf')
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'
    assert not any(item.get('alert_type')=='print_failed' for item in outgoing)


def test_reserved_observation_without_policy_cannot_use_generic_terminal_path(hms_boundary):
    db,handler,registry,outgoing=hms_boundary
    filename='odin-'+'a'*32+'.3mf'
    db.execute(text('UPDATE print_jobs SET filename=:filename WHERE id=201'),{'filename':filename})
    db.commit()
    result=handler.process_hms_errors(9,STOPPING_ERROR,observed_filename=filename)
    db.expire_all()
    assert result['education_suppressed'] is True
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'
    assert outgoing==[]


@pytest.mark.parametrize('filename',['current.3mf','/Metadata/current.3mf',r'C:\Metadata\current.3mf'])
def test_matching_generic_filename_still_fails_current_job(hms_boundary,filename):
    db,handler,registry,outgoing=hms_boundary
    handler.process_hms_errors(9,STOPPING_ERROR,observed_filename=filename)
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='failed'
    assert len([item for item in outgoing if item.get('alert_type')=='print_failed'])==1


def test_unknown_observed_identity_does_not_fail_current_job(hms_boundary):
    db,handler,registry,outgoing=hms_boundary
    handler.process_hms_errors(9,STOPPING_ERROR)
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'
    assert not any(item.get('alert_type')=='print_failed' for item in outgoing)


def test_mismatched_education_token_preserves_authorized_current_submission(hms_boundary,monkeypatch):
    db,handler,registry,outgoing=hms_boundary
    from modules.organizations.education_policy import reserve_dispatch,confirm_dispatch_started
    from modules.organizations.services import EducationPolicyService
    reservation=reserve_dispatch(db,job_id=13,printer_id=9,expected_revision=3,extension='.gcode')
    assert reservation is not None
    db.commit()
    confirmed=confirm_dispatch_started(db,claim_id=reservation['claim_id'],submission_id=14,job_id=13,printer_id=9,expected_revision=3)
    assert confirmed['transitioned'] is True
    db.commit()
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=13,filename=:filename WHERE id=201'),{'filename':reservation['remote_filename']})
    db.commit()
    policy=EducationPolicyService()
    monkeypatch.setattr(registry,'get_provider',lambda name:policy if name=='EducationPolicyProvider' else None)
    before=db.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=14')).one()
    handler.process_hms_errors(9,STOPPING_ERROR,observed_filename='odin-'+'b'*32+'.gcode')
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'
    assert db.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=14')).one()==before
    assert outgoing==[]


@pytest.mark.parametrize('classification',[{},None,{'education_owned':'yes'}])
def test_invalid_classification_outcome_is_unresolved_before_effects(hms_boundary,monkeypatch,classification):
    db,handler,registry,outgoing=hms_boundary
    class InvalidPolicy:
        def classify_monitor_observation(self,*args,**kwargs):
            return classification
    monkeypatch.setattr(registry,'get_provider',lambda name:InvalidPolicy() if name=='EducationPolicyProvider' else None)
    result=handler.process_hms_errors(9,STOPPING_ERROR,observed_filename='current.3mf')
    assert result['classification_state']=='unresolved' and result['retryable'] is True
    assert outgoing==[]
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'


def test_callback_retries_unresolved_classification_then_deduplicates_success(hms_boundary,monkeypatch):
    from types import SimpleNamespace
    from modules.organizations.services import EducationPolicyService
    from modules.printers.monitors import mqtt_printer
    db,handler,registry,outgoing=hms_boundary
    calls=[]
    actual_policy=EducationPolicyService()
    class RecoveringPolicy:
        def classify_monitor_observation(self,*args,**kwargs):
            calls.append('classify')
            if len(calls)==1:
                raise RuntimeError('Synthetic initial classification failure')
            return actual_policy.classify_monitor_observation(*args,**kwargs)
        def terminal_monitor_observation(self,*args,**kwargs):
            return actual_policy.terminal_monitor_observation(*args,**kwargs)
    monkeypatch.setattr(registry,'get_provider',lambda name:RecoveringPolicy() if name=='EducationPolicyProvider' else None)
    clock=[1000.0]
    monkeypatch.setattr(mqtt_printer,'time',SimpleNamespace(time=lambda:clock[0]))
    monkeypatch.setattr(mqtt_printer,'get_db',handler.get_db)
    monkeypatch.setattr(mqtt_printer,'mqtt_republish',None)
    monkeypatch.setattr(mqtt_printer,'ws_push',lambda *args,**kwargs:None)
    monitor=mqtt_printer.PrinterMonitor(9,'Synthetic retry','192.0.2.1','synthetic','')
    monitor._camera_discovered=True
    monkeypatch.setattr(monitor,'_on_state_change',lambda *args:None)
    monkeypatch.setattr(monitor,'_check_spool_levels',lambda:None)
    packet=SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','subtask_name':'current.3mf','hms':STOPPING_ERROR}})
    monitor._on_status(packet)
    assert outgoing==[]
    assert getattr(monitor,'_last_hms_processed_key',None) is None
    clock[0]=1010
    monitor._on_status(packet)
    successful_calls=len(calls)
    assert successful_calls>1
    assert [item['alert_type'] for item in outgoing]==['printer_error','print_failed']
    db.expire_all()
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='failed'
    clock[0]=1020
    monitor._on_status(packet)
    assert len(calls)==successful_calls
    assert len(outgoing)==2


def test_replaced_claim_rejects_old_observed_token(hms_boundary):
    db,handler,registry,outgoing=hms_boundary
    from core.interfaces.education_policy import parse_education_token
    from modules.organizations.education_policy import education_token_digest
    from modules.organizations.education_policy import reserve_dispatch,confirm_dispatch_started,terminal_monitor_observation
    reservation=reserve_dispatch(db,job_id=13,printer_id=9,expected_revision=3,extension='.3mf')
    assert reservation is not None
    db.commit()
    assert confirm_dispatch_started(db,claim_id=reservation['claim_id'],submission_id=14,job_id=13,printer_id=9,expected_revision=3)['transitioned']
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL,filename=:filename WHERE id=201'),{'filename':reservation['remote_filename']})
    from modules.organizations.education_policy import claim_monitor_observation
    assert claim_monitor_observation(db,print_job_id=201,printer_id=9,observed_filename=reservation['remote_filename'])['authorized'] is True
    replacement=education_token_digest(parse_education_token('odin-'+'b'*32+'.3mf'))
    db.execute(text('UPDATE education_monitor_claims SET token_digest=:digest WHERE job_id=13'),{'digest':replacement})
    db.commit()
    result=terminal_monitor_observation(db,print_job_id=201,printer_id=9,terminal_status='failed',observed_filename=reservation['remote_filename'])
    assert result['transitioned'] is False
    assert result['authorized'] is False
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'
    assert db.execute(text('SELECT status FROM education_submissions WHERE id=14')).scalar_one()=='printing'
    assert db.execute(text('SELECT token_digest FROM education_monitor_claims WHERE job_id=13')).scalar_one()==replacement


def test_claim_digest_cas_rolls_back_partial_terminal_changes(hms_boundary,monkeypatch):
    db,handler,registry,outgoing=hms_boundary
    from modules.organizations import education_policy as policy
    reservation=policy.reserve_dispatch(db,job_id=13,printer_id=9,expected_revision=3,extension='.3mf')
    db.commit()
    assert policy.confirm_dispatch_started(db,claim_id=reservation['claim_id'],submission_id=14,job_id=13,printer_id=9,expected_revision=3)['transitioned']
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL,filename=:filename WHERE id=201'),{'filename':reservation['remote_filename']})
    from modules.organizations.education_policy import claim_monitor_observation
    assert claim_monitor_observation(db,print_job_id=201,printer_id=9,observed_filename=reservation['remote_filename'])['authorized'] is True
    db.commit()
    original=policy._ExecutionAdapter.execute
    reached=[]
    def replace_before_delete(adapter,statement,parameters=None):
        if statement.startswith('DELETE FROM education_monitor_claims'):
            reached.append(True)
            original(adapter,'UPDATE education_monitor_claims SET token_digest=:digest WHERE job_id=13',{'digest':'synthetic-replacement'})
        return original(adapter,statement,parameters)
    monkeypatch.setattr(policy._ExecutionAdapter,'execute',replace_before_delete)
    result=policy.terminal_monitor_observation(db,print_job_id=201,printer_id=9,terminal_status='failed',observed_filename=reservation['remote_filename'])
    assert reached==[True]
    assert result['transitioned'] is False and result['authorized'] is False
    assert db.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=14')).one()==('printing',4)
    assert db.execute(text('SELECT status FROM jobs WHERE id=13')).scalar_one()=='printing'
    assert db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()=='running'
    assert db.execute(text('SELECT count(*) FROM education_monitor_claims WHERE job_id=13')).scalar_one()==1


def test_unlinked_reserved_print_rejects_mismatched_observed_token(hms_boundary):
    db,handler,registry,outgoing=hms_boundary
    from modules.organizations.education_policy import terminal_monitor_observation
    stored='odin-'+'a'*32+'.3mf'
    db.execute(text('UPDATE print_jobs SET filename=:filename WHERE id=201'),{'filename':stored})
    db.commit()
    result=terminal_monitor_observation(db,print_job_id=201,printer_id=9,terminal_status='failed',observed_filename='odin-'+'b'*32+'.3mf')
    assert result['transitioned'] is False and result['authorized'] is False
    assert db.execute(text('SELECT status,error_code FROM print_jobs WHERE id=201')).one()==('running',None)


def test_terminal_token_retry_is_safe_without_claim_and_legacy_ack_is_preserved(hms_boundary):
    db,handler,registry,outgoing=hms_boundary
    from modules.organizations import education_policy as policy
    reservation=policy.reserve_dispatch(db,job_id=13,printer_id=9,expected_revision=3,extension='.3mf')
    db.commit()
    assert policy.confirm_dispatch_started(db,claim_id=reservation['claim_id'],submission_id=14,job_id=13,printer_id=9,expected_revision=3)['transitioned']
    db.execute(text('UPDATE print_jobs SET filename=:filename WHERE id=201'),{'filename':reservation['remote_filename']})
    assert policy.claim_monitor_observation(db,print_job_id=201,printer_id=9,observed_filename=reservation['remote_filename'])['authorized']
    db.commit()
    first=policy.terminal_monitor_observation(db,print_job_id=201,printer_id=9,terminal_status='failed',observed_filename=reservation['remote_filename'])
    assert first['transitioned'] is True and first['authorized'] is True
    db.commit()
    again=policy.terminal_monitor_observation(db,print_job_id=201,printer_id=9,terminal_status='failed',observed_filename=reservation['remote_filename'])
    assert again['transitioned'] is False and again['authorized'] is False
    legacy=policy.terminal_monitor_observation(db,print_job_id=201,printer_id=9,terminal_status='failed')
    assert legacy['transitioned'] is False and legacy['idempotent'] is True and legacy['authorized'] is True
    assert db.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=14')).one()==('failed',5)


def test_generic_to_education_linkage_retries_without_terminal_under_child_lock(hms_boundary,monkeypatch):
    db,handler,registry,outgoing=hms_boundary
    from modules.organizations.services import EducationPolicyService
    actual=EducationPolicyService()
    terminal=[];prepared=[]
    class GuardedPolicy:
        def classify_monitor_observation(self,*args,**kwargs):
            return actual.classify_monitor_observation(*args,**kwargs)
        def terminal_monitor_observation(self,*args,**kwargs):
            terminal.append(True)
            raise AssertionError('Education terminal under generic lock')
    monkeypatch.setattr(registry,'get_provider',lambda name:GuardedPolicy() if name=='EducationPolicyProvider' else None)
    original=handler.prepare_print_job_write
    def link_after_fence(conn,identifier):
        result=original(conn,identifier)
        conn.cursor().execute('UPDATE print_jobs SET scheduled_job_id=13 WHERE id=?',(identifier,))
        prepared.append(True)
        return result
    monkeypatch.setattr(handler,'prepare_print_job_write',link_after_fence)
    handler._fail_active_job_for_hms(9,'0C000300_00030006','Synthetic stop','current.3mf')
    assert prepared==[True,True,True]
    assert terminal==[] and outgoing==[]
    db.expire_all()
    assert db.execute(text('SELECT status,scheduled_job_id FROM print_jobs WHERE id=201')).one()==('running',None)


@pytest.mark.parametrize('change', ['filename','status','linkage'])
def test_unlinked_quarantine_cas_preserves_intervening_change(hms_boundary,monkeypatch,change):
    db,handler,registry,outgoing=hms_boundary
    from modules.organizations import education_policy as policy
    stored='odin-'+'a'*32+'.3mf'
    db.execute(text('UPDATE print_jobs SET filename=:filename WHERE id=201'),{'filename':stored})
    db.commit()
    original=policy._ExecutionAdapter.execute
    reached=[]
    def change_before_update(adapter,statement,parameters=None):
        if statement.startswith('UPDATE print_jobs SET status=:status'):
            reached.append(True)
            mutations={
                'filename':("UPDATE print_jobs SET filename=:filename WHERE id=201",{'filename':'odin-'+'b'*32+'.3mf'}),
                'status':("UPDATE print_jobs SET status='completed' WHERE id=201",{}),
                'linkage':("UPDATE print_jobs SET scheduled_job_id=13 WHERE id=201",{}),
            }
            original(adapter,*mutations[change])
        return original(adapter,statement,parameters)
    monkeypatch.setattr(policy._ExecutionAdapter,'execute',change_before_update)
    result=policy.terminal_monitor_observation(db,print_job_id=201,printer_id=9,terminal_status='failed',observed_filename=stored)
    assert reached==[True]
    assert result['transitioned'] is False
    row=db.execute(text('SELECT status,filename,scheduled_job_id FROM print_jobs WHERE id=201')).one()
    assert row[0]==('completed' if change=='status' else 'running')
    assert row[1]==('odin-'+'b'*32+'.3mf' if change=='filename' else stored)
    assert row[2]==(13 if change=='linkage' else None)
