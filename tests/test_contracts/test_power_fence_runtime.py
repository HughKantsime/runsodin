"""Real 027 storage/target locks and independently initialized processes.

Commands are inert worker receipts; no hardware, credentials or network listeners.
"""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client


def _child_env():
    return {**{name: os.environ[name] for name in ('PATH', 'TMPDIR', 'LANG') if name in os.environ},
            'PYTHONPATH': str(Path.cwd() / 'backend'),
            'JWT_SECRET_KEY': 'test-key-for-contract-tests-only'}


@pytest.fixture
def runtime(review_db, monkeypatch):
    from modules.printers import smart_plug, dispatch
    from modules.printers import plug_power_acquisition as workers
    import core.auth
    monkeypatch.setattr(core.auth, 'SECRET_KEY', 'test-key-for-contract-tests-only')
    engine = review_db.get_bind()
    monkeypatch.setattr(smart_plug, 'engine', engine)
    # dispatch loader deliberately uses core.db's engine, as in production.
    import core.db
    monkeypatch.setattr(core.db, 'engine', engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO printers(id,name,api_type,api_host,is_active,shared,plug_type,plug_host,plug_auto_on,plug_auto_off,plug_cooldown_minutes,gcode_state,last_seen) VALUES(599,'Fence fixture','moonraker','192.0.2.12',1,0,'tasmota','192.0.2.13',1,1,0,'RUNNING',CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,observed_received_at) VALUES(599,599,'fixture.gcode','fixture',CURRENT_TIMESTAMP,'running',CURRENT_TIMESTAMP)"))
    calls = []
    def command(config, deadline, action):
        with engine.connect() as conn:
            assert conn.execute(text("SELECT state FROM power_target_operations WHERE state='command_started'")).scalar_one() == 'command_started'
        calls.append(action)
        return workers._OwnedHttpResult(workers.PowerAttempt('accepted', action == 'ON', True, True, True, False), workers._RECEIPT_SEAL)
    monkeypatch.setattr(workers, 'bounded_http_power_action', command)
    return smart_plug, engine, calls, workers


def rows(engine):
    with engine.connect() as conn:
        return conn.execute(text('SELECT state,revision,outcome FROM power_target_operations')).all()


def test_observed_on_and_cooldown_off_commit_fences(runtime):
    module, engine, calls, workers = runtime
    module.on_print_start(599, print_job_id=599)
    assert calls == ['ON']
    assert rows(engine) == [('complete', 3, 'on_observed')]
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET gcode_state='IDLE',last_seen=CURRENT_TIMESTAMP WHERE id=599"))
        conn.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=599"))
    # Exercise the actual callback synchronously with an inert timer.
    callbacks = []
    monkey = pytest.MonkeyPatch()
    monkey.setattr(module.threading, 'Timer', lambda delay, callback: (callbacks.append(callback) or SimpleNamespace(start=lambda: None, cancel=lambda: None)))
    try:
        module.on_print_complete(599, print_job_id=599)
        callbacks[0]()
        assert calls == ['ON', 'OFF']
        assert rows(engine) == [('complete', 3, 'on_observed'), ('complete', 3, 'off_observed')]
    finally:
        monkey.undo()


@pytest.mark.parametrize('phase', ['preparing', 'command_started', 'release_pending', 'cleanup_uncertain'])
def test_dispatch_guard_and_restart_retain_every_active_fence(runtime, phase):
    from core.power_target_operations import _stage_operation, _stage_transition
    from modules.printers.power_fence import _snapshot
    from core.dispatch_target_guard import unresolved_target
    module, engine, calls, workers = runtime
    facts, config = _snapshot(module, 599, 'observed_on', 599)
    with engine.begin() as conn:
        owner = _stage_operation(conn, facts)
        if phase in ('command_started', 'release_pending'):
            _stage_transition(conn, owner, 0, 'preparing', 'command_started')
        if phase == 'release_pending':
            _stage_transition(conn, owner, 1, 'command_started', 'release_pending', outcome='on_observed')
        if phase == 'cleanup_uncertain':
            _stage_transition(conn, owner, 0, 'preparing', phase, reason='command_uncertain')
    before = rows(engine)
    assert unresolved_target(engine, 599, facts.target_key)
    source = '''from sqlalchemy import create_engine,event
from core.dispatch_target_guard import unresolved_target
engine=create_engine(%r)
event.listen(engine,'connect',lambda db,_:db.execute('PRAGMA foreign_keys=ON'))
assert unresolved_target(engine,599,%r)
with engine.connect() as c:
 from sqlalchemy import text
 assert c.execute(text('SELECT state,revision,outcome FROM power_target_operations')).all()==%r
''' % (str(engine.url), facts.target_key, [tuple(r) for r in before])
    subprocess.run([sys.executable, '-c', source], check=True, timeout=10,
                   env=_child_env())
    module.on_print_start(599, print_job_id=599)
    assert calls == [] and rows(engine) == before


def test_uncertain_command_never_auto_clears(runtime, monkeypatch):
    module, engine, calls, workers = runtime
    monkeypatch.setattr(workers, 'bounded_http_power_action', lambda *args: workers._OwnedHttpResult(workers.PowerAttempt('uncertain', reaped=True, ipc_closed=True), workers._RECEIPT_SEAL))
    module.on_print_start(599, print_job_id=599)
    assert rows(engine) == [('cleanup_uncertain', 2, None)]
    before = rows(engine)
    for _ in range(3):
        module.on_print_start(599, print_job_id=599)
    assert rows(engine) == before and calls == []


def test_cross_process_second_command_cannot_send(runtime, monkeypatch):
    module, engine, calls, workers = runtime
    original = workers.bounded_http_power_action
    def competing(config, deadline, action):
        # Parent is blocked inside command seam with committed fence and target.
        source = '''from sqlalchemy import create_engine,event
import core.db
from modules.printers import smart_plug
from modules.printers import plug_power_acquisition as workers
core.db.engine=smart_plug.engine=create_engine(%r)
event.listen(core.db.engine,'connect',lambda db,_:db.execute('PRAGMA foreign_keys=ON'))
from core.dispatch_target_lock import acquire_target_lock
assert acquire_target_lock(core.db.engine,%r).status=='conflict'
workers.bounded_http_power_action=lambda *a: __import__('os')._exit(9)
smart_plug.on_print_start(599,print_job_id=599)
''' % (str(engine.url), __import__('modules.printers.power_fence', fromlist=['_snapshot'])._snapshot(module,599,'observed_on',599)[0].target_key)
        subprocess.run([sys.executable, '-c', source], check=True, timeout=10,
                       env=_child_env())
        return original(config, deadline, action)
    monkeypatch.setattr(workers, 'bounded_http_power_action', competing)
    module.on_print_start(599, print_job_id=599)
    assert calls == ['ON'] and rows(engine)[0][0] == 'complete'


@pytest.mark.parametrize('kind', ['homeassistant', 'mqtt'])
def test_legacy_protocol_on_uses_no_fence_and_does_not_block_dispatch(runtime, monkeypatch, kind):
    from core.dispatch_target_guard import unresolved_target
    module, engine, calls, workers = runtime
    with engine.begin() as conn:
        conn.execute(text('UPDATE printers SET plug_type=:kind WHERE id=599'), {'kind': kind})
    legacy = []
    monkeypatch.setattr(module, 'power_on', lambda pid: legacy.append(pid))
    module.on_print_start(599, print_job_id=599)
    assert legacy == [599] and calls == [] and rows(engine) == []
    assert not unresolved_target(engine, 599, 'a' * 64)


def test_missing_storage_guard_fails_closed(runtime):
    from core.dispatch_target_guard import unresolved_target
    from sqlalchemy import create_engine
    assert unresolved_target(create_engine('sqlite://'), 599, 'a' * 64)


@pytest.mark.parametrize('phase', ['preparing', 'cleanup_uncertain'])
def test_real_dispatch_refuses_power_fence(review_db, tmp_path, monkeypatch, phase):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    from modules.printers import dispatch
    from core.dispatch_identity import build_identity
    from core.auth import SECRET_KEY
    from core.power_target_operations import PowerFacts, _stage_operation, _stage_transition
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    identity = build_identity(dispatch._get_printer_info(59), SECRET_KEY.encode())
    facts = PowerFacts(59, identity.target_key, '2' * 64, '3' * 64, 'observed_on', physical_job_id=599, observation_key='4' * 64)
    with engine.begin() as conn:
        owner = _stage_operation(conn, facts)
        if phase == 'cleanup_uncertain':
            _stage_transition(conn, owner, 0, 'preparing', phase, reason='command_uncertain')
    uploads = []
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', lambda *a, **kw: (uploads.append(True) or (False, 'unexpected upload')))
    success, reason = dispatch.dispatch_job(59, 53)
    assert not success and 'unresolved' in reason
    assert uploads == []
    assert rows(engine)[0][0] == phase


@pytest.mark.parametrize('action,observed', [('on', True), ('off', False), ('toggle', True)])
def test_manual_selected_request_uses_same_fence_and_nonce_replay(runtime, token_client, monkeypatch, action, observed):
    from modules.printers import routes_smart_plug
    module, engine, calls, workers = runtime
    client, issue = token_client
    client.app.include_router(routes_smart_plug.router)
    def command(config, deadline, verb):
        with engine.connect() as conn:
            row = conn.execute(text("SELECT state,auth_kind,request_nonce,physical_job_id FROM power_target_operations WHERE state='command_started'")).one()
            assert tuple(row) == ('command_started', 'scoped_api_key', 'a' * 32, None)
        calls.append(verb)
        return workers._OwnedHttpResult(workers.PowerAttempt('accepted', observed, True, True, True, False), workers._RECEIPT_SEAL)
    monkeypatch.setattr(workers, 'bounded_http_power_action', command)
    headers = {**issue(['write:printers']), 'X-Power-Request-Nonce': 'a' * 32}
    for _ in range(2):
        response = client.post('/printers/599/plug/' + action, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()['power_state'] is observed
    assert calls == [action.upper()]
    assert rows(engine)[0][0] == 'complete'
    # Reusing the nonce for another purpose refuses without another command.
    other = 'off' if action != 'off' else 'on'
    assert client.post('/printers/599/plug/' + other, headers=headers).status_code == 409
    assert len(calls) == 1


@pytest.mark.parametrize('change', ['revoked', 'foreign'])
def test_manual_invalid_authority_never_sends(runtime, token_client, change):
    from modules.printers import routes_smart_plug
    module, engine, calls, workers = runtime
    client, issue = token_client
    client.app.include_router(routes_smart_plug.router)
    headers = issue(['write:printers'])
    headers['X-Power-Request-Nonce'] = 'b' * 32
    with engine.begin() as conn:
        if change == 'revoked':
            conn.execute(text('DELETE FROM api_tokens'))
        elif change == 'foreign':
            conn.execute(text('UPDATE printers SET org_id=2 WHERE id=599'))
    response = client.post('/printers/599/plug/on', headers=headers)
    assert response.status_code in (401, 403, 404, 422)
    assert calls == [] and rows(engine) == []


def test_repeated_completed_observation_never_sends_again(runtime):
    module, engine, calls, workers = runtime
    module.on_print_start(599, print_job_id=599)
    before = rows(engine)
    module.on_print_start(599, print_job_id=599)
    assert calls == ['ON'] and rows(engine) == before


def test_final_cas_failure_retains_release_pending(runtime, monkeypatch):
    from modules.printers import power_fence
    module, engine, calls, workers = runtime
    original = power_fence._stage_transition
    def transition(conn, owner, revision, state, target, **kwargs):
        if target == 'complete':
            raise RuntimeError('inert final commit fault')
        return original(conn, owner, revision, state, target, **kwargs)
    monkeypatch.setattr(power_fence, '_stage_transition', transition)
    module.on_print_start(599, print_job_id=599)
    assert rows(engine) == [('release_pending', 2, 'on_observed')]
    module.on_print_start(599, print_job_id=599)
    assert calls == ['ON']


def test_post_command_config_drift_retains_fence_without_replacement_cache_write(runtime, monkeypatch):
    module, engine, calls, workers = runtime
    original = workers.bounded_http_power_action
    def command(*args):
        receipt = original(*args)
        with engine.begin() as conn:
            conn.execute(text("UPDATE printers SET plug_host='192.0.2.99' WHERE id=599"))
        return receipt
    monkeypatch.setattr(workers, 'bounded_http_power_action', command)
    module.on_print_start(599, print_job_id=599)
    assert rows(engine) == [('cleanup_uncertain', 2, None)]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT plug_power_state FROM printers WHERE id=599')).scalar_one() is None


@pytest.mark.parametrize('phase', ['preparing', 'command_started'])
def test_actual_owner_exit_retains_committed_fence(runtime, phase):
    from core.dispatch_target_guard import unresolved_target
    from modules.printers.power_fence import _snapshot
    module, engine, calls, workers = runtime
    facts, _ = _snapshot(module, 599, 'observed_on', 599)
    source = '''import os
from sqlalchemy import create_engine,event
import core.db
from modules.printers import smart_plug,power_fence,plug_power_acquisition
core.db.engine=smart_plug.engine=create_engine(%r)
event.listen(core.db.engine,'connect',lambda db,_:db.execute('PRAGMA foreign_keys=ON'))
%s
smart_plug.on_print_start(599,print_job_id=599)
raise AssertionError('owner must exit at seam')
''' % (str(engine.url),
        "power_fence._confirm=lambda *a:os._exit(0)" if phase == 'preparing' else
        "plug_power_acquisition.bounded_http_power_action=lambda *a:os._exit(0)")
    subprocess.run([sys.executable, '-c', source], check=True, timeout=10,
                   env=_child_env())
    before = rows(engine)
    assert before[0][0] == phase
    assert unresolved_target(engine, 599, facts.target_key)
    module.on_print_start(599, print_job_id=599)
    assert rows(engine) == before and calls == []


def test_generic_scheduled_completion_uses_current_policy_and_physical_identity(review_db, tmp_path, monkeypatch):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    from modules.printers import smart_plug, plug_power_acquisition as workers
    from modules.printers.power_fence import run
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET plug_type='tasmota',plug_host='192.0.2.13',plug_auto_off=TRUE,gcode_state='FINISH',last_seen=CURRENT_TIMESTAMP WHERE id=59"))
        conn.execute(text("UPDATE jobs SET status='completed' WHERE id=53"))
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,filename,started_at,status,observed_received_at) VALUES(599,59,53,'fixture.gcode',CURRENT_TIMESTAMP,'completed',CURRENT_TIMESTAMP)"))
    calls = []
    def command(config, deadline, action):
        calls.append(action)
        return workers._OwnedHttpResult(workers.PowerAttempt('accepted', False, True, True, True, False), workers._RECEIPT_SEAL)
    monkeypatch.setattr(workers, 'bounded_http_power_action', command)
    assert run(smart_plug, 59, 'cooldown_off', physical_id=599) is False
    assert calls == ['OFF']
    with engine.connect() as conn:
        assert conn.execute(text('SELECT scheduled_job_id,state FROM power_target_operations')).one() == (53, 'complete')


def test_target_is_acquired_before_nested_process_lock(runtime, monkeypatch):
    from modules.printers import power_fence
    import threading
    module, engine, calls, workers = runtime
    local = threading.RLock()
    depth = 0
    target_held = False
    order = []
    class Local:
        def acquire(self, *args, **kwargs):
            nonlocal depth
            assert depth == 0
            result = local.acquire(*args, **kwargs)
            if result:
                depth += 1
                order.append(('local', target_held))
            return result
        def release(self):
            nonlocal depth
            depth -= 1
            local.release()
        def __enter__(self):
            self.acquire()
            return self
        def __exit__(self, *args):
            self.release()
    original = power_fence.acquire_target_lock
    def target(*args):
        nonlocal target_held
        assert depth == 0
        result = original(*args)
        target_held = result.status == 'acquired'
        order.append(('target', target_held))
        return result
    monkeypatch.setattr(module, '_printer_power_lock', lambda pid: Local())
    monkeypatch.setattr(power_fence, 'acquire_target_lock', target)
    module.on_print_start(599, print_job_id=599)
    assert order == [('local', False), ('target', True), ('local', True)]
    assert calls == ['ON']


def test_unconfirmed_worker_retains_owned_target(runtime, monkeypatch):
    from modules.printers import power_fence
    from core.dispatch_target_lock import acquire_target_lock
    module, engine, calls, workers = runtime
    monkeypatch.setattr(workers, 'bounded_http_power_action', lambda *args:
        workers._OwnedHttpResult(workers.PowerAttempt('uncertain', reaped=False), workers._RECEIPT_SEAL))
    before = len(power_fence._uncertain_handles)
    module.on_print_start(599, print_job_id=599)
    assert rows(engine) == [('cleanup_uncertain', 2, None)]
    assert len(power_fence._uncertain_handles) == before + 1
    handle = power_fence._uncertain_handles[-1]
    try:
        assert handle.held()
        facts, _ = power_fence._snapshot(module, 599, 'observed_on', 599)
        assert acquire_target_lock(engine, facts.target_key).status == 'conflict'
    finally:
        # This test owns an inert fake worker; retire its test-only handle.
        assert handle.close().released
        power_fence._uncertain_handles.remove(handle)


@pytest.mark.parametrize('carrier', ['bearer', 'session', 'global'])
def test_manual_current_selected_carriers(runtime, token_client, monkeypatch, carrier):
    from core.auth import create_access_token
    from modules.printers import routes_smart_plug
    module, engine, calls, workers = runtime
    client, _ = token_client
    client.app.include_router(routes_smart_plug.router)
    headers = {'X-Power-Request-Nonce': 'c' * 32}
    if carrier == 'global':
        monkeypatch.setenv('API_KEY', 'fixture-global-key-not-a-real-credential')
        headers['X-API-Key'] = 'fixture-global-key-not-a-real-credential'
    else:
        token = create_access_token({'sub': 'admin'})
        if carrier == 'session':
            client.cookies.set('session', token)
        else:
            headers['Authorization'] = 'bEaReR ' + token
    response = client.post('/printers/599/plug/on', headers=headers)
    assert response.status_code == 200
    assert calls == ['ON']
    with engine.connect() as conn:
        kind = conn.execute(text('SELECT auth_kind FROM power_target_operations')).scalar_one()
        assert kind == {'bearer':'bearer_jwt','session':'session_jwt','global':'legacy_global_api_key'}[carrier]


def test_revoked_selected_session_cannot_fall_back_to_valid_api_key(runtime, token_client, monkeypatch):
    import jwt
    from core.auth import create_access_token, SECRET_KEY, ALGORITHM
    from modules.printers import routes_smart_plug, power_fence
    module, engine, calls, workers = runtime
    client, issue = token_client
    client.app.include_router(routes_smart_plug.router)
    token = create_access_token({'sub': 'admin'})
    jti = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])['jti']
    client.cookies.set('session', token)
    headers = {**issue(['write:printers']), 'X-Power-Request-Nonce': 'd' * 32}
    original = power_fence._snapshot
    revoked = False
    def snapshot(*args, **kwargs):
        nonlocal revoked
        if not revoked:
            with engine.begin() as conn:
                conn.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expires)'),
                             {'jti': jti, 'expires': '2099-01-01 00:00:00'})
            revoked = True
        return original(*args, **kwargs)
    monkeypatch.setattr(power_fence, '_snapshot', snapshot)
    assert client.post('/printers/599/plug/on', headers=headers).status_code == 409
    assert calls == [] and rows(engine) == []


def test_missing_fence_storage_blocks_dispatch_not_monitor_startup(monkeypatch):
    # Telemetry must not depend on power storage; every dispatch admission
    # inspects it and fails closed when it cannot.
    from core.dispatch_target_guard import unresolved_target
    from sqlalchemy import create_engine
    assert unresolved_target(create_engine('sqlite://'), 9, 'a' * 64) is True


def test_cooldown_captures_trigger_before_idle_telemetry_then_rechecks_at_send(runtime, monkeypatch):
    module, engine, calls, workers = runtime
    callbacks = []
    monkeypatch.setattr(module.threading, 'Timer', lambda delay, callback:
        (callbacks.append(callback) or SimpleNamespace(start=lambda: None, cancel=lambda: None)))
    with engine.begin() as conn:
        conn.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=599"))
    # Completion may commit before the next printer telemetry poll reaches IDLE.
    module.on_print_complete(599, print_job_id=599)
    assert calls == []
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET gcode_state='IDLE',last_seen=CURRENT_TIMESTAMP WHERE id=599"))
    callbacks[0]()
    assert calls == ['OFF'] and rows(engine) == [('complete', 3, 'off_observed')]


@pytest.mark.parametrize('host', ['plug?', 'plug#', '@plug', 'plug:', 'pl\tug', 'plug/path', 'user@plug'])
def test_destination_refuses_ambiguous_url_builder_inputs(host):
    from modules.printers.power_fence import _destination
    assert _destination({'type':'tasmota', 'host':host}) is None


def test_manual_missing_nonce_is_fenced_and_sent_once(runtime, token_client):
    from modules.printers import routes_smart_plug
    module, engine, calls, workers = runtime
    client, issue = token_client
    client.app.include_router(routes_smart_plug.router)
    response = client.post('/printers/599/plug/on', headers=issue(['write:printers']))
    assert response.status_code == 200, response.text
    assert response.json() == {'power_state': True}
    assert calls == ['ON']
    assert rows(engine) == [('complete', 3, 'on_observed')]
    with engine.connect() as conn:
        nonce, kind = conn.execute(text('SELECT request_nonce,auth_kind FROM power_target_operations')).one()
    import re
    assert re.fullmatch('[0-9a-f]{32}', nonce)
    assert kind == 'scoped_api_key'


def test_loopback_worker_exemption_is_installed_at_interpreter_startup(monkeypatch):
    from tests.support.loopback_devices import allow_loopback_devices
    monkeypatch.delenv('PYTHONPATH', raising=False)
    allow_loopback_devices(monkeypatch)
    source = """import core.webhook_utils as w
assert not w.is_blocked_device_address('127.0.0.1')
assert not w.is_blocked_device_address('::1')
assert w.is_blocked_device_address('169.254.169.254')
"""
    subprocess.run([sys.executable, '-c', source], check=True, timeout=10)
