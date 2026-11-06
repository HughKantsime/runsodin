"""D5 real-schema lifecycle, storage, privacy, and authenticated HTTP contracts."""
import asyncio
import hashlib
import importlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text, inspect
from tests.test_contracts.test_education_review_workflow import review_db
from modules.organizations.education_term_retention import TermPolicy, create_policy, preview, policy, authority
from modules.organizations.education_term_cleanup import purge, unlink_upload
from modules.organizations.education_term_privacy import anonymize, export_term_data, eligible_student

ADMIN = {'id': 3, 'role': 'admin', 'group_id': 1, 'is_active': True, '_auth_kind': 'session_jwt'}
M = importlib.import_module('core.schema.migrations.053_term_retention')


@pytest.fixture
def retained(review_db, tmp_path, monkeypatch):
    db = review_db
    root = tmp_path / 'uploads'
    (root / '1').mkdir(parents=True)
    payload = root / '1' / 'upload-1.3mf'
    payload.write_bytes(b'owned test bytes')
    monkeypatch.setenv('EDUCATION_UPLOAD_ROOT', str(root))
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    db.execute(text("UPDATE education_submissions SET status='completed',created_at='2020-01-15 00:00:00'"))
    db.execute(text("UPDATE jobs SET status='completed'"))
    db.execute(text("UPDATE print_files SET stored_path=:path,thumbnail_b64='preview',mesh_data='mesh'"), {'path': str(payload)})
    db.execute(text('UPDATE education_upload_operations SET final_path=:path,expected_hash=:hash'), {'path': str(payload), 'hash': digest})
    db.execute(text("UPDATE models SET thumbnail_b64='preview'"))
    db.execute(text("INSERT INTO education_storage_accounts(org_id,scope_kind,scope_id,user_id,accounted_bytes) VALUES(1,'user',1,1,100),(1,'tenant',1,NULL,100)"))
    db.execute(text("INSERT INTO print_archives(job_id,print_file_id,education_submission_id,org_id,thumbnail_b64,plate_thumbnails,file_path) VALUES(13,11,14,1,'preview','[]',:path)"), {'path': str(payload)})
    db.commit()
    body = TermPolicy(cost_center_id=7, name='Spring 2020', starts_at='2020-01-01T00:00:00Z',
                      ends_at='2020-02-01T00:00:00Z', files_days=30, thumbnails_days=30,
                      timelapses_days=30, portfolio_days=30)
    term = create_policy(db, body, ADMIN)
    return db, term, payload


def run(retained, **kw):
    db, term, _ = retained
    return purge(db, term['id'], preview(db, term)['fingerprint'], ADMIN, **kw)


def test_purge_keeps_school_history_and_releases_content_once(retained):
    db, term, path = retained
    before = {t: db.execute(text('SELECT COUNT(*) FROM ' + t)).scalar() for t in
              ('users', 'education_submissions', 'education_memberships', 'jobs', 'print_archives', 'models', 'print_files')}
    result = run(retained)
    assert len(result['results']) == 4
    assert {r['state'] for r in result['results']} == {'done'}
    assert not path.exists()
    assert db.execute(text('SELECT blob_state,storage_bytes,thumbnail_b64,mesh_data FROM print_files')).one() == ('purged', 0, None, None)
    assert db.execute(text('SELECT thumbnail_b64,plate_thumbnails FROM print_archives')).one() == (None, None)
    assert db.execute(text('SELECT archived_at FROM education_submissions')).scalar()
    assert db.execute(text('SELECT accounted_bytes FROM education_upload_operations')).scalar() == 0
    assert all(db.execute(text('SELECT COUNT(*) FROM ' + t)).scalar() == count for t, count in before.items())
    assert run(retained)['results'] == []
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='term_retention_item_completed'")).scalar() == 4
    assert len(export_term_data(db, 1)['content_cleanup']) == 4


@pytest.mark.parametrize('field', ['files_days', 'thumbnails_days', 'portfolio_days', 'timelapses_days'])
def test_keep_and_not_yet_due(retained, field):
    db, term, path = retained
    term[field] = None
    assert not preview(db, term)['due'][field.removesuffix('_days')]
    term[field] = 30
    assert not preview(db, term, at=datetime(2020, 3, 1, tzinfo=timezone.utc))['due'][field.removesuffix('_days')]
    assert preview(db, term, at=datetime(2020, 3, 2, tzinfo=timezone.utc))['due'][field.removesuffix('_days')]
    assert path.exists()


@pytest.mark.parametrize('target', ['jobs', 'education_submissions'])
def test_nonterminal_refused(retained, target):
    db, term, path = retained
    db.execute(text("UPDATE " + target + " SET status='printing'")); db.commit()
    assert run(retained)['blocked_submissions'][0]['blocked_reason'] == 'work_not_terminal'
    assert path.exists()


def test_stale_preview_and_tenant_boundaries(retained):
    db, term, path = retained
    digest = preview(db, term)['fingerprint']
    db.execute(text('UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1')); db.commit()
    with pytest.raises(HTTPException) as error:
        purge(db, term['id'], digest, ADMIN)
    assert error.value.status_code == 409 and path.exists()
    with pytest.raises(HTTPException): policy(db, term['id'], 2)
    with pytest.raises(HTTPException): authority(db, {'id': 2})
    with pytest.raises(HTTPException): authority(db, ADMIN, 2)


def test_shared_file_and_archive_are_refused(retained):
    db, term, path = retained
    db.execute(text("INSERT INTO jobs(item_name,model_id,status) VALUES('shared',12,'completed')")); db.commit()
    assert run(retained)['blocked_submissions'][0]['blocked_reason'] == 'shared_or_unconfirmed_content'
    assert path.exists()


@pytest.mark.parametrize('attack', ['symlink', 'hardlink', 'changed', 'outside'])
def test_filesystem_refuses_unowned_bytes(retained, tmp_path, attack):
    db, term, path = retained
    if attack == 'symlink':
        path.unlink(); path.symlink_to(tmp_path / 'victim'); (tmp_path / 'victim').write_bytes(b'private')
    elif attack == 'hardlink':
        import os
        os.link(path, tmp_path / 'alias')
    elif attack == 'changed': path.write_bytes(b'changed')
    else:
        db.execute(text('UPDATE print_files SET stored_path=:path'), {'path': str(tmp_path / 'victim')})
        db.execute(text('UPDATE education_upload_operations SET final_path=:path'), {'path': str(tmp_path / 'victim')})
        db.execute(text('UPDATE print_archives SET file_path=:path'), {'path': str(tmp_path / 'victim')})
        db.commit()
    result = run(retained)
    assert next(r for r in result['results'] if r['kind'] == 'files')['state'] == 'blocked'
    assert path.exists()


def test_interrupted_unlink_can_resume(retained, monkeypatch):
    from modules.organizations import education_term_cleanup as cleanup
    db, term, path = retained
    original = cleanup.unlink_upload
    def interrupted(*a, **k):
        original(*a, **k)
        raise OSError('simulated interruption after unlink')
    monkeypatch.setattr(cleanup, 'unlink_upload', interrupted)
    assert run(retained)['results'][0]['state'] == 'blocked'
    assert not path.exists()
    assert db.execute(text("SELECT state FROM education_term_cleanup WHERE kind='files'")).scalar() == 'pending'
    monkeypatch.setattr(cleanup, 'unlink_upload', original)
    assert len(run(retained)['results']) == 4


def test_policy_overlap_and_validation(retained):
    db, term, _ = retained
    with pytest.raises(HTTPException) as error:
        create_policy(db, TermPolicy(**{k: term[k] for k in TermPolicy.model_fields}), ADMIN)
    assert error.value.status_code == 409
    with pytest.raises(ValueError):
        TermPolicy(cost_center_id=7, name='x', starts_at='2020-02-01T00:00:00Z', ends_at='2020-01-01T00:00:00Z')
    with pytest.raises(ValueError):
        TermPolicy(cost_center_id=7, name='x', starts_at='2020-01-01', ends_at='2020-02-01')


def test_bulk_graduation_reuses_real_erasure(retained):
    db, term, path = retained
    assert not eligible_student(db, 1, 1)
    db.execute(text('UPDATE users SET is_active=FALSE WHERE id=1'))
    assert not eligible_student(db, 1, 1)
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1")); db.commit()
    assert eligible_student(db, 1, 1)
    result = anonymize(db, term['id'], [1], preview(db, term)['fingerprint'], ADMIN)
    assert result['results'] == [{'user_id': 1, 'state': 'anonymized'}]
    assert db.execute(text('SELECT username,email FROM users WHERE id=1')).one() == ('[deleted-1]', '[deleted]')
    assert db.execute(text('SELECT COUNT(*) FROM education_submissions')).scalar() == 1
    assert path.exists()  # Erasure and byte retention are separate explicit choices.
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='gdpr_erasure'")).scalar() == 1


def test_graduation_preflights_whole_selection(retained):
    db, term, path = retained
    with pytest.raises(HTTPException):
        anonymize(db, term['id'], [1, 5], preview(db, term)['fingerprint'], ADMIN)
    assert db.execute(text('SELECT username FROM users WHERE id=1')).scalar() == 'student'


def test_migration_inventory_repeat_and_shape(retained):
    from core.schema import bootstrap_database
    from modules.system.postgres_backup_service import canonical_tables
    db, _, _ = retained
    db.rollback()
    assert bootstrap_database(db.get_bind())['applied'] == []
    for table in M.COLUMNS:
        assert table in canonical_tables()
        assert {c['name'] for c in inspect(db.get_bind()).get_columns(table)} == set(M.COLUMNS[table])
    with db.get_bind().begin() as c: M.validate(c)


@pytest.fixture
def client(retained, monkeypatch, tier_license):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations.routes_term_retention import router
    db, _, _ = retained
    tier_license('education')
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    return TestClient(app), app, tier_license


def test_http_license_and_live_role_enforced(client, retained):
    from core.dependencies import get_current_user
    c, app, install = client
    for tier in (None, 'pro'):
        install(tier)
        response = c.get('/education/term-retention')
        assert response.status_code == 403 and 'term_retention' in response.text
    for tier in ('education', 'enterprise'):
        install(tier)
        assert c.get('/education/term-retention').status_code == 200
    app.dependency_overrides[get_current_user] = lambda: {**ADMIN, 'id': 2, 'role': 'viewer'}
    assert c.get('/education/term-retention').status_code == 403


def test_http_dry_run_and_confirm(client, retained):
    c, _, _ = client
    db, term, path = retained
    response = c.get(f"/education/term-retention/{term['id']}/dry-run")
    assert response.status_code == 200, response.text
    assert response.headers['cache-control'] == 'private, no-store'
    assert path.exists()
    response = c.post(f"/education/term-retention/{term['id']}/purge", json={'fingerprint': response.json()['fingerprint']})
    assert response.status_code == 200, response.text
    assert not path.exists()


@pytest.mark.parametrize('role,uid', [('viewer', 1), ('viewer', 2), ('operator', 4)])
@pytest.mark.parametrize('method,suffix,body', [
    ('GET', '', None), ('POST', '', {}), ('DELETE', '/{id}', None), ('GET', '/{id}/dry-run', None),
    ('POST', '/{id}/purge', {'fingerprint': '0'*64}),
    ('POST', '/{id}/anonymize', {'fingerprint': '0'*64, 'user_ids': [1]}),
    ('POST', '/erasures/1/resume', None),
])
def test_all_routes_refuse_nonadmins(client, retained, role, uid, method, suffix, body):
    from core.dependencies import get_current_user
    c, app, _ = client
    app.dependency_overrides[get_current_user] = lambda: {**ADMIN, 'id': uid, 'role': role}
    response = c.request(method, '/education/term-retention' + suffix.format(id=retained[1]['id']), json=body)
    assert response.status_code == 403


@pytest.mark.parametrize('kind', ['user_api_token', 'legacy_global_api_key'])
def test_noninteractive_credentials_cannot_cleanup(client, retained, kind):
    from core.dependencies import get_current_user
    c, app, _ = client
    app.dependency_overrides[get_current_user] = lambda: {**ADMIN, '_auth_kind': kind, '_token_scopes': ['write:education', 'write']}
    assert c.get('/education/term-retention').status_code == 403


def test_superadmin_requires_explicit_org(retained):
    db, term, _ = retained
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(99,'platform','x','admin',TRUE)")); db.commit()
    with pytest.raises(HTTPException) as e: authority(db, {'id': 99})
    assert e.value.status_code == 400
    assert authority(db, {'id': 99}, 1) == 1


def test_policy_creator_erasure_and_retained_foreign_keys(retained):
    from modules.organizations.education_term_privacy import erase_term_identity
    from sqlalchemy.exc import IntegrityError
    db, term, _ = retained
    assert export_term_data(db, 3)['policies_created'][0]['id'] == term['id']
    erase_term_identity(db, 3); db.commit()
    assert export_term_data(db, 3)['policies_created'] == []
    for table, condition in [('groups', 'id=1'), ('education_cost_centers', 'id=7')]:
        with pytest.raises(IntegrityError):
            db.execute(text('DELETE FROM ' + table + ' WHERE ' + condition)); db.commit()
        db.rollback()


def test_storage_is_released_once_and_missing_account_blocks(retained):
    db, term, path = retained
    db.execute(text("DELETE FROM education_storage_accounts WHERE scope_kind='tenant'")); db.commit()
    assert run(retained)['results'][0]['state'] == 'blocked'
    assert path.exists()
    db.execute(text("INSERT INTO education_storage_accounts(org_id,scope_kind,scope_id,accounted_bytes) VALUES(1,'tenant',1,100)")); db.commit()
    assert run(retained)['results'][0]['state'] == 'done'
    assert db.execute(text('SELECT SUM(accounted_bytes) FROM education_storage_accounts')).scalar() == 0
    run(retained)
    assert db.execute(text('SELECT SUM(accounted_bytes) FROM education_storage_accounts')).scalar() == 0


def test_files_only_keeps_portfolio_and_thumbnails(retained):
    db, term, path = retained
    db.execute(text('UPDATE education_term_policies SET portfolio_days=NULL,thumbnails_days=NULL,timelapses_days=NULL'))
    db.commit(); term.update(portfolio_days=None, thumbnails_days=None, timelapses_days=None)
    assert run(retained)['results'] == [{'submission_id': 14, 'kind': 'files', 'state': 'done'}]
    assert db.execute(text('SELECT archived_at FROM education_submissions')).scalar() is None
    assert db.execute(text('SELECT thumbnail_b64 FROM print_files')).scalar() == 'preview'


def test_changed_file_graph_invalidates_preview(retained):
    db, term, path = retained
    digest = preview(db, term)['fingerprint']
    db.execute(text("UPDATE education_upload_operations SET expected_hash='changed'")); db.commit()
    with pytest.raises(HTTPException) as error: purge(db, term['id'], digest, ADMIN)
    assert error.value.status_code == 409 and path.exists()


def test_unknown_media_is_reported_not_silently_purged(retained):
    db, term, path = retained
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,started_at) VALUES(80,9,13,CURRENT_TIMESTAMP)"))
    db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(81,9,80,'legacy.mp4','ready')"))
    db.commit()
    result = run(retained)
    assert len([r for r in result['results'] if r['state'] == 'done']) == 3
    assert next(r for r in result['results'] if r['kind'] == 'timelapses')['reason'] == 'timelapse_ownership_unconfirmed'
    assert not path.exists()


def test_pagination_is_bound_to_the_confirmed_page(retained, monkeypatch):
    from modules.organizations import education_term_retention as service
    db, term, path = retained
    db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,accounted_bytes,final_path) VALUES('second',1,1,'committed',0,:path)"), {'path': str(path)})
    db.execute(text("INSERT INTO print_files(id,org_id,created_by,stored_path) VALUES(21,1,1,:path)"), {'path': str(path)})
    db.execute(text("INSERT INTO models(id,name,print_file_id,org_id) VALUES(22,'second',21,1)"))
    db.execute(text("INSERT INTO jobs(id,item_name,model_id,status,charged_to_org_id,submitted_by,charged_to_user_id) VALUES(23,'second',22,'completed',1,1,1)"))
    db.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision,created_at) VALUES(24,1,'second',23,21,22,7,1,'completed',1,'2020-01-20 00:00:00')")); db.commit()
    monkeypatch.setattr(service, 'MAX_ITEMS', 1)
    first = preview(db, term)
    second = preview(db, term, after_id=first['next_after_id'])
    assert first['next_after_id'] == 14 and second['next_after_id'] is None
    assert [i['submission_id'] for i in second['items']] == [24]
    with pytest.raises(HTTPException) as error:
        purge(db, term['id'], first['fingerprint'], ADMIN, after_id=14)
    assert error.value.status_code == 409 and path.exists()


def test_content_replaced_after_purge_admission_is_not_deleted(retained, monkeypatch):
    from modules.organizations import education_term_cleanup as cleanup
    db, term, path = retained
    original = cleanup.audit
    def mutate(db, action, term_id, details):
        original(db, action, term_id, details)
        if action == 'purge_started':
            db.execute(text("UPDATE education_upload_operations SET expected_hash='new-payload'"))
    monkeypatch.setattr(cleanup, 'audit', mutate)
    result = run(retained)
    assert all(r['state'] == 'blocked' for r in result['results'])
    assert path.exists()


def test_student_with_grant_but_no_prints_can_graduate(retained):
    from modules.organizations.education_term_privacy import term_student_ids
    db, term, _ = retained
    db.execute(text('UPDATE users SET is_active=FALSE WHERE id=4'))
    db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by,granted_at,revoked_at) VALUES(1,7,4,'student','revoked',3,'2020-01-01','2020-02-01')")); db.commit()
    assert 4 in term_student_ids(db, term)
    result = anonymize(db, term['id'], [4], preview(db, term)['fingerprint'], ADMIN)
    assert result['results'] == [{'user_id': 4, 'state': 'anonymized'}]


def test_concurrent_cleanup_preserves_receipts_and_storage_balance(retained):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    db, term, path = retained
    fingerprint = preview(db, term)['fingerprint']
    db.rollback()
    barrier = Barrier(2)
    def execute():
        with Session(db.get_bind()) as worker:
            barrier.wait(timeout=5)
            try:
                return purge(worker, term['id'], fingerprint, ADMIN)
            except HTTPException as error:
                assert error.status_code == 409  # The other request changed the preview.
                return None
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(lambda _: execute(), range(2)))
    assert any(result is not None for result in outcomes)
    assert not path.exists()
    assert db.execute(text('SELECT SUM(accounted_bytes) FROM education_storage_accounts')).scalar() == 0
    assert db.execute(text("SELECT COUNT(*) FROM education_term_cleanup WHERE state='done'")).scalar() == 4
    assert db.execute(text('SELECT COUNT(*) FROM education_submissions')).scalar() == 1
