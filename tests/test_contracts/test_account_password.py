"""Self-service password changes through real credential checks and owned databases."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.auth import hash_password
from core.rate_limit import limiter
from modules.organizations import routes_auth
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_audit_actor_requests import client as audit_client
from tests.test_contracts.test_education_classes_migration import native_engine, pg_engine
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_mfa_code_lockout import account_app, both_databases
from tests.test_contracts.test_password_reset_behavior import new_password, reset_client, seed_link

PATH = '/auth/me/password'


def change(client, headers, current, replacement):
    return client.post(PATH, headers=headers, json={'current_password': current, 'new_password': replacement})


@pytest.mark.parametrize('role', ['admin', 'operator', 'viewer'])
def test_each_role_can_change_own_password(role, audit_client, review_db):
    client, _, _, _ = audit_client
    limiter._storage.reset()
    current, replacement = new_password(), new_password()
    # The viewer in this fixture also has an active Education student grant.
    review_db.execute(text('UPDATE users SET role=:role, password_hash=:h WHERE id=1'),
                      {'role': role, 'h': hash_password(current)})
    review_db.commit()
    login = client.post('/auth/login', data={'username': 'student', 'password': current})
    assert login.status_code == 200
    assert client.get('/auth/me').json()['has_local_password'] is True
    assert change(client, {}, current, replacement).status_code == 200
    assert client.get('/auth/me').status_code == 200
    row = review_db.execute(text("SELECT actor_user_id,entity_id,details FROM audit_logs "
                                 "WHERE action='user.password_changed'")).one()
    assert row.actor_user_id == row.entity_id == 1
    assert json.loads(row.details) == {'actor_user_id': 1, 'target_user_id': 1}
    assert client.post('/auth/login', data={'username': 'student', 'password': current}).status_code == 401
    assert client.post('/auth/login', data={'username': 'student', 'password': replacement}).status_code == 200


@pytest.mark.parametrize('limit,seed_count,expected', [('account', 4, 423), ('address', 9, 429)])
def test_wrong_password_counts_toward_sign_in_limits(reset_client, review_db, limit, seed_count, expected):
    client, sessions, current = reset_client
    review_db.execute(text('INSERT INTO login_attempts(username,ip,attempted_at,success) VALUES(:u,:ip,:t,0)'),
                      [{'u': 'student' if limit == 'account' else f'other{i}',
                        'ip': 'testclient' if limit == 'address' else '192.0.2.1', 't': time.time()}
                       for i in range(seed_count)])
    review_db.commit()
    assert change(client, sessions[0], new_password(), 'weak').status_code == 401
    assert change(client, sessions[0], current, new_password()).status_code == expected
    assert client.post('/auth/login', data={'username': 'student', 'password': current}).status_code == expected
    assert client.get('/auth/me', headers=sessions[0]).status_code == 200


def test_sign_in_and_change_share_minute_address_limit(reset_client):
    client, sessions, current = reset_client
    for _ in range(5):
        assert client.post('/auth/login', data={'username': 'student', 'password': current}).status_code == 200
        assert change(client, sessions[0], current, 'weak').status_code == 400
    assert change(client, sessions[0], current, new_password()).status_code == 429
    assert client.post('/auth/login', data={'username': 'student', 'password': current}).status_code == 429


def test_rejects_same_or_weak_replacement_without_revocation(reset_client):
    client, sessions, current = reset_client
    for replacement in (current, 'weak'):
        assert change(client, sessions[0], current, replacement).status_code == 400
    assert client.get('/auth/me', headers=sessions[1]).status_code == 200
    assert change(client, sessions[0], current, new_password()).status_code == 200


@pytest.mark.parametrize('scope', ['admin', 'agent:write'])
def test_noninteractive_tokens_cannot_change_password(token_client, scope):
    client, issue = token_client
    client.app.include_router(routes_auth.router)
    assert change(client, issue([scope]), new_password(), new_password()).status_code == 403


def test_sso_only_account_is_refused_before_checking_password(reset_client, review_db):
    client, sessions, current = reset_client
    review_db.execute(text("UPDATE users SET oidc_issuer='fixture',oidc_subject='subject' WHERE id=1"))
    review_db.commit()
    assert client.get('/auth/me', headers=sessions[0]).json()['has_local_password'] is True
    review_db.execute(text("UPDATE users SET password_hash='' WHERE id=1"))
    review_db.commit()
    assert client.get('/auth/me', headers=sessions[0]).json()['has_local_password'] is False
    assert change(client, sessions[0], current, 'weak').status_code == 409
    assert review_db.execute(text("SELECT password_hash FROM users WHERE id=1")).scalar_one() == ''
    assert review_db.execute(text('SELECT count(*) FROM login_attempts')).scalar_one() == 0
    assert client.get('/auth/me', headers=sessions[1]).status_code == 200


@both_databases
def test_revokes_other_sessions_and_biometrics_only(account_app, native_engine):
    current = new_password()
    with native_engine.begin() as db:
        db.execute(text('UPDATE users SET password_hash=:h WHERE id=1'), {'h': hash_password(current)})
    client = TestClient(account_app)
    tokens = []
    for _ in range(2):
        login = client.post('/auth/login', data={'username': 'student', 'password': current})
        assert login.status_code == 200
        tokens.append(login.json()['access_token'])
    with native_engine.begin() as db:
        db.execute(text("INSERT INTO biometric_tokens(user_id,device_id,token_hash,expires_at,is_revoked) "
                        "VALUES(1,'fixture','fixture','2099-01-01',FALSE)"))
    client.cookies.clear()
    headers = {'Authorization': 'Bearer ' + tokens[0], 'Cookie': 'session=expired'}
    assert change(client, headers, current, new_password()).status_code == 200
    assert client.get('/auth/me', headers=headers).status_code == 200
    assert client.get('/auth/me', headers={'Authorization': 'Bearer ' + tokens[1]}).status_code == 401
    with native_engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM active_sessions')).scalar_one() == 1
        assert db.execute(text('SELECT count(*) FROM token_blacklist')).scalar_one() == 1
        assert db.execute(text('SELECT count(*) FROM biometric_tokens')).scalar_one() == 0


def test_invalidates_reset_and_pending_enrollment_preserving_mfa(reset_client, review_db):
    client, sessions, current = reset_client
    link = seed_link(review_db)
    review_db.execute(text("UPDATE users SET mfa_enabled=TRUE,mfa_secret='fixture' WHERE id=1"))
    review_db.execute(text("INSERT INTO mfa_recovery_codes(user_id,code_salt,code_hash,created_at) "
                           "VALUES(1,'salt','hash',CURRENT_TIMESTAMP)"))
    review_db.execute(text("INSERT INTO mfa_enrollments(jti,user_id,expires_at,state,created_at) "
                           "VALUES('pending',1,253402300799,'pending',100)"))
    review_db.commit()
    codes = text('SELECT * FROM mfa_recovery_codes WHERE user_id=1')
    before = review_db.execute(codes).all()
    assert change(client, sessions[0], current, new_password()).status_code == 200
    assert client.post('/auth/reset-password', json={'token': link, 'new_password': new_password()}).status_code == 400
    assert review_db.execute(text("SELECT state FROM mfa_enrollments WHERE jti='pending'")).scalar_one() == 'cancelled'
    assert review_db.execute(text('SELECT mfa_enabled,mfa_secret FROM users WHERE id=1')).one() == (True, 'fixture')
    assert review_db.execute(codes).all() == before
