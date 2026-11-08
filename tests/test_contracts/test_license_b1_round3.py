"""Shared socket policy, mixed-carrier perimeter and bounded export ownership."""
import asyncio
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
import sqlite3

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core import license_lock as policy
from tests.test_contracts.test_license_clock import clock_env
from tests.test_contracts.test_license_policy_b1 import policy_db, pick, token
from tests.test_contracts.test_education_review_workflow import review_db
from modules.system import backup_api_export as exports


class Socket:
    def __init__(self):
        self.closed = None

    async def accept(self):
        pass

    async def close(self, **kwargs):
        self.closed = kwargs


def test_heartbeat_one_revision_snapshot_and_session_per_cycle_for_500_sockets(policy_db, monkeypatch):
    from core import db as database
    from core.app_websocket import ConnectionManager
    manager = ConnectionManager()
    sockets = [Socket() for _ in range(500)]
    sessions, snapshots = [], []
    original_session, original_snapshot = database.SessionLocal, policy.snapshot

    def session():
        sessions.append(1)
        return original_session()

    def snapshot(db):
        result = original_snapshot(db)
        snapshots.append(result)
        return result

    monkeypatch.setattr(database, 'SessionLocal', session)
    monkeypatch.setattr(policy, 'snapshot', snapshot)

    async def cycle():
        heartbeat = None
        for ws in sockets:
            await manager.connect(ws, {'id': 3})
            heartbeat = heartbeat or manager._heartbeat_task
            assert manager._heartbeat_task is heartbeat
        assert heartbeat is not None
        # Drive cycles explicitly so timing on slow hosts cannot add a tick.
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await manager.heartbeat_cycle()
        assert manager._heartbeat_task is heartbeat
        # No per-socket snapshot or SessionLocal, even with 500 connections.
        assert len(sessions) == len(snapshots) == 1
        await manager.heartbeat_cycle()
        assert len(sessions) == len(snapshots) == 2
        assert snapshots[0] is snapshots[1]  # durable revision cache

    asyncio.run(cycle())
    assert all(ws.closed is None for ws in sockets)
    revision = snapshots[-1].revision
    pick(policy_db, [5], recovery=5)
    sessions.clear()
    snapshots.clear()
    asyncio.run(manager.heartbeat_cycle())
    assert len(sessions) == len(snapshots) == 1
    assert snapshots[0].revision > revision
    assert all(ws.closed['code'] == 4403 for ws in sockets)
    assert not manager.active
    # Admission must check the durable revision even before another heartbeat.
    pick(policy_db, [3], recovery=3)
    assert manager.principal_allowed({'id': 3})
    pick(policy_db, [5], recovery=5)
    assert not manager.principal_allowed({'id': 3})


def test_heartbeat_outage_retains_decisions_and_broadcast_never_reads_policy(policy_db, monkeypatch):
    from core.app_websocket import ConnectionManager
    from core import controlled_events
    manager = ConnectionManager()
    ws = Socket()
    sent = []

    async def send(message):
        sent.append(message)

    ws.send_json = send
    principal = {'id': 3, '_license_allowed': True}
    manager.active = [(ws, principal)]
    calls = []

    def unavailable():
        calls.append(1)
        raise RuntimeError('temporary policy outage')

    monkeypatch.setattr(manager, 'policy_snapshot', unavailable)
    monkeypatch.setattr(controlled_events, 'audience_messages', lambda ps, msg: [msg] * len(ps))
    asyncio.run(manager.heartbeat_cycle())
    assert principal['_license_allowed'] is True and ws.closed is None
    assert asyncio.run(manager.broadcast({'type': 'test'})) == 1
    assert len(calls) == 1 and len(sent) == 1


@pytest.mark.parametrize('known', ['cookie', 'bearer'])
@pytest.mark.parametrize('extra', ['query', 'api_key', 'both'])
@pytest.mark.parametrize('locked', [True, False])
def test_mixed_carriers_cannot_bypass_perimeter(policy_db, monkeypatch, known, extra, locked):
    from core.app import _register_http_middleware
    from core.config import settings
    monkeypatch.setattr(settings, 'api_key', '')
    if locked:
        pick(policy_db, [5], recovery=5)
    app = FastAPI()
    called = []

    @app.get('/api/v1/private-without-route-auth')
    def endpoint():
        called.append(True)
        return {'ok': True}

    _register_http_middleware(app)
    http = TestClient(app)
    headers = {}
    if known == 'cookie':
        http.cookies.set('session', token('admin'))
    else:
        headers['Authorization'] = 'Bearer ' + token('admin')
    if extra in {'api_key', 'both'}:
        headers['X-API-Key'] = 'unrelated-carrier'
    query = {'token': 'unrelated-carrier'} if extra in {'query', 'both'} else {}
    response = http.get('/api/v1/private-without-route-auth', headers=headers, params=query)
    assert response.status_code == (403 if locked else 200), response.text
    assert len(called) == (0 if locked else 1)
    if locked:
        assert response.json()['detail']['code'] == 'license_user_locked'


@pytest.fixture
def jobs(monkeypatch):
    """Controlled worker completions and expiry clocks, with real export files."""
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    monkeypatch.setattr(exports, '_jobs', {})
    monkeypatch.setattr(exports, '_live_jobs', set())
    submitted, timers = [], []

    class Executor:
        def submit(self, fn, *args):
            future = Future()
            submitted.append((future, fn, args))
            return future

    class Timer:
        def __init__(self, interval, fn, args):
            assert interval == 900
            self.fn, self.args = fn, args
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            if not self.cancelled:
                self.fn(*self.args)

    monkeypatch.setattr(exports, '_executor', Executor())
    monkeypatch.setattr(exports.threading, 'Timer', Timer)

    def complete(index):
        future, fn, args = submitted[index]
        response = fn(*args)
        future.set_result(response)
        return response

    yield submitted, timers, complete
    for future, _, _ in submitted:
        if future.done():
            exports._discard(future)
        else:
            future.cancel()


def source_file(tmp_path, name='backup.db'):
    source = tmp_path / name
    with closing(sqlite3.connect(source)) as db:
        db.execute('CREATE TABLE system_config(key TEXT PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO system_config VALUES('license_lock_evidence', 'private')")
        db.commit()
    return source


def test_export_refuses_only_live_cap_and_completed_jobs_release_capacity(tmp_path, jobs):
    submitted, timers, complete = jobs
    sources = [source_file(tmp_path, f'backup-{i}.db') for i in range(9)]
    for source in sources[:8]:
        assert exports.download_response(source).status_code == 202
    # Polling an existing live job does not require a new slot.
    assert exports.download_response(sources[0]).status_code == 202
    with pytest.raises(HTTPException) as exc:
        exports.download_response(sources[8])
    assert exc.value.status_code == 503 and len(submitted) == 8
    ready = complete(0)
    assert exports.download_response(sources[8]).status_code == 202
    assert len(submitted) == 9 and Path(ready.path).exists()
    assert exports.download_response(sources[0]) is ready
    assert timers[0].cancelled
    asyncio.run(ready.background())
    assert not Path(ready.path).exists() and sources[0].exists()


@pytest.mark.parametrize('state', ['queued', 'running', 'ready'])
@pytest.mark.parametrize('bundle', [False, True])
def test_unpolled_export_expiry_cancels_or_deletes_without_new_request(tmp_path, jobs, state, bundle):
    submitted, timers, complete = jobs
    source = source_file(tmp_path)
    if bundle:
        make_companion(source)
    exports.download_response(source, bundle=bundle)
    future = submitted[0][0]
    response = None
    if state == 'running':
        assert future.set_running_or_notify_cancel()
    elif state == 'ready':
        response = complete(0)
        assert Path(response.path).exists()
    timers[0].fire()
    assert not exports._jobs
    if state == 'queued':
        assert future.cancelled() and not exports._live_jobs
    elif state == 'running':
        assert future in exports._live_jobs
        response = complete(0)
    if response:
        assert not Path(response.path).exists()
    assert not exports._live_jobs and source.exists()


def test_expired_running_job_keeps_live_slot_until_worker_finishes(tmp_path, jobs, monkeypatch):
    monkeypatch.setattr(exports, '_MAX_LIVE_JOBS', 1)
    submitted, timers, complete = jobs
    first, second = source_file(tmp_path, 'one.db'), source_file(tmp_path, 'two.db')
    exports.download_response(first)
    submitted[0][0].set_running_or_notify_cancel()
    timers[0].fire()
    with pytest.raises(HTTPException) as exc:
        exports.download_response(second)
    assert exc.value.status_code == 503
    response = complete(0)
    assert not Path(response.path).exists()
    assert exports.download_response(second).status_code == 202


def test_ready_artifact_eviction_deletes_files_instead_of_blocking_new_jobs(tmp_path, jobs, monkeypatch):
    monkeypatch.setattr(exports, '_MAX_READY_JOBS', 2)
    submitted, timers, complete = jobs
    responses = []
    for index in range(4):
        source = source_file(tmp_path, f'{index}.db')
        assert exports.download_response(source).status_code == 202
        responses.append(complete(index))
    assert len(exports._jobs) == 2 and not exports._live_jobs
    assert all(not Path(r.path).exists() for r in responses[:2])
    assert all(Path(r.path).exists() for r in responses[2:])
    assert all(t.cancelled for t in timers[:2])


@pytest.mark.parametrize('failure', [None, OSError, asyncio.CancelledError])
@pytest.mark.parametrize('bundle', [False, True])
def test_response_deletes_temporary_file_on_completion_or_abandonment(tmp_path, monkeypatch, failure, bundle):
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    source = source_file(tmp_path)
    if bundle:
        make_companion(source)
    created = []
    original = exports.tempfile.mkstemp

    def mkstemp(**kwargs):
        descriptor, name = original(dir=tmp_path, **kwargs)
        created.append(Path(name))
        return descriptor, name

    monkeypatch.setattr(exports.tempfile, 'mkstemp', mkstemp)
    response = exports._response(source, bundle)
    assert len(created) == (2 if bundle else 1)
    assert Path(response.path).exists()
    assert all(not path.exists() for path in created if path != Path(response.path))
    scope = {'type': 'http', 'method': 'GET', 'headers': [], 'extensions': {}}

    async def receive():
        return {'type': 'http.disconnect'}

    async def send(message):
        if failure and message['type'] == 'http.response.body':
            raise failure()

    if failure:
        with pytest.raises(failure):
            asyncio.run(response(scope, receive, send))
    else:
        asyncio.run(response(scope, receive, send))
    assert not Path(response.path).exists() and source.exists()


def make_companion(source):
    import hashlib
    import json
    import zipfile
    from modules.system.audit_backup import companion
    with zipfile.ZipFile(companion(source), 'w') as archive:
        archive.writestr('manifest.json', json.dumps({
            'database_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}))


@pytest.mark.parametrize('bundle', [False, True])
def test_audit_failure_deletes_prepared_export(tmp_path, monkeypatch, bundle):
    from modules.system import routes_backup as routes
    from core import controlled_exports
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    source = source_file(tmp_path)
    if bundle:
        make_companion(source)
    response = exports._response(source, bundle)
    monkeypatch.setattr(routes, '_safe_backup_path', lambda _: source)
    monkeypatch.setattr(controlled_exports, 'acknowledge_export', lambda *a: None)
    monkeypatch.setattr(exports, 'download_response', lambda *a: response)

    def fail_audit(*args, **kwargs):
        raise RuntimeError('audit failed')

    monkeypatch.setattr(routes, 'audit_download', fail_audit)
    with pytest.raises(RuntimeError, match='audit failed'):
        routes._download_backup(source.name, {}, object(), bundle=bundle)
    assert not Path(response.path).exists() and source.exists()
