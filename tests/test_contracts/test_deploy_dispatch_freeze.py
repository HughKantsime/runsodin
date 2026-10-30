"""Bounded deploy windows refuse every start path without taking a claim."""
from datetime import datetime, timedelta, timezone
import json
import os
import time
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core import deploy_freeze
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def window(tmp_path, monkeypatch):
    path = tmp_path / 'deploy-freeze.json'
    clock = SimpleNamespace(now=datetime.now(timezone.utc), ticks=100.0)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now
    monkeypatch.setattr(deploy_freeze, 'datetime', Clock)
    monkeypatch.setattr(deploy_freeze, 'time', SimpleNamespace(monotonic=lambda: clock.ticks))
    monkeypatch.setattr(deploy_freeze, '_cache', None)
    monkeypatch.setenv('ODIN_DEPLOY_FREEZE_PATH', str(path))

    def write(*, seconds=1500, age=0, content=None):
        data = {'version': 'test-release', 'opened_at': clock.now.isoformat(),
                'until': (clock.now + timedelta(seconds=seconds)).isoformat(), 'ignored': 'not public'}
        path.write_text(json.dumps(data) if content is None else content)
        stamp = clock.now.timestamp() - age
        os.utime(path, (stamp, stamp))
        return data
    return path, clock, write


@pytest.mark.parametrize('kind', ['absent', 'expired', 'malformed_expired'])
def test_inactive_windows(window, kind):
    _, _, write = window
    if kind == 'expired':
        write(seconds=-1)
    elif kind == 'malformed_expired':
        write(age=1801, content='{')
    assert deploy_freeze.active() is None


@pytest.mark.parametrize('content', ['{', 'null', '[]', '{}',
    pytest.param('[' * 2000, id='deeply_nested'),
    pytest.param('{"version":"test","opened_at":"2020-01-01T00:00:00Z","until":"2020-01-01T01:00:00Z"}'
                 + ' ' * 16385 + 'invalid', id='oversized'),
    '{"version":"test","opened_at":"invalid","until":"2099-01-01T00:00:00Z"}',
    '{"version":"test","opened_at":"2026-10-30T00:00:00Z","until":"2026-10-30T01:00:00"}',
    '{"version":7,"opened_at":"2026-10-30T00:00:00Z","until":"2099-01-01T00:00:00Z"}'])
def test_malformed_windows_fail_closed_only_for_thirty_minutes(window, content):
    _, clock, write = window
    write(content=content)
    frozen = deploy_freeze.active()
    assert frozen == {'release': None, 'until': (clock.now + timedelta(minutes=30)).isoformat()}
    clock.now += timedelta(minutes=30)
    assert deploy_freeze.active() is None


def test_until_is_capped_by_mtime(window):
    path, clock, write = window
    write(seconds=86400, age=600)
    expected = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) + timedelta(minutes=30)
    assert deploy_freeze.active() == {'release': 'test-release', 'until': expected.isoformat()}
    clock.now = expected
    assert deploy_freeze.active() is None


def test_cache_stats_every_call_and_never_delays_expiry(window, monkeypatch):
    path, clock, write = window
    reads = []
    original = deploy_freeze._read
    def read(*args):
        reads.append(True)
        return original(*args)
    monkeypatch.setattr(deploy_freeze, '_read', read)
    write(seconds=1)
    assert deploy_freeze.active() and deploy_freeze.active()
    assert len(reads) == 1
    clock.now += timedelta(seconds=1)
    assert deploy_freeze.active() is None
    assert len(reads) == 1
    write(seconds=60)
    assert deploy_freeze.active() and len(reads) == 2
    clock.ticks += 2
    assert deploy_freeze.active() and len(reads) == 3
    path.unlink()
    assert deploy_freeze.active() is None
    write()
    assert deploy_freeze.active() and len(reads) == 4


def test_configured_path_change_invalidates_cache(window, tmp_path, monkeypatch):
    _, _, write = window
    write()
    assert deploy_freeze.active()
    monkeypatch.setenv('ODIN_DEPLOY_FREEZE_PATH', str(tmp_path / 'other.json'))
    assert deploy_freeze.active() is None


@pytest.fixture
def dispatch_case(review_db, tmp_path, monkeypatch, window):
    from core import db as core_db, job_timeline
    from modules.printers import dispatch, dispatch_background, routes_start_next, start_next
    from modules.jobs.routes import jobs_lifecycle, dispatch_status, jobs_crud
    original_notify = dispatch._notify_start_outcome
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    monkeypatch.setattr(dispatch, '_notify_start_outcome', original_notify)
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='pending',charged_to_user_id=3 WHERE id=53"))
    writer = job_timeline.TimelineWriter(engine.url, process_name='deploy-freeze-test')
    monkeypatch.setattr(job_timeline, '_writer', writer)
    dispatch_background.startup()
    app = FastAPI()
    for router in (jobs_lifecycle.router, dispatch_status.router, routes_start_next.router, jobs_crud.router):
        app.include_router(router, prefix='/api')
    def database():
        with Session(engine) as db:
            yield db
    def user():
        return {'id': 3, 'role': 'admin', 'group_id': 1, 'is_superadmin': True}
    app.dependency_overrides[core_db.get_db] = database
    for route in app.routes:
        for dependency in getattr(getattr(route, 'dependant', None), 'dependencies', []):
            if dependency.name == 'current_user':
                app.dependency_overrides[dependency.call] = user
    monkeypatch.setattr(start_next, 'next_suggestion', lambda *a, **k: {'job_id': 53, 'fingerprint': 'f' * 64})
    with TestClient(app) as client:
        try:
            yield engine, client
        finally:
            assert dispatch_background.drain(5)
            dispatch_background.startup()
            writer.close()


def assert_unstarted(engine, status='pending'):
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts')).scalar_one() == 0
        assert conn.execute(text('SELECT status,actual_start FROM jobs WHERE id=53')).one() == (status, None)


def assert_recorded(engine):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT code,detail FROM job_timeline_events WHERE job_id=53 AND kind='gate_refused'")).first()
        if row:
            break
        time.sleep(.02)
    assert row and row[0] == 'DEPLOY_WINDOW'
    assert json.loads(row[1]) == {'gate': 'DEPLOY_WINDOW'}
    with engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=53')).scalar_one() == 0


@pytest.mark.parametrize('entry', ['direct', 'background', 'start_next', 'start_next_background', 'auto'])
def test_all_dispatch_entries_refuse_before_claim(dispatch_case, window, monkeypatch, entry, caplog):
    from core import dispatch_claims
    from modules.printers import dispatch, dispatch_acquisition, smart_plug
    from modules.printers.start_next import load
    from modules.printers.dispatch_background import result_key
    engine, client = dispatch_case
    window[2]()
    def forbidden(*args, **kwargs):
        pytest.fail('Frozen dispatch reached claim acquisition or device I/O')
    monkeypatch.setattr(dispatch_claims, 'acquire', forbidden)
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', forbidden)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', forbidden)
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', forbidden)
    if entry == 'auto':
        with engine.begin() as conn:
            conn.execute(text("UPDATE jobs SET status='scheduled' WHERE id=53"))
        monkeypatch.setenv('AUTO_DISPATCH', 'true')
        dispatch.attempt_dispatch(59)
        assert 'deploy_window: ' + deploy_freeze.MESSAGE in caplog.text
    else:
        if entry.startswith('start_next'):
            query = '?wait=true' if entry == 'start_next' else ''
            response = client.post('/api/printers/59/start-next' + query, json={
                'suggested_job_id': 53, 'suggestion_fingerprint': 'f' * 64,
                'request_id': str(uuid.uuid4()), 'bed_clear_confirmed': True})
        else:
            response = client.post('/api/jobs/53/dispatch' + ('?wait=true' if entry == 'direct' else ''))
        body = response.json()
        if entry == 'start_next':
            assert response.status_code == 200 and body['state'] == 'refused'
            assert body['code'] == 'deploy_window'
            message = body['message']
        else:
            assert response.status_code == 400, body
            message = body['detail']
        assert message == 'deploy_window: ' + deploy_freeze.MESSAGE
        if entry in ('background', 'start_next_background'):
            with engine.connect() as conn:
                result = load(conn, result_key(53))
            response = client.get('/api/jobs/53/dispatch-status', params={'attempt_id': result['attempt_id']})
            assert response.status_code == 200
            assert response.json()['phase'] == 'refused'
            assert response.json()['message'] == message
    assert_unstarted(engine, 'scheduled' if entry == 'auto' else 'pending')
    assert_recorded(engine)


def test_admit_refuses_without_opening_a_claim_transaction(dispatch_case, window):
    from modules.printers.dispatch_admission import admit
    engine, _ = dispatch_case
    window[2]()
    result = admit(None, job={'id': 53, 'printer_id': 59})
    assert not result.admitted and result.reason == 'deploy_window'
    assert_unstarted(engine)
    assert_recorded(engine)


def test_window_opened_during_preparation_is_refused_at_admission(dispatch_case, window, monkeypatch):
    from modules.printers import dispatch, dispatch_acquisition
    engine, _ = dispatch_case
    def ready(*args):
        window[2]()
        return True
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', ready)
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', lambda *a: pytest.fail('Frozen final admission sent a command'))
    assert dispatch.dispatch_job(59, 53) == (False, 'deploy_window: ' + deploy_freeze.MESSAGE)
    assert_unstarted(engine)
    assert_recorded(engine)


def test_manual_start_refuses_and_hold_changes_remain_allowed(dispatch_case, window):
    engine, client = dispatch_case
    path, _, write = window
    write()
    response = client.post('/api/jobs/53/start')
    assert response.status_code == 409
    assert response.json()['detail'] == 'deploy_window: ' + deploy_freeze.MESSAGE
    assert_unstarted(engine)
    for held in (True, False):
        response = client.patch('/api/jobs/53', json={'hold': held})
        assert response.status_code == 200, response.json()
        assert response.json()['hold'] is held
    path.unlink()
    assert client.post('/api/jobs/53/start').status_code == 200


@pytest.mark.parametrize('kind', ['absent', 'expired', 'malformed_expired'])
def test_inactive_windows_allow_dispatch(dispatch_case, window, monkeypatch, kind):
    from modules.printers import dispatch
    engine, _ = dispatch_case
    _, _, write = window
    if kind == 'expired':
        write(seconds=-1)
    elif kind == 'malformed_expired':
        write(age=1801, content='{')
    calls = []
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', lambda *a: calls.append(True) or (True, 'Synthetic accepted'))
    assert dispatch.dispatch_job(59, 53) == (True, 'Print started')
    assert calls == [True]


@pytest.mark.parametrize('malformed', [False, True])
def test_public_health_exposes_only_version_and_effective_until(window, monkeypatch, malformed):
    from modules.system import routes_health
    _, clock, write = window
    monkeypatch.setattr(routes_health, 'get_spoolman_url', lambda: '')
    write(seconds=86400, content='{' if malformed else None)
    app = FastAPI()
    app.include_router(routes_health.router, prefix='/api')
    with TestClient(app) as client:
        response = client.get('/api/health')
        assert response.status_code == 200
        assert response.json()['dispatch_freeze'] == {
            'release': None if malformed else 'test-release',
            'until': (clock.now + timedelta(minutes=30)).isoformat()}
        clock.now += timedelta(minutes=30)
        assert client.get('/api/health').json()['dispatch_freeze'] is None


def test_unreadable_freeze_path_never_fails_health(window, monkeypatch):
    from pathlib import Path
    from modules.system import routes_health
    def denied(self, *args, **kwargs):
        raise PermissionError('synthetic')
    monkeypatch.setattr(Path, 'stat', denied)
    assert deploy_freeze.active() is None
    monkeypatch.setattr(routes_health, 'get_spoolman_url', lambda: '')
    app = FastAPI()
    app.include_router(routes_health.router, prefix='/api')
    with TestClient(app) as client:
        response = client.get('/api/health')
    assert response.status_code == 200 and response.json()['dispatch_freeze'] is None


def test_frozen_health_never_echoes_the_release_as_version(window, monkeypatch):
    from modules.system import routes_health
    window[2]()
    monkeypatch.setattr(routes_health, 'get_spoolman_url', lambda: '')
    app = FastAPI()
    app.include_router(routes_health.router, prefix='/api')
    with TestClient(app) as client:
        body = client.get('/api/health').text
    assert '"release":"test-release"' in body and '"version":"test-release"' not in body
