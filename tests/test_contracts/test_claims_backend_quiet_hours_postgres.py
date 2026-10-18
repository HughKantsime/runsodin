"""Claims F13-13.2-04/05, F10-10.9-07: quiet-hours digest idempotency on owned PostgreSQL.

Starts an owned, pinned PostgreSQL container bound to 127.0.0.1 only, bootstraps the
current ODIN schema, then races real digest workers (separate connections) through
process_quiet_hours_digest(), restarts with a fresh engine, and inspects constraints.
Skips only if Docker is unavailable. Container is always removed.
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
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / 'artifacts' / 'application-certification' / 'claims-protocols-backend'
IMAGE = 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
END = datetime(2026, 10, 10, 7, tzinfo=timezone.utc)


def _docker(*args, timeout=60):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope='module')
def pg_url():
    if not shutil.which('docker') or _docker('info', timeout=20).returncode:
        pytest.skip('Docker unavailable')
    name = 'odin-claims-quiet-' + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
    receipt = {'container': name, 'image': IMAGE}
    try:
        started = _docker('run', '-d', '--name', name, '-p', '127.0.0.1::5432', '--tmpfs', '/var/lib/postgresql/data:rw,size=256m',
                          '-e', 'POSTGRES_PASSWORD=' + password, IMAGE, timeout=120)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 60
        while _docker('exec', name, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', timeout=10).returncode:
            assert time.monotonic() < deadline, 'owned PostgreSQL not ready'
            time.sleep(0.3)
        port = _docker('port', name, '5432/tcp').stdout.strip().rsplit(':', 1)[-1]
        receipt['server_version'] = _docker('exec', name, 'psql', '-U', 'postgres', '-tAc', 'SHOW server_version').stdout.strip()
        url = f'postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/postgres'
        for _ in range(50):
            try:
                create_engine(url).connect().close(); break
            except Exception:
                time.sleep(0.3)
        yield url, receipt
    finally:
        receipt['owned_container_removed'] = _docker('rm', '-f', name).returncode == 0
        RECEIPTS.mkdir(parents=True, exist_ok=True)
        (RECEIPTS / 'quiet-hours-postgres-container.json').write_text(json.dumps(receipt, indent=1))


@pytest.fixture(scope='module')
def pg_engine(pg_url):
    from core.schema import bootstrap_database
    url, receipt = pg_url
    engine = create_engine(url, pool_size=10)
    bootstrap_database(engine, ROOT / 'backend')
    with engine.begin() as c:
        c.execute(text("INSERT INTO groups (id,name,is_org) VALUES (1,'school',TRUE)"))
        c.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
                       "(3,'admin','admin@example.test','fixture','admin',TRUE,1)"))
        c.execute(text("INSERT INTO printers (id,name,model,is_active,api_type,org_id) VALUES (9,'P1S-09','P1S',TRUE,'bambu',1)"))
        c.execute(text("INSERT INTO alert_preferences(user_id,alert_type,email,browser_push) VALUES(3,'print_complete',TRUE,TRUE)"))
        # Migration 032 is registered: webhook digests come from the per-alert deferral row the
        # dispatcher records when quiet hours suppress the org webhook (record_suppressed).
        from modules.notifications import deferred_deliveries
        assert deferred_deliveries.record_suppressed(c, org_id=1, scope='org:1', recipients={}, alert_type='print_complete',
                                                     severity='info', title='Synthetic finished', printer_id=9) == 1
        c.execute(text("UPDATE quiet_hours_deferred_deliveries SET created_at=:stamp"), {'stamp': END.replace(hour=6)})
    yield engine, receipt
    engine.dispose()


@pytest.fixture
def digest(monkeypatch):
    from modules.notifications import quiet_hours
    from modules.reporting import report_runner
    target = {'id': 1, 'name': 'Synthetic school', 'config': {'enabled': True, 'start': '22:00', 'end': '07:00', 'webhook_url': 'https://hooks.example.invalid/x'}}
    alerts = [{'user_id': 3, 'printer_id': 9, 'alert_type': 'print_complete', 'severity': 'info', 'title': 'Synthetic finished', 'created_at': END.isoformat()}]
    monkeypatch.setattr(quiet_hours, 'iter_orgs_with_digest_enabled', lambda: [target])
    monkeypatch.setattr(quiet_hours, 'compute_last_window_end', lambda config: END)
    monkeypatch.setattr(quiet_hours, 'get_suppressed_alerts_for_window', lambda *a, **k: alerts)
    monkeypatch.setattr(report_runner, 'get_smtp_config', lambda session: {'server': 'synthetic.invalid'})
    calls = []
    lock = threading.Lock()

    def record(kind):
        def deliver(*a, **k):
            with lock:
                calls.append(kind)
            time.sleep(0.05)
            return True
        return deliver
    for kind in ('email', 'push', 'webhook'):
        monkeypatch.setattr(report_runner, '_deliver_digest_' + kind, record(kind))
    return report_runner, calls


@pytest.mark.usefixtures("pro_license")  # digest email is Pro (email_notifications; review O6)
def test_postgres_racing_workers_and_restart_deliver_each_channel_once(pg_engine, digest, pg_url):
    engine, receipt = pg_engine
    runner, calls = digest
    workers = 8
    barrier = threading.Barrier(workers)
    errors = []

    def worker():
        try:
            with Session(engine) as session:
                barrier.wait(timeout=10)
                runner.process_quiet_hours_digest(session)
        except Exception as exc:  # pragma: no cover - recorded
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker) for _ in range(workers)]
    [t.start() for t in threads]; [t.join(timeout=60) for t in threads]
    assert errors == []
    # Daemon restart: brand-new engine/connection pool, same window.
    restarted = create_engine(engine.url)
    with Session(restarted) as session:
        runner.process_quiet_hours_digest(session)
    restarted.dispose()
    with engine.connect() as c:
        user_rows = c.execute(text('SELECT user_id,org_id,scope_key,window_ended_at,delivery_status FROM quiet_hours_digest_sends')).fetchall()
        org_rows = c.execute(text('SELECT org_id,scope_key,window_ended_at,delivery_status FROM quiet_hours_org_digest_sends')).fetchall()
    receipt['race'] = {'workers': workers, 'restart_runs': 1, 'delivery_calls': sorted(calls),
                       'user_claim_rows': [list(map(str, r)) for r in user_rows], 'org_claim_rows': [list(map(str, r)) for r in org_rows]}
    assert sorted(calls) == ['email', 'push', 'webhook']
    assert len(user_rows) == 1 and user_rows[0][4] == 'sent'
    assert len(org_rows) == 1 and org_rows[0][3] == 'sent'


def test_postgres_unique_constraint_shape_and_duplicate_insert_rejected(pg_engine):
    engine, receipt = pg_engine
    insp = inspect(engine)
    uniques = {t: [tuple(u['column_names']) for u in insp.get_unique_constraints(t)]
               for t in ('quiet_hours_digest_sends', 'quiet_hours_org_digest_sends')}
    receipt['unique_constraints'] = {k: [list(x) for x in v] for k, v in uniques.items()}
    assert uniques['quiet_hours_digest_sends'] == [('user_id', 'scope_key', 'window_ended_at')]
    window = '2026-10-11T07:00:00+00:00'
    with engine.begin() as c:
        c.execute(text("INSERT INTO quiet_hours_digest_sends(user_id,org_id,scope_key,window_ended_at) VALUES(3,1,'org:1',:w)"), {'w': window})
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text("INSERT INTO quiet_hours_digest_sends(user_id,org_id,scope_key,window_ended_at) VALUES(3,1,'org:1',:w)"), {'w': window})
    receipt['duplicate_insert_rejected'] = True
