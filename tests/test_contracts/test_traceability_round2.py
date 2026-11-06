"""E4 round two: late accounting, failure isolation, retention and export privacy."""
import asyncio
import io
import json
import zipfile
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.test_contracts.test_traceability import world
from core import traceability as service
from core.print_attempts import bind_print_attempt, bump_print_attempt, sqlalchemy_runner


def physical(db, snapshot='{}'):
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,spool_snapshot) VALUES(1,1,1,'completed','2026-11-01 10:00:00',:snapshot)"), {'snapshot':snapshot})
    bind_print_attempt(sqlalchemy_runner(db), 1, 1)


def test_late_usage_is_attempt_bound_and_does_not_change_snapshot(world):
    client, engine, *_ = world
    with Session(engine) as db:
        physical(db)
        db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Test','PLA','PLA')"))
        db.execute(text("INSERT INTO spools(id,filament_id,org_id) VALUES(1,1,1)"))
        from core.traceability_evidence import capture_completion
        capture_completion(db, 1)
        db.commit()
    before = client.get('/api/jobs/1/traceability').json()
    assert before['filament'] == []
    with Session(engine) as db:
        # Archive accounting arrives after completion, even after the next reset.
        bump_print_attempt(sqlalchemy_runner(db), 1)
        db.execute(text("INSERT INTO spool_usage(job_id,print_job_id,spool_id,weight_used_g) VALUES(1,1,1,12)"))
        db.commit()
    old = client.get('/api/jobs/1/traceability?attempt=0').json()
    assert [u['weight_used_g'] for u in old['filament']] == [12]
    assert old['snapshot_sha256'] == before['snapshot_sha256']
    assert client.get('/api/jobs/1/traceability').json()['filament'] == []


@pytest.mark.parametrize('executor_kind', ['session', 'connection', 'dbapi'])
def test_capture_failure_rolls_back_only_capture(world, executor_kind, monkeypatch, caplog):
    from core.traceability_evidence import capture_completion
    _, engine, *_ = world
    with Session(engine) as db:
        physical(db, '[1]')
        db.commit()
    executor = {'session':lambda:Session(engine), 'connection':engine.connect,
                'dbapi':engine.raw_connection}[executor_kind]()
    from core.dispatch_observation_executor import TransactionView
    view = TransactionView(executor)
    try:
        view.execute(text("UPDATE jobs SET item_name='Completion committed' WHERE id=1"))
        capture_completion(executor, 1)
        executor.commit()
    finally:
        executor.close()
    with engine.connect() as c:
        assert c.execute(text('SELECT item_name FROM jobs WHERE id=1')).scalar() == 'Completion committed'
        assert c.execute(text('SELECT count(*) FROM traceability_records')).scalar() == 0
    assert 'capture' in caplog.text.lower()
    # First access identifies late capture and explicitly unavailable material
    # evidence, even if the original malformed snapshot remains unchanged.
    record = world[0].get('/api/jobs/1/traceability').json()
    assert record['captured_after_completion'] is True
    assert record['attempt_material_evidence'][0]['reason'] == 'malformed_spool_snapshot'


def test_capture_sql_failure_leaves_no_partial_record(world, monkeypatch):
    from core.traceability_evidence import capture_completion
    _, engine, *_ = world
    original = service.record
    def broken(db, job, **kwargs):
        original(db, job, **kwargs)
        db.execute(text('INSERT INTO missing_e4_capture_table VALUES(1)'))
    monkeypatch.setattr(service, 'record', broken)
    with Session(engine) as db:
        db.execute(text("UPDATE jobs SET item_name='Completed' WHERE id=1"))
        capture_completion(db, 1)
        db.commit()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM traceability_records')).scalar() == 0
        assert c.execute(text('SELECT item_name FROM jobs WHERE id=1')).scalar() == 'Completed'


@pytest.mark.parametrize('export', ['bundle', 'batch'])
def test_export_json_excludes_raw_audit(world, export):
    client, engine, *_ = world
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) VALUES('job.completed','job',1,'PRIVATE-DETAIL')"))
    response = (client.get('/api/jobs/1/certificate?bundle=true') if export == 'bundle' else
                client.get('/api/jobs/traceability/export', params={'start':'2026-11-01T00:00:00Z','end':'2026-11-02T00:00:00Z'}))
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        record = json.loads(archive.read('traceability.json' if export == 'bundle' else 'job-1.json'))
    assert 'audit_trail' not in record
    assert record['lifecycle'][0]['action'] == 'job.completed'
    assert 'PRIVATE-' not in json.dumps(record)


def test_reset_then_bulk_delete_retains_delivered_record(world):
    from modules.jobs.routes.jobs_crud import bulk_update_jobs
    client, engine, user, *_ = world
    assert client.post('/api/jobs/1/deliver').status_code == 200
    with Session(engine) as db:
        bump_print_attempt(sqlalchemy_runner(db), 1)
        db.execute(text("UPDATE jobs SET status='pending' WHERE id=1"))
        db.commit()
        with pytest.raises(HTTPException, match='Delivered traceability'):
            asyncio.run(bulk_update_jobs({'job_ids':[1], 'action':'delete'}, user, db))
        db.rollback()
        assert db.execute(text('SELECT count(*) FROM traceability_records WHERE job_id=1')).scalar() == 1


def test_database_restricts_direct_job_delete(world):
    client, engine, *_ = world
    client.post('/api/jobs/1/deliver')
    with engine.connect() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=ON')
        with pytest.raises(IntegrityError):
            c.execute(text('DELETE FROM jobs WHERE id=1'))
        c.rollback()


def test_retention_skips_delivered_but_deletes_undelivered_and_unrecorded(world):
    from modules.system.routes_config import run_retention_cleanup
    client, engine, user, *_ = world
    client.post('/api/jobs/1/deliver')
    client.get('/api/jobs/2/traceability')
    with Session(engine) as db:
        db.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(3,'Unrecorded','completed')"))
        db.execute(text("UPDATE jobs SET updated_at='2000-01-01'"))
        db.execute(text("INSERT INTO system_config(key,value) VALUES('data_retention',:config)"), {'config':json.dumps({'completed_jobs_days':1,'audit_logs_days':0,'timelapses_days':0,'alert_history_days':0})})
        db.commit()
        result = asyncio.run(run_retention_cleanup(user, db))
        assert result['deleted']['completed_jobs'] == 2
        assert db.execute(text('SELECT id FROM jobs')).scalars().all() == [1]
        assert db.execute(text('SELECT job_id FROM traceability_records')).scalars().all() == [1]


def test_malformed_snapshot_does_not_abort_real_completion(world):
    from modules.jobs.routes.jobs_lifecycle import complete_job
    client, engine, user, *_ = world
    with Session(engine) as db:
        physical(db, '[1]')
        db.execute(text("UPDATE jobs SET status='printing',actual_start='2026-11-01 10:00:00',actual_end=NULL WHERE id=1"))
        db.commit()
        complete_job(1, current_user=user, db=db)
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM jobs WHERE id=1')).scalar() == 'completed'
        assert c.execute(text('SELECT count(*) FROM traceability_records')).scalar() == 0


@pytest.mark.parametrize('executor_kind', ['session', 'connection', 'dbapi'])
def test_capture_never_commits_the_callers_transaction(world, executor_kind):
    from core.traceability_evidence import capture_completion
    _, engine, *_ = world
    executor = {'session':lambda:Session(engine), 'connection':engine.connect,
                'dbapi':engine.raw_connection}[executor_kind]()
    try:
        # No prior writes: SQLite must have an outer transaction before SAVEPOINT.
        capture_completion(executor, 1)
        executor.rollback()
    finally:
        executor.close()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM traceability_records')).scalar() == 0
