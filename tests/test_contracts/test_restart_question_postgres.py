"""PostgreSQL checks using the existing disposable-server harness (pg_engine).

Skipped only where Docker is unavailable, like every other PostgreSQL contract test; the release
gate refuses skips. No application or production DB is used.
Product lifecycle cases also run against disposable SQLite.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_contracts.test_post027_migration_033_postgres import pg_engine

pytestmark = pytest.mark.postgresql


@pytest.fixture(scope='module')
def restart_pg(request):
    from core.schema import bootstrap_database
    engine = request.getfixturevalue('pg_engine')
    bootstrap_database(engine)
    return engine


@pytest.mark.parametrize('table', ['printers', 'print_jobs', 'system_config'])
def test_restart_lock_refuses_concurrent_dml_and_recovers(restart_pg, table):
    from core.restart_questions import begin_transaction, locked, load, save
    with restart_pg.connect() as writer, Session(restart_pg) as answer:
        # Even an unrelated writer holds ROW EXCLUSIVE until its transaction ends.
        field = 'key' if table == 'system_config' else 'id'
        writer.execute(text(f'UPDATE {table} SET {field}={field} WHERE FALSE'))
        with pytest.raises(DBAPIError) as failure:
            begin_transaction(answer)
            with locked(answer) as db:
                save(db, 'restart_question:contention-test', {'state': 'pending'})
        assert failure.value.orig.sqlstate == '55P03'
        answer.rollback()
        writer.rollback()
        assert load(answer, 'restart_question:contention-test') is None
        answer.rollback()
        begin_transaction(answer)
        with locked(answer) as db:
            save(db, 'restart_question:contention-test', {'state': 'pending'})
        answer.rollback()
        assert load(answer, 'restart_question:contention-test') is None


def test_monitor_api_timestamp_and_json_roundtrip(restart_pg):
    from core.db_utils import _ConnectionAdapter
    from core.dispatch_observation_executor import TransactionView
    from core.restart_questions import load, read_row, save
    with restart_pg.begin() as db:
        db.execute(text("INSERT INTO printers(id,name,api_type) VALUES(901,'Synthetic','bambu')"))
        db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(902,901,'running',:stamp)"),
                   {'stamp': '2026-11-13T10:11:12.123456+00:00'})
    raw = restart_pg.raw_connection()
    try:
        monitor = TransactionView(_ConnectionAdapter(raw, True))
        monitor.execute(text("SET TIME ZONE 'UTC'"))
        stamp = str(read_row(monitor, 902)['started_at'])
        document = {'state': 'pending', 'row_started_at': stamp, 'org_id': None, 'alert_ids': [1, 2]}
        save(monitor, 'restart_question:901', document)
        raw.commit()
        with Session(restart_pg) as api:
            api.execute(text("SET TIME ZONE 'UTC'"))
            assert str(read_row(api, 902)['started_at']) == stamp
            assert load(api, 'restart_question:901') == document
            save(api, 'restart_question:901', dict(document, state='same'))
            api.commit()
        assert load(monitor, 'restart_question:901') == dict(document, state='same')
        save(monitor, 'restart_question:901', dict(document, state='new'))
        raw.rollback()
        with Session(restart_pg) as api:
            assert load(api, 'restart_question:901')['state'] == 'same'
    finally:
        raw.close()


def seed_product(engine, monkeypatch):
    """Real daemon connections and product entry points; no printer transport."""
    from core import db_utils
    from core.db_live import live_get_db
    from core.monitor_observations import persist
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    from modules.printers.monitors.restart_question import initialize

    monkeypatch.setattr(db_utils, 'engine', engine)
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    suffix = uuid4().hex
    with engine.begin() as db:
        org_id = db.execute(text('INSERT INTO groups(name,is_org) VALUES(:name,TRUE) RETURNING id'),
                            {'name': suffix}).scalar_one()
        user_id = db.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id) "
                                  "VALUES(:name,'fixture','operator',TRUE,:org) RETURNING id"),
                             {'name': suffix, 'org': org_id}).scalar_one()
        printer_id = db.execute(text("INSERT INTO printers(name,api_type,org_id) "
                                     "VALUES(:name,'bambu',:org) RETURNING id"),
                                {'name': f'Synthetic {suffix}', 'org': org_id}).scalar_one()
        job_id = db.execute(text("INSERT INTO jobs(item_name,printer_id,charged_to_org_id,status,hold) "
                                 "VALUES('Retained start',:printer,:org,'scheduled',TRUE) RETURNING id"),
                            {'printer': printer_id, 'org': org_id}).scalar_one()
        db.execute(text('INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,'
                        'payload_sha256,token,owner_org_id,state) '
                        "VALUES(:printer,:job,:physical,:config,:hash,:token,:org,'uncertain')"),
                   {'printer': printer_id, 'job': job_id, 'physical': suffix * 2, 'config': 'b' * 64,
                    'hash': 'c' * 64, 'token': suffix, 'org': org_id})
        row_id = db.execute(text('INSERT INTO print_jobs(printer_id,job_name,filename,status,started_at,'
                                'progress_percent,current_layer,remaining_minutes) '
                                "VALUES(:printer,'bracket','plate.gcode','running',:start,13,6,615) RETURNING id"),
                           {'printer': printer_id, 'start': datetime.now(timezone.utc) - timedelta(hours=2)}).scalar_one()
        persist(db, printer_id, 'RUNNING')
    monitor = SimpleNamespace(printer_id=printer_id, name='Synthetic', _current_job_id=None,
                              _monitor_started_at=datetime.now(timezone.utc),
                              _state={'gcode_state': 'RUNNING', 'subtask_name': 'bracket'})
    runtime = SimpleNamespace(get_db=live_get_db, time=time, log=logging.getLogger(__name__))
    initialize(monitor, runtime)
    assert monitor._restart_check_needed

    def question():
        from core.restart_questions import load
        with engine.connect() as db:
            return load(db, f'restart_question:{printer_id}')

    def rows():
        with engine.connect() as db:
            return [dict(row) for row in db.execute(text('SELECT * FROM print_jobs WHERE printer_id=:id ORDER BY id'),
                                                   {'id': printer_id}).mappings()]

    return SimpleNamespace(engine=engine, monitor=monitor, runtime=runtime, question=question, rows=rows,
                           printer_id=printer_id, row_id=row_id,
                           actor={'id': user_id, 'role': 'operator', 'group_id': org_id})


@pytest.fixture
def pg_product(restart_pg, monkeypatch):
    return seed_product(restart_pg, monkeypatch)


@pytest.fixture(params=['sqlite', 'postgresql'])
def product(request, tmp_path, monkeypatch):
    # Run the same lifecycle assertions locally on SQLite. PostgreSQL still uses
    # the disposable-server fixture above.
    if request.param == 'postgresql':
        return seed_product(request.getfixturevalue('restart_pg'), monkeypatch)
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'restart.db'}")

    @event.listens_for(engine, 'connect')
    def foreign_keys(connection, _record):
        connection.execute('PRAGMA foreign_keys=ON')

    request.addfinalizer(engine.dispose)
    bootstrap_database(engine)
    return seed_product(engine, monkeypatch)


def raise_question(case):
    from modules.printers.monitors.restart_question import handle
    assert handle(case.monitor, case.runtime) is True
    question = case.question()
    assert question['state'] == 'pending'
    assert question['print_job_id'] == case.row_id
    assert len(question['alert_ids']) == 1
    return question


def test_monitor_raises_one_question_without_changing_print(product):
    from modules.printers.monitors.restart_question import handle, initialize
    before = product.rows()
    question = raise_question(product)
    assert handle(product.monitor, product.runtime) is True
    initialize(product.monitor, product.runtime)
    assert handle(product.monitor, product.runtime) is True
    assert product.question() == question
    assert product.rows() == before
    with product.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM alerts WHERE printer_id=:id'),
                          {'id': product.printer_id}).scalar_one() == 1


@pytest.mark.parametrize('decision', ['same', 'new'])
def test_product_answer_and_finished_cleanup(product, decision):
    from core.physical_print import CLOSED_UNKNOWN
    from core.restart_questions import answer, load
    from modules.printers.monitors.restart_question import clear_finished
    original = raise_question(product)
    before = product.rows()[0]
    with Session(product.engine) as db:
        assert not db.in_transaction()
        result = answer(db, product.printer_id, product.actor, original['id'], decision)
    rows = product.rows()
    active_id = result['print_job_id']
    assert result['outcome'] == decision
    if decision == 'same':
        assert rows == [before]
        assert active_id == product.row_id
    else:
        assert len(rows) == 2
        assert rows[0] == dict(before, status='cancelled', ended_at=None, error_code=CLOSED_UNKNOWN)
        assert active_id == rows[1]['id']
        assert rows[1]['status'] == 'running' and rows[1]['scheduled_job_id'] is None
        assert 'first-seen-mid-print' in json.loads(rows[1]['spool_snapshot'])['source']
    question = product.question()
    assert question['state'] == decision and question['active_print_job_id'] == active_id
    with product.engine.connect() as db:
        details = db.execute(text("SELECT details FROM audit_logs WHERE action='restart_question_answered' "
                                  'AND entity_id=:id'), {'id': product.printer_id}).scalar_one()
        details = json.loads(details) if isinstance(details, str) else details
        assert details == {'question_id': original['id'], 'print_job_id': product.row_id,
                           'active_print_job_id': active_id, 'answer': decision,
                           'actor_id': product.actor['id'], 'answered_at': question['answered_at']}
        assert db.execute(text('SELECT count(*) FROM alerts WHERE printer_id=:id AND is_dismissed IS FALSE'),
                          {'id': product.printer_id}).scalar_one() == 0
        assert db.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=:id'),
                          {'id': product.printer_id}).scalar_one() == 'uncertain'
        if decision == 'new':
            assert load(db, f'restart_print:{active_id}')['first_seen_mid_print'] is True
    clear_finished(product.monitor, product.runtime)
    assert product.question() == question  # a running record is not consumed
    with product.engine.begin() as db:
        db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=:id"), {'id': active_id})
    clear_finished(product.monitor, product.runtime)
    assert product.question() is None
    assert product.monitor._restart_check_needed is False


@pytest.mark.parametrize('decision', ['same', 'new'])
@pytest.mark.parametrize('table', ['printers', 'print_jobs', 'system_config'])
def test_busy_product_answer_refuses_then_recovers(pg_product, decision, table):
    from fastapi import HTTPException
    from core.restart_questions import answer
    case = pg_product
    question = raise_question(case)
    before = case.rows()
    with case.engine.connect() as writer, Session(case.engine) as db:
        field = 'key' if table == 'system_config' else 'id'
        writer.execute(text(f'UPDATE {table} SET {field}={field} WHERE FALSE'))
        with pytest.raises(HTTPException) as failure:
            answer(db, case.printer_id, case.actor, question['id'], decision)
        assert failure.value.status_code == 409
        assert failure.value.detail == 'The print question could not be updated. Refresh and try again.'
        assert not db.in_transaction()
        assert case.rows() == before and case.question() == question
        with case.engine.connect() as check:
            assert check.execute(text("SELECT count(*) FROM audit_logs WHERE action='restart_question_answered' "
                                      'AND entity_id=:id'), {'id': case.printer_id}).scalar_one() == 0
            assert check.execute(text('SELECT is_dismissed FROM alerts WHERE id=:id'),
                                 {'id': question['alert_ids'][0]}).scalar_one() is False
        writer.rollback()
        answer(db, case.printer_id, case.actor, question['id'], decision)
    assert len(case.rows()) == (1 if decision == 'same' else 2)
    assert case.question()['state'] == decision


@pytest.mark.parametrize('table', ['printers', 'print_jobs', 'system_config'])
def test_busy_monitor_withholds_writes_then_recovers(pg_product, table):
    from modules.printers.monitors.restart_question import handle
    case = pg_product
    before = case.rows()
    with case.engine.connect() as writer:
        field = 'key' if table == 'system_config' else 'id'
        writer.execute(text(f'UPDATE {table} SET {field}={field} WHERE FALSE'))
        assert handle(case.monitor, case.runtime) is True
        assert case.monitor._restart_retry_at > time.time()
        assert case.question() is None and case.rows() == before
        writer.rollback()
    case.monitor._restart_retry_at = 0
    raise_question(case)
    assert case.rows() == before
