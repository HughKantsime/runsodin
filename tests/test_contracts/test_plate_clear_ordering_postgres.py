"""Plate-clear endpoints work with the canonical job enum (no waiting state).

PostgreSQL cases use ODIN Dev's disposable native harness. SQLite cases exercise
the same HTTP fixtures locally; waiting-job ordering stays in test_plate_clear_ordering.
"""
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_post027_migration_033_postgres import pg_engine  # noqa: F401
from tests.test_contracts.test_plate_clear_state import read


@pytest.fixture(params=['sqlite', pytest.param('postgresql', marks=pytest.mark.postgresql)])
def plate_database(request, tmp_path):
    from core.schema import bootstrap_database

    if request.param == 'postgresql':
        try:
            engine = request.getfixturevalue('pg_engine')
        except pytest.skip.Exception as exc:
            pytest.fail(f"PostgreSQL harness is required for these regressions: {exc}")
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'plate.db'}")
    bootstrap_database(engine)
    if request.param == 'postgresql':
        with engine.connect() as connection:
            labels = connection.execute(text(
                "SELECT enumlabel FROM pg_enum JOIN pg_type ON pg_enum.enumtypid=pg_type.oid "
                "WHERE pg_type.typname='jobstatus'"
            )).scalars().all()
            assert 'completed' in labels and 'waiting_plate_clear' not in labels
    yield engine
    if request.param == 'sqlite':
        engine.dispose()


@pytest.fixture(params=[False, True], ids=['unlinked-print', 'linked-print'])
def plate_http(plate_database, request):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.printers import routes_controls, routes_crud, routes_integrations
    from modules.printers.models import Printer

    # Route commits stay inside an outer transaction, rolled back between cases.
    with plate_database.connect() as connection, connection.begin(), Session(
        bind=connection, join_transaction_mode='create_savepoint'
    ) as db:
        db.add(Printer(id=199, name='Finished printer', api_type='moonraker', gcode_state='FINISH'))
        db.flush()
        if request.param:
            db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status,actual_start,actual_end) "
                            "VALUES(801,'Finished part',199,'completed','2020-01-01 10:00:00','2020-01-01 11:00:00')"))
        db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at,ended_at,scheduled_job_id) "
                        "VALUES(901,199,'completed','2020-01-01 10:00:00','2020-01-01 11:00:00',:job)"),
                   {'job': 801 if request.param else None})
        db.commit()
        app = FastAPI()
        for router in (routes_controls.router, routes_crud.router, routes_integrations.router):
            app.include_router(router)
        app.dependency_overrides[get_db] = lambda: db
        app.dependency_overrides[get_current_user] = lambda: {
            'role': 'admin', 'group_id': None, 'username': 'Plate operator',
        }
        with TestClient(app) as client:
            yield client
        connection.rollback()


@pytest.mark.parametrize('surface', ['list', 'detail', 'feed'])
def test_finished_print_answers_with_no_waiting_enum_value(plate_http, surface):
    plate = read(plate_http, {}, surface)['plate_clear']
    ended = datetime.fromisoformat(plate.pop('last_print_ended_at'))
    assert ended == datetime(2020, 1, 1, 11, tzinfo=timezone.utc)
    assert plate == {
        'needed': True, 'has_waiting_job': False, 'releases_job': None,
    }


@pytest.mark.parametrize('body', [None, {'expected_release_job_id': None},
                                 {'expected_has_waiting_job': False}])
def test_confirmation_answers_with_no_waiting_enum_value(plate_http, body):
    response = plate_http.post('/printers/199/plate-cleared', **({} if body is None else {'json': body}))
    assert response.status_code == 200, response.text
    assert response.json()['success'] is True
    assert response.json()['message'] == 'Plate clear recorded'
    assert response.json()['plate_clear_confirmed_at']
    for surface in ['list', 'detail', 'feed']:
        printer = read(plate_http, {}, surface)
        assert printer['plate_clear']['needed'] is False
        assert printer['plate_clear_confirmed_at']
