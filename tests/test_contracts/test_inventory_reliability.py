"""Receiving, aggregate reorder policy, departure evidence and QR integration."""
import sqlite3
import pytest
from sqlalchemy import text
from core.base import SpoolStatus
from core.models import SystemConfig
from modules.inventory.models import Spool
from modules.inventory.routes.stock import ReceiveRolls, ReorderPolicy, receive_rolls, reorder_rollup, set_reorder_policy
from modules.inventory.stock import seal_key
from tests.test_contracts.test_spool_slot_prevention import graph, review_db, USER


def test_receive_storage_sealed_rollup_and_scan_assignment(graph, monkeypatch):
    from modules.inventory.routes.spool_ops import scan_assign_spool, generate_spool_label
    from modules.inventory.routes._helpers import ScanAssignRequest
    result = receive_rolls(ReceiveRolls(filament_id=199, quantity=2, storage_location='Shelf A'), USER, graph)
    ids = [row['id'] for row in result['spools']]
    assert len(ids) == len(set(ids)) == 2
    rows = graph.query(Spool).filter(Spool.id.in_(ids)).all()
    assert all(s.storage_location == 'Shelf A' and s.location_printer_id is None and s.location_slot is None and s.qr_code.startswith('SPL-') for s in rows)
    total = reorder_rollup(None, USER, graph)[0]
    assert total['sealed_rolls'] == 2 and total['sealed_g'] == 2000
    import qrcode
    encoded = []
    original = qrcode.QRCode.add_data
    def capture(qr, data, *args, **kwargs):
        encoded.append(data)
        return original(qr, data, *args, **kwargs)
    monkeypatch.setattr(qrcode.QRCode, 'add_data', capture)
    label = generate_spool_label(ids[0], 'small', USER, graph)
    assert label.media_type == 'image/png'
    assert encoded == [result['spools'][0]['qr_code']]
    assigned = scan_assign_spool(ScanAssignRequest(qr_code=result['spools'][0]['qr_code'], printer_id=10, slot=1), USER, graph)
    assert assigned.success
    assert graph.get(SystemConfig, seal_key(ids[0])) is None
    assert reorder_rollup(None, USER, graph)[0]['sealed_rolls'] == 1


def test_rollup_combines_brands_normalizes_color_and_excludes_inactive_foreign(graph):
    graph.execute(text("UPDATE spools SET remaining_weight_g=250,status='active' WHERE id=199"))
    graph.execute(text("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(200,'Other','Same color','pla','#123abc')"))
    for sid, org, status, grams in [(200,1,'active',100),(201,2,'active',9000),(202,1,'empty',8000),(203,1,'archived',7000)]:
        graph.execute(text('INSERT INTO spools(id,filament_id,org_id,status,remaining_weight_g) VALUES(:id,200,:org,:status,:grams)'),dict(id=sid,org=org,status=status,grams=grams))
    graph.commit()
    set_reorder_policy(ReorderPolicy(material='pla', color='#123abc', threshold_g=400), USER, graph)
    rows = reorder_rollup(None, USER, graph)
    assert len(rows) == 1
    assert rows[0]['total_g'] == rows[0]['open_g'] == 350
    assert rows[0]['reorder'] and rows[0]['shortfall_g'] == 50
    foreign = {**USER, 'group_id':2, 'is_superadmin':False, 'role':'operator'}
    assert reorder_rollup(None, foreign, graph)[0]['threshold_g'] == 1000
    graph.execute(text("UPDATE spools SET status='archived' WHERE org_id=1")); graph.commit()
    assert reorder_rollup(None, USER, graph)[0]['total_g'] == 0


@pytest.mark.parametrize('bad', [{'quantity':0}, {'quantity':101}, {'initial_weight_g':float('nan')}, {'storage_location':' '}, {'location_slot':1}])
def test_receiving_validates_storage_and_bounds(bad):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ReceiveRolls(**({'filament_id':199,'quantity':1,'storage_location':'Shelf'} | bad))


def test_receiving_failure_rolls_back_whole_batch(graph, monkeypatch):
    from modules.inventory.routes import stock
    original = stock._create_spool_record
    count = 0
    def fail(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError('synthetic failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(stock, '_create_spool_record', fail)
    before = graph.query(Spool).count()
    with pytest.raises(RuntimeError):
        receive_rolls(ReceiveRolls(filament_id=199,quantity=2,storage_location='Shelf'),USER,graph)
    assert graph.query(Spool).count() == before


@pytest.mark.parametrize('material,tag,grams,departed,status', [('',None,250,True,'active'), ('PLA','new-rfid',0,True,'empty'), ('PLA','new-rfid',250,True,'active'), ('PETG',None,250,False,'active'), ('PLA','move-rfid',250,False,'active')])
def test_raw_departure_evidence_is_audited_and_never_guesses(graph, material, tag, grams, departed, status):
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    graph.execute(text('UPDATE spools SET remaining_weight_g=:g,status=\'active\' WHERE id=199'), {'g':grams});graph.commit()
    conn = sqlite3.connect(graph.get_bind().url.database)
    try:
        sync_ams_inventory(conn,9,[{'id':'0','tray':[{'id':'0','tray_type':material,'tag_uid':tag,'tray_color':'FFFFFF'}]}])
        conn.commit()
        row = conn.execute('SELECT location_printer_id,status,storage_location FROM spools WHERE id=199').fetchone()
        assert row[0] == (None if departed else 9)
        assert row[1] == status
        assert conn.execute('SELECT COUNT(*) FROM inventory_repairs WHERE spool_id=199').fetchone()[0] == int(departed)
        assert conn.execute('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=9 AND slot_number=1').fetchone()[0] == (None if departed else 199)
    finally:
        conn.close()


def test_orm_departure_uses_move_protocol(graph):
    from core.interfaces.inventory_moves import inventory_moves
    from modules.inventory.slot_assignments import slot_integrity_report
    inventory_moves().observe_slots(graph,9,[(1,True,None)],set(),USER)
    graph.commit()
    assert graph.get(Spool,199).location_printer_id is None
    assert slot_integrity_report(graph) == {'duplicates': [], 'contradictions': []}
    assert graph.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='telemetry.relocate'")).scalar() == 1


def test_anycubic_empty_relocates_qr_spool_but_color_report_preserves_it(graph, monkeypatch):
    from modules.printers.monitors import anycubic_lan_monitor as monitor
    from modules.inventory.slot_assignments import slot_integrity_report
    graph.execute(text('UPDATE spools SET rfid_tag=NULL,remaining_weight_g=250 WHERE id=199'));graph.commit()
    monkeypatch.setattr(monitor,'engine',graph.get_bind())
    observed = [{'slot_number':1,'material':'PETG','color_hex':'FFFFFF'}]
    monkeypatch.setattr(monitor,'filament_slots',lambda boxes: observed)
    monitor.sync_slots(9, [{}])
    graph.expire_all()
    assert graph.get(Spool,199).location_printer_id == 9
    observed[0]['material'] = 'EMPTY'
    monitor.sync_slots(9, [{}])
    graph.expire_all()
    assert graph.get(Spool,199).location_printer_id is None
    assert graph.get(Spool,199).storage_location == 'Unlocated (removed from slot)'
    assert slot_integrity_report(graph) == {'duplicates': [], 'contradictions': []}


from tests.test_contracts.test_api_token_delegation_behavior import token_client

@pytest.fixture
def stock_client(token_client, review_db):
    from modules.inventory.routes.stock import router
    client, issue = token_client
    client.app.include_router(router)
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(199,'Test','Black','PLA','000000')"))
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"));review_db.commit()
    return client, issue


@pytest.mark.parametrize('path,method,body', [('/spools/receive','post',{'filament_id':199,'quantity':1,'storage_location':'Shelf'}),('/spools/reorder-policy','put',{'material':'PLA','color':'000000','threshold_g':400})])
def test_new_writes_enforce_scopes_roles_and_matrix(stock_client, review_db, path, method, body):
    import json
    client, issue = stock_client
    assert getattr(client, method)(path, headers=issue(['read:spools']), json=body).status_code == 403
    headers = issue(['write:spools'])
    assert getattr(client, method)(path, headers=headers, json=body).status_code == 200
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3")); review_db.commit()
    assert getattr(client, method)(path, headers=headers, json=body).status_code == 403
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"))
    review_db.add(SystemConfig(key='rbac_permissions', value={'action_access':{'spools.edit':['admin']}}))
    review_db.commit()
    assert getattr(client, method)(path, headers=headers, json=body).status_code == 403


def test_reorder_read_filters_foreign_tenant_even_requested_explicitly(stock_client, review_db):
    client, issue = stock_client
    review_db.execute(text("INSERT INTO spools(id,filament_id,status,org_id,remaining_weight_g) VALUES(199,199,'active',2,98765)")); review_db.commit()
    response = client.get('/spools/reorder?org_id=2', headers=issue(['read:spools']))
    assert response.status_code == 200 and response.json() == []


def test_moonraker_only_explicit_empty_gate_is_departure_evidence(monkeypatch):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    from modules.printers.monitors import moonraker_monitor
    slots = MoonrakerPrinter._parse_mmu_slots(object.__new__(MoonrakerPrinter), {
        "num_gates": 4, "gate_status": [-1, 0, 1, 2], "gate_material": ["PLA"] * 4})
    assert [s.gate_status for s in slots] == [-1, 0, 1, 2]
    seen = []

    class Moves:
        def observe_empty_slots(self, conn, printer_id, numbers):
            seen.append(list(numbers))
            raise RuntimeError("stop after departure selection")

    monkeypatch.setattr("core.interfaces.inventory_moves.inventory_moves", lambda: Moves())
    monitor = object.__new__(moonraker_monitor.MoonrakerMonitor)
    monitor.printer_id, monitor.name = 9, "fixture"
    monitor._sync_filament_slots(slots)
    # Unknown (-1) and buffered (2) gates keep their spool; only gate 2 (status 0) departs.
    assert seen == [[2]]
