"""Remaining Foundation acceptance: legacy observations, durable sends and crashes."""
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from core.job_timeline import TimelineWriter, event, history_complete, now
from core.job_terminal_observations import claim_terminal, dispatch_terminal_alert
from tests.test_contracts.test_post027_migration_040 import engine
from tests.test_contracts.test_job_timeline_foundation import seed
from tests.test_contracts.test_private_attempt_valuation_route import private_attempt, creators, review_db
from tests.test_contracts.test_automatic_power_terminal_delivery import terminal_environment, auto_power, monitor_db


def wait_for(predicate):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.01)
    pytest.fail('Writer did not reach expected state')


@pytest.mark.parametrize('source',['mqtt','moonraker','generic'])
@pytest.mark.parametrize('outcome',['completed','failed','cancelled'])
@pytest.mark.parametrize('unbound',[False,True])
def test_missing_identity_terminal_is_durable_without_claim(terminal_environment,monkeypatch,request,source,outcome,unbound):
    engine,_,_,_,mqtt,moon=terminal_environment
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import job_events
    from core import job_timeline
    writer=TimelineWriter(engine.url)
    request.addfinalizer(writer.close)
    monkeypatch.setattr(job_timeline,'_writer',writer)
    with engine.begin() as c:
        if unbound:c.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=553'))
        assert c.execute(text('SELECT economic_uid FROM print_jobs WHERE id=553')).scalar_one() is None
    @contextmanager
    def database():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(job_events,'get_db',database)
    for name in ('dispatch_alert','record_error','increment_care_counters','_start_bed_cooled_monitor'):
        monkeypatch.setattr(job_events,name,lambda *a,**kw:None)
    if source=='mqtt':
        mqtt.record_job_ended(551,'Owned terminal',553,None if unbound else 552,outcome,{}, {},lambda **kw:None)
    elif source=='moonraker':
        monitor=moon.MoonrakerMonitor(551,'Owned terminal','127.0.0.1')
        monitor._current_job_db_id=553
        monitor._job_ended(outcome,SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None))
    elif outcome=='cancelled':job_events.job_cancelled(551,553)
    else:job_events.job_completed(551,553,success=outcome=='completed')
    writer.close()
    with engine.connect() as c:
        linked=c.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=553')).scalar_one()
        if linked is not None:
            assert c.execute(text('SELECT kind,print_attempt_uid FROM job_timeline_events WHERE job_id=:job AND kind=:kind'),{'job':linked,'kind':outcome}).first()==(outcome,None)
        assert c.execute(text('SELECT status,ended_at FROM print_jobs WHERE id=553')).first()[0]==outcome
        assert c.execute(text('SELECT ended_at FROM print_jobs WHERE id=553')).scalar_one() is not None
        assert not claim_terminal(c,553)
        assert c.execute(text('SELECT count(*) FROM job_summary_outbox')).scalar_one()==0
        assert c.execute(text("SELECT count(*) FROM alert_outbox WHERE alert_kind LIKE 'terminal:%'")).scalar_one()==0


@pytest.mark.parametrize('outcome',['completed','failed'])
@pytest.mark.parametrize('crash_before_send',[True,False])
def test_terminal_claim_drains_034_once_with_mobile(private_attempt,review_db,monkeypatch,outcome,crash_before_send):
    from core import db as core_db
    from modules.notifications import alert_outbox,alert_dispatcher
    from modules.push import fanout
    identifier=private_attempt[3]
    engine=review_db.get_bind()
    monkeypatch.setattr(core_db,'engine',engine)
    external=[];mobile=[]
    monkeypatch.setattr(alert_dispatcher,'_deliver_staged_alert',lambda db,plan:external.append(plan))
    monkeypatch.setattr(alert_outbox,'_deliveries',[])
    monkeypatch.setattr(fanout,'deliver_alert',lambda db,plan:mobile.append(plan))
    review_db.execute(text('UPDATE print_jobs SET status=:status,ended_at=CURRENT_TIMESTAMP WHERE id=:id'),{'status':outcome,'id':identifier})
    assert claim_terminal(review_db.connection(),identifier)
    review_db.commit()
    outbox_id=review_db.execute(text("SELECT id FROM alert_outbox WHERE alert_kind=:kind"),{'kind':'terminal:'+str(identifier)}).scalar_one()
    review_db.rollback()
    fallback=Mock()
    if crash_before_send:assert alert_outbox.dispatch(engine,outbox_id)
    else:
        from core.base import AlertType,AlertSeverity
        dispatch_terminal_alert(identifier,fallback,alert_type=AlertType.PRINT_COMPLETE if outcome=='completed' else AlertType.PRINT_FAILED,
            severity=AlertSeverity.INFO if outcome=='completed' else AlertSeverity.CRITICAL,title='Original monitor title',message='Original monitor message',printer_id=None,job_id=None,metadata={'progress_percent':42})
        assert external[0].title=='Original monitor title'
    fallback.assert_not_called()
    assert len(external)==len(mobile)==1
    assert external[0] is mobile[0]
    assert external[0].alert_type.value==('print_complete' if outcome=='completed' else 'print_failed')
    assert not alert_outbox.dispatch(engine,outbox_id)
    assert len(mobile)==1
    with engine.connect() as c:
        assert not claim_terminal(c,identifier)


def test_education_claim_has_no_generic_alert_intent(private_attempt,review_db):
    identifier=private_attempt[3]
    review_db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=:id"),{'id':identifier})
    assert claim_terminal(review_db.connection(),identifier,deliver_alerts=False)
    review_db.commit()
    assert review_db.execute(text("SELECT count(*) FROM alert_outbox WHERE alert_kind LIKE 'terminal:%'")).scalar_one()==0


def test_startup_registration_and_clean_exit_before_first_event(engine):
    writer=TimelineWriter(engine.url,process_name='startup-test')
    with engine.connect() as c:
        state=c.execute(text('SELECT process_id,clean_shutdown FROM timeline_writer_state')).one()
        assert state==(writer.process_id,0)
    writer.close()
    with engine.connect() as c:
        assert c.execute(text('SELECT clean_shutdown FROM timeline_writer_state')).scalar_one()==1


def test_actual_crash_restart_window_is_incomplete(engine):
    seed(engine)
    code='''import os,sys
from sqlalchemy.engine import make_url
from core.job_timeline import TimelineWriter,event
writer=TimelineWriter(make_url(sys.argv[1]),process_name="crash-test")
writer.enqueue(event(1,"paused",printer_id=1))
import time
time.sleep(.4)
os._exit(17)
'''
    # Only the database URL and source path enter this owned subprocess.
    result=subprocess.run([sys.executable,'-c',"import sys;sys.path.insert(0,'backend');"+code,str(engine.url)],capture_output=True)
    assert result.returncode==17
    restart=TimelineWriter(engine.url,process_name='crash-test')
    try:
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM timeline_writer_state')).scalar_one()==2
            crashed=c.execute(text("SELECT started_at,last_drained_at FROM timeline_writer_state WHERE clean_shutdown=0 AND process_id!=:pid"),{'pid':restart.process_id}).one()
            # The old writer drained beyond this end, but the unclean restart
            # still intersects the attempt and must invalidate completeness.
            start=crashed[0]
            end=crashed[1]
            assert not history_complete(c,1,start,end)
    finally:restart.close()


def test_injected_failure_advances_durable_error_epoch(engine,monkeypatch):
    seed(engine)
    writer=TimelineWriter(engine.url)
    original=writer.engine.begin
    try:
        def fail():raise RuntimeError('injected writer failure')
        monkeypatch.setattr(writer.engine,'begin',fail)
        writer.enqueue(event(1,'paused',printer_id=1))
        wait_for(lambda:writer.error_epoch>0)
        epoch=writer.error_epoch
        monkeypatch.setattr(writer.engine,'begin',original)
        writer.wake.set()
        def persisted():
            with engine.connect() as c:
                return c.execute(text('SELECT error_epoch FROM timeline_writer_state')).scalar_one()>=epoch
        wait_for(persisted)
    finally:writer.close()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM job_timeline_events')).scalar_one()==1
        assert not history_complete(c,1,writer.started,now()-timedelta(milliseconds=1))


def test_dispatch_progress_and_telemetry_latency_under_writer_lock(engine,monkeypatch):
    from core import job_timeline
    from modules.printers import dispatch
    seed(engine)
    writer=TimelineWriter(engine.url,capacity=4096,busy_timeout_ms=50)
    monkeypatch.setattr(job_timeline,'_writer',writer)
    delivered=[]
    monkeypatch.setattr(dispatch,'_ws_push',lambda kind,payload:delivered.append(payload['status']))
    def produce(connection):
        started=time.monotonic()
        for _ in range(100):
            dispatch._ws(1,'uploading','Progress')
            job_timeline.for_printer(1,'paused')
        # Real dispatch-owned write remains in its original transaction.
        connection.execute(text("UPDATE jobs SET status='printing' WHERE id=1"))
        return time.monotonic()-started
    try:
        with engine.begin() as c:baseline=produce(c)
        with engine.connect() as c:
            c.exec_driver_sql('BEGIN IMMEDIATE')
            c.execute(text("UPDATE jobs SET item_name='dispatch owns lock' WHERE id=1"))
            elapsed=produce(c)
            wait_for(lambda:writer.error_epoch>0)
            assert elapsed<max(.1,baseline*5)
            assert c.execute(text('SELECT item_name FROM jobs WHERE id=1')).scalar_one()=='dispatch owns lock'
            assert delivered==['uploading']*200
            c.commit()
    finally:writer.close()
    with engine.connect() as c:
        assert c.execute(text('SELECT status,item_name FROM jobs WHERE id=1')).one()==('printing','dispatch owns lock')


def test_all_runtime_entrypoints_register_before_monitor_work():
    import ast
    for filename in ('mqtt_monitor','anycubic_lan_monitor','prusalink_monitor','elegoo_monitor','moonraker_monitor'):
        source=Path(f'backend/modules/printers/monitors/{filename}.py').read_text()
        tree=ast.parse(source)
        calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='start' and node.args and isinstance(node.args[0],ast.Constant) and node.args[0].value==filename]
        assert len(calls)==1
    assert "start('vision_monitor')" in Path('backend/modules/vision/monitor.py').read_text()
    source=Path('backend/core/app.py').read_text()
    assert source.index('start("api", engine.url)')<source.index('outbox_task =')
    assert 'finally:\n            shutdown()' in source


@pytest.mark.parametrize('source,outcome',[(source,outcome) for source in ('mqtt','moonraker','generic') for outcome in ('completed','failed')]+[('moonraker','cancelled')])
def test_actual_identified_monitor_terminal_uses_durable_mobile(private_attempt,review_db,monkeypatch,source,outcome):
    from core import db as core_db
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import job_events,alert_dispatch,alert_dispatcher,alert_outbox,event_dispatcher
    from modules.printers.monitors import mqtt_job_lifecycle as mqtt,moonraker_monitor as moon
    from modules.archives import archive
    from modules.push import fanout
    identifier=private_attempt[3]
    printer_id,job_id=review_db.execute(text('SELECT printer_id,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).one()
    review_db.rollback()
    engine=review_db.get_bind()
    monkeypatch.setattr(core_db,'engine',engine)
    monkeypatch.setattr(moon,'engine',engine)
    @contextmanager
    def database():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(mqtt,'get_db',database)
    monkeypatch.setattr(job_events,'get_db',database)
    monkeypatch.setattr(archive,'create_print_archive',lambda **kw:None)
    for module in (event_dispatcher,job_events):
        for name in ('record_error','increment_care_counters'):
            monkeypatch.setattr(module,name,lambda *a,**kw:None)
    monkeypatch.setattr(alert_dispatch,'_start_bed_cooled_monitor',lambda *a:None)
    monkeypatch.setattr(job_events,'_start_bed_cooled_monitor',lambda *a:None)
    for module in (event_dispatcher,job_events,alert_dispatch):
        def unexpected_raw_send(*a,**kw):pytest.fail('Identified terminal bypassed durable drain')
        monkeypatch.setattr(module,'dispatch_alert',unexpected_raw_send)
    external=[];mobile=[]
    monkeypatch.setattr(alert_dispatcher,'_deliver_staged_alert',lambda db,plan:external.append(plan))
    monkeypatch.setattr(alert_outbox,'_deliveries',[])
    monkeypatch.setattr(fanout,'deliver_alert',lambda db,plan:mobile.append(plan))
    if source=='mqtt':mqtt.record_job_ended(printer_id,'Owned',identifier,job_id,outcome,{}, {},lambda **kw:None)
    elif source=='moonraker':
        monitor=moon.MoonrakerMonitor(printer_id,'Owned','127.0.0.1')
        monitor._current_job_db_id=identifier
        monitor._job_ended(outcome,SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None))
    else:job_events.job_completed(printer_id,identifier,success=outcome=='completed')
    assert len(external)==len(mobile)==1
    assert mobile[0].printer_id==printer_id
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==outcome
        assert c.execute(text('SELECT count(*) FROM job_summary_outbox')).scalar_one()==1
        assert c.execute(text("SELECT count(*) FROM alert_outbox WHERE alert_kind LIKE 'terminal:%'")).scalar_one()==0


def test_clean_shutdown_drains_multiple_batches(engine):
    seed(engine)
    writer=TimelineWriter(engine.url,capacity=512)
    for _ in range(200):writer.enqueue(event(1,'paused',printer_id=1))
    writer.close()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM job_timeline_events')).scalar_one()==200
        assert c.execute(text('SELECT clean_shutdown FROM timeline_writer_state')).scalar_one()==1


def test_process_start_registration_is_idempotent_and_exit_marks_clean(engine):
    code='''import sys
sys.path.insert(0,"backend")
from sqlalchemy import create_engine,text
from core.job_timeline import start
engine=create_engine(sys.argv[1])
writer=start("api",engine.url)
assert start("api",engine.url) is writer
with engine.connect() as c:
    assert c.execute(text("SELECT count(*) FROM timeline_writer_state")).scalar_one()==1
    assert c.execute(text("SELECT clean_shutdown FROM timeline_writer_state")).scalar_one()==0
'''
    result=subprocess.run([sys.executable,'-c',code,str(engine.url)],capture_output=True)
    assert result.returncode==0,result.stderr.decode()
    with engine.connect() as c:
        assert c.execute(text('SELECT clean_shutdown FROM timeline_writer_state')).scalar_one()==1


def test_crash_replay_keeps_operator_terminal_alert_printer_scoped(private_attempt,review_db,monkeypatch):
    from modules.notifications import alert_outbox,alert_dispatcher
    identifier=private_attempt[3]
    review_db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=:id"),{'id':identifier})
    assert claim_terminal(review_db.connection(),identifier,alert_job_id=None)
    review_db.commit()
    outbox_id=review_db.execute(text("SELECT id FROM alert_outbox WHERE alert_kind=:kind"),{'kind':'terminal:'+str(identifier)}).scalar_one()
    review_db.rollback()
    delivered=[]
    monkeypatch.setattr(alert_dispatcher,'_deliver_staged_alert',lambda db,plan:delivered.append(plan))
    monkeypatch.setattr(alert_outbox,'deliver_mobile',lambda db,plan:None)
    assert alert_outbox.dispatch(review_db.get_bind(),outbox_id)
    assert len(delivered)==1 and delivered[0].job_id is None


@pytest.mark.parametrize('old_clean',[0,1])
def test_new_attempt_after_restart_can_have_complete_history(engine,old_clean):
    with engine.begin() as c:
        for pid,born,clean,drained in (
            ('monitor:old','2026-10-25 11:00:00',old_clean,'2026-10-25 12:00:00'),
            ('monitor:new','2026-10-25 12:02:00',0,'2026-10-25 12:10:00')):
            c.execute(text('INSERT INTO timeline_writer_state(process_id,started_at,clean_shutdown,last_drained_at,printer_ids) VALUES(:pid,:born,:clean,:drained,:printers)'),
                dict(pid=pid,born=born,clean=clean,drained=drained,printers='[1]'))
        assert history_complete(c,1,'2026-10-25 12:03:00','2026-10-25 12:04:00')
