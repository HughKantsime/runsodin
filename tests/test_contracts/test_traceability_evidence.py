"""Retained dispatch evidence and Education batch exclusion on real schemas."""
import asyncio
import io
import json
import zipfile
import pytest
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_traceability import world
from tests.test_contracts.test_education_review_workflow import review_db
from core.print_attempts import bind_print_attempt, sqlalchemy_runner


def test_dispatched_hash_and_attempt_materials_win_over_mutable_metadata(world):
    client, engine, *_ = world
    with Session(engine) as db:
        db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Test','PLA','PLA')"))
        db.execute(text("INSERT INTO spools(id,filament_id,org_id,lot_number) VALUES(1,1,1,'FIRST-LOT')"))
        snapshot = {'material_plan':{'state':'complete','allocations':[{'spool_id':1,'spool_org_id':1,'slot_number':1}]},
                    'slots':[{'spool_id':1,'spool_org_id':1},{'spool_id':999,'spool_org_id':2}],
                    'content_owner':{'private':'do not export'}}
        db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,spool_snapshot) VALUES(1,1,1,'completed','2026-11-01 10:00:00',:snapshot)"), {'snapshot':json.dumps(snapshot)})
        bind_print_attempt(sqlalchemy_runner(db),1,1)
        db.execute(text('''INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,
            prepared_power_key,observed_filename_key,observed_print_job_id) VALUES(:token,1,1,:key,:key,:key,1)'''), {'token':'a'*32,'key':'a'*64})
        db.execute(text('''INSERT INTO dispatch_sender_registrations(claim_token,printer_id,job_id,physical_key,
            configuration_key,payload_sha256,origin_key,runtime_incarnation,registration_key,state)
            VALUES(:token,1,1,:key,:key,:hash,:token,:token,:token,'active')'''), {'token':'a'*32,'key':'a'*64,'hash':'c'*64})
        from core.traceability_evidence import capture_completion
        capture_completion(db,1)
        db.commit()
        db.execute(text("UPDATE spools SET lot_number='LATER-LOT' WHERE id=1"))
        db.commit()
    response = client.get('/api/jobs/1/traceability')
    assert response.status_code == 200, response.text
    record = response.json()
    assert record['file']['sha256'] == 'c'*64
    assert 'dispatch_attempt_receipts' in record['file']['hash_source']
    assert record['attempt_material_evidence'][0]['spools'][0]['lot_number'] == 'FIRST-LOT'
    assert '999' not in json.dumps(record['attempt_material_evidence'])
    assert 'do not export' not in response.text


def test_batch_excludes_education_submission_in_sql(world, review_db):
    from modules.jobs.traceability_batch import export_batch
    review_db.execute(text("UPDATE jobs SET status='completed',actual_end='2026-11-01 12:00:00' WHERE id=13"))
    review_db.commit()
    def no_education_load(*args, **kwargs):
        raise AssertionError('Education job reached the generic export loader')
    response = export_batch(review_db, world[2], datetime(2026,11,1,tzinfo=timezone.utc),
                            datetime(2026,11,2,tzinfo=timezone.utc), no_education_load)
    async def collect():
        return b''.join([chunk async for chunk in response.body_iterator])
    with zipfile.ZipFile(io.BytesIO(asyncio.run(collect()))) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['job_ids'] == [] and manifest['skipped'] == []


@pytest.mark.parametrize('protected', ['foreign', 'education'])
def test_completion_does_not_snapshot_unauthorized_file(world, protected):
    from core.traceability_evidence import capture_completion
    client, engine, *_ = world
    with Session(engine) as db:
        if protected == 'foreign':
            db.execute(text('UPDATE print_files SET org_id=2 WHERE id=1'))
        else:
            db.execute(text("UPDATE print_files SET stored_path='/education/retained.gcode' WHERE id=1"))
            db.execute(text('INSERT INTO education_memberships(user_id,org_id) VALUES(1,1)'))
            db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,final_path) VALUES('e4-private',1,1,'committed','/education/retained.gcode')"))
        capture_completion(db,1)
        db.commit()
        assert db.execute(text('SELECT count(*) FROM traceability_records')).scalar() == 0
    assert client.get('/api/jobs/1/traceability').status_code == (404 if protected == 'foreign' else 409)
