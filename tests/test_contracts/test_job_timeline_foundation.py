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
