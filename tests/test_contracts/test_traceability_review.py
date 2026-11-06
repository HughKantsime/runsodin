"""E4 review regressions: transaction capture, actor provenance, exports and privacy."""
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from tests.test_contracts.test_traceability import world, verify_pdf
from core import traceability as service
from core.audit import log_audit
from core.audit_actor import RequestAuditScope, bind_verified_actor


def actor(db, uid, name):
    scope = RequestAuditScope(None)
    scope.register(db)
    bind_verified_actor(db, {'id':uid, 'username':name, '_auth_kind':'session_jwt'})
    return scope


def test_operators_real_actor_binding_excludes_qa_and_freezes(world):
    client, engine, *_ = world
    with Session(engine) as db:
        scope = actor(db, 1, 'operator')
        log_audit(db, 'job.started', 'job', 1)
        log_audit(db, 'job.completed', 'job', 1)
        db.commit()
        scope.clear()
        scope = actor(db, 2, 'other')
        log_audit(db, 'traceability.inspected', 'job', 1, {'private':'not a lifecycle operator'})
        db.commit()
        scope.clear()
    result = client.get('/api/jobs/1/traceability').json()
    assert result['operator_ids'] == [1]
    assert result['operators'] == [{'id':1, 'username':'operator'}]
    with Session(engine) as db:
        scope = actor(db, 2, 'other')
        log_audit(db, 'job.started', 'job', 1)
        db.commit()
        scope.clear()
    assert client.get('/api/jobs/1/traceability').json()['operator_ids'] == [1]


def test_completion_captures_before_first_read_and_rolls_back_atomically(world):
    from modules.jobs.routes.jobs_lifecycle import complete_job
    client, engine, user, *_ = world
    with engine.begin() as c:
        c.execute(text("UPDATE jobs SET status='printing',actual_start='2026-11-01 10:00:00',actual_end=NULL WHERE id=1"))
    with Session(engine) as db:
        scope = actor(db, 1, 'operator')
        complete_job(1, current_user=user, db=db)
        scope.clear()
    with engine.begin() as c:
        captured = c.execute(text('SELECT snapshot FROM traceability_records WHERE job_id=1')).scalar_one()
        assert json.loads(captured)['captured_after_completion'] is False
        c.execute(text("UPDATE printers SET name='Renamed',nozzle_type='changed',tags='[]' WHERE id=1"))
        c.execute(text("UPDATE print_files SET original_filename='renamed.gcode',file_hash=:h WHERE id=1"), {'h':'b'*64})
    result = client.get('/api/jobs/1/traceability').json()
    assert result['file']['name'] == 'widget.gcode' and result['file']['sha256'] == 'a'*64
    assert result['printer']['name'] == 'P1' and result['nozzle']['type'] == 'hardened'
    assert result['operator_ids'] == [1]
    assert result['snapshot_sha256'] == hashlib.sha256(captured.encode()).hexdigest()
    from core.traceability_evidence import capture_completion
    with Session(engine) as db:
        capture_completion(db, 2)
        db.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM traceability_records WHERE job_id=2')).scalar() == 0


def test_terminal_capture_dbapi_and_legacy_pdf_label(world):
    from core.job_terminal_observations import claim_terminal
    from pypdf import PdfReader
    client, engine, *_ = world
    with engine.begin() as c:
        c.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) VALUES(1,1,1,'completed','2026-11-01 10:00:00')"))
    raw = engine.raw_connection()
    try:
        claim_terminal(raw, 1)
        raw.commit()
    finally:
        raw.close()
    result = client.get('/api/jobs/1/traceability').json()
    assert result['captured_after_completion'] is False
    response = client.get('/api/jobs/2/certificate')
    assert response.status_code == 200
    assert 'captured_after_completion' in ''.join(p.extract_text() for p in PdfReader(io.BytesIO(response.content)).pages)


def test_existing_record_survives_reset_reclassification_and_printer_deletion(world):
    from core.print_attempts import bump_print_attempt, sqlalchemy_runner
    client, engine, *_ = world
    before = client.get('/api/jobs/1/traceability').json()
    with Session(engine) as db:
        bump_print_attempt(sqlalchemy_runner(db), 1)
        db.execute(text("UPDATE jobs SET status='pending',printer_id=NULL,print_file_id=NULL WHERE id=1"))
        db.execute(text('DELETE FROM printers WHERE id=1'))
        db.commit()
    response = client.get('/api/jobs/1/traceability?attempt=0')
    assert response.status_code == 200, response.text
    assert response.json()['file'] == before['file']
    assert response.json()['warnings']
    assert client.get('/api/jobs/1/certificate?attempt=0').status_code == 200
    assert client.get('/api/jobs/1/traceability?attempt=9').status_code in (404,409)


def test_frozen_policy_tags_and_product(world):
    client, engine, *_ = world
    client.get('/api/jobs/1/traceability')
    with engine.begin() as c:
        c.execute(text("UPDATE printers SET tags='[]' WHERE id=1"))
    client.put('/api/jobs/traceability/policies', json={'org_id':1,'target_kind':'printer_tag','target_key':'QA','required':True})
    response = client.post('/api/jobs/1/deliver')
    assert response.status_code == 409 and 'inspection_required' in response.text


def test_glyph_substitution_sidecar_and_curated_lifecycle(world):
    from pypdf import PdfReader
    client, engine, *_ = world
    assert client.put('/api/jobs/1/inspection', json={'result':'pass','notes':'Measured 🦄 sample'}).status_code == 200
    assert client.post('/api/jobs/1/deliver').status_code == 200
    with Session(engine) as db:
        log_audit(db, 'traceability.read', 'job', 1, {'private':'RAW-AUDIT-DETAIL-SECRET'})
        db.commit()
    pdf = client.get('/api/jobs/1/certificate')
    sig = client.get('/api/jobs/1/certificate.sig')
    assert pdf.status_code == sig.status_code == 200
    verify_pdf(pdf.content, sig.json())
    rendered = ''.join(p.extract_text() for p in PdfReader(io.BytesIO(pdf.content)).pages)
    assert '[?]' in rendered and 'RAW-AUDIT-DETAIL-SECRET' not in rendered
    assert sig.json()['key_fingerprint'] in rendered
    record = client.get('/api/jobs/1/traceability').json()
    assert record['snapshot_sha256'] in rendered and '🦄' in record['inspection']['inspection_notes']
    assert client.put('/api/jobs/2/inspection', json={'result':'pass','notes':'bad\x00notes'}).status_code == 422


def test_batch_skips_refusal_caps_and_does_not_lock_reads(world, monkeypatch):
    from fastapi import HTTPException
    from modules.jobs import traceability_batch
    client, engine, _, _, policy = world
    client.get('/api/jobs/1/traceability')
    client.get('/api/jobs/2/traceability')
    def education(db, job_id):
        if job_id == 2:
            raise HTTPException(409, 'Education workflow required')
    policy.assert_generic_job_mutation_allowed.side_effect = education
    queries = []
    def observed(conn, cursor, statement, params, context, many):
        queries.append(statement)
    event.listen(engine, 'before_cursor_execute', observed)
    params = {'start':'2026-11-01T00:00:00-04:00','end':'2026-11-02T00:00:00-05:00'}
    response = client.get('/api/jobs/traceability/export', params=params)
    event.remove(engine, 'before_cursor_execute', observed)
    assert response.status_code == 200, response.text
    assert not any(q.lstrip().upper().startswith('UPDATE') for q in queries)
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        manifest = json.loads(z.read('manifest.json'))
        assert manifest['job_ids'] == [1] and manifest['skipped'][0]['job_id'] == 2
        assert manifest['skipped'][0]['reason'] == 'Education workflow required'
        assert manifest['start'] == '2026-11-01T04:00:00Z' and manifest['end'] == '2026-11-02T05:00:00Z'
    monkeypatch.setattr(traceability_batch, 'MAX_BYTES', 100)
    assert client.get('/api/jobs/traceability/export', params=params).status_code == 413
    monkeypatch.setattr(traceability_batch, 'MAX_JOBS', 1)
    assert client.get('/api/jobs/traceability/export', params=params).status_code == 413


def test_erasure_retains_quality_notes_updates_digest_and_excludes_other_inspectors(world):
    from core.traceability_privacy import export_personal, erase_personal
    client, engine, *_ = world
    client.put('/api/jobs/1/inspection', json={'result':'pass','notes':'operator@example.test measured hole: 10 mm'})
    with Session(engine) as db:
        db.execute(text('UPDATE jobs SET submitted_by=2 WHERE id=1'))
        db.commit()
        assert export_personal(db,2)[0]['inspection_notes'] is None
        erase_personal(db,1,{'operator@example.test','widget.gcode'})
        db.commit()
        row = db.execute(text('SELECT * FROM traceability_records WHERE job_id=1')).mappings().one()
        assert row['inspection_notes'] == '[deleted-user] measured hole: 10 mm'
        assert row['snapshot_sha256'] == hashlib.sha256(row['snapshot'].encode()).hexdigest()
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='traceability.erased'")).scalar() == 1


def test_filament_window_normalizes_offsets(world):
    from core.print_attempts import bump_print_attempt, sqlalchemy_runner
    client, engine, *_ = world
    with Session(engine) as db:
        bump_print_attempt(sqlalchemy_runner(db),1)
        db.execute(text("UPDATE system_config SET value='\"2026-11-01T12:00:00Z\"' WHERE key='quota_attempt:started:1:1'"))
        db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Test','PLA','PLA')"))
        db.execute(text("INSERT INTO spools(id,filament_id,org_id) VALUES(1,1,1)"))
        db.execute(text("INSERT INTO spool_usage(job_id,spool_id,weight_used_g,used_at) VALUES(1,1,1,'2026-11-01T08:01:00-04:00'),(1,1,2,'2026-11-01T13:00:00+02:00')"))
        db.commit()
    result = client.get('/api/jobs/1/traceability')
    assert result.status_code == 200, result.text
    assert [u['weight_used_g'] for u in result.json()['filament']] == [1]
