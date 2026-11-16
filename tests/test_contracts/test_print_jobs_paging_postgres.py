"""Native timestamp/date-filter contract on the owned Cert-E PostgreSQL fixture."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401


@pytest.mark.postgresql
def test_print_history_dates_order_and_count_ignore_session_timezone(pg_engine):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.jobs.routes import presets
    with pg_engine.begin() as connection:
        connection.execute(text("INSERT INTO printers(id,name,org_id) VALUES(901,'History printer',1)"))
        connection.execute(text(
            "INSERT INTO print_jobs(id,printer_id,status,started_at,ended_at) VALUES "
            "(901,901,'completed','2026-11-01 00:00:00','2026-11-01 01:00:00'),"
            "(902,901,'completed','2026-11-01 01:00:00','2026-11-01 02:00:00'),"
            "(903,901,'completed','2026-11-01 01:00:00','2026-11-01 02:00:00'),"
            "(904,901,'completed','2026-11-01 03:00:00','2026-11-01 04:00:00'),"
            "(905,901,'completed','2026-11-02 00:00:00','2026-11-02 01:00:00')"))
    app = FastAPI()
    app.include_router(presets.router)
    app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'viewer', 'group_id': 1}

    def session():
        with Session(pg_engine) as db:
            db.execute(text("SELECT set_config('TimeZone', :zone, true)"), {'zone': zone})
            yield db

    app.dependency_overrides[get_db] = session
    params = {'started_after': '2026-11-01', 'started_before': '2026-11-02',
              'ended_after': '2026-11-01T03:00:00+02:00', 'ended_before': '2026-11-01T04:00:00Z',
              'printer_id': 901, 'limit': 2}
    with TestClient(app) as client:
        for zone in ('UTC', 'America/New_York', 'Asia/Tokyo'):
            for offset, expected in ((0, [903, 902]), (2, [901]), (4, [])):
                response = client.get('/print-jobs', params={**params, 'offset': offset})
                assert response.status_code == 200, response.text
                assert [row['id'] for row in response.json()] == expected
                assert response.headers['X-Total-Count'] == '3'
