"""Review-round regressions exercise the real retained graph and failure windows."""
import json
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from tests.test_contracts.test_term_retention import retained, client, ADMIN, run
from tests.test_contracts.test_education_review_workflow import review_db
from modules.organizations.education_term_retention import preview, delete_policy
from modules.organizations.education_term_privacy import anonymize, eligible_student


def test_each_category_has_school_audit_and_upload_state(retained):
    db, _, _ = retained
    assert len(run(retained)['results']) == 4
    rows = db.execute(text("SELECT action,details_json FROM education_audit_events WHERE action LIKE 'term_retention_%'")).all()
    assert {json.loads(r.details_json)['kind'] for r in rows} == {'files', 'thumbnails', 'portfolio', 'timelapses'}
    assert any(r.action == 'term_retention_archived' for r in rows)
    assert db.execute(text('SELECT state FROM education_upload_operations')).scalar() == 'purged'


@pytest.mark.parametrize('reference', ['archive', 'product', 'preset', 'submission'])
def test_every_shared_reference_blocks_content(retained, reference):
    db, _, path = retained
    if reference == 'archive':
        db.execute(text('UPDATE print_archives SET education_submission_id=NULL'))
    elif reference == 'product':
        db.execute(text("INSERT INTO products(id,name) VALUES(61,'shared')"))
        db.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(61,12,1)'))
    elif reference == 'preset':
        db.execute(text("INSERT INTO print_presets(name,model_id) VALUES('shared',12)"))
    else:
        # Another retained submission can reference this model despite a separate file/job/upload.
        db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state) VALUES('other',1,1,'committed')"))
        db.execute(text("INSERT INTO print_files(id,filename,org_id,created_by) VALUES(21,'other',1,1)"))
        db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,submitted_by) VALUES(23,'other','completed',1,1,1)"))
        db.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status) VALUES(24,1,'other',23,21,12,7,1,'completed')"))
    db.commit()
    result = run(retained)
    assert {r['reason'] for r in result['results']} == {'shared_or_unconfirmed_content'}
    assert path.exists()


def test_invalid_upload_skips_all_categories_and_audits_reason(retained):
    db, _, path = retained
    path.write_bytes(b'replaced')
    result = run(retained)
    assert len(result['results']) == 4
    assert {r['reason'] for r in result['results']} == {'upload_ownership_or_hash_failed'}
    assert db.execute(text('SELECT thumbnail_b64 FROM print_files')).scalar() == 'preview'
    assert db.execute(text('SELECT archived_at FROM education_submissions')).scalar() is None
    assert all('upload_ownership_or_hash_failed' in r[0] for r in db.execute(
        text("SELECT details FROM audit_logs WHERE action='term_retention_item_blocked'")))


def test_hashing_has_no_writer_lock(retained, monkeypatch):
    from modules.organizations import education_term_files as files
    db, _, _ = retained
    original = files.os.read
    calls = []
    def read(*args):
        calls.append(True)
        assert not db.in_transaction()
        return original(*args)
    monkeypatch.setattr(files.os, 'read', read)
    assert {r['state'] for r in run(retained)['results']} == {'done'}
    assert calls


@pytest.mark.parametrize('change', ['role', 'bytes', 'revision'])
def test_post_intent_window_rechecks_before_unlink(retained, monkeypatch, change):
    from modules.organizations import education_term_cleanup as service
    db, _, path = retained
    original = service.audit
    def audit(db, action, term, details):
        original(db, action, term, details)
        if action == 'item_started' and details['kind'] == 'files':
            if change == 'role': db.execute(text("UPDATE users SET role='viewer' WHERE id=3"))
            elif change == 'revision': db.execute(text('UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1'))
            else: path.write_bytes(b'replaced after hash')
    monkeypatch.setattr(service, 'audit', audit)
    result = run(retained)
    assert {r['state'] for r in result['results']} == {'blocked'}
    assert path.exists()
    assert db.execute(text("SELECT COUNT(*) FROM education_term_cleanup WHERE state='done'")).scalar() == 0


def test_live_database_role_overrides_authenticated_role(client, retained):
    c, _, _ = client
    db, _, _ = retained
    db.execute(text("UPDATE users SET role='viewer' WHERE id=3")); db.commit()
    assert c.get('/education/term-retention').status_code == 403


def test_unused_policy_can_be_deleted_and_replaced(client, retained):
    from modules.organizations.education_term_retention import create_policy, TermPolicy
    c, _, _ = client
    db, term, _ = retained
    assert c.delete('/education/term-retention/' + term['id']).status_code == 200
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='term_retention_policy_deleted'")).scalar() == 1
    replacement = create_policy(db, TermPolicy(**{k: term[k] for k in TermPolicy.model_fields}), ADMIN)
    assert replacement['id'] != term['id']


def test_even_pending_receipt_prevents_policy_deletion(retained):
    db, term, _ = retained
    db.execute(text("INSERT INTO education_term_cleanup VALUES(:term,14,'files','pending','2020-01-01')"), {'term': term['id']}); db.commit()
    with pytest.raises(HTTPException) as error: delete_policy(db, term['id'], ADMIN)
    assert error.value.status_code == 409


def test_database_error_is_reported_as_blocked(retained, monkeypatch):
    from modules.organizations import education_term_cleanup as service
    original = service._thumbnails
    def fail(*args): raise OperationalError('owned failure', {}, Exception())
    monkeypatch.setattr(service, '_thumbnails', fail)
    assert {r['reason'] for r in run(retained)['results']} == {'database_error'}
    monkeypatch.setattr(service, '_thumbnails', original)
    assert {r['state'] for r in run(retained)['results']} == {'done'}


def test_vanished_submission_maps_to_404(retained):
    from modules.organizations.education_term_cleanup import current_submission
    db, term, _ = retained
    with pytest.raises(HTTPException) as error: current_submission(db, term, 999)
    assert error.value.status_code == 404


def test_request_item_and_time_budgets_return_resume_cursor(retained, monkeypatch):
    from modules.organizations import education_term_cleanup as service
    db, _, path = retained
    monkeypatch.setattr(service, 'MAX_REQUEST_ITEMS', 0)
    assert run(retained)['next_after_id'] == 0
    monkeypatch.setattr(service, 'MAX_REQUEST_ITEMS', 25)
    monkeypatch.setattr(service, 'MAX_REQUEST_SECONDS', 0)
    assert run(retained)['next_after_id'] == 0
    assert path.exists()


def departed(db):
    db.execute(text('UPDATE users SET is_active=FALSE WHERE id IN (1,4)'))
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1"))
    db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by,granted_at,revoked_at) VALUES(1,7,4,'student','revoked',3,'2020-01-01','2020-02-01')"))
    db.commit()


def test_failed_post_account_erasure_resumes_original_identifiers_and_continues(retained, monkeypatch):
    from core import identity_scrub
    db, term, _ = retained
    departed(db)
    db.execute(text("INSERT INTO alerts(user_id,alert_type,severity,title,message) VALUES(3,'print_complete','info','student','student@example.test')")); db.commit()
    original = identity_scrub.scrub_text
    def fail(*args): raise OperationalError('owned interrupted chunk', {}, Exception())
    monkeypatch.setattr(identity_scrub, 'scrub_text', fail)
    result = anonymize(db, term['id'], [1,4], preview(db, term)['fingerprint'], ADMIN)
    assert result['results'] == [{'user_id': 1, 'state': 'erasure_incomplete'}, {'user_id': 4, 'state': 'anonymized'}]
    assert db.execute(text('SELECT username FROM users WHERE id=1')).scalar() == '[deleted-1]'
    assert eligible_student(db, 1, 1) and not eligible_student(db, 4, 1)
    assert 'student@example.test' in db.execute(text('SELECT identifiers_json FROM education_erasure_pending')).scalar()
    monkeypatch.setattr(identity_scrub, 'scrub_text', original)
    assert anonymize(db, term['id'], [1], preview(db, term)['fingerprint'], ADMIN)['results'][0]['state'] == 'anonymized'
    assert db.execute(text('SELECT count(*) FROM education_erasure_pending')).scalar() == 0
    assert 'student' not in str(db.execute(text('SELECT title,message FROM alerts')).all())
    assert not eligible_student(db, 1, 1)


def test_graduation_route_is_synchronous_and_names_selection(client, retained):
    import inspect
    from modules.organizations.routes_term_retention import graduate
    c, _, _ = client
    db, term, _ = retained
    departed(db)
    assert not inspect.iscoroutinefunction(graduate)
    data = c.get(f"/education/term-retention/{term['id']}/dry-run").json()
    assert {'id': 1, 'username': 'student'} in data['eligible_students']


def test_one_submission_batches_categories_in_two_write_transactions(retained, monkeypatch):
    from modules.organizations import education_term_cleanup as service
    original = service.authority
    locked = []
    def authority(*args, **kwargs):
        if kwargs.get('lock'): locked.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(service, 'authority', authority)
    assert len(run(retained)['results']) == 4
    assert len(locked) == 2


def test_hash_deadline_records_blocked_without_creating_intent(retained, monkeypatch):
    from modules.organizations import education_term_files as files
    from types import SimpleNamespace
    db, _, path = retained
    monkeypatch.setattr(files, 'time', SimpleNamespace(monotonic=lambda: float('inf')))
    result = run(retained)
    assert result['next_after_id'] is None
    assert len(result['results']) == 4
    assert {r['reason'] for r in result['results']} == {'hash_budget_exhausted'}
    assert db.execute(text('SELECT count(*) FROM education_term_cleanup')).scalar() == 0
    assert path.exists()


@pytest.mark.parametrize('changed', [False, True])
def test_file_verification_precedes_media_retirement(retained, monkeypatch, changed):
    from modules.organizations import education_term_cleanup as cleanup, education_term_retention as retention
    db, _, path = retained
    media = [{'id': 81, 'status': 'ready', 'owner_state': 'known', 'owner_org_id': 1,
              'education_submission_id': 14, 'ownership_operation_id': 'owned'}]
    monkeypatch.setattr(retention, 'media_rows', lambda *a: list(media))
    monkeypatch.setattr(cleanup, 'media_rows', lambda *a: list(media))
    original = cleanup.audit
    def audit(db, action, term, details):
        original(db, action, term, details)
        if changed and action == 'item_started' and details['kind'] == 'files':
            path.write_bytes(b'changed after intent')
    monkeypatch.setattr(cleanup, 'audit', audit)
    called = []
    def retire(row):
        called.append(row['id'])
        assert not path.exists() and not db.in_transaction()
        media.clear()
    result = run(retained, retire_media=retire)
    assert called == ([] if changed else [81])
    assert {r['state'] for r in result['results']} == ({'blocked'} if changed else {'done'})


def test_disappeared_selection_returns_blocked_result(client, retained, monkeypatch):
    from modules.organizations import education_term_cleanup as cleanup
    c, _, _ = client
    db, term, path = retained
    original = cleanup.audit
    def audit(db, action, term_id, details):
        original(db, action, term_id, details)
        if action == 'purge_started':
            db.execute(text("UPDATE education_submissions SET created_at='2021-01-01'"))
    monkeypatch.setattr(cleanup, 'audit', audit)
    fingerprint = preview(db, term)['fingerprint']
    response = c.post(f"/education/term-retention/{term['id']}/purge", json={'fingerprint': fingerprint})
    assert response.status_code == 200 and path.exists()
    assert {r['reason'] for r in response.json()['results']} == {'submission_gone'}


def test_unknown_timelapse_does_not_require_media_service_or_block_other_http_cleanup(client, retained, monkeypatch):
    from modules.organizations import education_term_media
    c, _, _ = client
    db, term, path = retained
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,started_at) VALUES(80,9,13,CURRENT_TIMESTAMP)"))
    db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(81,9,80,'legacy.mp4','ready')")); db.commit()
    def prohibited(*args): pytest.fail('Unknown legacy video must not acquire retirement credentials')
    monkeypatch.setattr(education_term_media, 'media_retirement', prohibited)
    response = c.post(f"/education/term-retention/{term['id']}/purge", json={'fingerprint': preview(db, term)['fingerprint']})
    assert response.status_code == 200 and not path.exists()
    assert len([r for r in response.json()['results'] if r['state'] == 'done']) == 3
