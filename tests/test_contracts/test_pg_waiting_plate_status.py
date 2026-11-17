"""Plate-clear reads and transitions accept legacy states on both database dialects."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

STATUS_URL = '/api/integrations/home-assistant/status'
CLEAR_URL = '/api/printers/9000/plate-cleared'


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def plate_db(request):
    if request.param == 'sqlite':
        yield request.getfixturevalue('review_db')
    else:
        engine = request.getfixturevalue('pg_engine')
        with engine.connect() as connection, connection.begin():
            with Session(connection, join_transaction_mode='create_savepoint') as db:
                yield db
            # The route commits its session; discard this test's rows afterwards.
            connection.rollback()


@pytest.fixture
def plate_client(plate_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.printers import routes_controls, routes_integrations
    from modules.printers.models import Printer

    plate_db.add(Printer(id=9000, name='Synthetic printer', gcode_state='FINISH',
                         last_seen=datetime.now(timezone.utc), is_active=True))
    plate_db.commit()
    app = FastAPI()
    app.include_router(routes_integrations.router, prefix='/api')
    app.include_router(routes_controls.router, prefix='/api')

    def session():
        yield plate_db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {
        'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None,
    }
    with TestClient(app) as client:
        yield client


def _finished(db):
    from core.base import JobStatus
    from modules.jobs.models import Job

    now = datetime.now(timezone.utc)
    db.add(Job(id=9001, item_name='Synthetic finished print', printer_id=9000,
               status=JobStatus.COMPLETED, actual_start=now - timedelta(hours=2),
               actual_end=now - timedelta(hours=1)))
    db.commit()


def test_status_feed_with_printers(plate_client):
    response = plate_client.get(STATUS_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['schema_version'] == 2
    printer = next(p for p in body['printers'] if p['id'] == 9000)
    assert printer['status'] == 'idle'


@pytest.mark.parametrize('finished', [False, True])
def test_plate_cleared_with_and_without_a_finished_print(plate_client, plate_db, finished):
    if finished:
        _finished(plate_db)
    response = plate_client.post(CLEAR_URL)
    assert response.status_code == 200, response.text
    body = response.json()
    if finished:
        assert body['success'] is True and body['message'] == 'Plate clear recorded'
        assert datetime.fromisoformat(body['plate_clear_confirmed_at'])
        assert plate_db.execute(text("SELECT COUNT(*) FROM job_timeline_events "
                                     "WHERE job_id=9001 AND kind='bed_clear_confirmed'")).scalar_one() == 1
    else:
        assert body == {'success': True, 'message': 'No jobs waiting for plate clear'}


def test_plate_transition_ignores_a_job_in_a_valid_enum_state(plate_client, plate_db):
    from modules.jobs.services import transition_job

    _finished(plate_db)
    assert transition_job(plate_db, 9001, 'pending', 'plate') is False
    assert plate_db.execute(text('SELECT status FROM jobs WHERE id=9001')).scalar_one() == 'completed'


@pytest.mark.parametrize('plate_db', ['sqlite'], indirect=True)
def test_sqlite_plate_queries_keep_the_postgresql_safe_cast(plate_client, plate_db):
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        if 'waiting_plate_clear' in statement:
            statements.append(' '.join(statement.split()))

    engine = plate_db.get_bind()
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert plate_client.get(STATUS_URL).status_code == 200
        assert plate_client.post(CLEAR_URL).status_code == 200
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert len(statements) == 2
    assert "CAST(j.status AS TEXT) = 'waiting_plate_clear'" in statements[0]
    assert "CAST(status AS TEXT) = 'waiting_plate_clear'" in statements[1]


@pytest.mark.parametrize('plate_db', ['sqlite'], indirect=True)
def test_sqlite_legacy_waiting_job_still_transitions(plate_client, plate_db):
    from modules.jobs.services import transition_job

    plate_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status) "
                          "VALUES(9001,'Synthetic waiting print',9000,'waiting_plate_clear')"))
    statements = []

    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)

    engine = plate_db.get_bind()
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        assert transition_job(plate_db, 9001, 'pending', 'plate') is True
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert any('CAST(status AS TEXT) IN' in statement for statement in statements)
    assert plate_db.execute(text('SELECT status FROM jobs WHERE id=9001')).scalar_one() == 'pending'
