"""Global recovery authority and unchanged safety gates through authenticated HTTP."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_dispatch_recovery_actions import (
    ATTEST, actions, recovery_client, control_client, token_client, review_db,
)


@pytest.fixture
def global_actions(actions, review_db):
    from importlib import import_module
    from modules.printers.routes_dispatch_recovery import configured_identity
    import_module('core.schema.migrations.056_dispatch_recovery_global').apply(review_db.connection())
    client, issue, calls, _, _ = actions
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text('UPDATE printers SET org_id=NULL WHERE id=299'))
    review_db.execute(text('UPDATE jobs SET charged_to_org_id=NULL,hold=1 WHERE id=953'))
    identity = configured_identity(299, review_db.connection())
    review_db.execute(text('UPDATE printer_dispatch_claims SET owner_org_id=NULL,configuration_key=:key WHERE printer_id=299'), {'key': identity.configuration_key})
    review_db.commit()
    headers = issue(['read:printers', 'write:printers', 'write:education'])

    def inspect():
        response = client.get('/printers/299/dispatch-recovery', headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def post(action, snapshot=None, **extras):
        snapshot = snapshot or inspect()
        body = {k: snapshot[k] for k in ('attempt_handle', 'snapshot_handle')}
        body['reason_category'] = 'verified_no_pending_dispatch' if action == 'release' else 'investigation'
        if action == 'release':
            body['attestation'] = ATTEST.copy()
        if action == 'reconcile-retain':
            body['education_action'] = 'none'
        body.update(extras)
        return client.post('/printers/299/dispatch-recovery/' + action, headers=headers, json=body)

    return client, issue, calls, inspect, post


def test_global_superadmin_hold_retain_release_and_next_acquisition(global_actions, review_db):
    from core.auth import SECRET_KEY
    from core.dispatch_claims import acquire, retire_terminal_locked
    from core.dispatch_target_lock import acquire_target_lock
    client, issue, calls, inspect, post = global_actions
    initial = inspect()
    assert initial['state'] == 'uncertain' and initial['job_id'] == 953
    assert initial['release_available'] and all(initial['actions'].values())
    assert initial['attempt_handle'].startswith('dr1.') and initial['snapshot_handle'].startswith('ds1.')
    claim = dict(review_db.execute(text('SELECT * FROM printer_dispatch_claims WHERE printer_id=299')).mappings().one())
    review_db.rollback()
    engine = review_db.get_bind()

    def next_dispatch_check():
        lock = acquire_target_lock(engine, claim['physical_key'])
        assert lock.status == 'acquired'
        try:
            return retire_terminal_locked(engine, printer_id=299, physical_key=claim['physical_key'],
                configuration_key=claim['configuration_key'], owner_org_id=None,
                target_handle=lock.handle, key=SECRET_KEY.encode())
        finally:
            assert lock.handle.close().released

    assert next_dispatch_check() == 'retained'
    review_db.execute(text('UPDATE jobs SET hold=0 WHERE id=953'))
    review_db.commit()
    assert post('hold').status_code == 200
    assert post('reconcile-retain').status_code == 200
    review_db.execute(text('UPDATE printers SET last_seen=CURRENT_TIMESTAMP WHERE id=299'))
    review_db.commit()
    snapshot = inspect()
    response = post('release', snapshot)
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'released'
    assert post('release', snapshot).json()['detail']['code'] == 'attempt_changed'
    assert review_db.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one() == 0
    assert review_db.execute(text('SELECT status,hold FROM jobs WHERE id=953')).one() == ('scheduled', 1)
    assert review_db.execute(text('SELECT count(*) FROM dispatch_recovery_history')).scalar_one() == 0
    assert review_db.execute(text('SELECT decision FROM dispatch_recovery_global_history ORDER BY id')).scalars().all() == ['hold', 'reconcile-retain', 'release']
    history = review_db.execute(text("SELECT actor_id,actor_identity,audit_id,facts FROM dispatch_recovery_global_history WHERE decision='release'")).mappings().one()
    facts = json.loads(history['facts'])
    assert facts['owner_org_id'] is None and facts['charged_org_id'] is None
    assert history['actor_id'] == 3
    assert history['actor_identity'] == review_db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
    assert json.loads(history['facts'])['attestation'] == ATTEST
    assert review_db.execute(text('SELECT action FROM audit_logs WHERE id=:id'), {'id': history['audit_id']}).scalar_one() == 'dispatch_recovery_release'
    review_db.rollback()
    assert next_dispatch_check() == 'clear'
    assert acquire(engine, 299, 953, claim['physical_key'], claim['configuration_key'], claim['payload_sha256']).status == 'acquired'
    assert calls == []


@pytest.mark.parametrize('change,code', [
    ('UPDATE jobs SET hold=0 WHERE id=953', 'hold_required'),
    ("UPDATE printers SET last_seen=datetime(CURRENT_TIMESTAMP,'-11 seconds') WHERE id=299", 'status_unavailable'),
    ("UPDATE printers SET gcode_state='RUNNING' WHERE id=299", 'active_observation'),
    ("UPDATE printers SET gcode_state=NULL WHERE id=299", 'status_unavailable'),
    ("UPDATE printers SET api_host='127.0.0.2:7125' WHERE id=299", 'configuration_changed'),
])
def test_global_release_prerequisites(global_actions, review_db, change, code):
    _, _, calls, inspect, post = global_actions
    review_db.execute(text(change)); review_db.commit()
    snapshot = inspect()
    assert not snapshot['release_available'] and code in snapshot['blockers']
    response = post('release', snapshot)
    assert response.status_code == 409 and response.json()['detail']['code'] == code
    assert review_db.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one() == 1
    assert calls == []


@pytest.mark.parametrize('missing', [key for key in ATTEST if key != 'version'])
def test_global_release_requires_every_attestation(global_actions, review_db, missing):
    _, _, calls, _, post = global_actions
    attestation = {key: value for key, value in ATTEST.items() if key != missing}
    assert post('release', attestation=attestation).status_code == 422
    assert review_db.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one() == 1
    assert calls == []


@pytest.mark.parametrize('role,group_id,scopes', [
    ('admin', 1, ['read:printers', 'write:printers']),
    ('operator', None, ['read:printers', 'write:printers']),
    ('viewer', None, ['read:printers', 'write:printers']),
    ('admin', None, ['read:printers']),
])
def test_global_unauthorized_principals_get_no_handles_or_actions(global_actions, review_db, role, group_id, scopes):
    client, issue, calls, inspect, _ = global_actions
    snapshot = inspect()
    review_db.execute(text('UPDATE users SET role=:role,group_id=:group_id WHERE id=3'), {'role': role, 'group_id': group_id})
    review_db.commit()
    headers = issue(scopes)
    response = client.get('/printers/299/dispatch-recovery', headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body['state'] == 'investigation_required' and not any(body['actions'].values())
    assert 'attempt_handle' not in body and 'snapshot_handle' not in body
    assert 'global administrator' in body['guidance']
    for action in ('hold', 'reconcile-retain', 'release'):
        payload = {k: snapshot[k] for k in ('attempt_handle', 'snapshot_handle')}
        payload['reason_category'] = 'verified_no_pending_dispatch' if action == 'release' else 'investigation'
        if action == 'release': payload['attestation'] = ATTEST
        if action == 'reconcile-retain': payload['education_action'] = 'none'
        assert client.post('/printers/299/dispatch-recovery/' + action, headers=headers, json=payload).status_code in (403, 404)
    assert review_db.execute(text('SELECT count(*) FROM dispatch_recovery_history')).scalar_one() == 0
    assert calls == []



@pytest.mark.parametrize('change', [
    'UPDATE printer_dispatch_claims SET owner_org_id=1 WHERE printer_id=299',
    'UPDATE jobs SET charged_to_org_id=1 WHERE id=953',
    'UPDATE printers SET org_id=1 WHERE id=299',
    'UPDATE jobs SET printer_id=NULL WHERE id=953',
])
def test_global_mixed_or_mismatched_chains_refused(global_actions, review_db, change):
    _, _, calls, inspect, post = global_actions
    snapshot = inspect()
    review_db.execute(text(change)); review_db.commit()
    body = inspect()
    assert body['state'] == 'investigation_required' and not any(body['actions'].values())
    assert 'attempt_handle' not in body and 'snapshot_handle' not in body
    for action in ('hold', 'reconcile-retain', 'release'):
        assert post(action, snapshot).status_code == 404
    assert calls == []


def test_global_superadmin_must_pass_action_policy(global_actions, review_db):
    _, _, calls, inspect, post = global_actions
    snapshot = inspect()
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('rbac_permissions',:policy)"),
        {'policy': json.dumps({'action_access': {'printers.dispatch_recovery': []}})})
    review_db.commit()
    body = inspect()
    assert body['state'] == 'investigation_required' and not any(body['actions'].values())
    assert 'attempt_handle' not in body and 'snapshot_handle' not in body
    for action in ('hold', 'reconcile-retain', 'release'):
        assert post(action, snapshot).status_code == 404
    assert calls == []


@pytest.mark.parametrize('reservation', [False, True])
def test_global_education_authority_stays_blocked(global_actions, review_db, monkeypatch, reservation):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    _, _, calls, inspect, post = global_actions
    snapshot = inspect()
    # Current tenant FKs cannot create a global Education chain. Simulate
    # retained legacy evidence, then restore enforcement before every request.
    with review_db.get_bind().connect() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=OFF')
        try:
            conn.execute(text('UPDATE education_submissions SET job_id=953,approved_printer_id=299 WHERE id=14'))
            if reservation:
                conn.execute(text("INSERT INTO education_monitor_claims(claim_id,org_id,submission_id,job_id,printer_id,authority_revision,token_digest,state) VALUES('synthetic',1,14,953,299,1,:digest,'reserved')"), {'digest': 'c' * 64})
            conn.commit()
        finally:
            conn.rollback()
            conn.exec_driver_sql('PRAGMA foreign_keys=ON')
            assert conn.exec_driver_sql('PRAGMA foreign_keys').scalar_one() == 1
    body = inspect()
    assert not body['release_available'] and not any(body['actions'].values())
    assert body['education'] is None
    for action in ('hold', 'reconcile-retain', 'release'):
        assert post(action, snapshot).status_code == 404
    assert review_db.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one() == 1
    assert calls == []


def test_global_superadmin_session_can_inspect(global_actions):
    from core.auth import create_access_token
    client, _, calls, _, _ = global_actions
    headers = {'Authorization': 'Bearer ' + create_access_token({'sub': 'admin', 'user_id': 3, 'role': 'admin', 'group_id': None})}
    response = client.get('/printers/299/dispatch-recovery', headers=headers)
    assert response.status_code == 200
    assert response.json()['release_available'] and all(response.json()['actions'].values())
    assert 'attempt_handle' in response.json() and 'snapshot_handle' in response.json()
    assert calls == []


def test_global_active_sender_blocks_release(global_actions, review_db):
    from core.dispatch_sender_schema import DispatchSenderRegistration
    _, _, calls, inspect, post = global_actions
    claim = review_db.execute(text('SELECT * FROM printer_dispatch_claims WHERE printer_id=299')).mappings().one()
    fields = {key: claim[key] for key in ('printer_id', 'job_id', 'physical_key', 'configuration_key', 'payload_sha256', 'owner_org_id')}
    fields.update(claim_token=claim['token'], origin_key='a'*32, runtime_incarnation='b'*32, registration_key='c'*32, state='active')
    review_db.execute(DispatchSenderRegistration.__table__.insert().values(**fields))
    review_db.commit()
    snapshot = inspect()
    assert not snapshot['release_available'] and 'sender_quiescence_unverified' in snapshot['blockers']
    response = post('release', snapshot)
    assert response.status_code == 409 and response.json()['detail']['code'] == 'sender_quiescence_unverified'
    assert calls == []


def test_global_linked_outside_print_must_finish_before_terminal_release(global_actions, review_db):
    from core.auth import SECRET_KEY
    from core.dispatch_claims import acquire, retire_terminal_locked
    from core.dispatch_target_lock import acquire_target_lock
    _, _, calls, inspect, post = global_actions
    claim = dict(review_db.execute(text('SELECT * FROM printer_dispatch_claims WHERE printer_id=299')).mappings().one())
    assert claim['state'] == 'uncertain'
    assert review_db.execute(text('SELECT count(*) FROM dispatch_attempt_receipts')).scalar_one() == 0
    # Persisted link-print relationship for a directly started outside print.
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,filename) VALUES(954,299,953,'running',CURRENT_TIMESTAMP,'outside.gcode')"))
    review_db.execute(text("UPDATE jobs SET status='printing',actual_start=CURRENT_TIMESTAMP WHERE id=953"))
    review_db.commit()
    snapshot = inspect()
    assert not snapshot['release_available'] and 'active_observation' in snapshot['blockers']
    response = post('release', snapshot, reason_category='verified_terminal_reconciled')
    assert response.status_code == 409 and response.json()['detail']['code'] == 'active_observation'
    assert review_db.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one() == 'uncertain'
    review_db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=954"))
    review_db.execute(text("UPDATE jobs SET status='completed',actual_end=CURRENT_TIMESTAMP WHERE id=953"))
    review_db.execute(text("UPDATE printers SET gcode_state='IDLE',last_seen=CURRENT_TIMESTAMP WHERE id=299"))
    review_db.commit()
    snapshot = inspect()
    assert snapshot['release_available']
    response = post('release', snapshot, reason_category='verified_terminal_reconciled')
    assert response.status_code == 200, response.text
    assert response.json()['outcome'] == 'released'
    assert review_db.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one() == 0
    history = review_db.execute(text('SELECT reason_category,facts FROM dispatch_recovery_global_history')).mappings().one()
    assert history['reason_category'] == 'verified_terminal_reconciled'
    assert json.loads(history['facts'])['attestation'] == ATTEST
    assert review_db.execute(text('SELECT status,hold FROM jobs WHERE id=953')).one() == ('completed', 1)
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status) VALUES(955,'Next dispatch',299,'scheduled')"))
    review_db.commit()
    engine = review_db.get_bind()
    lock = acquire_target_lock(engine, claim['physical_key'])
    assert lock.status == 'acquired'
    try:
        assert retire_terminal_locked(engine, printer_id=299, physical_key=claim['physical_key'],
            configuration_key=claim['configuration_key'], owner_org_id=None,
            target_handle=lock.handle, key=SECRET_KEY.encode()) == 'clear'
    finally:
        assert lock.handle.close().released
    assert acquire(engine, 299, 955, claim['physical_key'], claim['configuration_key'], claim['payload_sha256']).status == 'acquired'
    assert calls == []


def test_global_release_requires_target_exclusion(global_actions, review_db):
    from core.dispatch_target_lock import acquire_target_lock
    _, _, calls, inspect, post = global_actions
    snapshot = inspect()
    key = review_db.execute(text('SELECT physical_key FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one()
    review_db.rollback()
    lock = acquire_target_lock(review_db.get_bind(), key)
    assert lock.status == 'acquired'
    try:
        response = post('release', snapshot)
        assert response.status_code == 409 and response.json()['detail']['code'] == 'target_busy'
    finally:
        assert lock.handle.close().released
    assert calls == []
