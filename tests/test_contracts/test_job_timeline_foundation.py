"""Writer isolation, bounded backpressure, mutability and deletion contracts."""
import ast
from datetime import timedelta
import json
from pathlib import Path
import time
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from core.job_timeline import TimelineWriter, event, history_complete, now
from tests.test_contracts.test_post027_migration_040 import engine


def seed(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(11,'actor','unused','operator',TRUE)"))
        c.execute(text("INSERT INTO printers(id,name) VALUES(1,'one')"))
        c.execute(text("INSERT INTO jobs(id,item_name,status,printer_id) VALUES(1,'one','printing',1)"))


def test_no_shared_connection_helpers():
    path=Path('backend/core/job_timeline.py')
    tree=ast.parse(path.read_text())
    forbidden={'dispatch_transaction','dispatch_scope','get_db','SessionLocal','TransactionView','live_engine'}
    for node in ast.walk(tree):
        if isinstance(node,ast.Name):assert node.id not in forbidden
        if isinstance(node,ast.Attribute):assert node.attr not in forbidden
        if isinstance(node,ast.ImportFrom):assert node.module not in ('core.dispatch_connection','core.db_utils')

@pytest.mark.parametrize('kind,detail,code',[
    ('paused',{'filename':'private'},None),('hms',{},'a path/secret'),
    ('completed',{'duration_seconds':float('nan')},None),
    ('bed_clear_confirmed',{},None),('runout',{'slot':True},None)])
def test_typed_allowlist_refuses_arbitrary_payload(kind,detail,code):
    with pytest.raises(ValueError):event(1,kind,detail=detail,code=code)


def test_writer_contention_does_not_block_dispatch_or_telemetry(engine):
    seed(engine)
    writer=TimelineWriter(engine.url,capacity=8,busy_timeout_ms=50)
    try:
        with engine.connect() as lock:
            lock.exec_driver_sql('BEGIN IMMEDIATE')
            lock.execute(text("UPDATE jobs SET item_name='uncommitted' WHERE id=1"))
            start=time.monotonic()
            for _ in range(1000):writer.enqueue(event(1,'paused',printer_id=1))
            elapsed=time.monotonic()-start
            assert elapsed<.25
            time.sleep(.15)
            assert lock.execute(text('SELECT item_name FROM jobs WHERE id=1')).scalar_one()=='uncommitted'
            assert len(writer.queue)<=8 and writer.drop_epoch>0 and writer.error_epoch>0
            lock.commit()
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            with engine.connect() as c:
                if c.execute(text('SELECT count(*) FROM job_timeline_events')).scalar_one():break
            time.sleep(.02)
        else:pytest.fail('Writer did not recover after contention')
    finally:writer.close()
    with engine.connect() as c:
        state=c.execute(text('SELECT clean_shutdown,drop_epoch,error_epoch FROM timeline_writer_state')).one()
        assert state[0]==1 and state[1]>0 and state[2]>0
        assert not history_complete(c,1,writer.started-timedelta(seconds=1),now())


def test_timeline_only_allows_actor_erasure_printer_null_and_job_cascade(engine):
    seed(engine)
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=ON')
        c.execute(text("INSERT INTO job_timeline_events(id,job_id,printer_id,kind,actor_user_id,occurred_at) VALUES('a',1,1,'bed_clear_confirmed',11,CURRENT_TIMESTAMP)"))
        for statement in ("UPDATE job_timeline_events SET detail='private'", "UPDATE job_timeline_events SET actor_user_id=12", "UPDATE job_timeline_events SET kind='paused'"):
            with pytest.raises(IntegrityError):c.exec_driver_sql(statement)
        c.execute(text('UPDATE job_timeline_events SET actor_user_id=NULL WHERE actor_user_id=11'))
        c.execute(text('DELETE FROM printers WHERE id=1'))
        assert c.execute(text('SELECT printer_id,actor_user_id FROM job_timeline_events')).one()==(None,None)
        c.execute(text('DELETE FROM jobs WHERE id=1'))
        assert c.execute(text('SELECT count(*) FROM job_timeline_events')).scalar_one()==0


def test_sqlite_backup_roundtrip_keeps_timeline_and_writer_state(engine,tmp_path):
    import sqlite3
    from modules.system import backup_service as b
    seed(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO job_timeline_events(id,job_id,kind,occurred_at) VALUES('roundtrip',1,'completed',CURRENT_TIMESTAMP)"))
        c.execute(text("INSERT INTO timeline_writer_state(process_id,started_at,clean_shutdown,last_drained_at) VALUES('roundtrip',CURRENT_TIMESTAMP,1,CURRENT_TIMESTAMP)"))
    candidate,metadata=b.create_online_backup(str(engine.url))
    try:
        assert b.validate_database(candidate)==metadata
        with sqlite3.connect(candidate) as copy:
            assert copy.execute("SELECT kind FROM job_timeline_events WHERE id='roundtrip'").fetchone()==('completed',)
            assert copy.execute("SELECT clean_shutdown FROM timeline_writer_state WHERE process_id='roundtrip'").fetchone()==(1,)
    finally:candidate.unlink(missing_ok=True)


def test_organization_job_cleanup_cascades_history(engine):
    from sqlalchemy.orm import Session
    from modules.jobs.services import delete_org_jobs
    seed(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE jobs SET charged_to_org_id=41 WHERE id=1'))
        c.execute(text("INSERT INTO job_timeline_events(id,job_id,org_id,kind,occurred_at) VALUES('org',1,41,'paused',CURRENT_TIMESTAMP)"))
    with Session(engine) as db:
        delete_org_jobs(db,41);db.commit()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM job_timeline_events')).scalar_one()==0


@pytest.mark.parametrize('clean,drained,history,expected',[
    (1,'2026-10-25 12:10:00',[],True),
    (0,'2026-10-25 12:00:30',[],False),
    (1,'2026-10-25 12:10:00',[['2026-10-25 12:00:10','drop',1]],False),
    (1,None,[],False)])
def test_durable_history_completeness(engine,clean,drained,history,expected):
    with engine.begin() as c:
        c.execute(text('INSERT INTO timeline_writer_state(process_id,started_at,clean_shutdown,last_drained_at,drop_epoch,printer_ids,epoch_history) VALUES(:pid,:start,:clean,:drained,:drops,:printers,:history)'),
            {'pid':'process-before-restart','start':'2026-10-25 11:00:00','clean':clean,'drained':drained,'drops':len(history),'printers':'[1]','history':json.dumps(history)})
        assert history_complete(c,1,'2026-10-25 12:00:00','2026-10-25 12:01:00') is expected


def test_idle_writer_skips_transactions_and_heartbeat_advances_completeness(engine,monkeypatch):
    from unittest.mock import Mock
    seed(engine)
    # Drive the worker deterministically, without wall-clock sleeps.
    monkeypatch.setattr('core.job_timeline.threading.Thread.start',lambda self:None)
    writer=TimelineWriter(engine.url)
    try:
        writer.enqueue(event(1,'paused',printer_id=1))
        assert writer._drain()
        ended=now()
        with engine.connect() as c:
            assert not history_complete(c,1,writer.started,ended)
        begin=Mock(wraps=writer.engine.begin)
        monkeypatch.setattr(writer.engine,'begin',begin)
        for _ in range(25):assert writer._drain()
        begin.assert_not_called()
        writer.last_heartbeat-=5
        assert writer._drain()
        assert begin.call_count==1
        with engine.connect() as c:
            assert history_complete(c,1,writer.started,ended)
    finally:writer.engine.dispose()


def test_timeline_private_engine_retains_sqlite_hooks(engine,monkeypatch):
    from sqlalchemy.pool import NullPool
    monkeypatch.setattr('core.job_timeline.threading.Thread.start',lambda self:None)
    writer=TimelineWriter(engine.url,busy_timeout_ms=37)
    try:
        assert isinstance(writer.engine.pool,NullPool)
        for _ in range(2):
            with writer.engine.connect() as c:
                assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==1
                assert c.exec_driver_sql('PRAGMA busy_timeout').scalar_one()==37
                assert c.exec_driver_sql('PRAGMA journal_mode').scalar_one()=='wal'
    finally:writer.engine.dispose()


def test_timeline_timestamps_normalize_to_aware_utc():
    from datetime import datetime,timezone
    assert now().tzinfo==timezone.utc
    for supplied in ('2026-10-25T12:00:00+02:00',datetime(2026,10,25,10)):
        value=event(1,'paused',occurred_at=supplied)['occurred']
        assert value==datetime(2026,10,25,10,tzinfo=timezone.utc)
        assert value.tzinfo==timezone.utc


def test_monitor_registration_failure_is_nonfatal_and_retries(engine,monkeypatch):
    from core import job_timeline
    from sqlalchemy.engine import Engine
    seed(engine)
    original=Engine.begin
    attempts=[]
    def temporarily_unavailable(self):
        attempts.append(1)
        if len(attempts)==1:raise RuntimeError('registration unavailable')
        return original(self)
    monkeypatch.setattr(Engine,'begin',temporarily_unavailable)
    writer=TimelineWriter(engine.url,process_name='monitor-retry')
    try:
        writer.enqueue(event(1,'paused',printer_id=1))
        deadline=time.monotonic()+3
        while time.monotonic()<deadline:
            with original(engine) as c:
                row=c.execute(text('SELECT error_epoch FROM timeline_writer_state WHERE process_id=:pid'),{'pid':writer.process_id}).first()
                if row is not None:break
            time.sleep(.02)
        else:pytest.fail('registration was not retried')
        assert row[0]>=1
    finally:writer.close()
    with engine.connect() as c:
        assert c.execute(text('SELECT kind FROM job_timeline_events')).scalar_one()=='paused'


def test_dispatch_progress_survives_timeline_event_or_enqueue_failure(monkeypatch):
    from core import job_timeline
    from modules.printers import dispatch
    sent=[]
    monkeypatch.setattr(dispatch,'_ws_push',lambda *args:sent.append(args))
    def fail(*a,**kw):raise RuntimeError('timeline unavailable')
    with monkeypatch.context() as patch:
        patch.setattr(job_timeline,'event',fail)
        dispatch._ws(1,'uploading','progress')
    monkeypatch.setattr(job_timeline,'enqueue',fail)
    dispatch._ws(1,'starting','progress')
    assert [args[1]['status'] for args in sent]==['uploading','starting']


@pytest.mark.parametrize('status',['pending','printing','paused','completed','failed','cancelled'])
def test_org_cleanup_hard_deletes_every_job_status(engine,status):
    from sqlalchemy.orm import Session
    from modules.jobs.services import delete_org_jobs
    seed(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE jobs SET charged_to_org_id=41,status=:status WHERE id=1'),{'status':status})
        c.execute(text("INSERT INTO job_timeline_events(id,job_id,kind,occurred_at) VALUES('org-all',1,'started',CURRENT_TIMESTAMP)"))
    with Session(engine) as db:
        delete_org_jobs(db,41)
        db.commit()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM jobs')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM job_timeline_events')).scalar_one()==0


def test_engine_creation_failure_schedules_nonfatal_start_retry(engine,monkeypatch):
    from core import job_timeline
    from core import database_config
    from unittest.mock import Mock
    job_timeline.shutdown()
    timers=[]
    class Timer:
        def __init__(self,delay,callback):
            assert delay==5
            self.callback=callback;self.cancelled=False;timers.append(self)
        def start(self):pass
        def cancel(self):self.cancelled=True
    original=database_config.create_database_engine
    monkeypatch.setattr(job_timeline.threading,'Timer',Timer)
    monkeypatch.setattr(database_config,'create_database_engine',Mock(side_effect=RuntimeError('unavailable')))
    try:
        assert job_timeline.start('mqtt_monitor',engine.url) is None
        assert len(timers)==1
        monkeypatch.setattr(database_config,'create_database_engine',original)
        timers[0].callback()
        assert job_timeline.start('mqtt_monitor',engine.url) is not None
    finally:job_timeline.shutdown()
    with engine.connect() as c:
        assert c.execute(text('SELECT clean_shutdown FROM timeline_writer_state')).scalar_one()==1
