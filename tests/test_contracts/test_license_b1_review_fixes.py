"""Executable regression coverage for B1 first-pass review findings."""
import asyncio
from datetime import timedelta
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlalchemy import text, event

from core import license_lock as policy
from tests.test_contracts.test_license_clock import clock_env, BASE
from tests.test_contracts.test_license_policy_b1 import policy_db, status, token
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('days', [3, 14])
def test_paid_cold_boot_after_power_off(clock_env, tier_license, monkeypatch, days):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('enterprise')
    wall[0] += timedelta(days=days)
    clock = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    assert lm.get_license().valid
    assert lm.load_license().valid
    assert lm.get_license().has_feature('sso')
    assert clock.sample()[1] is False


@pytest.mark.parametrize('elapsed', [14 * 86400, 8 * 3600])
def test_two_week_suspend_and_laptop_sleep(clock_env, tier_license, monkeypatch, elapsed):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new()
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    tier_license('enterprise')
    # The production elapsed clock includes suspend (CLOCK_BOOTTIME / mach_continuous_time).
    tick[0] += elapsed
    wall[0] += timedelta(seconds=elapsed)
    assert lm.get_license().valid and lm.get_license().has_feature('sso')
    assert lm.load_license().valid


def test_running_forward_jump_recovers_after_stable_window(clock_env):
    wall, tick, store, new, _ = clock_env
    clock = new()
    clock.sample(persist=True)
    wall[0] += timedelta(days=3)
    assert clock.sample(persist=True)[1]
    assert store.read() == BASE
    for _ in range(5):
        wall[0] += timedelta(seconds=1)
        tick[0] += 1
        result = clock.sample()
    assert result == (wall[0], False)


def test_old_restored_copy_does_not_quarantine_current_wall(clock_env):
    wall, tick, store, new, engine = clock_env
    store.write(BASE)
    with engine.begin() as db:
        db.execute(text('UPDATE system_config SET value=:v'), {'v': json.dumps((BASE-timedelta(days=3)).isoformat())})
    assert new().sample(persist=True) == (BASE, False)


def test_community_uncertain_clock_keeps_fast_cache(clock_env, tier_license, monkeypatch):
    import license_manager as lm
    wall, tick, store, new, _ = clock_env
    clock = new()
    clock.sample()
    wall[0] -= timedelta(days=3)
    monkeypatch.setattr(lm, 'license_clock', lambda: clock)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    tier_license(None)
    def unexpected():
        pytest.fail('Community fast path reloaded files')
    monkeypatch.setattr(lm, '_find_license_file', unexpected)
    for _ in range(10):
        assert lm.get_license().verification_state == 'community_initial'


def test_public_status_never_reconciles_or_writes(policy_db, monkeypatch):
    from modules.system import routes_health
    import license_manager as lm
    statements = []
    event.listen(policy_db.get_bind(), 'before_cursor_execute', lambda c, cur, s, *args: statements.append(s))
    def broken(*args, **kwargs):
        raise RuntimeError('policy unavailable')
    monkeypatch.setattr(policy, 'snapshot', broken)
    monkeypatch.setattr(policy, '_lock', broken)
    monkeypatch.setattr(lm.license_clock().store, 'write', broken)
    app = FastAPI()
    app.add_api_route('/license', routes_health.get_license_info)
    response = TestClient(app).get('/license')
    assert response.status_code == 200
    assert response.json()['tier'] == 'enterprise'
    assert not {'reason_code', 'verification_warning'} & response.json().keys()
    assert not any(s.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for s in statements)


@pytest.mark.parametrize('path', ['/api/auth/login', '/api/v1/auth/login', '/health', '/api/v1/health', '/static/a', '/assets/a', '/api/setup/status', '/api/v1/license'])
def test_public_paths_ignore_invalid_cookie(policy_db, monkeypatch, path):
    from core.app import _register_http_middleware
    from core import dependencies
    def forbidden(*args, **kwargs):
        pytest.fail('public path resolved credentials')
    monkeypatch.setattr(dependencies, 'resolve_current_user_sync', forbidden)
    app = FastAPI()
    app.add_api_route(path, lambda: {'ok': True})
    _register_http_middleware(app)
    response = TestClient(app).get(path, cookies={'session': 'invalid'})
    assert response.status_code == 200


def test_gate_resolves_off_loop_once_and_dependency_reuses(policy_db, monkeypatch):
    from core.app import _register_http_middleware
    from core import dependencies
    app = FastAPI()
    _register_http_middleware(app)
    calls = []
    original = dependencies.resolve_current_user_sync
    def resolve(*args, **kwargs):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        calls.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(dependencies, 'resolve_current_user_sync', resolve)
    from core.db import get_db
    app.dependency_overrides[get_db] = lambda: policy_db
    @app.get('/api/v1/check')
    async def check(principal=Depends(dependencies.get_current_user)):
        return {'id': principal['id']}
    response = TestClient(app).get('/api/v1/check', headers={'Authorization': 'Bearer ' + token('admin')})
    assert response.status_code == 200, response.text
    assert response.json()['id'] == 3
    assert len(calls) == 1


def test_unknown_user_set_shape_invalidates(policy_db, monkeypatch):
    from core import sql_write_shape
    old = status(policy_db)
    monkeypatch.setattr(sql_write_shape, 'set_columns', lambda *args: frozenset())
    policy_db.execute(text("UPDATE users SET username='new-student' WHERE id=1"))
    policy_db.commit()
    updated = status(policy_db)
    assert updated['membership_generation'] > old['membership_generation']
    assert updated['revision'] > old['revision']


def test_evidence_stable_and_read_selection_does_not_write(policy_db):
    before = policy._read(policy_db, policy.EVIDENCE_KEY)
    statements = []
    event.listen(policy_db.get_bind(), 'before_cursor_execute', lambda c, cur, s, *args: statements.append(s))
    policy.selection_status(policy_db, read_only=True)
    assert not any(s.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for s in statements)
    policy.reconcile(policy_db)
    assert policy._read(policy_db, policy.EVIDENCE_KEY) == before


def test_policy_outage_is_503_and_broadcast_delivers(policy_db, monkeypatch):
    from core.app_websocket import ConnectionManager
    manager = ConnectionManager()
    def broken(**kwargs):
        raise RuntimeError('DB transient')
    monkeypatch.setattr(manager, 'policy_snapshot', broken)
    with pytest.raises(policy.LicenseLockError) as exc:
        manager.principal_allowed({'id': 3})
    assert exc.value.status_code == 503
    assert exc.value.detail['code'] == 'license_policy_unavailable'
    class Socket:
        async def send_json(self, message): self.message = message
    ws = Socket()
    manager.active = [(ws, {'id': 3, 'role': 'admin', 'group_id': None})]
    from core import controlled_events
    monkeypatch.setattr(controlled_events, 'audience_messages', lambda principals, msg: [msg])
    assert asyncio.run(manager.broadcast({'type': 'test'})) == 1
    assert ws.message == {'type': 'test'}


@pytest.mark.parametrize('failure', ['intent', 'policy', 'file', 'commit'])
def test_removal_failure_preserves_installed_file(policy_db, monkeypatch, failure):
    import license_manager as lm
    path = Path(lm._find_license_file())
    original = path.read_bytes()
    def broken(*args, **kwargs):
        raise policy.LicenseLockError('license_policy_unavailable', status=503)
    if failure == 'intent':
        monkeypatch.setattr(policy, '_write', broken)
    elif failure == 'policy':
        monkeypatch.setattr(policy, 'mark_community_selected', broken)
    elif failure == 'file':
        monkeypatch.setattr(lm, 'quarantine_installed_license', broken)
    else:
        commit = policy_db.commit
        count = [0]
        def fail_second():
            count[0] += 1
            if count[0] == 2: broken()
            commit()
        monkeypatch.setattr(policy_db, 'commit', fail_second)
    with pytest.raises(policy.LicenseLockError) as exc:
        policy.remove_installed_license(policy_db, 3, 'license.removed')
    assert exc.value.status_code == 503
    assert path.read_bytes() == original


def test_removal_has_durable_intent_before_touching_file(policy_db, monkeypatch):
    import license_manager as lm
    from sqlalchemy.orm import Session
    original = lm.quarantine_installed_license
    def check():
        with Session(policy_db.get_bind()) as observer:
            assert policy._read(observer, 'license_lock_removal_intent') == {'community_selected': True}
        return original()
    monkeypatch.setattr(lm, 'quarantine_installed_license', check)
    policy.remove_installed_license(policy_db, 3, 'license.removed')
    assert policy._read(policy_db, policy.EVIDENCE_KEY)['community_selected']
    assert lm._find_license_file() is None


def test_sqlite_api_export_scrubs_every_private_key(tmp_path):
    import sqlite3
    from modules.system.backup_api_export import prepare_export
    source = tmp_path / 'backup.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE system_config(key TEXT PRIMARY KEY, value TEXT)')
        db.executemany('INSERT INTO system_config VALUES (?,?)', [('public', 'visible'),
            ('license_lock_evidence', 'synthetic-signed-envelope'), ('license_lock_future', 'private-future'),
            ('license_active_selection', 'private-selection')])
    exported, cleanup = prepare_export(source)
    try:
        with sqlite3.connect(exported) as db:
            assert db.execute('SELECT * FROM system_config').fetchall() == [('public', 'visible')]
        assert b'synthetic-signed-envelope' not in exported.read_bytes()
        with sqlite3.connect(source) as db:
            assert db.execute('SELECT COUNT(*) FROM system_config').fetchone()[0] == 4
    finally:
        cleanup.unlink()


def test_media_initial_lock_is_typed_http_error(tmp_path):
    import os
    from core.timelapse_fd_response import AdmittedFDResponse
    path = tmp_path / 'media'; path.write_bytes(b'video')
    def deny(): raise policy.LicenseLockError('license_user_locked')
    response = AdmittedFDResponse(os.open(path, os.O_RDONLY), size=5, authorize=deny)
    messages = []
    async def run():
        async def send(msg): messages.append(msg)
        async def receive(): await asyncio.Event().wait()
        await response({'type': 'http'}, receive, send)
    asyncio.run(run())
    assert messages[0]['status'] == 403
    assert (b'x-odin-license-lock', b'license_user_locked') in messages[0]['headers']
    assert json.loads(messages[1]['body'])['detail']['code'] == 'license_user_locked'


def test_pool_membership_flags_clear_when_disabled(policy_db, monkeypatch):
    connection = policy_db.connection()
    connection.info['license_membership_write'] = True
    connection.info['license_had_recovery'] = True
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    policy_db.commit()
    assert not {'license_membership_write', 'license_had_recovery'} & policy_db.connection().info.keys()


def test_stable_issuance_does_not_rewrite_policy(policy_db):
    policy.snapshot(policy_db)
    policy_db.commit()
    statements = []
    event.listen(policy_db.get_bind(), 'before_cursor_execute', lambda c, cur, s, *args: statements.append(s))
    policy.authorize_user(policy_db, 3, issuance=True)
    assert not any(s.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for s in statements)


def test_classroom_lock_precedes_state_consumption(policy_db, monkeypatch):
    from modules.organizations import routes_classroom as routes
    async def unexpected(*args, **kwargs):
        pytest.fail('locked principal consumed provider state/code')
    monkeypatch.setattr(routes, 'complete_connection', unexpected)
    with pytest.raises(policy.LicenseLockError) as exc:
        asyncio.run(routes.classroom_callback(state='state', code='code', principal={'id': 1, 'group_id': 1}, db=policy_db))
    assert exc.value.status_code == 403


@pytest.mark.parametrize('fault', [False, True])
def test_idle_websocket_keeps_thirty_second_ping_cadence(policy_db, monkeypatch, fault):
    import time
    from core.app import create_app
    from core.app_websocket import ws_manager
    from core.auth import create_access_token
    credential = create_access_token({'sub': 'admin', 'role': 'admin', 'ws': True,
        'user_id': 3, 'capability_snapshot_id': 'owned-test'}, timedelta(minutes=5))
    with TestClient(create_app()).websocket_connect('/ws?token=' + credential) as ws:
        if fault:
            def unavailable(*args, **kwargs):
                raise policy.LicenseLockError('license_policy_unavailable', status=503)
            monkeypatch.setattr(ws_manager, 'principal_allowed', unavailable)
        time.sleep(2.2)
        ws.send_text('ping')
        assert ws.receive_text() == 'pong'  # no unsolicited 1-second pings


def test_websocket_admission_outage_is_http_503(policy_db, monkeypatch):
    from core.app import create_app
    from core.app_websocket import ws_manager
    from core.auth import create_access_token
    from starlette.testclient import WebSocketDenialResponse
    credential = create_access_token({'sub': 'admin', 'role': 'admin', 'ws': True,
        'user_id': 3, 'capability_snapshot_id': 'owned-test'}, timedelta(minutes=5))
    def unavailable(*args, **kwargs):
        raise policy.LicenseLockError('license_policy_unavailable', status=503)
    monkeypatch.setattr(ws_manager, 'principal_allowed', unavailable)
    with pytest.raises(WebSocketDenialResponse) as exc:
        with TestClient(create_app()).websocket_connect('/ws?token=' + credential):
            pass
    assert exc.value.status_code == 503


def test_postgres_archive_never_serves_private_table_data(tmp_path, monkeypatch):
    from modules.system import backup_api_export as export
    from fastapi import HTTPException
    archive = tmp_path / 'backup.dump'; archive.write_bytes(b'PGDMP' + bytes(20))
    monkeypatch.setattr(export.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(stdout=b'license_lock_future\tprivate'))
    with pytest.raises(HTTPException) as exc:
        export.prepare_export(archive)
    assert exc.value.status_code == 409


def test_crash_journal_marks_missing_artifact_as_deliberate(policy_db):
    import license_manager as lm
    policy._write(policy_db, 'license_lock_removal_intent', {'community_selected': True})
    policy_db.commit()
    lm.quarantine_installed_license()
    lm._cached_license = None
    assert lm.get_license().verification_state == 'community_selected'
    assert status(policy_db)['entitlement']['state'] == 'community_selected'


def test_sqlite_api_export_scrubs_deleted_evidence(tmp_path):
    import sqlite3
    from modules.system.backup_api_export import prepare_export
    source = tmp_path / 'backup.db'
    with sqlite3.connect(source) as db:
        db.execute('PRAGMA secure_delete=OFF')
        db.execute('CREATE TABLE system_config(key TEXT PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO system_config VALUES('license_lock_evidence', :value)", {'value': 'old-private-envelope' * 1000})
        db.commit()
        db.execute('DELETE FROM system_config')
    assert b'old-private-envelope' in source.read_bytes()
    exported, cleanup = prepare_export(source)
    try:
        assert b'old-private-envelope' not in exported.read_bytes()
    finally:
        cleanup.unlink()


def test_bundle_uses_redacted_database_and_matching_manifest(tmp_path):
    import hashlib
    import sqlite3
    import zipfile
    from modules.system.backup_api_export import prepare_export
    from modules.system.audit_backup import companion, download_bundle
    source = tmp_path / 'backup.db'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE system_config(key TEXT PRIMARY KEY, value TEXT)')
        db.execute("INSERT INTO system_config VALUES('license_lock_evidence', 'private-envelope')")
    with zipfile.ZipFile(companion(source), 'w') as archive:
        archive.writestr('manifest.json', json.dumps({'database_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}))
    exported, cleanup = prepare_export(source)
    try:
        response = download_bundle(source, database_export=exported)
        try:
            with zipfile.ZipFile(response.path) as archive:
                data = archive.read(source.name)
                assert b'private-envelope' not in data
                manifest = json.loads(archive.read('audit-keys/manifest.json'))
                assert manifest['database_sha256'] == hashlib.sha256(data).hexdigest()
        finally:
            asyncio.run(response.background())
    finally:
        cleanup.unlink()


def test_reused_principal_keeps_route_token_scope_check(policy_db):
    from core.app import _register_http_middleware
    from core.auth import hash_password
    from core.db import get_db
    from core.dependencies import get_current_user
    credential = 'odin_b1-review-read-only'
    policy_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'test',:h,:p,'[\"read\"]')"),
                      {'h': hash_password(credential), 'p': credential[:10]})
    policy_db.commit()
    reached = []
    app = FastAPI()
    _register_http_middleware(app)
    app.dependency_overrides[get_db] = lambda: policy_db
    @app.post('/api/v1/private')
    def private(principal=Depends(get_current_user)):
        reached.append(True)
        return {'ok': True}
    result = TestClient(app).post('/api/v1/private', headers={'X-API-Key': credential})
    assert result.status_code == 403
    assert reached == []
