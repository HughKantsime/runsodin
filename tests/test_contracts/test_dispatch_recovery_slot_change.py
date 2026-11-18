"""Release a retained start using synthetic claim-time evidence on either SQL dialect."""
import uuid
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_dispatch_recovery_actions import ATTEST
from tests.test_contracts.test_post027_migration_033_postgres import pg_engine


@pytest.fixture(scope='module', params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def slot_engine(request, tmp_path_factory):
    from core.schema import bootstrap_database
    engine = (create_engine('sqlite:///' + str(tmp_path_factory.mktemp('recovery') / 'farm.db'))
              if request.param == 'sqlite' else request.getfixturevalue('pg_engine'))
    bootstrap_database(engine)
    yield engine
    if request.param == 'sqlite':
        engine.dispose()


@pytest.fixture(scope='module')
def farm(slot_engine):
    return seed_farm(slot_engine)


def seed_farm(engine):
    with engine.begin() as conn:
        group = text("INSERT INTO groups(name,is_org) VALUES(:name,true) RETURNING id")
        suffix = uuid.uuid4().hex
        owner = conn.execute(group, {'name': f'Synthetic-{suffix}'}).scalar_one()
        other = conn.execute(group, {'name': f'Other-{suffix}'}).scalar_one()
        username = f'Recovery-{suffix}'
        user = conn.execute(text("INSERT INTO users(username,email,password_hash,role,is_active,group_id) "
                                 "VALUES(:name,:email,'fixture','admin',true,:org) RETURNING id"),
                            {'name': username, 'email': f'{suffix}@example.test', 'org': owner}).scalar_one()
    return dict(id=user, username=username, role='admin', group_id=owner, _auth_kind='bearer_jwt'), other


def test_second_farm_on_same_database_gets_fresh_ids(slot_engine, farm):
    actor, other = farm
    second, second_other = seed_farm(slot_engine)
    assert second['id'] != actor['id']
    assert {actor['group_id'], other}.isdisjoint({second['group_id'], second_other})


@pytest.fixture
def attempt(slot_engine, farm, monkeypatch):
    from core.auth import SECRET_KEY
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.dispatch_claims import acquire, enter_adapter, record_outcome
    from core.dispatch_identity import build_identity
    from core.dispatch_receipts import store_admission_receipt
    from modules.printers.dispatch import _get_printer_info
    from modules.printers.routes_dispatch_recovery import router
    actor, other_org = farm
    with slot_engine.connect() as conn:
        pid = conn.execute(text("SELECT COALESCE(MAX(id), 0) + 1 FROM "
                                "(SELECT MAX(id) AS id FROM printers UNION ALL SELECT MAX(id) FROM jobs) AS used")).scalar_one()
    app = FastAPI()
    app.include_router(router)
    def sessions():
        with Session(slot_engine) as db:
            yield db
    app.dependency_overrides[get_db] = sessions
    app.dependency_overrides[get_current_user] = lambda: actor
    monkeypatch.setattr('core.crypto.decrypt', lambda value: value)
    url = f'/printers/{pid}/dispatch-recovery'
    def change(sql, **values):
        with slot_engine.begin() as conn:
            conn.execute(text(sql), {'pid': pid, 'org': actor['group_id'], 'other_org': other_org, **values})

    def prepare(kind='new', *, recovery_overrides=None):
        change("INSERT INTO printers(id,name,api_type,api_host,api_key,is_active,shared,org_id,gcode_state,last_seen) "
               "VALUES(:pid,:name,'bambu',:host,:credential,true,false,:org,'IDLE',CURRENT_TIMESTAMP)",
               name=f'Test printer {pid}', host=f'printer-{pid}.example.test', credential=f'Serial-{pid}|synthetic')
        change("INSERT INTO jobs(id,item_name,status,hold,printer_id,charged_to_org_id) "
               "VALUES(:pid,'Test job','scheduled',true,:pid,:org)")
        change("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES (:pid,1,'PLA')")
        key = SECRET_KEY.encode()
        with slot_engine.connect() as conn:
            creds = _get_printer_info(pid, connection=conn)
        identity = build_identity(creds, key)
        recovery_creds = {**creds, **(recovery_overrides or {})} if kind in ('new', 'admitted') else None
        claim = acquire(slot_engine, pid, pid, identity.target_key, identity.configuration_key, 'a'*64, actor['group_id'],
                        recovery_creds=recovery_creds)
        assert claim.status == 'acquired' and enter_adapter(slot_engine, claim.token)
        if kind in ('receipt', 'backfill', 'admitted'):
            with slot_engine.begin() as conn:
                store_admission_receipt(conn, token=claim.token, job={'id':pid,'printer_id':pid}, creds=creds, key=key,
                    configuration_key=identity.configuration_key, filename=f'odin-dispatch-{claim.token}.gcode',
                    broker_connection=None, power_accepted=True)
            if kind == 'backfill':
                change("UPDATE dispatch_attempt_receipts SET stable_key_source='backfilled_no_printer_audit' WHERE printer_id=:pid")
        assert record_outcome(slot_engine, claim.token, 'uncertain')

    with TestClient(app) as client:
        def inspect(as_actor=None):
            app.dependency_overrides[get_current_user] = lambda: {**actor, **(as_actor or {})}
            response = client.get(url)
            assert response.status_code == 200, response.text
            return response.json()
        def release(snapshot):
            body = {k:snapshot[k] for k in ('attempt_handle','snapshot_handle')}
            return client.post(url+'/release', json={**body,'reason_category':'verified_no_pending_dispatch','attestation':ATTEST})
        yield prepare, change, inspect, release, pid


@pytest.mark.parametrize('kind', ['new', 'receipt', 'admitted'])
def test_slot_move_allows_release_and_records_history(attempt, slot_engine, kind):
    prepare, change, inspect, release, pid = attempt
    prepare(kind)
    before = inspect()
    assert before['release_available'] and before['filament_slots_changed'] is False
    change("UPDATE filament_slots SET slot_number=3 WHERE printer_id=:pid")
    assert release(before).json()['detail']['code'] == 'snapshot_changed'
    after = inspect()
    assert after['release_available'] and after['filament_slots_changed'] is True
    response = release(after)
    assert response.status_code == 200, response.text
    with slot_engine.connect() as conn:
        assert not conn.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:pid'), {'pid':pid}).first()
        assert not conn.execute(text('SELECT 1 FROM system_config WHERE key=:key'),
                                {'key': f'dispatch_recovery_keys:{pid}'}).first()
        facts = conn.execute(text('SELECT facts FROM dispatch_recovery_history WHERE printer_id=:pid'), {'pid':pid}).scalar_one()
        assert json.loads(facts)['filament_slots_changed'] is True


@pytest.mark.parametrize('kind', ['new', 'receipt', 'admitted'])
@pytest.mark.parametrize('edit', ['host', 'port', 'serial', 'credentials', 'protocol', 'sharing', 'inactive'])
def test_connection_change_still_refuses_after_reload(attempt, kind, edit):
    prepare, change, inspect, release, pid = attempt
    prepare(kind)
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    updates = {'host': ('api_host', 'other.example.test'), 'port': ('api_host', f'printer-{pid}.example.test:443'),
               'serial': ('api_key', f'SERIAL-{pid}|synthetic'), 'credentials': ('api_key', f'Serial-{pid}|changed'),
               'protocol': ('api_type', 'moonraker'), 'sharing': ('shared', True), 'inactive': ('is_active', False)}
    column, value = updates[edit]
    change(f'UPDATE printers SET {column}=:value WHERE id=:pid', value=value)
    evidence = inspect()
    assert not evidence['release_available'] and 'configuration_changed' in evidence['blockers']
    assert release(evidence).json()['detail']['code'] == 'configuration_changed'


@pytest.mark.parametrize('kind', ['new', 'admitted'])
def test_changed_owner_keeps_attempt_private(attempt, kind):
    prepare, change, inspect, release, _ = attempt
    prepare(kind)
    before = inspect()
    change('UPDATE printers SET org_id=:other_org WHERE id=:pid')
    assert release(before).status_code == 404


@pytest.mark.parametrize('kind', ['missing', 'backfill'])
def test_legacy_claim_without_original_proof_stays_blocked(attempt, kind):
    prepare, change, inspect, release, _ = attempt
    prepare(kind)
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    snapshot = inspect()
    assert not snapshot['release_available']
    assert release(snapshot).json()['detail']['code'] == 'configuration_changed'


@pytest.mark.parametrize('edit,code', [
    ("UPDATE jobs SET hold=false WHERE id=:pid", 'hold_required'),
    ("UPDATE printers SET last_seen=NULL WHERE id=:pid", 'status_unavailable'),
    ("UPDATE printers SET gcode_state='PRINTING' WHERE id=:pid", 'active_observation'),
])
def test_slot_change_does_not_clear_another_blocker(attempt, edit, code):
    prepare, change, inspect, release, _ = attempt
    prepare()
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    change(edit)
    evidence = inspect()
    assert evidence['filament_slots_changed'] and not evidence['release_available'] and code in evidence['blockers']
    assert release(evidence).json()['detail']['code'] == code


@pytest.mark.parametrize('kind', ['new', 'admitted', 'receipt'])
def test_power_edit_blocks_new_claims_and_is_unknown_for_old_receipts(attempt, kind):
    prepare, change, inspect, release, _ = attempt
    prepare(kind)
    change("UPDATE printers SET plug_host='new-plug.example.test' WHERE id=:pid")
    evidence = inspect()
    assert evidence['filament_slots_changed'] is None
    if kind == 'receipt':
        assert evidence['release_available'] and release(evidence).status_code == 200
    else:
        assert not evidence['release_available'] and 'configuration_changed' in evidence['blockers']
        assert release(evidence).json()['detail']['code'] == 'configuration_changed'


def test_replacement_claim_cannot_reuse_prior_recovery_keys(attempt):
    prepare, change, inspect, release, _ = attempt
    prepare()
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    change("UPDATE printer_dispatch_claims SET token=:replacement WHERE printer_id=:pid", replacement=uuid.uuid4().hex)
    snapshot = inspect()
    assert not snapshot['release_available']
    assert release(snapshot).json()['detail']['code'] == 'configuration_changed'


@pytest.mark.parametrize('invalid', [{'ip': ''}, {'filament_slots': [{}]}, {'active_materials': [None, 'PLA']}])
def test_unbuildable_recovery_identity_preserves_admission(attempt, slot_engine, invalid):
    prepare, change, inspect, release, pid = attempt
    prepare(recovery_overrides=invalid)
    with slot_engine.connect() as conn:
        assert conn.execute(text('SELECT 1 FROM system_config WHERE key=:key'),
                            {'key': f'dispatch_recovery_keys:{pid}'}).first() is None
    assert inspect()['release_available']
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    assert release(inspect()).json()['detail']['code'] == 'configuration_changed'


def test_unbuildable_filtered_identity_blocks_inspect_and_release(attempt, monkeypatch):
    from core import dispatch_recovery_identity as recovery
    prepare, change, inspect, release, _ = attempt
    prepare('admitted')
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    original = recovery.build_identity
    def build(creds, key):
        return original(creds, key) if 'filament_slots' in creds else None
    monkeypatch.setattr(recovery, 'build_identity', build)
    snapshot = inspect()
    assert not snapshot['release_available'] and 'configuration_changed' in snapshot['blockers']
    assert release(snapshot).json()['detail']['code'] == 'configuration_changed'


@pytest.mark.parametrize('field,value', [('scheme', 'https'), ('api_key', 'other'), ('username', 'other'),
                                         ('password', 'other'), ('org_id', 1000000), ('model', 'other'), ('machine_type', 'other'),
                                         ('bed_x_mm', 245), ('bed_y_mm', 235), ('nozzle_diameter', 0.6)])
def test_admitted_claim_checks_all_configured_facts(attempt, monkeypatch, field, value):
    from modules.printers import dispatch
    prepare, _, inspect, release, pid = attempt
    prepare('admitted')
    original = dispatch._get_printer_info
    def read(printer_id, **kwargs):
        creds = original(printer_id, **kwargs)
        return {**creds, field: value} if printer_id == pid else creds
    monkeypatch.setattr(dispatch, '_get_printer_info', read)
    snapshot = inspect()
    assert not snapshot['release_available'] and 'configuration_changed' in snapshot['blockers']
    assert release(snapshot).json()['detail']['code'] == 'configuration_changed'


def test_stored_mismatch_cannot_use_matching_receipt(attempt):
    prepare, change, inspect, release, pid = attempt
    prepare('admitted')
    change('UPDATE system_config SET value=:value WHERE key=:key',
           key=f'dispatch_recovery_keys:{pid}', value=json.dumps({'connection': '0'*64, 'slots': '1'*64}))
    snapshot = inspect()
    assert not snapshot['release_available'] and 'configuration_changed' in snapshot['blockers']
    assert release(snapshot).json()['detail']['code'] == 'configuration_changed'


@pytest.mark.parametrize('actor', [{'role': 'viewer'}, {'role': 'operator'}, {'_token_scopes': ['read:printers']}])
def test_read_only_panel_omits_slot_comparison(attempt, monkeypatch, actor):
    from unittest.mock import Mock
    from modules.printers import routes_dispatch_recovery as routes
    prepare, change, inspect, _, _ = attempt
    prepare('admitted')
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    compare = Mock(wraps=routes.recovery_configuration)
    monkeypatch.setattr(routes, 'recovery_configuration', compare)
    snapshot = inspect(actor)
    assert not snapshot['actions']['release'] and 'filament_slots_changed' not in snapshot
    compare.assert_not_called()


@pytest.mark.parametrize('state', ['preparing', 'adapter_entered'])
def test_prestart_release_clears_only_owned_fingerprint(attempt, slot_engine, state):
    from core.dispatch_claims import release_preparing, release_unstarted
    prepare, change, _, _, pid = attempt
    prepare()
    change('UPDATE printer_dispatch_claims SET state=:state WHERE printer_id=:pid', state=state)
    statement = text('SELECT 1 FROM system_config WHERE key=:key')
    params = {'key': f'dispatch_recovery_keys:{pid}'}
    release = release_preparing if state == 'preparing' else release_unstarted
    with slot_engine.connect() as conn:
        token = conn.execute(text('SELECT token FROM printer_dispatch_claims WHERE printer_id=:pid'), {'pid': pid}).scalar_one()
    assert not release(slot_engine, '0'*32)
    with slot_engine.connect() as conn:
        assert conn.execute(statement, params).first()
    assert release(slot_engine, token)
    with slot_engine.connect() as conn:
        assert conn.execute(statement, params).first() is None


@pytest.mark.parametrize('creds', [None, {'ip': ''}])
def test_replacement_without_keys_removes_stale_record(attempt, slot_engine, creds):
    from core.dispatch_claims import acquire
    prepare, _, _, _, pid = attempt
    prepare()
    with slot_engine.begin() as conn:
        old = conn.execute(text('DELETE FROM printer_dispatch_claims WHERE printer_id=:pid RETURNING '
                                'physical_key,configuration_key,payload_sha256,owner_org_id'), {'pid': pid}).mappings().one()
    assert acquire(slot_engine, pid, pid, **old, recovery_creds=creds).status == 'acquired'
    with slot_engine.connect() as conn:
        assert not conn.execute(text('SELECT 1 FROM system_config WHERE key=:key'),
                                {'key': f'dispatch_recovery_keys:{pid}'}).first()


def test_deleted_printer_clears_recovery_keys(attempt, slot_engine, farm, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers.printer_deletion import delete_printer
    _, change, _, _, pid = attempt
    change("INSERT INTO printers(id,name,org_id) VALUES(:pid,'Deleted printer',:org)")
    change("INSERT INTO system_config(key,value) VALUES(:key,'{}')", key=f'dispatch_recovery_keys:{pid}')
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    with Session(slot_engine) as db:
        assert delete_printer(db, pid, farm[0]).status_code == 204
        assert not db.execute(text('SELECT 1 FROM system_config WHERE key=:key'),
                              {'key': f'dispatch_recovery_keys:{pid}'}).first()


def test_config_cannot_list_or_set_recovery_keys(attempt, slot_engine, farm):
    from modules.system.routes_config import get_config, update_config
    from modules.system.schemas import ConfigUpdate
    prepare, _, _, _, pid = attempt
    prepare()
    key = f'dispatch_recovery_keys:{pid}'
    query = text('SELECT value FROM system_config WHERE key=:key')
    with Session(slot_engine) as db:
        before = db.execute(query, {'key': key}).scalar_one()
        assert key not in get_config(farm[0], db)
        update_config(ConfigUpdate.model_validate({key: 'replacement'}), farm[0], db)
        assert db.execute(query, {'key': key}).scalar_one() == before
