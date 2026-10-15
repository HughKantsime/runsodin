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
        deadline = time.monotonic() + 60
        while _docker('exec', name, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', timeout=10).returncode:
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


def test_postgres_fresh_vapid_key_generation_persists_native_json(pg_engine):
    """F13-13.1-01 on PostgreSQL: the first /push/vapid-key request generates and stores a pair
    in the JSON column; the stored value is native JSON and the served key is stable."""
    from core.db import get_db
    from modules.notifications.routes import providers
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
