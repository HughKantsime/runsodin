"""Retry protection through create_app(), real credentials and PostgreSQL jobs.

Database pattern: test_cert_E_notif_postgres (also used by the quiet-hours
claims): an owned pinned container, loopback port, bootstrap_database, finally
remove. Unlike those probes, missing Docker is an error, never a skip, and no
receipts are written. Dev runs this file with pg-test.sh.

TestClient deliberately does not enter lifespan: the schema is bootstrapped
here and scheduler/monitor/background delivery must not start. All production
routes, middleware, authentication and job writes remain installed.
"""
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.postgresql
ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'postgres@sha256:1d5b02d3a9941fbd52437db4107f8c52d6297dac553ca1e5c299ebb3756b4857'  # PostgreSQL 16.16 (Alpine); read from the image by Dev on 2026-11-20.
PASSWORD = 'Synthetic-retry-test-only-1!'


def _docker(*args, timeout=60):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope='module')
def pg_engine():
    assert shutil.which('docker'), 'Docker is required; run this file with pg-test.sh on Dev'
    assert _docker('info', timeout=20).returncode == 0, 'Docker is not available'
    from core.schema import bootstrap_database

    name = 'odin-retry-' + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
    engine = None
    try:
        started = _docker('run', '-d', '--pull=never', '--name', name,
                          '-p', '127.0.0.1::5432', '--tmpfs', '/var/lib/postgresql/data:rw,size=256m',
                          '-e', 'POSTGRES_PASSWORD=' + password, IMAGE, timeout=120)
        assert started.returncode == 0, 'Could not start owned PostgreSQL (pinned image must be installed)'
        deadline = time.monotonic() + 180
        while _docker('exec', name, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', timeout=10).returncode:
            assert time.monotonic() < deadline, 'Owned PostgreSQL did not become ready'
            time.sleep(0.3)
        port = _docker('port', name, '5432/tcp').stdout.strip().rsplit(':', 1)[-1]
        engine = create_engine(f'postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres',
                               pool_size=10, connect_args={'options': '-c timezone=UTC'})
        bootstrap_database(engine, ROOT / 'backend')
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        assert _docker('rm', '-f', name).returncode == 0, 'Owned PostgreSQL cleanup failed'


@pytest.fixture
def retry_app(pg_engine, monkeypatch):
    from core import auth, db, db_compat, db_utils, dependencies
    from core.app import create_app
    from core.config import settings
    from core.middleware import idempotency
    from core.rate_limit import limiter
    from core.registry import registry

    monkeypatch.setattr(db, 'engine', pg_engine)
    monkeypatch.setattr(db_utils, 'engine', pg_engine)
    # Preserve the factory object imported by other modules and restore its bind
    # afterwards, so importing the full app cannot leak a test session factory.
    monkeypatch.setattr(db.SessionLocal, 'kw', dict(db.SessionLocal.kw))
    db.SessionLocal.configure(bind=pg_engine)
    for module in (db, db_compat):
        monkeypatch.setattr(module, 'IS_SQLITE', False)
        monkeypatch.setattr(module, 'IS_POSTGRES', True)
    monkeypatch.setattr(settings, 'api_key', 'synthetic-retry-perimeter')
    monkeypatch.setattr(settings, 'cookie_secure', False)
    monkeypatch.setattr(settings, 'trusted_hosts', '*')
    monkeypatch.setattr(settings, 'odin_public_url', '')
    monkeypatch.setattr(settings, 'odin_trusted_proxies', '')
    monkeypatch.setattr(dependencies, '_last_seen_cache', {})
    monkeypatch.setattr(idempotency, '_SCHEMA_READY_CACHE', {'ready': False, 'checked_at': 0.0})
    monkeypatch.setattr(registry, '_providers', {})
    monkeypatch.setattr(registry, '_declared_requires', [])
    limiter._storage.reset()
    nonce = uuid.uuid4().hex
    users = []
    with pg_engine.begin() as conn:
        # New identities and job names isolate each case without deleting rows
        # from the shared, owned database (including its economic identities).
        for index in range(2):
            username = f'retry-{nonce}-{index}'
            uid = conn.execute(text(
                'INSERT INTO users(username,password_hash,role,is_active) '
                "VALUES(:name,:password,'admin',TRUE) RETURNING id"),
                {'name': username, 'password': auth.hash_password(PASSWORD)}).scalar_one()
            users.append((uid, username))

    app = create_app()
    body = {'item_name': 'Retry ' + nonce, 'quantity': 1, 'hold': True}

    def credentials(kind, user=0, expiry=None):
        uid, username = users[user]
        if kind == 'session':
            client = TestClient(app)
            try:
                response = client.post('/api/v1/auth/login', data={'username': username, 'password': PASSWORD})
                # Successful login bodies contain credentials; never include them in assertions.
                assert response.status_code == 200
                cookie = client.cookies.get('session')
                assert cookie is not None
                return SimpleNamespace(headers={'Cookie': 'session=' + cookie}, token_id=None, user_id=uid)
            finally:
                client.close()
        raw = 'odin_' + secrets.token_urlsafe(32)
        with pg_engine.begin() as conn:
            token_id = conn.execute(text(
                'INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes,expires_at) '
                "VALUES(:uid,'Retry test',:hash,:prefix,:scopes,:expiry) RETURNING id"),
                {'uid': uid, 'hash': auth.hash_password(raw), 'prefix': raw[:10],
                 'scopes': '["agent:write"]', 'expiry': expiry}).scalar_one()
        return SimpleNamespace(headers={'X-API-Key': raw}, token_id=token_id, user_id=uid)

    def post(credential, key, payload=None):
        # Each thread owns its client and portal; no cookie jar is shared.
        client = TestClient(app)
        try:
            return client.post('/api/v1/jobs', json=body if payload is None else payload,
                               headers={**credential.headers, 'Idempotency-Key': key})
        finally:
            client.close()

    def count():
        with pg_engine.connect() as conn:
            return conn.execute(text('SELECT COUNT(*) FROM jobs WHERE item_name=:name'),
                                {'name': body['item_name']}).scalar_one()

    return SimpleNamespace(app=app, engine=pg_engine, body=body, credentials=credentials,
                           post=post, count=count, key=nonce)


@pytest.fixture(params=['session', 'api_token'])
def caller(retry_app, request):
    return retry_app.credentials(request.param)


def _assert_created(app, response, rows=1):
    assert (response.status_code, app.count()) == (201, rows), response.text


def _assert_replay(first, second):
    assert (second.status_code, second.content) == (first.status_code, first.content)
    assert second.headers.get('X-Idempotent-Replay') == 'true'


def test_same_key_same_body_runs_once_and_replays(retry_app, caller):
    first = retry_app.post(caller, retry_app.key)
    _assert_created(retry_app, first)
    second = retry_app.post(caller, retry_app.key)
    _assert_created(retry_app, second)
    _assert_replay(first, second)


@pytest.mark.parametrize('kind,shape', [('session', None), ('api_token', 'aware'), ('api_token', 'naive')])
def test_expiring_api_token_same_key_runs_once(retry_app, kind, shape):
    expiry = datetime.now(timezone.utc) + timedelta(days=1)
    if shape == 'naive':
        expiry = expiry.replace(tzinfo=None)
    credential = retry_app.credentials(kind, expiry=expiry)
    if kind == 'api_token':
        # DATETIME becomes timestamptz in migrator.py. Both inputs are accepted,
        # but PostgreSQL cannot return a naive datetime from this column.
        column = next(c for c in inspect(retry_app.engine).get_columns('api_tokens') if c['name'] == 'expires_at')
        assert column['type'].timezone is True
        with retry_app.engine.connect() as conn:
            stored = conn.execute(text('SELECT expires_at FROM api_tokens WHERE id=:id'),
                                  {'id': credential.token_id}).scalar_one()
        assert isinstance(stored, datetime) and stored.utcoffset() is not None
        assert stored == expiry.replace(tzinfo=timezone.utc)
    first = retry_app.post(credential, retry_app.key)
    _assert_created(retry_app, first)
    second = retry_app.post(credential, retry_app.key)
    _assert_created(retry_app, second)
    _assert_replay(first, second)


def test_same_key_different_body_is_conflict(retry_app, caller):
    first = retry_app.post(caller, retry_app.key)
    _assert_created(retry_app, first)
    second = retry_app.post(caller, retry_app.key, {**retry_app.body, 'quantity': 2})
    assert (second.status_code, retry_app.count()) == (409, 1), second.text
    assert second.json()['error']['code'] == 'idempotency_conflict'


def test_concurrent_same_key_one_runs(retry_app, caller, monkeypatch):
    from modules.jobs.routes import jobs_crud

    entered, release = threading.Event(), threading.Event()
    stage = jobs_crud._stage_prospective_queue_job

    def held_stage(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=30), 'Test did not release job creation'
        return stage(*args, **kwargs)

    monkeypatch.setattr(jobs_crud, '_stage_prospective_queue_job', held_stage)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(retry_app.post, caller, retry_app.key)
        try:
            assert entered.wait(timeout=15), 'First request did not reach job creation'
            second = pool.submit(retry_app.post, caller, retry_app.key).result(timeout=15)
            # The winning handler is still held: the pending claim must be visible
            # to the other connection before any job has been committed.
            assert retry_app.count() == 0
            assert second.status_code == 409, second.text
            assert second.json()['error']['code'] == 'idempotency_in_progress'
            assert second.json()['error']['retriable'] is True
        finally:
            release.set()
        winner = first.result(timeout=15)
    _assert_created(retry_app, winner)
    replay = retry_app.post(caller, retry_app.key)
    _assert_created(retry_app, replay)
    _assert_replay(winner, replay)


def test_unverifiable_caller_is_refused_not_run(retry_app, caller, monkeypatch):
    from core.middleware import idempotency

    def unavailable(*args, **kwargs):
        raise RuntimeError('Synthetic caller lookup failure')

    monkeypatch.setattr(idempotency, '_resolve_user_context', unavailable)
    response = retry_app.post(caller, retry_app.key)
    assert (response.status_code, retry_app.count()) == (503, 0), response.text
    assert response.json()['error']['code'] == 'idempotency_unavailable'
    assert response.json()['error']['retriable'] is True


@pytest.mark.parametrize('kind', ['session', 'api_token'])
def test_different_users_same_key_do_not_cross_replay(retry_app, kind):
    first_user, second_user = [retry_app.credentials(kind, user=i) for i in range(2)]
    first = retry_app.post(first_user, retry_app.key)
    _assert_created(retry_app, first)
    second = retry_app.post(second_user, retry_app.key)
    _assert_created(retry_app, second, rows=2)
    assert first.json()['id'] != second.json()['id']
    with retry_app.engine.connect() as conn:
        owners = dict(conn.execute(text('SELECT id, charged_to_user_id FROM jobs WHERE item_name=:name'),
                                   {'name': retry_app.body['item_name']}).all())
    assert owners == {first.json()['id']: first_user.user_id, second.json()['id']: second_user.user_id}
    for credential, original in ((first_user, first), (second_user, second)):
        replay = retry_app.post(credential, retry_app.key)
        _assert_replay(original, replay)
    assert retry_app.count() == 2


@pytest.mark.parametrize('kind,invalidation', [
    ('session', 'revoked'), ('api_token', 'revoked'), ('api_token', 'expired'),
])
def test_expired_or_revoked_token_gets_401_not_replay(retry_app, kind, invalidation):
    credential = retry_app.credentials(kind)
    first = retry_app.post(credential, retry_app.key)
    _assert_created(retry_app, first)
    if kind == 'session':
        client = TestClient(retry_app.app)
        try:
            assert client.post('/api/v1/auth/logout', headers=credential.headers).status_code == 200
        finally:
            client.close()
    else:
        with retry_app.engine.begin() as conn:
            if invalidation == 'revoked':
                # API token revocation deletes the row (routes_sessions.py).
                conn.execute(text('DELETE FROM api_tokens WHERE id=:id'), {'id': credential.token_id})
            else:
                conn.execute(text('UPDATE api_tokens SET expires_at=:expiry WHERE id=:id'),
                             {'id': credential.token_id, 'expiry': datetime.now(timezone.utc) - timedelta(days=1)})
    second = retry_app.post(credential, retry_app.key)
    assert (second.status_code, retry_app.count()) == (401, 1), second.text
    assert second.content != first.content
    assert 'X-Idempotent-Replay' not in second.headers
