"""The restart reconciliation contracts on PostgreSQL, where the daemons' DBAPI adapter differs."""
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_monitor_restart_reconciliation import (  # noqa: F401
    delivered, dispatched_moonraker, generic, manual_bambu, moonraker, running_bambu,
    test_dispatched_print_ended_in_downtime_is_closed_once,
    test_failed_recovery_retries_on_the_next_report,
    test_generic_different_print_does_not_adopt_the_old_row,
    test_generic_dispatched_attempt_keeps_the_receipt_rules,
    test_generic_print_continuing_through_a_restart_keeps_its_row,
    test_generic_unnamed_print_does_not_resume_or_close_anything,
    test_loadout_changed_in_downtime_still_proves_the_end,
    test_moonraker_dispatched_end_follows_the_receipt_rules,
    test_moonraker_dispatched_end_retires_the_claim_and_dispatches_again,
    test_moonraker_end_without_history_closes_its_row_once,
    test_moonraker_history_supplies_truth_long_after_the_end,
    test_no_status_after_the_wait_leaves_a_live_print_alone,
    test_older_duplicates_stay_stale_and_cannot_take_the_outcome,
    test_refused_identity_keeps_the_fence,
    test_unproven_end_keeps_the_dispatch_fence,
    test_unreserved_print_ended_in_downtime_maps_like_a_watched_end,
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'
TEMPLATE = 'reconciliation_template'


def _docker(*args, timeout=60):
    return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)


@pytest.fixture(scope='module')
def pg_server():
    if not shutil.which('docker') or _docker('info', timeout=20).returncode:
        pytest.skip('Docker unavailable')
    from core.schema import bootstrap_database
    name = 'reconciliation_' + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(24)
    try:
        started = _docker('run', '-d', '--name', name, '-p', '127.0.0.1::5432', '--tmpfs',
                          '/var/lib/postgresql/data:rw,size=512m', '-e', 'POSTGRES_PASSWORD=' + password, IMAGE, timeout=120)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 180
        while _docker('exec', name, 'pg_isready', '-U', 'postgres', '-h', '127.0.0.1', timeout=60).returncode:
            assert time.monotonic() < deadline, 'owned PostgreSQL not ready'
            time.sleep(0.3)
        port = _docker('port', name, '5432/tcp').stdout.strip().rsplit(':', 1)[-1]
        base = f'postgresql+psycopg://postgres:{password}@127.0.0.1:{port}/'
        admin = create_engine(base + 'postgres', isolation_level='AUTOCOMMIT')
        for _ in range(50):
            try:
                admin.connect().close()
                break
            except Exception:
                time.sleep(0.3)
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE {TEMPLATE}'))
        seeded = create_engine(base + TEMPLATE)
        bootstrap_database(seeded, ROOT / 'backend')
        with seeded.begin() as conn:
            conn.execute(text("INSERT INTO groups (id,name,is_org) VALUES (1,'school',TRUE),(2,'other',TRUE)"))
            conn.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
                              "(3,'admin','admin@example.test','fixture','admin',TRUE,1)"))
        seeded.dispose()
        yield base, admin
        admin.dispose()
    finally:
        _docker('rm', '-f', name)


@pytest.fixture
def review_db(pg_server, monkeypatch):
    """Same name as the SQLite fixture, so the imported contracts run here unchanged."""
    from core import db_compat
    # The daemons' SQL fragments follow the process's configured database, not the engine.
    monkeypatch.setattr(db_compat, 'IS_SQLITE', False)
    monkeypatch.setattr(db_compat, 'IS_POSTGRES', True)
    base, admin = pg_server
    name = 'case_' + uuid.uuid4().hex[:12]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE {name} TEMPLATE {TEMPLATE}'))
    engine = create_engine(base + name, pool_size=10)
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE {name} WITH (FORCE)'))
