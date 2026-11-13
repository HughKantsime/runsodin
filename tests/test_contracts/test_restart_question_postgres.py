"""Opt-in PostgreSQL checks using the existing disposable-server harness.

ODIN_RESTART_POSTGRES_TESTS=1 enables these tests. Keep disabled for local-only
sessions that prohibit network access; no application or production DB is used.
"""
import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from tests.test_contracts.test_post027_migration_033_postgres import pg_engine

pytestmark = pytest.mark.postgresql


@pytest.fixture(scope='module')
def restart_pg(request):
    if os.getenv('ODIN_RESTART_POSTGRES_TESTS') != '1':
        pytest.skip('Disposable PostgreSQL requires explicit opt-in; disabled for no-network work')
    from core.schema import bootstrap_database
    engine = request.getfixturevalue('pg_engine')
    bootstrap_database(engine)
    return engine


@pytest.mark.parametrize('table', ['printers', 'print_jobs', 'system_config'])
def test_restart_lock_refuses_concurrent_dml_and_recovers(restart_pg, table):
    from core.restart_questions import locked, load, save
    with restart_pg.connect() as writer, Session(restart_pg) as answer:
        # Even an unrelated writer holds ROW EXCLUSIVE until its transaction ends.
        field = 'key' if table == 'system_config' else 'id'
        writer.execute(text(f'UPDATE {table} SET {field}={field} WHERE FALSE'))
        with pytest.raises(DBAPIError) as failure:
            with locked(answer) as db:
                save(db, 'restart_question:contention-test', {'state': 'pending'})
        assert failure.value.orig.sqlstate == '55P03'
        answer.rollback()
        writer.rollback()
        assert load(answer, 'restart_question:contention-test') is None
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
