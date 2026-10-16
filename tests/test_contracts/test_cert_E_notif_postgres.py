"""Cert-E PostgreSQL native probe: digest idempotency constraints and SMTP config typing.

Owned, pinned PostgreSQL container on 127.0.0.1 (name prefix certfixE_notif_), current
ODIN schema bootstrapped, removed afterwards. Skips only if Docker is unavailable.
"""
import json
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_channels import SmtpPeer, smtp_peer  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
END = datetime(2026, 10, 10, 7, tzinfo=timezone.utc)


def _docker(*args, timeout=60):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope='module')
def pg_engine():
    if not shutil.which('docker') or _docker('info', timeout=20).returncode:
        pytest.skip('Docker unavailable')
    from core.schema import bootstrap_database
    name = 'certfixE_notif_' + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
    try:
        started = _docker('run', '-d', '--name', name, '-p', '127.0.0.1::5432', '--tmpfs',
                          '/var/lib/postgresql/data:rw,size=256m', '-e', 'POSTGRES_PASSWORD=' + password, IMAGE, timeout=120)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 180
        while _docker('exec', name, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', timeout=60).returncode:
            assert time.monotonic() < deadline, 'owned PostgreSQL not ready'
            time.sleep(0.3)
        port = _docker('port', name, '5432/tcp').stdout.strip().rsplit(':', 1)[-1]
        url = f'postgresql+psycopg2://postgres:{password}@127.0.0.1:{port}/postgres'
        engine = create_engine(url, pool_size=10)
        for _ in range(50):
            try:
                engine.connect().close()
                break
            except Exception:
                time.sleep(0.3)
        bootstrap_database(engine, ROOT / 'backend')
        with engine.begin() as c:
            c.execute(text("INSERT INTO groups (id,name,is_org) VALUES (1,'school',TRUE)"))
            c.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
                           "(1,'student','student@example.test','fixture','viewer',TRUE,1),"
                           "(3,'admin','admin@example.test','fixture','admin',TRUE,NULL)"))
        yield engine
        engine.dispose()
    finally:
        _docker('rm', '-f', name)


def test_postgres_digest_claim_uniques_match_scoped_contract(pg_engine):
    insp = inspect(pg_engine)
    uniques = {t: sorted(tuple(u['column_names']) for u in insp.get_unique_constraints(t))
               for t in ('quiet_hours_digest_sends', 'quiet_hours_org_digest_sends')}
    assert uniques == {'quiet_hours_digest_sends': [('user_id', 'scope_key', 'window_ended_at')],
                       'quiet_hours_org_digest_sends': [('scope_key', 'window_ended_at')]}
    window = END.isoformat()
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO quiet_hours_org_digest_sends(org_id,scope_key,window_ended_at) VALUES(1,'org:1',:w)"), {'w': window})
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as c:
            c.execute(text("INSERT INTO quiet_hours_org_digest_sends(org_id,scope_key,window_ended_at) VALUES(1,'org:1',:w)"), {'w': window})
    # The same user may hold one claim per scope for the same window (org vs system).
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO quiet_hours_digest_sends(user_id,org_id,scope_key,window_ended_at) VALUES(1,1,'org:1',:w),(1,NULL,'system',:w)"), {'w': window})


@pytest.mark.parametrize('claim', ['user', 'org'])
def test_postgres_racing_claims_admit_exactly_one_sender(pg_engine, claim):
    from modules.reporting import report_runner
    window = datetime(2026, 10, 11, 7, tzinfo=timezone.utc) if claim == 'user' else datetime(2026, 10, 12, 7, tzinfo=timezone.utc)
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes, errors = [], []

    def worker():
        try:
            with Session(pg_engine) as session:
                barrier.wait(timeout=10)
                won = (report_runner._claim_digest_send(session, 1, 1, window) if claim == 'user'
                       else report_runner._claim_org_webhook_send(session, 1, window))
                outcomes.append(won)
        except Exception as exc:  # pragma: no cover - recorded
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker) for _ in range(workers)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert errors == [] and sorted(outcomes) == [False] * (workers - 1) + [True]
    # Restart: a fresh engine sees the durable claim and declines.
    fresh = create_engine(pg_engine.url)
    with Session(fresh) as session:
        again = (report_runner._claim_digest_send(session, 1, 1, window) if claim == 'user'
                 else report_runner._claim_org_webhook_send(session, 1, window))
    fresh.dispose()
    assert again is False


def test_postgres_daemon_email_uses_http_saved_smtp_config(pg_engine, smtp_peer, monkeypatch):
    from core import db_utils
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications import channels
    from modules.notifications.routes import alerts
    monkeypatch.setattr(db_utils, 'engine', pg_engine)
    monkeypatch.delenv('ODIN_ITAR_MODE', raising=False)
    app = FastAPI()
    app.include_router(alerts.router)

    def session():
        with Session(pg_engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'admin', 'group_id': None}
    with TestClient(app) as client:
        assert client.put('/smtp-config', json={'enabled': True, 'host': '127.0.0.1', 'port': smtp_peer.port,
                                                'from_address': 'odin@example.test', 'use_tls': False}).status_code == 200
    with pg_engine.connect() as c:
        raw = c.execute(text("SELECT value FROM system_config WHERE key='smtp_config'")).scalar_one()
    assert isinstance(raw, dict)  # native JSON decoding on PostgreSQL
    channels.send_email(1, 'print_failed', 'Synthetic PG title', 'Synthetic PG body')
    message, = smtp_peer.wait(1, timeout=3)
    assert message['To'] == 'student@example.test'


def test_postgres_vapid_key_endpoint_reads_stored_keys(pg_engine):
    from core.db import get_db
    from modules.notifications.routes import providers
    with pg_engine.begin() as c:
        c.execute(text("INSERT INTO system_config(key,value) VALUES('vapid_keys',:v) ON CONFLICT (key) DO NOTHING"),
                  {'v': json.dumps({'public_key': 'BSyntheticPublicKey', 'private_key': 'synthetic-noncredential'})})
    app = FastAPI()
    app.include_router(providers.router)

    def session():
        with Session(pg_engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        assert client.get('/push/vapid-key').json() == {'public_key': 'BSyntheticPublicKey', 'enabled': True}


def test_postgres_fresh_vapid_key_generation_persists_native_json(pg_engine, monkeypatch):
    """F13-13.1-01 on PostgreSQL: the first /push/vapid-key request generates and stores a pair
    in the JSON column; the stored value is native JSON and the served key is stable."""
    import core.crypto as crypto
    from cryptography.fernet import Fernet
    from core.db import get_db
    from modules.notifications.routes import providers
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.delenv('VAPID_PRIVATE_KEY', raising=False)
    with pg_engine.begin() as c:
        c.execute(text("DELETE FROM system_config WHERE key='vapid_keys'"))
    app = FastAPI()
    app.include_router(providers.router)

    def session():
        with Session(pg_engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        first = client.get('/push/vapid-key').json()
        assert first['enabled'] is True and len(first['public_key']) == 87
        assert client.get('/push/vapid-key').json() == first
    with pg_engine.connect() as c:
        stored = c.execute(text("SELECT value FROM system_config WHERE key='vapid_keys'")).scalar_one()
    assert isinstance(stored, dict) and stored['public_key'] == first['public_key']
    assert crypto.is_encrypted(stored['private_key'])  # review H1: never plaintext at rest


def test_postgres_quiet_hours_config_put_round_trips(pg_engine):
    """System quiet-hours PUT stores values in the system_config JSON column; GET and the
    suppression config read them back on PostgreSQL."""
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications import quiet_hours
    from modules.system import routes_config
    app = FastAPI()
    app.include_router(routes_config.router)

    def session():
        with Session(pg_engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None}
    with TestClient(app) as client:
        put = client.put('/config/quiet-hours', json={'enabled': True, 'start': '21:30', 'end': '06:15', 'digest': False})
        assert put.status_code == 200, put.text
        assert client.get('/config/quiet-hours').json() == {'enabled': True, 'start': '21:30', 'end': '06:15', 'digest': False}
        # Update path (rows exist now).
        assert client.put('/config/quiet-hours', json={'enabled': False}).status_code == 200
        assert client.get('/config/quiet-hours').json()['enabled'] is False
    import core.db as core_db
    original = quiet_hours.engine
    quiet_hours.engine = pg_engine
    try:
        quiet_hours.invalidate_cache()
        assert quiet_hours._get_config() == {'enabled': False, 'start': '21:30', 'end': '06:15', 'digest_enabled': False}
    finally:
        quiet_hours.engine = original
        quiet_hours.invalidate_cache()


def test_postgres_mqtt_republish_config_put_round_trips(pg_engine, monkeypatch):
    """PUT /config/mqtt-republish writes text values into the system_config JSON column on
    PostgreSQL; GET and the republish loader read them back (password stays encrypted)."""
    import core.crypto as crypto
    from cryptography.fernet import Fernet
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications import mqtt_republish
    from modules.system import routes_config
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    app = FastAPI()
    app.include_router(routes_config.router)

    def session():
        with Session(pg_engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None}
    monkeypatch.setattr(routes_config, 'mqtt_republish', None)
    body = {'enabled': True, 'host': 'broker.example.test', 'port': 8883, 'username': 'odin',
            'password': 'synthetic-mqtt-secret', 'topic_prefix': 'odin/plant', 'use_tls': True}
    with TestClient(app) as client:
        put = client.put('/config/mqtt-republish', json=body)
        assert put.status_code == 200, put.text
        got = client.get('/config/mqtt-republish').json()
        assert got == {**body, 'password': '••••••••'}
        assert client.put('/config/mqtt-republish', json={'host': 'other.example.test'}).status_code == 200
        assert client.get('/config/mqtt-republish').json()['host'] == 'other.example.test'
    with pg_engine.connect() as c:
        stored = c.execute(text("SELECT value FROM system_config WHERE key='mqtt_republish_password'")).scalar_one()
    assert stored != 'synthetic-mqtt-secret'
    monkeypatch.setattr(mqtt_republish, 'engine', pg_engine)
    monkeypatch.setattr(mqtt_republish, '_config_cache', None)
    monkeypatch.setattr(mqtt_republish, '_config_ts', 0)
    config = mqtt_republish._get_config()
    assert config == {'host': 'other.example.test', 'port': 8883, 'username': 'odin', 'password': 'synthetic-mqtt-secret',
                      'topic_prefix': 'odin/plant', 'use_tls': True}


def test_postgres_migration_032_deferred_deliveries_table_and_outbox_logic(pg_engine):
    """Native probe for migration 032: registered (bootstrap creates the table), additive onto a
    pre-032 database, idempotent, constraint-enforced, and the record -> window read -> mark ->
    purge cycle on PostgreSQL types (timestamptz)."""
    import importlib
    from datetime import timedelta
    from modules.notifications import deferred_deliveries as dd
    migration = importlib.import_module('core.schema.migrations.032_quiet_hours_deferred_deliveries')
    with pg_engine.begin() as c:
        assert dd.available(c) is True  # registered: the bootstrapped database has it
        c.execute(text('DROP TABLE quiet_hours_deferred_deliveries'))  # model a pre-032 database
    with pg_engine.begin() as c:
        assert dd.available(c) is False
        migration.apply(c)
    with pg_engine.begin() as c:
        migration.apply(c)  # idempotent
        migration.validate(c)
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as c:
            c.execute(text("INSERT INTO quiet_hours_deferred_deliveries(id,user_id,scope_key,channel,alert_type,severity,title) "
                           "VALUES('bad',1,'tenant-1','email','print_failed','info','t')"))
    with pg_engine.begin() as c:
        assert dd.record_suppressed(c, org_id=1, scope='system', recipients={1: {'email', 'push'}}, alert_type='print_failed',
                                    severity='critical', title='PG deferred', message='m', printer_id=None) == 3
    with pytest.raises(IntegrityError):
        with pg_engine.begin() as c:  # a recipient row needs a user; a webhook row must not have one
            c.execute(text("INSERT INTO quiet_hours_deferred_deliveries(id,user_id,scope_key,channel,alert_type,severity,title) "
                           "VALUES('bad2',1,'system','webhook','print_failed','info','t')"))
    now = datetime.now(timezone.utc)
    window = (now - timedelta(minutes=5), now + timedelta(minutes=5))
    with pg_engine.begin() as c:
        rows = dd.pending_for_window(c, 'system', *window)
        assert sorted(r['channel'] for r in rows) == ['email', 'push'] and {r['org_id'] for r in rows} == {1}
        hooks = dd.pending_for_window(c, 'system', *window, channels=('webhook',))
        assert [(r['user_id'], r['title']) for r in hooks] == [(None, 'PG deferred')]
        assert dd.pending_for_window(c, 'org:1', *window) == []
        ids = [r['id'] for r in rows + hooks]
        assert dd.mark(c, ids, 'sent') == 3
        assert dd.mark(c, ids, 'sent') == 0  # idempotent
    with pg_engine.begin() as c:
        assert dd.purge(c, now=now + timedelta(days=8)) == 3
        assert c.execute(text('SELECT COUNT(*) FROM quiet_hours_deferred_deliveries')).scalar_one() == 0
