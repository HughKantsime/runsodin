"""Regression coverage for physical spool moves and read-only graph diagnosis."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
import sqlite3

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.requests import Request
from tests.test_contracts.test_education_review_workflow import review_db
from modules.inventory.slot_assignments import assign_slot, slot_integrity_report
from modules.inventory.models import Spool
from modules.printers.models import FilamentSlot

USER = {'id': 3, 'user_id': 3, 'role': 'admin', 'group_id': 1, 'is_superadmin': True}


@pytest.fixture
def graph(review_db):
    review_db.execute(text("DROP INDEX IF EXISTS uq_filament_slots_spool"))
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(199,'Synthetic','PLA','PLA','123ABC')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,qr_code,org_id,initial_weight_g,rfid_tag,location_printer_id,location_slot) VALUES(199,199,'move-test',1,1000,'move-rfid',9,1)"))
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE printer_id=9 AND slot_number=1'))
    review_db.execute(text('UPDATE printers SET slot_count=4 WHERE id IN (9,10)'))
    review_db.commit()
    return review_db


def assert_moved(db, printer_id=10, number=1):
    db.flush()
    db.expire_all()
    rows = db.query(FilamentSlot).filter(FilamentSlot.assigned_spool_id == 199).all()
    assert [(row.printer_id, row.slot_number) for row in rows] == [(printer_id, number)]
    spool = db.get(Spool, 199)
    assert (spool.location_printer_id, spool.location_slot) == (printer_id, number)
    assert slot_integrity_report(db) == {'duplicates': [], 'contradictions': []}


@pytest.mark.parametrize('writer', ['agent', 'legacy', 'load', 'scan', 'update', 'configure'])
def test_move_on_each_assignment_writer(graph, monkeypatch, writer):
    db = graph
    if writer == 'agent':
        from modules.printers.routes_filament_slots import assign_spool_to_slot, AssignSpoolRequest
        assign_spool_to_slot(AssignSpoolRequest(spool_id=199, printer_id=10), Request({'type':'http','state':{}}), USER, USER, db)
    elif writer == 'legacy':
        from modules.inventory.routes.filament_slots import assign_spool_to_slot
        assign_spool_to_slot(10, 1, 199, USER, db)
    elif writer == 'load':
        from modules.inventory.routes.spools import load_spool
        from modules.inventory.routes._helpers import SpoolLoadRequest
        load_spool(199, SpoolLoadRequest(printer_id=10, slot_number=1), USER, db)
    elif writer == 'scan':
        from modules.inventory.routes.spool_ops import scan_assign_spool
        from modules.inventory.routes._helpers import ScanAssignRequest
        scan_assign_spool(ScanAssignRequest(qr_code='move-test', printer_id=10, slot=1), USER, db)
    elif writer == 'update':
        from modules.printers.routes_crud import update_filament_slot
        from modules.printers.schemas import FilamentSlotUpdate
        update_filament_slot(10, 1, FilamentSlotUpdate(assigned_spool_id=199), USER, db)
    else:
        import asyncio
        from modules.printers.routes_ams_env import configure_ams_slot, AMSSlotConfigRequest
        from modules.printers import route_utils
        monkeypatch.setattr(route_utils, '_bambu_command_direct', lambda *args: True)
        asyncio.run(configure_ams_slot(10, 0, 1, AMSSlotConfigRequest(material='PLA', color='#123ABC', spool_id=199), USER, db))
    assert_moved(db)


@pytest.mark.parametrize('writer', ['unload', 'empty', 'unassign'])
def test_unassign_clears_location(graph, writer):
    db = graph
    if writer == 'unload':
        from modules.inventory.routes.spools import unload_spool
        unload_spool(199, None, USER, db)
    else:
        from modules.printers.routes_crud import update_filament_slot
        from modules.printers.schemas import FilamentSlotUpdate
        update_filament_slot(9, 1, FilamentSlotUpdate(**({'filament_type':'empty'} if writer == 'empty' else {'assigned_spool_id':None})), USER, db)
    db.expire_all()
    assert db.query(FilamentSlot).filter(FilamentSlot.assigned_spool_id == 199).count() == 0
    assert (db.get(Spool,199).location_printer_id, db.get(Spool,199).location_slot) == (None,None)


def test_displaced_spool_loses_location(graph):
    db = graph
    db.execute(text('INSERT INTO spools(id,filament_id,org_id,location_printer_id,location_slot) VALUES(200,199,1,10,1)'))
    db.execute(text('UPDATE filament_slots SET assigned_spool_id=200 WHERE printer_id=10'));db.commit()
    assign_slot(db, db.query(FilamentSlot).filter_by(printer_id=10).one(), 199, USER);db.commit()
    assert_moved(db)
    assert (db.get(Spool,200).location_printer_id, db.get(Spool,200).location_slot) == (None,None)


def test_integrity_reports_bad_graph_without_writes(graph):
    db = graph
    db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE printer_id=10'));db.commit()
    before = list(db.execute(text('SELECT id,assigned_spool_id FROM filament_slots ORDER BY id')))
    report = slot_integrity_report(db)
    assert report['duplicates'][0]['spool_id'] == 199
    assert len(report['duplicates'][0]['filament_slot_ids']) == 2
    assert report['contradictions'][0]['direction'] == 'slot'
    db.execute(text('UPDATE spools SET location_slot=4 WHERE id=199'));db.commit()
    assert any(row['direction'] == 'location' for row in slot_integrity_report(db)['contradictions'])
    assert list(db.execute(text('SELECT id,assigned_spool_id FROM filament_slots ORDER BY id'))) == before
    assert not db.dirty


def test_concurrent_same_spool_assignments_have_one_owner(graph):
    engine = graph.get_bind()
    barrier = Barrier(2)
    def move(printer_id):
        with Session(engine) as db:
            # Both sessions read the old association before either obtains the lock.
            slot = db.query(FilamentSlot).filter_by(printer_id=printer_id).one()
            db.get(Spool,199)
            barrier.wait(timeout=10)
            assign_slot(db, slot, 199, USER)
            db.commit()
    graph.rollback()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(move, [9,10]))
    graph.expire_all()
    rows = graph.query(FilamentSlot).filter_by(assigned_spool_id=199).all()
    assert len(rows) == 1
    assert_moved(graph, rows[0].printer_id)


def test_daemon_sync_accepts_repaired_graph(graph):
    from modules.printers.monitors.ams_inventory import sync_ams_inventory
    db = graph
    assign_slot(db, db.query(FilamentSlot).filter_by(printer_id=10).one(), 199, USER);db.commit()
    conn = sqlite3.connect(db.get_bind().url.database)
    try:
        assert sync_ams_inventory(conn, 10, [{'id':'0','tray':[{'id':'0','tray_type':'PLA','tag_uid':'move-rfid','remain':50}]}]) == 1
        conn.commit()
    finally:
        conn.close()
    assert_moved(db)


def test_troubleshooting_exposes_integrity_report(graph, monkeypatch):
    from modules.system import troubleshooting as module
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='api RUNNING',returncode=0))
    graph.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE printer_id=10'));graph.commit()
    result = module.server_checks(graph)
    check = next(row for row in result['checks'] if row['stage'] == 'spool_slot_integrity')
    assert check['status'] == 'fail' and check['details']['duplicates'][0]['spool_id'] == 199


from tests.test_contracts.test_ams_sync_acceptance import sync_client, own_ams_operator, ams_client, token_client


def test_http_ams_sync_moves_existing_rfid(sync_client, review_db):
    client, headers, _ = sync_client
    review_db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE printer_id=9'))
    review_db.execute(text('UPDATE spools SET location_printer_id=9,location_slot=1 WHERE id=199'));review_db.commit()
    response = client.post('/printers/199/sync-ams', headers=headers)
    assert response.status_code == 200
    assert_moved(review_db,199,1)


def test_http_ams_new_rfid_clears_displaced_location(sync_client, review_db, monkeypatch):
    from modules.printers.telemetry.bambu import session
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    client, headers, _ = sync_client
    review_db.execute(text('UPDATE filament_slots SET slot_number=1,assigned_spool_id=199 WHERE id=199'))
    review_db.execute(text('UPDATE spools SET location_printer_id=199,location_slot=1 WHERE id=199'));review_db.commit()
    section=BambuPrintSection.model_validate({'ams':{'ams':[{'id':'0','tray':[{'id':'0','tray_type':'PLA','tag_uid':'synthetic-new-move-tag'}]}]}})
    monkeypatch.setattr(session,'read_status_once',lambda *a,**k:SimpleNamespace(success=True,section=section))
    assert client.post('/printers/199/sync-ams',headers=headers).status_code == 200
    review_db.expire_all()
    spool = review_db.get(Spool,199)
    assert (spool.location_printer_id, spool.location_slot) == (None,None)
    assert slot_integrity_report(review_db) == {'duplicates': [], 'contradictions': []}


@pytest.mark.parametrize('writer', ['load', 'scan'])
def test_load_and_scan_create_missing_slot_with_matching_location(graph, writer):
    db = graph
    db.execute(text('DELETE FROM filament_slots WHERE printer_id=10'));db.commit()
    if writer == 'load':
        from modules.inventory.routes.spools import load_spool
        from modules.inventory.routes._helpers import SpoolLoadRequest
        load_spool(199, SpoolLoadRequest(printer_id=10, slot_number=1), USER, db)
    else:
        from modules.inventory.routes.spool_ops import scan_assign_spool
        from modules.inventory.routes._helpers import ScanAssignRequest
        scan_assign_spool(ScanAssignRequest(qr_code='move-test', printer_id=10, slot=1), USER, db)
    assert_moved(db)


def test_move_clears_all_historical_duplicates_and_rolls_back_atomically(graph):
    db = graph
    db.execute(text('UPDATE filament_slots SET assigned_spool_id=199 WHERE printer_id=10'));db.commit()
    destination = db.query(FilamentSlot).filter_by(printer_id=10).one()
    assign_slot(db, destination, 199, USER)
    assert_moved(db)
    db.rollback()
    assert len(slot_integrity_report(db)['duplicates']) == 1
    assign_slot(db, destination, 199, USER);db.commit()
    assert_moved(db)


def test_postgres_uses_ams_order_and_exact_lock_set(monkeypatch):
    """Compile actual WHERE/ORDER/lock statements against a PostgreSQL dialect."""
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql
    from contextlib import nullcontext
    from modules.inventory import slot_assignments as module
    statements = []
    # printer 99 / slot 99 / spool 99 are intentionally unrelated.
    printers = (9, 10)
    slots = ((1, 199, 9, 1), (2, 200, 10, 1))
    spools = ((199, 9, 1), (200, 10, 1), (201, 10, 1))
    monkeypatch.setattr(module, '_involved_graph', lambda *args: (printers, slots, spools))
    class Query:
        def __init__(self, column): self.statement = select(column)
        def filter(self, predicate):
            self.statement = self.statement.where(predicate); return self
        def order_by(self, column):
            self.statement = self.statement.order_by(column); return self
        def with_for_update(self, **kwargs):
            self.statement = self.statement.with_for_update(**kwargs); return self
        def all(self):
            statements.append(str(self.statement.compile(dialect=postgresql.dialect(), compile_kwargs={'literal_binds': True}))); return []
    db = SimpleNamespace(no_autoflush=nullcontext(),
                         connection=lambda:SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                         query=lambda column: Query(column))
    module.lock_slot_graph(db, 2, 199)
    assert len(statements) == 3
    for sql, table, ids in zip(statements, ['printers', 'filament_slots', 'spools'], ['9, 10', '1, 2', '199, 200, 201']):
        assert f'WHERE {table}.id IN ({ids}) ORDER BY {table}.id FOR UPDATE NOWAIT' in sql
        assert 'IN (99,' not in sql


def test_involved_graph_includes_displaced_previous_and_all_links(graph):
    from modules.inventory.slot_assignments import _involved_graph
    graph.execute(text('INSERT INTO spools(id,filament_id,org_id,location_printer_id,location_slot) VALUES(200,199,1,10,1),(201,199,1,NULL,NULL)'))
    graph.execute(text('UPDATE filament_slots SET assigned_spool_id=201 WHERE printer_id=10'))
    graph.commit()
    target = graph.query(FilamentSlot).filter_by(printer_id=10).one()
    printers, slots, spools = _involved_graph(graph, target.id, 199)
    assert printers == (9, 10)
    assert {s[0] for s in spools} == {199, 200, 201}
    assert {s[2] for s in slots} == {9, 10}


def test_postgres_contention_returns_retryable_conflict():
    from contextlib import nullcontext
    from sqlalchemy.exc import OperationalError
    from fastapi import HTTPException
    from modules.inventory.slot_assignments import lock_slot_graph
    class Busy(Exception): sqlstate = '55P03'
    def busy(*_): raise OperationalError('locked', {}, Busy())
    rolled_back = []
    db = SimpleNamespace(no_autoflush=nullcontext(),
                         connection=lambda:SimpleNamespace(dialect=SimpleNamespace(name='postgresql')),
                         query=busy, rollback=lambda:rolled_back.append(True))
    with pytest.raises(HTTPException) as error:
        lock_slot_graph(db, 1, 199)
    assert error.value.status_code == 409 and rolled_back == [True]


def test_sqlite_stale_explicit_snapshot_refuses_without_duplicate(graph):
    from fastapi import HTTPException
    engine = graph.get_bind()
    graph.rollback()
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
    with Session(engine) as stale, Session(engine) as fresh:
        stale.execute(text('BEGIN'))
        destination = stale.query(FilamentSlot).filter_by(printer_id=9).one()
        assign_slot(fresh, fresh.query(FilamentSlot).filter_by(printer_id=10).one(), 199, USER)
        fresh.commit()
        with pytest.raises(HTTPException) as error:
            assign_slot(stale, destination, 199, USER)
        assert error.value.status_code == 409
    assert_moved(graph)
