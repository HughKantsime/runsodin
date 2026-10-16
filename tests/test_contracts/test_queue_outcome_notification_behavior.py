"""Actual scheduling and dispatch outcomes with only physical adapters substituted."""
from datetime import datetime,timezone
import pytest
from sqlalchemy import text
from core.base import AlertType
from modules.notifications.models import AlertPreference
from tests.test_contracts.test_education_review_workflow import review_db

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("enterprise_license")


@pytest.fixture
def quiet_outcomes(review_db,monkeypatch):
    from core import db,event_bus
    from core.event_bus import InMemoryEventBus
    from modules.notifications import alert_dispatcher
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    monkeypatch.setattr(db,'engine',review_db.get_bind())
    monkeypatch.setattr(event_bus,'_bus',InMemoryEventBus())
    monkeypatch.setattr(alert_dispatcher,'should_suppress_notification',lambda **_:True)
    return review_db


@pytest.mark.parametrize('reason',['unknown_quantity','no_fit','no_active_printers'])
@pytest.mark.parametrize('enabled',[False,True])
def test_scheduler_final_skipped_job_notifies_only_configured_creator(quiet_outcomes,reason,enabled):
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    db=quiet_outcomes
    for uid in [3,5]:db.add(AlertPreference(user_id=uid,alert_type=AlertType.QUEUE_SKIPPED,in_app=enabled,email=False,browser_push=False))
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,submitted_by,quantity,duration_hours,hold) VALUES(901,'Synthetic unscheduled','pending',1,3,3,:quantity,:duration,FALSE)"),
        {'quantity':None if reason=='unknown_quantity' else 1,'duration':240 if reason=='no_fit' else 1})
    if reason=='no_active_printers':db.execute(text('UPDATE printers SET is_active=FALSE'))
    db.commit()
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count==0
    assert result.skipped_count==1
    db.expire_all()
    rows=set(db.execute(text("SELECT user_id,job_id FROM alerts WHERE alert_type='queue_skipped'")).all())
    assert rows==({(3,901)} if enabled else set())
    if enabled and reason=='unknown_quantity':
        assert 'Set the job quantity' in db.execute(text('SELECT message FROM alerts WHERE job_id=901')).scalar_one()
    assert db.execute(text('SELECT status FROM jobs WHERE id=901')).scalar_one()=='pending'


@pytest.fixture
def dispatch_outcome(quiet_outcomes,tmp_path,monkeypatch):
    from modules.printers import dispatch, smart_plug, dispatch_acquisition
    from core import auth
    import secrets
    monkeypatch.setattr(auth, 'SECRET_KEY', secrets.token_hex(32))
    monkeypatch.setattr(smart_plug, 'engine', quiet_outcomes.get_bind())
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', lambda *args: True)
    db=quiet_outcomes
    payload=tmp_path/'synthetic.gcode';payload.write_text('; inert test payload\n')
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(911,'Synthetic dispatch model',1)"))
    db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,created_by) VALUES(912,911,'synthetic.gcode','synthetic.gcode',:path,1,3)"),{'path':str(payload)})
    db.execute(text("UPDATE printers SET api_type='moonraker',api_host='127.0.0.1',api_key=NULL WHERE id=9"))
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,status,printer_id,charged_to_org_id,charged_to_user_id,submitted_by) VALUES(913,911,'Synthetic queued dispatch','scheduled',9,1,3,3)"))
    db.commit()
    monkeypatch.setattr(dispatch,'_ws',lambda *args:None)
    return db,dispatch


@pytest.mark.parametrize('outcome',['rejected','unknown'])
@pytest.mark.parametrize('enabled',[False,True])
def test_actual_dispatch_outcome_creates_safe_creator_failed_start_alert(dispatch_outcome,monkeypatch,outcome,enabled):
    db,dispatch=dispatch_outcome
    for uid in [3,5]:db.add(AlertPreference(user_id=uid,alert_type=AlertType.QUEUE_FAILED_START,in_app=enabled,email=False,browser_push=False))
    db.commit()
    calls=[]
    def adapter(*args):
        calls.append(True)
        if outcome=='unknown':raise RuntimeError('Synthetic private adapter marker')
        return False,'Synthetic private adapter marker'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    success,message=dispatch.dispatch_job(9,913)
    assert success is False
    assert calls==[True]
    rows=db.execute(text("SELECT user_id,job_id,title,message FROM alerts WHERE alert_type='queue_failed_start'")).all()
    assert len(rows)==(1 if enabled else 0)
    if enabled:
        assert rows[0][0:2]==(3,913)
        assert 'Synthetic private adapter marker' not in rows[0][2]+rows[0][3]
        if outcome=='unknown':assert 'reconciliation' in rows[0][3].lower() and 'unknown' in rows[0][3].lower()
    assert db.execute(text('SELECT status FROM jobs WHERE id=913')).scalar_one()=='scheduled'


def test_unchanged_scheduler_outcome_deduplicates_but_new_reason_resurfaces(quiet_outcomes):
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    db=quiet_outcomes
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_SKIPPED,in_app=True,email=False,browser_push=False))
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,hold) VALUES(901,'Synthetic changing obstacle','pending',1,3,NULL,1,FALSE)"));db.commit()
    scheduler=Scheduler(SchedulerConfig(horizon_days=1));start=datetime(2026,10,12,12,tzinfo=timezone.utc)
    for _ in range(2):assert scheduler.run(db,start_date=start).skipped_count==1
    assert db.execute(text("SELECT COUNT(*) FROM alerts WHERE job_id=901 AND alert_type='queue_skipped'")).scalar_one()==1
    db.execute(text('UPDATE jobs SET quantity=1,duration_hours=240 WHERE id=901'));db.commit()
    assert scheduler.run(db,start_date=start).skipped_count==1
    assert db.execute(text("SELECT COUNT(*) FROM alerts WHERE job_id=901 AND alert_type='queue_skipped'")).scalar_one()==2


def test_same_transaction_queue_outcome_dedup_sees_staged_alert(quiet_outcomes):
    from core.base import AlertSeverity
    from modules.notifications import alert_dispatcher
    db=quiet_outcomes
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_FAILED_START,in_app=True,email=False,browser_push=False))
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id) VALUES(914,'Synthetic staged failure','scheduled',1,3)"));db.commit()
    first=alert_dispatcher.stage_alert(db,AlertType.QUEUE_FAILED_START,AlertSeverity.WARNING,'Synthetic failure',job_id=914,metadata={'reason':'adapter_unknown'},target_user_ids=[3])
    second=alert_dispatcher.stage_alert(db,AlertType.QUEUE_FAILED_START,AlertSeverity.WARNING,'Synthetic failure',job_id=914,metadata={'reason':'adapter_unknown'},target_user_ids=[3])
    assert first.alerts_created==1 and second.alerts_created==0
    db.commit()
    assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=914')).scalar_one()==1


@pytest.mark.parametrize('operation',['stage','run_log','commit','uncertain_commit'])
def test_scheduler_alert_and_run_record_failures_preserve_atomicity(quiet_outcomes,monkeypatch,operation):
    from sqlalchemy.orm import Session
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    from modules.jobs.models import SchedulerRun
    from modules.notifications import alert_dispatcher
    db=quiet_outcomes
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_SKIPPED,in_app=True,email=False,browser_push=False))
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,hold) VALUES(901,'Synthetic skip','pending',1,3,NULL,1,FALSE),(902,'Synthetic schedulable','pending',1,3,1,1,FALSE)"));db.execute(text('UPDATE jobs SET priority=3 WHERE id IN (901,902)'));db.commit()
    delivered=[];monkeypatch.setattr(alert_dispatcher,'deliver_staged_alert',lambda *args:delivered.append(True))
    def fail(*args,**kwargs):raise RuntimeError('Synthetic scheduler transaction failure')
    if operation=='stage':monkeypatch.setattr(alert_dispatcher,'stage_alert',fail)
    elif operation=='run_log':
        original=Session.add
        def add(session,row,*args,**kwargs):
            if isinstance(row,SchedulerRun):fail()
            return original(session,row,*args,**kwargs)
        monkeypatch.setattr(Session,'add',add)
    else:
        original=Session.commit
        def commit(session):
            if operation=='uncertain_commit':original(session)
            fail()
        monkeypatch.setattr(Session,'commit',commit)
    with pytest.raises(RuntimeError,match='Synthetic scheduler'):
        Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert delivered==[]
    db.rollback()
    expected=operation=='uncertain_commit'
    assert db.execute(text('SELECT status FROM jobs WHERE id=902')).scalar_one()==('scheduled' if expected else 'pending')
    assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=901')).scalar_one()==int(expected)
    assert db.execute(text('SELECT COUNT(*) FROM scheduler_runs')).scalar_one()==int(expected)


def test_rejected_printer_with_later_fit_is_not_a_skipped_job(quiet_outcomes):
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    db=quiet_outcomes
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_SKIPPED,in_app=True,email=False,browser_push=False))
    db.execute(text("UPDATE printers SET tags=:tags WHERE id=:id"),[{'id':9,'tags':'["red"]'},{'id':10,'tags':'["blue"]'}])
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,hold,priority,required_tags) VALUES(901,'Synthetic eventual fit','pending',1,3,1,1,FALSE,3,'[\"blue\"]')"));db.commit()
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.scheduled_count==1 and result.skipped_count==0
    assert db.execute(text('SELECT printer_id FROM jobs WHERE id=901')).scalar_one()==10
    assert db.execute(text("SELECT COUNT(*) FROM alerts WHERE alert_type='queue_skipped'")).scalar_one()==0


@pytest.mark.parametrize('change',['completed','held'])
def test_skipped_observation_rechecks_final_status_before_alert(quiet_outcomes,monkeypatch,change):
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    from modules.notifications import alert_dispatcher
    db=quiet_outcomes
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_SKIPPED,in_app=True,email=False,browser_push=False))
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic changed state','pending',1,3,NULL,1,FALSE,3)"));db.commit()
    original=alert_dispatcher.lock_queue_alert
    def lock(session,identifier,typ):
        if identifier==901:
            session.execute(text("UPDATE jobs SET status='completed' WHERE id=901") if change=='completed' else text('UPDATE jobs SET hold=TRUE WHERE id=901'))
        return original(session,identifier,typ)
    monkeypatch.setattr(alert_dispatcher,'lock_queue_alert',lock)
    result=Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc))
    assert result.skipped_count==1
    assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=901')).scalar_one()==0


def test_competing_sqlite_notification_connections_deduplicate_zero_row_lock(quiet_outcomes,monkeypatch):
    from threading import Event,get_ident
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import event
    from sqlalchemy.orm import Session
    from core.base import AlertSeverity
    from modules.notifications import alert_dispatcher
    db=quiet_outcomes;engine=db.get_bind()
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_FAILED_START,in_app=True,email=False,browser_push=False))
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id) VALUES(914,'Synthetic concurrent notice','scheduled',1,3)"));db.commit()
    held=Event();release=Event();second_attempted=Event();second_completed=Event();peer={}
    original=alert_dispatcher._deliver_in_app
    def add(*args,**kwargs):
        alert=original(*args,**kwargs);held.set()
        assert release.wait(3)
        return alert
    monkeypatch.setattr(alert_dispatcher,'_deliver_in_app',add)
    def observe(connection,cursor,statement,parameters,context,executemany):
        if get_ident()==peer.get('id') and statement.startswith('UPDATE alerts SET is_read=is_read'):
            second_attempted.set()
    event.listen(engine,'before_cursor_execute',observe)
    def notify(second=False):
        if second:peer['id']=get_ident()
        with Session(engine) as session:
            plan=alert_dispatcher.stage_alert(session,AlertType.QUEUE_FAILED_START,AlertSeverity.WARNING,'Synthetic contention',
                job_id=914,metadata={'reason':'adapter_unknown'},target_user_ids=[3])
            session.commit()
        if second:second_completed.set()
        return plan.alerts_created
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(notify)
            assert held.wait(3)
            second=executor.submit(notify,True)
            try:
                assert second_attempted.wait(3)
                assert not second_completed.wait(.05)
            finally:release.set()
            assert first.result(timeout=5)==1
            assert second.result(timeout=5)==0
    finally:
        release.set();event.remove(engine,'before_cursor_execute',observe)
    assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=914')).scalar_one()==1


@pytest.mark.parametrize('failure',['stage','commit','uncertain_commit'])
def test_dispatch_notification_failure_never_changes_or_retries_physical_result(dispatch_outcome,monkeypatch,caplog,failure):
    from sqlalchemy.orm import Session
    from modules.notifications import alert_dispatcher
    db,dispatch=dispatch_outcome
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_FAILED_START,in_app=True,email=False,browser_push=False));db.commit()
    calls=[]
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:(calls.append(True) or False,'Synthetic adapter rejected'))
    events=[];monkeypatch.setattr(alert_dispatcher,'deliver_staged_alert',lambda *args:events.append(True))
    def fail(*args,**kwargs):raise RuntimeError('Synthetic private notification exception marker')
    if failure=='stage':monkeypatch.setattr(alert_dispatcher,'stage_alert',fail)
    else:
        original=Session.commit
        def commit(session):
            if failure=='uncertain_commit':original(session)
            fail()
        monkeypatch.setattr(Session,'commit',commit)
    success,message=dispatch.dispatch_job(9,913)
    assert success is False and message.startswith('Synthetic adapter rejected') and 'reconciliation' in message
    assert calls==[True] and events==[]
    assert 'Synthetic private notification exception marker' not in caplog.text
    db.rollback()
    assert db.execute(text('SELECT status FROM jobs WHERE id=913')).scalar_one()=='scheduled'
    assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=913')).scalar_one()==int(failure=='uncertain_commit')


def test_dispatch_precommand_file_denial_does_not_emit_physical_start_failure(dispatch_outcome,monkeypatch):
    from pathlib import Path
    db,dispatch=dispatch_outcome
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_FAILED_START,in_app=True,email=False,browser_push=False));db.commit()
    Path(db.execute(text('SELECT stored_path FROM print_files WHERE id=912')).scalar_one()).unlink()
    calls=[];monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:calls.append(True))
    success,message=dispatch.dispatch_job(9,913)
    assert success is False and calls==[]
    assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=913')).scalar_one()==0


def test_dispatch_creator_change_after_adapter_does_not_redirect_alert(dispatch_outcome,monkeypatch):
    db,dispatch=dispatch_outcome
    for uid in [3,5]:db.add(AlertPreference(user_id=uid,alert_type=AlertType.QUEUE_FAILED_START,in_app=True,email=False,browser_push=False))
    db.commit()
    def adapter(*args):
        with db.get_bind().begin() as connection:
            connection.execute(text('UPDATE jobs SET charged_to_org_id=2,charged_to_user_id=5,submitted_by=5 WHERE id=913'))
        return False,'Synthetic adapter rejected'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    assert dispatch.dispatch_job(9,913)[0] is False
    assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=913')).scalar_one()==0


def test_accepted_command_state_failure_requires_reconciliation_without_second_command(dispatch_outcome,monkeypatch):
    from core import order_production
    db,dispatch=dispatch_outcome
    db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_FAILED_START,in_app=True,email=False,browser_push=False));db.commit()
    calls=[];monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:(calls.append(True) or True,'Synthetic accepted'))
    original = order_production.prepare_job_write
    def fail(*args, **kwargs):
        if calls: raise RuntimeError('Synthetic state recording failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(order_production,'prepare_job_write',fail)
    success,message=dispatch.dispatch_job(9,913)
    assert success is False and calls==[True]
    assert 'reconciliation' in message
    alert=db.execute(text('SELECT message FROM alerts WHERE job_id=913')).scalar_one()
    assert 'accepted' in alert and 'reconciliation' in alert
    assert db.execute(text('SELECT status FROM jobs WHERE id=913')).scalar_one()=='scheduled'
    assert db.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one()=='uncertain'
    db.rollback()
    assert not dispatch.dispatch_job(9,913)[0]
    assert calls==[True]


from tests.test_contracts.test_mqtt_protocol_peer_behavior import mqtt_peer

@pytest.mark.parametrize('origin',['scheduler','dispatch'])
def test_outcome_origin_reaches_owned_actual_mqtt_wire(dispatch_outcome,mqtt_peer,monkeypatch,origin):
    import json
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from modules.notifications import mqtt_republish,quiet_hours
    from modules.jobs.scheduler import Scheduler,SchedulerConfig
    db,dispatch=dispatch_outcome
    typ=AlertType.QUEUE_SKIPPED if origin=='scheduler' else AlertType.QUEUE_FAILED_START
    db.add(AlertPreference(user_id=3,alert_type=typ,in_app=True,email=False,browser_push=False))
    if origin=='scheduler':
        db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,hold,priority) VALUES(901,'Synthetic MQTT skip','pending',1,3,NULL,1,FALSE,3)"))
    port,packets,disconnected,published,_=mqtt_peer
    for key,value in {'mqtt_republish_enabled':'true','mqtt_republish_host':'127.0.0.1',
            'mqtt_republish_port':str(port),'mqtt_republish_topic_prefix':'synthetic/outcome'}.items():
        db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':key,'value':value})
    db.commit()
    monkeypatch.setattr(mqtt_republish,'engine',db.get_bind());monkeypatch.setattr(mqtt_republish,'_config_cache',None)
    monkeypatch.setattr(mqtt_republish,'_client',None);monkeypatch.setattr(quiet_hours,'engine',db.get_bind())
    monkeypatch.setattr(quiet_hours,'_config_cache',None)
    bus=InMemoryEventBus();monkeypatch.setattr(event_bus,'_bus',bus);mqtt_republish.register_subscribers(bus)
    try:
        if origin=='scheduler':
            assert Scheduler(SchedulerConfig(horizon_days=1)).run(db,start_date=datetime(2026,10,12,12,tzinfo=timezone.utc)).skipped_count==1
        else:
            calls=[];monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:(calls.append(True) or False,'Synthetic rejected'))
            assert dispatch.dispatch_job(9,913)[0] is False
            assert calls==[True]
        assert published.wait(3)
        frames=[(header,body) for header,body in packets if header>>4==3]
        assert len(frames)==1
        header,body=frames[0];length=int.from_bytes(body[:2],'big')
        assert body[2:2+length].decode()=='synthetic/outcome/alerts'
        payload=json.loads(body[2+length:]);assert payload['type']==typ.value
        assert '_ws_user_ids' not in payload and 'job_id' not in payload
    finally:mqtt_republish.disconnect()
