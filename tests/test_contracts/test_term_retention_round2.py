"""D5 round two: slow hashes, stale plans, concurrent winners and erasure recovery."""
from contextlib import contextmanager
import pytest
from sqlalchemy import text
from tests.test_contracts.test_term_retention import retained, client, ADMIN, run
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_term_retention_review import departed
from modules.organizations import education_term_cleanup as cleanup
from modules.organizations.education_term_retention import preview


def test_slow_hash_advances_cursor_past_the_blocked_file(retained, monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    db, term, path = retained
    planned = preview(db, term)
    planned['items'].append({**planned['items'][0], 'submission_id': 15})
    monkeypatch.setattr(cleanup, 'preview', lambda *a, **k: planned)
    elapsed = [0]
    monkeypatch.setattr(cleanup, 'time', SimpleNamespace(monotonic=lambda: elapsed[0]))
    @contextmanager
    def slow_hash(*args, **kwargs):
        elapsed[0] = 11
        raise HTTPException(409, 'request_budget_exhausted')
        yield  # pragma: no cover - contextmanager protocol
    monkeypatch.setattr(cleanup, 'prepare_upload', slow_hash)
    result = cleanup.purge(db, term['id'], planned['fingerprint'], ADMIN)
    assert result['next_after_id'] == 14
    assert {r['reason'] for r in result['results']} == {'hash_budget_exhausted'}
    assert path.exists()


def test_stale_content_is_rejected_before_any_file_hash(retained, monkeypatch):
    db, _, path = retained
    original = cleanup.audit
    def audit(db, action, term, details):
        original(db, action, term, details)
        if action == 'purge_started':
            db.execute(text('UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1'))
    monkeypatch.setattr(cleanup, 'audit', audit)
    monkeypatch.setattr(cleanup, 'prepare_upload', lambda *a, **k: pytest.fail('Stale plan must not hash'))
    assert {r['reason'] for r in run(retained)['results']} == {'submission_changed'}
    assert path.exists()


@pytest.mark.parametrize('window', ['before_hash', 'after_hash', 'after_intent'])
def test_losing_purge_never_audits_winners_completed_items(retained, monkeypatch, window):
    db, _, _ = retained
    original_audit, original_prepare, original_commit = cleanup.audit, cleanup.prepare_upload, db.commit
    won = False
    intent = False
    def winner():
        nonlocal won
        if not won:
            won = True
            assert {r['state'] for r in run(retained)['results']} == {'done'}
    def audit(db, action, term, details):
        nonlocal intent
        original_audit(db, action, term, details)
        if not won and action == 'purge_started' and window == 'before_hash':
            original_commit()
            winner()
        if not won and action == 'item_started':
            intent = True
    @contextmanager
    def prepare(*args, **kwargs):
        with original_prepare(*args, **kwargs) as prepared:
            if window == 'after_hash':
                winner()
            yield prepared
    def commit():
        original_commit()
        if intent and window == 'after_intent':
            winner()
    monkeypatch.setattr(cleanup, 'audit', audit)
    monkeypatch.setattr(cleanup, 'prepare_upload', prepare)
    monkeypatch.setattr(db, 'commit', commit)
    assert run(retained)['results'] == []
    assert won
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='term_retention_item_blocked'")).scalar() == 0
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='term_retention_item_completed'")).scalar() == 4


def test_only_unfinished_categories_are_reported_blocked(retained):
    db, term, _ = retained
    db.execute(text("INSERT INTO education_term_cleanup VALUES(:term,14,'files','done','2020-01-01')"), {'term': term['id']})
    db.commit()
    results = []
    cleanup.record_blocked(db, term['id'], 14, ['files', 'thumbnails'], 'submission_changed', results)
    assert [r['kind'] for r in results] == ['thumbnails']
    assert 'files' not in str(db.execute(text("SELECT details FROM audit_logs WHERE action='term_retention_item_blocked'")).all())


def test_vanished_submission_continues_to_later_plan_items(retained, monkeypatch):
    db, term, _ = retained
    planned = preview(db, term)
    # A selected row vanishes after preview; the later real submission must run.
    planned['items'].insert(0, {**planned['items'][0], 'submission_id': 10})
    monkeypatch.setattr(cleanup, 'preview', lambda *a, **k: planned)
    result = cleanup.purge(db, term['id'], planned['fingerprint'], ADMIN)
    assert {r['reason'] for r in result['results'] if r['submission_id'] == 10} == {'submission_gone'}
    assert {r['state'] for r in result['results'] if r['submission_id'] == 14} == {'done'}


def test_admin_can_find_and_resume_interrupted_erasure(client, retained, monkeypatch):
    from core import identity_scrub
    c, _, _ = client
    db, term, _ = retained
    departed(db)
    db.execute(text("INSERT INTO alerts(user_id,alert_type,severity,title,message) VALUES(3,'print_complete','info','student','student@example.test')"))
    db.commit()
    original = identity_scrub.scrub_text
    def fail(*a):
        raise RuntimeError('Interrupted erasure chunk')
    monkeypatch.setattr(identity_scrub, 'scrub_text', fail)
    response = c.post(f"/education/term-retention/{term['id']}/anonymize", json={
        'fingerprint': preview(db, term)['fingerprint'], 'user_ids': [1]})
    assert response.status_code == 200
    assert response.json()['results'] == [{'user_id': 1, 'state': 'erasure_incomplete'}]
    listing = c.get('/education/term-retention')
    assert listing.json()['pending_erasures'] == [{'user_id': 1, 'username': '[deleted-1]'}]
    assert listing.headers['Cache-Control'] == 'private, no-store'
    assert 'student@example.test' not in listing.text and 'identifiers_json' not in listing.text
    assert c.post('/education/term-retention/erasures/1/resume').json()['state'] == 'erasure_incomplete'
    monkeypatch.setattr(identity_scrub, 'scrub_text', original)
    assert c.post('/education/term-retention/erasures/1/resume').json()['state'] == 'anonymized'
    assert c.get('/education/term-retention').json()['pending_erasures'] == []
    assert c.post('/education/term-retention/erasures/1/resume').status_code == 409


def test_pending_erasure_list_and_resume_are_tenant_scoped(client, retained):
    c, _, _ = client
    db, _, _ = retained
    departed(db)
    db.execute(text("INSERT INTO education_erasure_pending VALUES(1,'{}')"))
    db.execute(text('UPDATE users SET group_id=2 WHERE id=1'))
    db.commit()
    assert c.get('/education/term-retention').json()['pending_erasures'] == []
    assert c.post('/education/term-retention/erasures/1/resume').status_code == 409
    assert db.execute(text('SELECT count(*) FROM education_erasure_pending')).scalar() == 1


@pytest.mark.parametrize('changed', ['active', 'grant', 'role'])
def test_resume_rechecks_student_eligibility(client, retained, changed):
    c, _, _ = client
    db, _, _ = retained
    departed(db)
    db.execute(text("INSERT INTO education_erasure_pending VALUES(1,'{}')"))
    db.execute(text({
        'active': 'UPDATE users SET is_active=TRUE WHERE id=1',
        'grant': "UPDATE education_cost_center_grants SET state='active' WHERE user_id=1",
        'role': "UPDATE users SET role='operator' WHERE id=1",
    }[changed]))
    db.commit()
    assert c.post('/education/term-retention/erasures/1/resume').status_code == 409
    assert db.execute(text('SELECT username FROM users WHERE id=1')).scalar() == 'student'


@pytest.mark.parametrize('kind', ['user_api_token', 'legacy_global_api_key'])
def test_resume_requires_interactive_administrator(client, kind):
    from core.dependencies import get_current_user
    c, app, _ = client
    app.dependency_overrides[get_current_user] = lambda: {**ADMIN, '_auth_kind': kind}
    assert c.post('/education/term-retention/erasures/1/resume').status_code == 403


def test_graduation_locks_each_eligibility_check(retained, monkeypatch):
    from modules.organizations import education_term_privacy as privacy
    db, term, _ = retained
    departed(db)
    original = privacy.eligible_student
    calls = []
    def eligible(db, uid, org, *, lock=False):
        calls.append((uid, lock))
        return original(db, uid, org, lock=lock)
    monkeypatch.setattr(privacy, 'eligible_student', eligible)
    assert privacy.anonymize(db, term['id'], [1], preview(db, term)['fingerprint'], ADMIN)['results'][0]['state'] == 'anonymized'
    assert calls == [(1, True), (1, True)]
