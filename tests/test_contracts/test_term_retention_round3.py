"""D5 round three: fair hash retries, durable recovery status and busy accounts."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_term_retention import retained, client
from tests.test_contracts.test_term_retention_review import departed
from modules.organizations import education_term_cleanup as cleanup
from modules.organizations import education_term_files as files
from modules.organizations.education_term_retention import preview


def test_later_hash_timeout_is_revisited_by_next_request(client, retained, monkeypatch):
    c, _, _ = client
    db, term, first_path = retained
    second_path = first_path.with_name('upload-2.3mf')
    second_path.write_bytes(first_path.read_bytes())
    for statement in (
        "INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,accounted_bytes,final_path,expected_hash) "
        "SELECT 'upload-2',org_id,user_id,state,accounted_bytes,:path,expected_hash FROM education_upload_operations WHERE operation_id='upload-1'",
        "INSERT INTO print_files(id,filename,org_id,created_by,stored_path,storage_bytes,blob_state) VALUES(21,'upload-2.3mf',1,1,:path,100,'present')",
        "INSERT INTO models(id,name,print_file_id,org_id) VALUES(22,'second',21,1)",
        "INSERT INTO jobs(id,item_name,model_id,status,submitted_by,charged_to_user_id,charged_to_org_id) VALUES(23,'second',22,'completed',1,1,1)",
        "INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision,created_at) "
        "VALUES(24,1,'upload-2',23,21,22,7,1,'completed',1,'2020-01-15')",
        "UPDATE education_storage_accounts SET accounted_bytes=200",
    ):
        db.execute(text(statement), {'path': str(second_path)})
    db.commit()
    elapsed, attempts = [0], []
    clock = SimpleNamespace(monotonic=lambda: elapsed[0])
    monkeypatch.setattr(cleanup, 'time', clock)
    monkeypatch.setattr(files, 'time', clock)
    original = cleanup.prepare_upload

    @contextmanager
    def slow_hash(row, **kwargs):
        attempts.append(row['id'])
        elapsed[0] += 9 if row['id'] == 14 else 2
        with original(row, **kwargs) as prepared:
            yield prepared

    monkeypatch.setattr(cleanup, 'prepare_upload', slow_hash)
    url = f"/education/term-retention/{term['id']}"
    first = c.post(url + '/purge', json={'fingerprint': preview(db, term)['fingerprint']})
    assert first.status_code == 200
    result = first.json()
    assert result['next_after_id'] == 14
    assert {r['submission_id'] for r in result['results']} == {14}
    assert {r['state'] for r in result['results']} == {'done'}
    assert not first_path.exists() and second_path.exists()
    assert db.execute(text('SELECT count(*) FROM education_term_cleanup WHERE submission_id=24')).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='term_retention_item_blocked'")).scalar() == 0
    after = result['next_after_id']
    planned = c.get(url + '/dry-run', params={'after_id': after}).json()
    assert [item['submission_id'] for item in planned['items']] == [24]
    second = c.post(url + '/purge', json={'fingerprint': planned['fingerprint'], 'after_id': after})
    assert second.status_code == 200
    assert {r['submission_id'] for r in second.json()['results']} == {24}
    assert {r['state'] for r in second.json()['results']} == {'done'}
    assert second.json()['next_after_id'] is None
    assert attempts == [14, 24, 24]
    assert not second_path.exists()


@pytest.mark.parametrize('stage', ['before_pending', 'uncommitted_pending', 'committed_pending'])
def test_erasure_result_depends_on_pending_row_after_rollback(client, retained, monkeypatch, stage):
    from modules.organizations import gdpr_erasure
    c, _, _ = client
    db, term, _ = retained
    departed(db)

    def interrupted(uid, current_user, db):
        if stage != 'before_pending':
            db.execute(text("INSERT INTO education_erasure_pending VALUES(:uid,'{}')"), {'uid': uid})
            if stage == 'committed_pending':
                db.commit()
        raise RuntimeError('private failure detail must not escape')

    monkeypatch.setattr(gdpr_erasure, 'erase_user_data_sync', interrupted)
    response = c.post(f"/education/term-retention/{term['id']}/anonymize", json={
        'fingerprint': preview(db, term)['fingerprint'], 'user_ids': [1]})
    assert response.status_code == 200
    pending = stage == 'committed_pending'
    expected = {'user_id': 1, 'state': 'erasure_incomplete'} if pending else {
        'user_id': 1, 'state': 'blocked', 'reason': 'erasure_failed'}
    assert response.json()['results'] == [expected]
    assert bool(db.execute(text('SELECT count(*) FROM education_erasure_pending')).scalar()) == pending
    assert bool(c.get('/education/term-retention').json()['pending_erasures']) == pending
    action = 'term_retention_anonymization_incomplete' if pending else 'term_retention_anonymization_blocked'
    assert db.execute(text('SELECT count(*) FROM audit_logs WHERE action=:action'), {'action': action}).scalar() == 1
    assert 'private failure' not in response.text


@pytest.mark.parametrize('changed', ['operator', 'admin', 'active', 'grant', 'job'])
def test_pending_list_excludes_accounts_that_cannot_resume(client, retained, changed):
    c, _, _ = client
    db, _, _ = retained
    departed(db)
    db.execute(text("INSERT INTO education_erasure_pending VALUES(1,'{}'),(4,'{}')"))
    db.execute(text({
        'operator': "UPDATE users SET role='operator' WHERE id=1",
        'admin': "UPDATE users SET role='admin' WHERE id=1",
        'active': 'UPDATE users SET is_active=TRUE WHERE id=1',
        'grant': "UPDATE education_cost_center_grants SET state='active' WHERE user_id=1",
        'job': "UPDATE jobs SET status='printing' WHERE submitted_by=1",
    }[changed]))
    db.commit()
    assert c.get('/education/term-retention').json()['pending_erasures'] == [{'user_id': 4, 'username': 'outsider'}]
    assert c.post('/education/term-retention/erasures/1/resume').status_code == 409
    assert db.execute(text('SELECT count(*) FROM education_erasure_pending')).scalar() == 2


@pytest.mark.parametrize('phase', ['preflight', 'per_account', 'resume'])
@pytest.mark.parametrize('code_attribute', ['sqlstate', 'pgcode'])
def test_student_lock_timeout_returns_http_409(client, retained, phase, code_attribute):
    from core.db import get_db
    c, app, _ = client
    db, term, _ = retained
    departed(db)
    if phase == 'resume':
        db.execute(text("INSERT INTO education_erasure_pending VALUES(1,'{}')"))
        db.commit()
    fingerprint = preview(db, term)['fingerprint']
    db.rollback()
    calls, rollbacks = [], []

    class ContendedSession:
        def __getattr__(self, name):
            return getattr(db, name)

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))

        def rollback(self):
            rollbacks.append(True)
            db.rollback()

        def execute(self, statement, params=None):
            sql = str(statement)
            if sql.startswith('SET LOCAL lock_timeout'):
                return None
            if sql.startswith('SELECT role,is_active,group_id,username FROM users') and sql.endswith('FOR UPDATE'):
                calls.append(params['uid'])
                if len(calls) == (2 if phase == 'per_account' else 1):
                    error = Exception('private PostgreSQL error')
                    setattr(error, code_attribute, '55P03')
                    raise OperationalError(sql, params, error)
            return db.execute(text(sql.removesuffix(' FOR UPDATE')), params or {})

    app.dependency_overrides[get_db] = lambda: ContendedSession()
    response = c.post('/education/term-retention/erasures/1/resume') if phase == 'resume' else c.post(
        f"/education/term-retention/{term['id']}/anonymize", json={'fingerprint': fingerprint, 'user_ids': [1]})
    assert response.status_code == 409
    assert response.json() == {'detail': 'student_busy_retry'}
    assert calls == ([1, 1] if phase == 'per_account' else [1])
    assert rollbacks
    assert db.execute(text('SELECT username FROM users WHERE id=1')).scalar() == 'student'
    assert db.execute(text('SELECT count(*) FROM education_erasure_pending')).scalar() == (1 if phase == 'resume' else 0)


def test_student_lock_other_database_errors_are_not_busy(retained, monkeypatch):
    from modules.organizations.education_term_privacy import eligible_student
    db, _, _ = retained
    monkeypatch.setattr(db, 'get_bind', lambda: SimpleNamespace(dialect=SimpleNamespace(name='postgresql')))
    error = OperationalError('query', {}, SimpleNamespace(sqlstate='08006'))
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(db, 'execute', fail)
    with pytest.raises(OperationalError) as caught:
        eligible_student(db, 1, 1, lock=True)
    assert caught.value is error
