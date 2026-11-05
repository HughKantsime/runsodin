"""Password and actorless SSO history erased through the real admin route."""
import json
import time

import pytest
from sqlalchemy import event, text

from tests.test_contracts.test_audit_actor_requests import client, review_db


def history(db):
    db.rollback()
    return {r.id: tuple(r) for r in db.execute(text(
        "SELECT id,action,entity_type,entity_id,CAST(details AS TEXT),ip_address "
        "FROM audit_logs ORDER BY id"))}


@pytest.mark.parametrize('username', [
    'student', 'stud%ent', 'stud_ent', 'username', 'zoë', 'a"b\\c', 'student from lab',
])
def test_password_history_erasure_is_exact(client, review_db, monkeypatch, username):
    from core import auth_helpers
    from core.audit import log_audit
    c, bearer, _, _ = client
    review_db.execute(text('UPDATE users SET username=:name WHERE id=1'), {'name': username})
    review_db.commit()
    monkeypatch.setattr(auth_helpers, '_last_login_cleanup', time.time())
    target_ids = set()
    # Prefixes, LIKE wildcards, case changes, and a different account sharing
    # the IP must not cause erasure of that account's audit or throttle rows.
    others = sorted({username + '2', username + '%other', username.upper(),
                     username.replace('%', 'XYZ').replace('_', 'X'), 'unrelated'} - {username})
    for name in [username] + others:
        before = set(history(review_db))
        for success in (True, False):
            assert auth_helpers._record_login_attempt(review_db, '192.0.2.9', name, success)
        log_audit(review_db, 'auth.login_failed', 'user', details={'username': name}, ip='192.0.2.9')
        review_db.commit()
        if name == username:
            target_ids.update(set(history(review_db)) - before)
    before = history(review_db)
    attempts_before = review_db.execute(text(
        'SELECT id,ip,username,attempted_at,success FROM login_attempts WHERE username != :name ORDER BY id'
    ), {'name': username}).all()
    review_db.rollback()
    response = c.delete('/users/1/erase', headers=bearer('admin'))
    assert response.status_code == 200, response.text
    after = history(review_db)
    for row_id in target_ids:
        row = after[row_id]
        assert row[:4] == before[row_id][:4]
        assert row[5] is None
        expected = {'login_success': 'Login: [deleted]', 'login_failed': 'Failed login: [deleted]',
                    'auth.login_failed': {'username': '[deleted]'}}[row[1]]
        assert json.loads(row[4]) == expected
    assert {i: after[i] for i in before if i not in target_ids} == {
        i: row for i, row in before.items() if i not in target_ids}
    assert review_db.execute(text(
        'SELECT id,ip,username,attempted_at,success FROM login_attempts ORDER BY id')).all() == attempts_before
    # Repeated erasure must preserve all surviving history.
    review_db.rollback()
    assert c.delete('/users/1/erase', headers=bearer('admin')).status_code == 200
    repeated = history(review_db)
    assert {i: repeated[i] for i in before} == {i: after[i] for i in before}


def test_actorless_sso_erasure_only_clears_target_login(client, review_db):
    from core.audit import log_audit
    c, bearer, _, _ = client
    for action, entity, uid in [('auth.login', 'user', 1), ('auth.login', 'user', 2),
                                ('auth.login', 'printer', 1), ('auth.login_failed', 'user', None)]:
        log_audit(review_db, action, entity, uid, {'method': 'oidc'}, ip='192.0.2.9')
    review_db.commit()
    before = history(review_db)
    assert c.delete('/users/1/erase', headers=bearer('admin')).status_code == 200
    after = history(review_db)
    for row_id, row in before.items():
        if row[1:4] == ('auth.login', 'user', 1):
            assert after[row_id][5] is None
        else:
            assert after[row_id] == row


def test_password_scrub_failure_rolls_back_erasure(client, review_db):
    from core.auth_helpers import _record_login_attempt
    c, bearer, _, _ = client
    assert _record_login_attempt(review_db, '192.0.2.9', 'student', False)
    before = history(review_db)
    review_db.rollback()
    def refuse(conn, cursor, statement, params, context, executemany):
        if statement.startswith('UPDATE audit_logs SET details='):
            raise RuntimeError('Owned password-history scrub refusal')
    engine = review_db.get_bind()
    event.listen(engine, 'before_cursor_execute', refuse)
    try:
        with pytest.raises(RuntimeError, match='Owned password-history scrub refusal'):
            c.delete('/users/1/erase', headers=bearer('admin'))
    finally:
        event.remove(engine, 'before_cursor_execute', refuse)
    assert history(review_db) == before
    assert review_db.execute(text('SELECT username FROM users WHERE id=1')).scalar_one() == 'student'
    assert review_db.execute(text('SELECT count(*) FROM audit_actor_erasures WHERE user_id=1')).scalar_one() == 0


def test_password_history_scrubs_multiple_pages(client, review_db):
    c, bearer, _, _ = client
    review_db.execute(text("INSERT INTO audit_logs(action,entity_type,details,ip_address) "
                           "VALUES('login_failed','user',:details,'192.0.2.9')"),
                      [{'details': json.dumps('Failed login: student from 192.0.2.9')} for _ in range(501)])
    review_db.commit()
    assert c.delete('/users/1/erase', headers=bearer('admin')).status_code == 200
    rows = review_db.execute(text("SELECT CAST(details AS TEXT),ip_address FROM audit_logs WHERE action='login_failed'")).all()
    assert len(rows) == 501
    assert all(json.loads(details) == 'Failed login: [deleted]' and ip is None for details, ip in rows)


def test_view_and_sso_history_exported_and_erased_through_routes(client, review_db):
    from core.audit import log_audit
    from core.audit_actor import RequestAuditScope, bind_verified_actor
    c, bearer, _, _ = client
    log_audit(review_db, 'auth.login', 'user', 1,
              {'method': 'oidc', 'username': 'student'}, ip='192.0.2.9')
    log_audit(review_db, 'auth.login_failed', 'user', details={
        'method': 'oidc', 'username': 'student', 'reason': 'user_inactive'}, ip='192.0.2.9')
    scope = RequestAuditScope('192.0.2.9'); scope.register(review_db)
    bind_verified_actor(review_db, {'id': 1, 'username': 'student'}, 'user_api_token')
    log_audit(review_db, 'camera.viewed', 'printer', 9, {'transport': 'snapshot'})
    log_audit(review_db, 'timelapse.viewed', 'timelapse', 9)
    review_db.commit(); scope.clear()
    exported = c.get('/users/1/export', headers=bearer('admin'))
    assert exported.status_code == 200
    assert {'camera.viewed', 'timelapse.viewed'} <= {
        row['action'] for row in exported.json()['audit_actions_performed']}
    assert c.delete('/users/1/erase', headers=bearer('admin')).status_code == 200
    review_db.rollback()
    found = review_db.execute(text("SELECT action,entity_id,details,actor_username,ip_address FROM audit_logs "
                                   "WHERE action IN ('auth.login','auth.login_failed','camera.viewed','timelapse.viewed')")).all()
    assert len(found) == 4
    for row in found:
        assert row.actor_username is None and row.ip_address is None
        assert 'student' not in row.details
        if row.action == 'auth.login': assert row.entity_id == 1
