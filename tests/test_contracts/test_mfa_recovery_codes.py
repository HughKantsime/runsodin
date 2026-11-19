"""Recovery codes across sign-in, account management, privacy and migration."""
from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import logging
import re
import secrets
import sqlite3
from threading import Barrier

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from core import auth
from core.db import get_db
from core.rate_limit import limiter
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError, apply_python_migration
from modules.organizations import routes_auth, routes_sessions, routes_users
from tests.test_contracts.test_education_classes_migration import pg_engine
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_mfa_authentication_behavior import challenge
from tests.test_contracts.test_password_reset_behavior import new_password, seed_link

CODES = '/auth/mfa/recovery-codes'
MIGRATION = importlib.import_module('core.schema.migrations.063_mfa_recovery_codes')


def headers(username='student', role='viewer'):
    return {'Authorization': 'Bearer ' + auth.create_access_token({'sub': username, 'role': role})}


def make_app(engine, monkeypatch):
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.delenv('API_KEY', raising=False)
    limiter._storage.reset()
    app = FastAPI()
    app.include_router(routes_auth.router)
    app.include_router(routes_sessions.router)
    app.include_router(routes_users.router)

    def database():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = database
    from core.middleware import idempotency
    from core import db, db_compat
    for module in (db, db_compat):
        monkeypatch.setattr(module, "IS_SQLITE", engine.dialect.name == "sqlite")
        monkeypatch.setattr(module, "IS_POSTGRES", engine.dialect.name == "postgresql")
    monkeypatch.setattr(idempotency, "_SCHEMA_READY_CACHE", {"ready": False, "checked_at": 0.0})
    monkeypatch.setattr(db, 'SessionLocal', sessionmaker(bind=engine))
    app.middleware('http')(idempotency.idempotency_middleware)
    return app


@pytest.fixture
def client(review_db, monkeypatch):
    return TestClient(make_app(review_db.get_bind(), monkeypatch))


def issue(client, who=None):
    who = who or headers()
    setup = client.post('/auth/mfa/setup', headers=who)
    assert setup.status_code == 200
    totp = pyotp.TOTP(setup.json()['secret'])
    response = client.post('/auth/mfa/confirm', headers={**who, 'Idempotency-Key': 'confirm'},
                           json={'code': totp.now()})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    codes = response.json()['recovery_codes']
    assert len(set(codes)) == len(codes) == 10
    assert all(re.fullmatch('[2-9A-HJKMNP-Z]{5}-[2-9A-HJKMNP-Z]{5}', code) for code in codes)
    return totp, codes


def verify(client, recovery_code, token=None):
    return client.post('/auth/mfa/verify', json={'mfa_token': token or challenge(), 'recovery_code': recovery_code})


def test_issue_use_replace_disable_and_privacy(client, review_db, caplog):
    caplog.set_level(logging.INFO)
    who = headers()
    totp, codes = issue(client, who)
    status = client.get(CODES, headers=who).json()
    assert set(status) == {'remaining', 'generated_at'} and status['generated_at'] and status['remaining'] == 10
    rows = review_db.execute(text('SELECT code_salt,code_hash FROM mfa_recovery_codes')).all()
    assert len({r.code_salt for r in rows}) == len({r.code_hash for r in rows}) == 10
    assert all(re.fullmatch('[0-9a-f]{32}', r.code_salt) and re.fullmatch('[0-9a-f]{64}', r.code_hash) for r in rows)

    password = new_password()
    review_db.execute(text('UPDATE users SET password_hash=:digest WHERE id=1'), {'digest': auth.hash_password(password)})
    review_db.commit()
    login = client.post('/auth/login', data={'username': 'student', 'password': password})
    assert login.status_code == 200 and login.json()['mfa_required'] and 'session' not in login.cookies
    token = login.json()['access_token']
    submitted = codes[0].lower().replace('-', ' ')
    response = verify(client, submitted, token)
    assert response.status_code == 200 and response.json()['recovery_codes_remaining'] == 9
    assert response.cookies.get('session') == response.json()['access_token']
    assert client.get('/auth/mfa/status').json() == {'mfa_enabled': True}
    assert verify(client, codes[1], token).status_code == 401  # replay cannot burn a different code
    assert verify(client, codes[0]).status_code == 401
    exported = client.get('/users/1/export').json()['mfa_recovery_codes']
    assert set(exported) == {'remaining', 'generated_at', 'used_at'}
    assert exported['remaining'] == 9 and exported['generated_at'] == status['generated_at']
    assert len(exported['used_at']) == 1 and exported['used_at'][0]

    rotate = {**who, 'Idempotency-Key': 'replace'}
    response = client.post(CODES, headers=rotate, json={'code': codes[1]})
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    new = response.json()['recovery_codes']
    assert len(set(new)) == 10
    assert client.post(CODES, headers=rotate, json={'code': codes[1]}).status_code == 409
    assert verify(client, codes[2]).status_code == 401
    assert verify(client, new[0]).status_code == 200
    assert client.request('DELETE', '/auth/mfa', headers=who, json={'recovery_code': codes[3]}).status_code == 401
    assert client.request('DELETE', '/auth/mfa', headers=who, json={'recovery_code': new[1]}).status_code == 200
    assert client.get(CODES, headers=who).json() == {'remaining': 0, 'generated_at': None}
    assert client.post(CODES, headers=who, json={'code': totp.now()}).status_code == 400
    assert review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='mfa_recovery_codes_regenerated'")).scalar_one() == 1
    details = review_db.execute(text("SELECT details FROM audit_logs WHERE action='mfa_recovery_code_used'")).scalars().all()
    assert [json.loads(d) for d in details] == [{'remaining': 9}, {'remaining': 9}]
    cache = review_db.execute(text('SELECT state,response_body FROM idempotency_keys')).all()
    assert len(cache) == 2 and all(r.state == 'uncacheable_success' and not r.response_body for r in cache)
    captured = caplog.text + str(review_db.execute(text('SELECT * FROM audit_logs')).all()) + str(rows)
    leaked = any(c in captured or c.replace('-', '') in captured for c in codes + new)
    assert not leaked and submitted not in captured


def test_failed_audit_preserves_challenge_and_code(client, review_db, monkeypatch):
    _, codes = issue(client)
    token = challenge()
    def fail_audit(*args, **kwargs):
        raise RuntimeError('test audit failure')
    with monkeypatch.context() as patch:
        patch.setattr(routes_auth, 'log_audit', fail_audit)
        refused = verify(client, codes[0], token)
    assert refused.status_code == 503 and 'session' not in refused.cookies
    assert client.get(CODES, headers=headers()).json()['remaining'] == 10
    assert verify(client, codes[0], token).status_code == 200


@pytest.mark.parametrize('failure', ['session_insert', 'user_lock'])
def test_recovery_login_failure_rolls_back(client, review_db, failure):
    _, codes = issue(client)
    token = challenge()
    engine = review_db.get_bind()

    def refuse_write(conn, cursor, statement, params, context, many):
        if ((failure == 'session_insert' and 'INSERT' in statement and 'active_sessions' in statement)
                or (failure == 'user_lock' and statement == 'BEGIN IMMEDIATE')):
            raise sqlite3.OperationalError('database is locked')

    event.listen(engine, 'before_cursor_execute', refuse_write)
    try:
        with TestClient(client.app, raise_server_exceptions=False) as caller:
            response = verify(caller, codes[0], token)
    finally:
        event.remove(engine, 'before_cursor_execute', refuse_write)
    assert response.status_code == 503
    assert response.json()['detail'] == 'Login temporarily unavailable. Try again later.'
    assert 'set-cookie' not in response.headers
    assert review_db.execute(text('SELECT count(used_at) FROM mfa_recovery_codes')).scalar_one() == 0
    assert review_db.execute(text('SELECT count(*) FROM active_sessions')).scalar_one() == 0
    assert review_db.execute(text('SELECT count(*) FROM token_blacklist')).scalar_one() == 0
    assert review_db.execute(text("SELECT count(*) FROM audit_logs WHERE action IN "
                                  "('mfa_recovery_code_used','auth.login')")).scalar_one() == 0
    retry = verify(client, codes[0], token)
    assert retry.status_code == 200


def test_null_mfa_flag_can_enroll(client, review_db):
    review_db.execute(text('UPDATE users SET mfa_enabled=NULL WHERE id=1'))
    review_db.commit()
    issue(client)
    assert client.get('/auth/mfa/status', headers=headers()).json() == {'mfa_enabled': True}


def test_recovery_code_bound_to_installation_key(client, review_db, monkeypatch):
    from modules.organizations.mfa_recovery import consume_code

    _, codes = issue(client)
    with monkeypatch.context() as patch:
        patch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
        accepted = consume_code(review_db, 1, codes[0])
        assert not accepted
    accepted = consume_code(review_db, 1, codes[0])
    assert accepted


@pytest.mark.parametrize('operation', ['confirm', 'regenerate'])
def test_issuing_without_encryption_key_returns_setup_error(client, review_db, monkeypatch, operation):
    who = headers()
    if operation == 'regenerate':
        totp, _ = issue(client, who)
        path = CODES
    else:
        setup = client.post('/auth/mfa/setup', headers=who)
        assert setup.status_code == 200
        totp = pyotp.TOTP(setup.json()['secret'])
        path = '/auth/mfa/confirm'
    monkeypatch.delenv('ENCRYPTION_KEY')
    with TestClient(client.app, raise_server_exceptions=False) as caller:
        response = caller.post(path, headers=who, json={'code': totp.now()})
    assert response.status_code == 503
    assert 'ENCRYPTION_KEY' in response.json()['detail']
    assert review_db.execute(text('SELECT count(*) FROM mfa_recovery_codes')).scalar_one() == (
        10 if operation == 'regenerate' else 0)


def test_existing_account_and_password_reset(client, review_db):
    totp, old = issue(client)
    review_db.execute(text('DELETE FROM mfa_recovery_codes'))
    review_db.commit()
    assert client.get(CODES, headers=headers()).json() == {'remaining': 0, 'generated_at': None}
    response = client.post(CODES, headers=headers(), json={'code': totp.now()})
    assert response.status_code == 200
    fresh = response.json()['recovery_codes']
    link = seed_link(review_db)
    assert client.post('/auth/reset-password', json={'token': link, 'new_password': new_password()}).status_code == 200
    assert verify(client, old[0]).status_code == 401 and verify(client, fresh[0]).status_code == 200


@pytest.mark.parametrize('route', ['verify', 'regenerate'])
def test_wrong_code_shares_totp_throttle(client, route):
    totp, codes = issue(client)
    wrong_totp = next(f'{i:06}' for i in range(10) if not totp.verify(f'{i:06}', valid_window=2))
    token = challenge()
    for i in range(10):
        bad = wrong_totp if i % 2 else 'OOOOO-OOOOO'
        if route == 'verify':
            field = 'code' if i % 2 else 'recovery_code'
            response = client.post('/auth/mfa/verify', json={'mfa_token': token, field: bad})
        else:
            response = client.post(CODES, headers=headers(), json={'code': bad})
        assert response.status_code == (401 if i < 4 else 429)
        if i >= 4:
            assert response.json()['detail'].startswith('Too many wrong codes.')
    response = (verify(client, codes[0], token) if route == 'verify'
                else client.post(CODES, headers=headers(), json={'code': codes[0]}))
    assert response.status_code == 429
    assert response.json()['detail'] == 'Too many wrong codes. Try again in 15 minutes.'
    assert int(response.headers['retry-after']) > 60
    assert client.get(CODES, headers=headers()).json()['remaining'] == 10


@pytest.mark.parametrize('path', ['/admin/users/4/mfa', '/users/4', '/users/4/erase'])
def test_removal_cleans_only_target_set(client, review_db, path):
    issue(client)
    issue(client, headers('outsider'))
    assert client.delete(path, headers=headers('admin', 'admin')).status_code == 200
    assert review_db.execute(text('SELECT user_id,count(*) FROM mfa_recovery_codes GROUP BY user_id')).all() == [(1, 10)]


def test_api_tokens_cannot_manage_codes(client, review_db):
    totp, codes = issue(client)
    token = 'odin_' + secrets.token_urlsafe(32)
    review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) "
                           "VALUES(3,'test',:digest,:prefix,:scopes)"),
                      {'digest': auth.hash_password(token), 'prefix': token[:10], 'scopes': '["admin"]'})
    review_db.commit()
    for authority in ({'Authorization': 'Bearer ' + token}, {'X-API-Key': token}):
        refused = client.get(CODES, headers=authority)
        assert refused.status_code == 403
        assert client.post(CODES, headers=authority, json={'code': totp.now()}).status_code == 403
        assert client.request('DELETE', '/auth/mfa', headers=authority, json={'recovery_code': codes[0]}).status_code == 403
        refused = verify(client, codes[0], token)
        assert refused.status_code == 401
    assert client.get(CODES, headers=headers()).json()['remaining'] == 10


def check_concurrent_use(app, engine):
    with TestClient(app) as client:
        _, codes = issue(client)
    barrier = Barrier(2)
    def redeem(_):
        with TestClient(app) as caller:
            token = challenge()
            barrier.wait(timeout=10)
            return verify(caller, codes[0], token).status_code
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert sorted(workers.map(redeem, range(2))) == [200, 401]
    with engine.connect() as db:
        assert db.execute(text('SELECT COUNT(used_at) FROM mfa_recovery_codes')).scalar_one() == 1
        assert db.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one() == 1
        assert db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one() == 1


def test_sqlite_concurrent_redemption(client, review_db):
    check_concurrent_use(client.app, review_db.get_bind())


def check_upgrade(engine):
    assert MIGRATION.MIGRATION_ID in bootstrap_database(engine)['applied']
    with engine.begin() as db:
        db.exec_driver_sql('DROP TABLE mfa_recovery_codes')
        db.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:mid'), {'mid': MIGRATION.MIGRATION_ID})
        db.exec_driver_sql("INSERT INTO users(id,username,role,password_hash,is_active) VALUES(1,'student','viewer','fixture',TRUE)")
    assert bootstrap_database(engine)['applied'] == [MIGRATION.MIGRATION_ID]
    assert bootstrap_database(engine)['applied'] == []
    with engine.begin() as db:
        MIGRATION.validate(db)
        db.exec_driver_sql("INSERT INTO users(id,username,role,password_hash) VALUES(2,'removed','viewer','fixture')")
        db.exec_driver_sql("INSERT INTO mfa_recovery_codes(user_id,code_salt,code_hash,created_at) VALUES(2,'salt','hash',CURRENT_TIMESTAMP)")
        db.exec_driver_sql('DELETE FROM users WHERE id=2')
        assert db.exec_driver_sql('SELECT count(*) FROM mfa_recovery_codes').scalar_one() == 0


def test_sqlite_upgrade():
    engine = create_engine('sqlite://')
    try:
        check_upgrade(engine)
    finally:
        engine.dispose()


def test_migration_refuses_an_incomplete_recovery_index():
    engine = create_engine('sqlite://')
    try:
        with engine.begin() as db:
            db.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY)')
            MIGRATION.apply(db)
            db.exec_driver_sql('DROP INDEX ix_mfa_recovery_codes_user')
            db.exec_driver_sql('CREATE INDEX ix_mfa_recovery_codes_user ON mfa_recovery_codes(user_id) WHERE used_at IS NULL')
            with pytest.raises(MigrationError, match='index differs'):
                apply_python_migration(db, MIGRATION.__name__)
            assert db.execute(text('SELECT 1 FROM odin_schema_migrations WHERE migration_id=:id'),
                              {'id': MIGRATION.MIGRATION_ID}).first() is None
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgres_upgrade_and_concurrent_redemption(pg_engine, monkeypatch):
    check_upgrade(pg_engine)
    with pg_engine.begin() as db:
        db.execute(text('UPDATE users SET mfa_enabled=NULL WHERE id=1'))
    check_concurrent_use(make_app(pg_engine, monkeypatch), pg_engine)
