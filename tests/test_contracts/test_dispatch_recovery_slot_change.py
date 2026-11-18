"""Release a retained start using synthetic claim-time evidence on either SQL dialect."""
import itertools
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
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO groups(id,name,is_org) VALUES(9100,'Synthetic',true),(9101,'Other',true)"))
        conn.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,group_id) "
                          "VALUES(9100,'Recovery admin','admin@example.test','fixture','admin',true,9100)"))
    yield engine
    if request.param == 'sqlite':
        engine.dispose()


_ids = itertools.count(9200)


@pytest.fixture
def attempt(slot_engine, monkeypatch):
    from core.auth import SECRET_KEY
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.dispatch_claims import acquire, enter_adapter, record_outcome
    from core.dispatch_identity import build_identity
    from core.dispatch_receipts import store_admission_receipt
    from modules.printers.dispatch import _get_printer_info
    from modules.printers.routes_dispatch_recovery import router
    pid = next(_ids)
    actor = dict(id=9100, username='Recovery admin', role='admin', group_id=9100, _auth_kind='bearer_jwt')
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
            conn.execute(text(sql), {'pid': pid, **values})

    def prepare(kind='new'):
        change("INSERT INTO printers(id,name,api_type,api_host,api_key,is_active,shared,org_id,gcode_state,last_seen) "
               "VALUES(:pid,:name,'bambu',:host,:credential,true,false,9100,'IDLE',CURRENT_TIMESTAMP)",
               name=f'Test printer {pid}', host=f'printer-{pid}.example.test', credential=f'Serial-{pid}|synthetic')
        change("INSERT INTO jobs(id,item_name,status,hold,printer_id,charged_to_org_id) "
               "VALUES(:pid,'Test job','scheduled',true,:pid,9100)")
        change("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES (:pid,1,'PLA')")
        key = SECRET_KEY.encode()
        with slot_engine.connect() as conn:
            creds = _get_printer_info(pid, connection=conn)
        identity = build_identity(creds, key)
        claim = acquire(slot_engine, pid, pid, identity.target_key, identity.configuration_key, 'a'*64, 9100,
                        recovery_creds=creds if kind == 'new' else None)
        assert claim.status == 'acquired' and enter_adapter(slot_engine, claim.token)
        if kind in ('receipt', 'backfill'):
            with slot_engine.begin() as conn:
                store_admission_receipt(conn, token=claim.token, job={'id':pid,'printer_id':pid}, creds=creds, key=key,
                    configuration_key=identity.configuration_key, filename=f'odin-dispatch-{claim.token}.gcode',
                    broker_connection=None, power_accepted=True)
            if kind == 'backfill':
                change("UPDATE dispatch_attempt_receipts SET stable_key_source='backfilled_no_printer_audit' WHERE printer_id=:pid")
        assert record_outcome(slot_engine, claim.token, 'uncertain')

    with TestClient(app) as client:
        def inspect():
            response = client.get(url)
            assert response.status_code == 200, response.text
            return response.json()
        def release(snapshot):
            body = {k:snapshot[k] for k in ('attempt_handle','snapshot_handle')}
            return client.post(url+'/release', json={**body,'reason_category':'verified_no_pending_dispatch','attestation':ATTEST})
        yield prepare, change, inspect, release, pid


@pytest.mark.parametrize('kind', ['new', 'receipt'])
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
        facts = conn.execute(text('SELECT facts FROM dispatch_recovery_history WHERE printer_id=:pid'), {'pid':pid}).scalar_one()
        assert json.loads(facts)['filament_slots_changed'] is True


@pytest.mark.parametrize('kind', ['new', 'receipt'])
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


def test_changed_owner_keeps_attempt_private(attempt):
    prepare, change, inspect, release, _ = attempt
    prepare()
    before = inspect()
    change('UPDATE printers SET org_id=9101 WHERE id=:pid')
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


@pytest.mark.parametrize('kind,expected', [('new', False), ('receipt', None)])
def test_power_edit_alone_never_reported_as_filament_change(attempt, kind, expected):
    prepare, change, inspect, release, _ = attempt
    prepare(kind)
    change("UPDATE printers SET plug_host='new-plug.example.test' WHERE id=:pid")
    evidence = inspect()
    assert evidence['release_available'] and evidence['filament_slots_changed'] is expected
    assert release(evidence).status_code == 200


def test_replacement_claim_cannot_reuse_prior_recovery_keys(attempt):
    prepare, change, inspect, release, _ = attempt
    prepare()
    change("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=:pid")
    change("UPDATE printer_dispatch_claims SET token=:replacement WHERE printer_id=:pid", replacement='c'*32)
    snapshot = inspect()
    assert not snapshot['release_available']
    assert release(snapshot).json()['detail']['code'] == 'configuration_changed'
