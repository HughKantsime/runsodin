"""Tray identity and consumption contracts on owned SQLite/PostgreSQL databases."""
import json
from pathlib import Path
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_spool_rfid_identity import (
    identity_engine, postgres_server, inventory, sync, seed, report, rows,
)

FIXTURES = json.loads((Path(__file__).parents[1] / 'fixtures/tray_identity/swaps.json').read_text())
USER = dict(id=1, role='admin', group_id=1, org_id=1)


def slot(engine):
    from modules.printers.models import FilamentSlot
    from modules.printers.schemas import FilamentSlotResponse
    with Session(engine) as db:
        return FilamentSlotResponse.model_validate(db.query(FilamentSlot).filter_by(printer_id=1, slot_number=1).one()).model_dump()


def assign(engine, spool=56):
    from modules.inventory.slot_assignments import assign_slot
    from modules.printers.models import FilamentSlot
    with Session(engine) as db:
        assign_slot(db, db.query(FilamentSlot).filter_by(printer_id=1,slot_number=1).one(), spool, USER)
        db.commit()


def confirm(engine, decision='same', spool_id=None, observed=None):
    from modules.inventory.routes.filament_slots import confirm_tray_identity, ConfirmTrayIdentity
    state = observed or slot(engine)['observed_state']
    with Session(engine) as db:
        return confirm_tray_identity(1, 1, ConfirmTrayIdentity(observed_state=state, decision=decision, spool_id=spool_id), current_user=USER, db=db)


@pytest.mark.parametrize('printer', [1, 2])
def test_rfid_to_nonrfid_unbinds_and_hides_old_identity(inventory, sync, printer):
    seed(inventory)
    sync(FIXTURES['rfid'], printer=printer)
    sync(FIXTURES['tagless'], printer=printer)
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(None,)]
    assert rows(inventory, 'SELECT location_printer_id,location_slot FROM spools') == [(None,None)]
    if printer == 1:
        result = slot(inventory)
        assert result['remaining'] is None and result['remaining_weight'] is None
        assert result['brand'] is None
        assert result['color'] == 'preset: Bambu PETG HF'


def test_unchanged_rfid_color_flicker_keeps_binding(inventory, sync):
    seed(inventory)
    sync(report())
    sync(report(tray_color='9E9E9EFF'))
    assert slot(inventory)['assigned_spool_id'] == 56
    assert not slot(inventory)['spool_change_pending']
    sync(report(tray_color='9E9E9EFF', remain=-1))
    assert slot(inventory)['remaining'] is None


@pytest.mark.parametrize('changes', [dict(tray_sub_brands='PETG Basic'), dict(tray_color='898989FF'), dict(tray_type='PLA')])
def test_single_weak_change_flags_keeps_binding_and_confirm_is_idempotent(inventory, sync, changes):
    seed(inventory, sealed=False)
    sync(FIXTURES['tagless'])
    assign(inventory)
    changed = {**FIXTURES['tagless'], **changes}
    sync(changed)
    value = slot(inventory)
    assert value['assigned_spool_id'] == 56 and value['spool_change_pending']
    assert value['remaining'] is None
    assert confirm(inventory)['replayed'] is False
    assert confirm(inventory)['replayed'] is True
    sync(changed)
    assert not slot(inventory)['spool_change_pending']
    assert slot(inventory)['spool_confirmed']
    assert rows(inventory, "SELECT count(*) FROM audit_logs WHERE action='tray_identity.confirmed'") == [(1,)]


def test_material_and_color_together_unbind(inventory, sync):
    seed(inventory, sealed=False)
    sync(FIXTURES['tagless']); assign(inventory)
    sync({**FIXTURES['tagless'], 'tray_type':'PLA', 'tray_color':'898989FF'})
    assert slot(inventory)['assigned_spool_id'] is None


def test_clear_then_set_across_queued_reports(inventory):
    from modules.printers.telemetry.bambu.ams_identity import inventory_report
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    from core.db_utils import _ConnectionAdapter
    seed(inventory, sealed=False)
    units = [{'id':'0', 'tray':[FIXTURES['tagless']]}]
    def send(value):
        raw = inventory.raw_connection(); conn = _ConnectionAdapter(raw, inventory.dialect.name == 'postgresql')
        try:
            sync_ams_inventory(conn, 1, value); conn.commit()
        finally:
            raw.close()
    send(inventory_report(units, '1')); assign(inventory)
    empty = inventory_report(units, '0')
    loaded = inventory_report(units, '1', empty)
    send(loaded)
    assert slot(inventory)['assigned_spool_id'] is None


def print_case(engine, target=0, observed_tray=None, after_capture=None, materials_json='[{"used_grams":165.89}]'):
    from modules.archives.attempt_accounting import capture_attempt_spools, account_archive, record_attempt_tray
    with engine.begin() as c:
        c.execute(text("INSERT INTO print_files(id,filename,org_id,filament_weight_grams,filaments_json) VALUES(8,'fixture.3mf',1,165.89,:materials)"), dict(materials=materials_json))
        c.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,charged_to_org_id,print_file_id,ams_mapping) VALUES(87,'fixture','completed',1,1,8,:mapping)"),dict(mapping=json.dumps([target]) if target is not None else None))
        c.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) VALUES(129,1,87,'running',CURRENT_TIMESTAMP)"))
        assert capture_attempt_spools(c,129,1,source='fixture')
        snapshot = c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=129')).scalar_one()
        if observed_tray is not None:
            record_attempt_tray(c, 129, 1, observed_tray)
    if after_capture:
        after_capture()
    with engine.begin() as c:
        c.execute(text("INSERT INTO print_archives(id,print_job_id,printer_id,job_id,print_name,status,ownership_state,org_id) VALUES(129,129,1,87,'fixture','completed','known',1)"))
        outcome, reason, entries = account_archive(c,archive_id=129,print_job_id=129,printer_id=1,snapshot_json=snapshot,scheduled_job_id=87,measured_grams=None,success=True)
        c.execute(text('UPDATE print_archives SET accounting_state=:state,accounting_reason=:reason WHERE id=129'),dict(state=outcome,reason=reason))
    return outcome, reason, entries


@pytest.mark.parametrize('target,observed_tray', [(3,None),(None,'3'),(None,None)])
def test_a1_slot_four_never_charges_spool_38_in_slot_three(inventory, sync, target, observed_tray):
    seed(inventory,38,sealed=False)
    sync({**FIXTURES['tagless'], 'id':'2'})
    with inventory.begin() as c:
        c.execute(text('UPDATE filament_slots SET assigned_spool_id=38'))
        c.execute(text('UPDATE spools SET location_printer_id=1,location_slot=3 WHERE id=38'))
    outcome, reason, _ = print_case(inventory,target,observed_tray)
    assert outcome == 'unresolved'
    assert rows(inventory,'SELECT remaining_weight_g FROM spools WHERE id=38') == [(1000,)]
    assert rows(inventory,'SELECT count(*) FROM spool_usage') == [(0,)]
    assert json.loads(rows(inventory,'SELECT consumption_json FROM print_archives')[0][0])[0]['spool_id'] is None


@pytest.mark.parametrize('decision,target', [('same',56),('new',57)])
@pytest.mark.parametrize('before_completion', [False,True])
def test_pending_consumption_waits_for_confirmed_spool(inventory,sync,decision,target,before_completion):
    seed(inventory,56,sealed=False); seed(inventory,57,sealed=False)
    sync(FIXTURES['tagless']); assign(inventory)
    sync(FIXTURES['grey'])
    state = slot(inventory)['observed_state']
    if before_completion:
        confirm(inventory,decision,57 if decision=='new' else None)
    outcome, reason, entries = print_case(inventory)
    if not before_completion:
        assert (outcome,reason) == ('unresolved','spool_change_pending')
        assert entries[0]['grams_used'] == 165.89
        assert rows(inventory,'SELECT count(*) FROM spool_usage') == [(0,)]
        confirm(inventory,decision,57 if decision=='new' else None)
    assert rows(inventory,'SELECT spool_id,weight_used_g FROM spool_usage') == [(target,165.89)]
    assert rows(inventory,f'SELECT remaining_weight_g FROM spools WHERE id={target}')[0][0] == pytest.approx(834.11)
    assert confirm(inventory,decision,57 if decision=='new' else None)['replayed']
    sync(FIXTURES['grey'])
    assert not slot(inventory)['spool_change_pending']
    assert slot(inventory)['observed_state'] == state


def test_stale_or_conflicting_confirmation_refused(inventory,sync):
    from fastapi import HTTPException
    seed(inventory,56,sealed=False); seed(inventory,57,sealed=False)
    sync(FIXTURES['tagless']); assign(inventory)
    sync(FIXTURES['grey'])
    old = slot(inventory)['observed_state']
    confirm(inventory)
    with pytest.raises(HTTPException) as conflict:
        confirm(inventory,'new',57)
    assert conflict.value.status_code == 409
    sync({**FIXTURES['grey'],'tray_sub_brands':'PETG Basic'})
    with pytest.raises(HTTPException) as stale:
        confirm(inventory,observed=old)
    assert stale.value.status_code == 409


def test_confirmation_does_not_change_dispatch_configuration(inventory,sync):
    from modules.printers.dispatch_loading import _get_printer_info
    seed(inventory,sealed=False)
    sync(FIXTURES['tagless']); assign(inventory)
    with inventory.connect() as c:
        before = _get_printer_info(1, connection=c)
    sync(FIXTURES['grey'])
    assert slot(inventory)['spool_change_pending']
    with inventory.connect() as c:
        after = _get_printer_info(1, connection=c)
    assert before is not None and before == after


@pytest.mark.parametrize('role,scope,org,status', [
    ('operator','write:printers',1,200), ('viewer','write:printers',1,403),
    ('operator','read:printers',1,403), ('operator','write:printers',2,404),
])
def test_confirmation_http_authorization(inventory,sync,role,scope,org,status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.inventory.routes.filament_slots import router
    seed(inventory,sealed=False)
    sync(FIXTURES['tagless']); assign(inventory); sync(FIXTURES['grey'])
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_current_user]=lambda:dict(id=1,role=role,group_id=org,_auth_kind='user_api_token',_token_scopes=[scope])
    def database():
        with Session(inventory) as db:
            yield db
    app.dependency_overrides[get_db]=database
    with TestClient(app) as client:
        result=client.post('/printers/1/slots/1/confirm-identity',json=dict(observed_state=slot(inventory)['observed_state'],decision='same'))
    assert result.status_code==status, result.text


def test_pending_unbound_tray_can_confirm_new_spool(inventory,sync):
    seed(inventory,sealed=False)
    sync(FIXTURES['tagless']); sync(FIXTURES['grey'])
    assert print_case(inventory)[1]=='spool_change_pending'
    confirm(inventory,'new',56)
    assert rows(inventory,'SELECT spool_id,weight_used_g FROM spool_usage')==[(56,165.89)]


def test_failed_audit_rolls_back_confirmation_and_consumption(inventory,sync,monkeypatch):
    from core import dependencies
    seed(inventory,sealed=False)
    sync(FIXTURES['tagless']);assign(inventory);sync(FIXTURES['grey'])
    print_case(inventory)
    def fail(*args,**kwargs):
        raise RuntimeError('fixture audit failure')
    monkeypatch.setattr(dependencies,'log_audit',fail)
    with pytest.raises(RuntimeError,match='fixture audit failure'):
        confirm(inventory)
    assert slot(inventory)['spool_change_pending']
    assert rows(inventory,'SELECT count(*) FROM tray_confirmations')==[(0,)]
    assert rows(inventory,'SELECT count(*) FROM spool_usage')==[(0,)]
    assert rows(inventory,'SELECT remaining_weight_g FROM spools')==[(1000,)]


def test_confirm_attributes_unknown_balance_without_inventing_remaining(inventory,sync):
    seed(inventory,sealed=False)
    sync(FIXTURES['tagless']);assign(inventory);sync(FIXTURES['grey'])
    with inventory.begin() as c:
        c.execute(text('UPDATE spools SET remaining_weight_g=NULL WHERE id=56'))
    assert print_case(inventory)[1]=='spool_change_pending'
    confirm(inventory)
    assert rows(inventory,'SELECT spool_id,weight_used_g FROM spool_usage')==[(56,165.89)]
    assert rows(inventory,'SELECT remaining_weight_g FROM spools')==[(None,)]


def test_one_confirmation_resolves_consumption_across_weak_updates(inventory,sync):
    seed(inventory,sealed=False)
    sync(FIXTURES['tagless']);assign(inventory);sync(FIXTURES['grey'])
    old = slot(inventory)['observed_state']
    print_case(inventory)
    sync({**FIXTURES['grey'], 'tray_sub_brands':'PETG Basic'})
    assert old != slot(inventory)['observed_state']
    confirm(inventory)
    assert rows(inventory,'SELECT spool_id,weight_used_g FROM spool_usage')==[(56,165.89)]
    assert rows(inventory,'SELECT accounting_state FROM print_archives')==[('accounted',)]


def test_tagless_empty_bit_departs_immediately():
    from core.tray_identity import advance
    initial,_=advance({}, {**FIXTURES['tagless'],'_exists':True})
    empty,unbind=advance(initial,{**FIXTURES['tagless'],'_exists':False})
    assert unbind
    assert empty['remaining'] is None
    _,unbind=advance(empty,{**FIXTURES['tagless'],'_exists':True})
    assert unbind


@pytest.mark.parametrize('flag_before_start', [False, True])
@pytest.mark.parametrize('decision,target', [('same',56),('new',57)])
def test_confirmation_during_print_resolves_start_snapshot(inventory,sync,flag_before_start,decision,target):
    seed(inventory,56,sealed=False);seed(inventory,57,sealed=False)
    sync(FIXTURES['tagless']);assign(inventory)
    if flag_before_start:
        sync(FIXTURES['grey'])
    def during_print():
        if not flag_before_start:
            sync(FIXTURES['grey'])
        confirm(inventory,decision,57 if decision=='new' else None)
        sync(FIXTURES['grey'])
    assert print_case(inventory,after_capture=during_print)[0]=='accounted'
    assert rows(inventory,'SELECT spool_id,weight_used_g FROM spool_usage')==[(target,165.89)]


def test_missing_exist_bits_preserve_last_reported_state():
    from modules.printers.telemetry.bambu.ams_identity import inventory_report
    units=[dict(id='0',tray=[FIXTURES['tagless']])]
    loaded=inventory_report(units,'1')
    unchanged=inventory_report(units,None,loaded)
    assert unchanged[0]['tray'][0]['_exists'] is True
    assert not unchanged[0]['tray'][0]['_removed']


@pytest.mark.parametrize('field', ['tag_uid', 'tray_uuid'])
def test_rfid_case_does_not_change_identity(field):
    from core.tray_identity import advance
    tray = {**FIXTURES['tagless'], field: 'aBcD1234'}
    bound = {'bound_tag' if field == 'tag_uid' else 'bound_uuid': 'AbCd1234'}
    initial, unbind = advance({}, tray, **bound)
    assert not unbind and not initial['pending']
    # Also normalize observation JSON saved before this fix.
    initial['observed']['tag' if field == 'tag_uid' else 'uuid'] = 'abcd1234'
    again, unbind = advance(initial, {**tray, field: 'ABCD1234'})
    assert not unbind and not again['pending']
    assert again['id'] == initial['id']


@pytest.mark.parametrize('grams', [0, 250])
@pytest.mark.parametrize('change', ['empty', 'both', 'rfid_loss'])
def test_departure_effects_share_authority(inventory, sync, grams, change):
    _departure_case(inventory, sync, grams, change)


# Queued _removed evidence exists only on the monitor path; the API path's empty
# report is covered above. Parametrized to the monitor, never skipped.
@pytest.mark.parametrize('sync', ['monitor'], indirect=True)
@pytest.mark.parametrize('grams', [0, 250])
def test_monitor_removed_evidence_departs(inventory, sync, grams):
    _departure_case(inventory, sync, grams, 'removed')


def _departure_case(inventory, sync, grams, change):
    seed(inventory, sealed=change == 'rfid_loss')
    first = FIXTURES['rfid'] if change == 'rfid_loss' else FIXTURES['tagless']
    sync(first)
    if change != 'rfid_loss':
        assign(inventory)
    with inventory.begin() as c:
        c.execute(text("UPDATE spools SET remaining_weight_g=:grams,status='active',storage_location='Shelf' WHERE id=56"), dict(grams=grams))
        c.execute(text("INSERT INTO system_config(key,value) VALUES('inventory.sealed.56','true')"))
    changes = {
        'empty': {**FIXTURES['tagless'], 'tray_type':''},
        'both': {**FIXTURES['tagless'], 'tray_type':'PLA', 'tray_color':'898989FF'},
        'removed': {**FIXTURES['tagless'], '_removed':True, '_exists':True},
        'rfid_loss': FIXTURES['tagless'],
    }
    sync(changes[change])
    assert rows(inventory, 'SELECT assigned_spool_id FROM filament_slots') == [(None,)]
    assert rows(inventory, 'SELECT location_printer_id,location_slot,status,storage_location,remaining_weight_g FROM spools') == [
        (None,None,'empty' if grams == 0 else 'active',None if grams == 0 else 'Unlocated (removed from slot)',grams)]
    assert rows(inventory, "SELECT key FROM system_config WHERE key='inventory.sealed.56'") == []
    table = 'audit_logs' if sync.path == 'api' else 'inventory_repairs'
    assert rows(inventory, f"SELECT action FROM {table} WHERE action LIKE 'telemetry.%'") == [('telemetry.retire' if grams == 0 else 'telemetry.relocate',)]


def test_zero_balance_relocation_is_not_retirement(inventory, sync):
    seed(inventory)
    sync(report())
    with inventory.begin() as c:
        c.execute(text("UPDATE spools SET remaining_weight_g=0,status='active' WHERE id=56"))
    sync(trays=[report(tray_type='', tag_uid=None, tray_uuid=None), report(id='1', remain=-1)])
    assert rows(inventory, 'SELECT location_printer_id,location_slot,status,storage_location,remaining_weight_g FROM spools') == [(1,2,'active',None,0)]
    assert rows(inventory, 'SELECT slot_number,assigned_spool_id FROM filament_slots ORDER BY slot_number') == [(1,None),(2,56)]
    table = 'audit_logs' if sync.path == 'api' else 'inventory_repairs'
    assert rows(inventory, f"SELECT action FROM {table} WHERE action LIKE 'telemetry.%'") == [('telemetry.relocate',)]



def test_departure_effects_roll_back_with_caller(inventory, sync, monkeypatch):
    from modules.inventory.slot_assignments import TelemetryInventoryMoves
    seed(inventory, sealed=False)
    sync(FIXTURES['tagless']); assign(inventory)
    with inventory.begin() as c:
        c.execute(text("UPDATE spools SET remaining_weight_g=0,status='active',storage_location='Shelf' WHERE id=56"))
        c.execute(text("INSERT INTO system_config(key,value) VALUES('inventory.sealed.56','true')"))
    before = rows(inventory, 'SELECT assigned_spool_id,tray_observation FROM filament_slots')
    method = 'observe_slots' if sync.path == 'api' else 'observed_departures'
    original = getattr(TelemetryInventoryMoves, method)
    def fail_after_departure(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError('after departure effects')
    monkeypatch.setattr(TelemetryInventoryMoves, method, fail_after_departure)
    with pytest.raises(RuntimeError, match='after departure effects'):
        sync({**FIXTURES['tagless'], 'tray_type':''})
    assert rows(inventory, 'SELECT assigned_spool_id,tray_observation FROM filament_slots') == before
    assert rows(inventory, 'SELECT location_printer_id,location_slot,status,storage_location FROM spools') == [(1,1,'active','Shelf')]
    assert rows(inventory, "SELECT key FROM system_config WHERE key='inventory.sealed.56'") == [('inventory.sealed.56',)]
    for table in ('audit_logs', 'inventory_repairs'):
        assert rows(inventory, f"SELECT count(*) FROM {table} WHERE action LIKE 'telemetry.%'") == [(0,)]
