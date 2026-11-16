"""Native JSON and real two-connection regressions; run on the owned PostgreSQL harness."""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_classes_migration import pg_engine  # noqa: F401

pytestmark = pytest.mark.postgresql


@pytest.fixture
def pg_settlement(pg_engine):
    from core.schema import bootstrap_database
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.archives.routes import router
    from modules.inventory.routes.spools import router as spools
    with pg_engine.begin() as db:
        db.exec_driver_sql('DROP SCHEMA public CASCADE')
        db.exec_driver_sql('CREATE SCHEMA public')
    bootstrap_database(pg_engine)
    with pg_engine.begin() as db:
        db.execute(text("INSERT INTO users(id,username,role,is_active,password_hash) VALUES(991240,'settle-test','admin',TRUE,'fixture')"))
        db.execute(text("INSERT INTO printers(id,name) VALUES(991240,'Settlement test')"))
        db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(991240,'Fixture','PLA')"))
        db.execute(text("INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,spool_weight_g,status) VALUES(991240,991240,1000,1000,250,'active')"))
        db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id) VALUES(991240,1,991240)"))
        db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at,ended_at) VALUES(991240,991240,'failed','2026-10-11 12:00:00','2026-10-11 13:00:00')"))
        archive = db.execute(text("INSERT INTO print_archives(print_job_id,printer_id,print_name,ownership_state,accounting_state,accounting_reason,completed_at) VALUES(991240,991240,'Fixture','known','unresolved','no_bound_spool','2026-10-11 13:00:00') RETURNING id")).scalar_one()
    user = dict(id=991240, username='settle-test', role='admin', group_id=None, is_superadmin=True)
    app = FastAPI()
    app.include_router(router)
    app.include_router(spools)
    def session():
        with Session(pg_engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, pg_engine, archive, user


def test_postgresql_weigh_writes_json_and_preview_decodes_it(pg_settlement):
    client, engine, archive, _ = pg_settlement
    response = client.post('/spools/991240/weigh', json={'gross_weight_g': 1200})
    assert response.status_code == 200, response.text
    with engine.connect() as db:
        assert db.execute(text("SELECT json_typeof(value) FROM system_config WHERE key='filament_balance_weighed:991240'")).scalar_one() == 'string'
        assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=991240')).scalar_one() == 950
    result = client.get(f'/archives/{archive}/filament-settlement?spool_id=991240')
    assert result.status_code == 200, result.text
    assert result.json()['balance_set_after_print'][0]['how'] == 'weighed'


def test_postgresql_busy_stock_uses_real_nowait_and_rolls_back(pg_settlement):
    client, engine, archive, _ = pg_settlement
    with engine.connect() as holder, holder.begin():
        holder.execute(text('SELECT id FROM spools WHERE id=991240 FOR UPDATE'))
        response = client.post(f'/archives/{archive}/settle-filament', json={'entries': [{'spool_id': 991240, 'grams': 40}]})
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'busy'
    with engine.connect() as db:
        assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=991240')).scalar_one() == 1000
        assert db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == 0
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'quota_attempt:filament:%'")).scalar_one() == 0
        assert db.execute(text('SELECT accounting_state FROM print_archives WHERE id=:id'), {'id': archive}).scalar_one() == 'unresolved'


def test_postgresql_stale_tray_candidate_uses_independent_settlement_connection(pg_settlement, monkeypatch):
    from modules.inventory.routes.filament_slots import confirm_tray_identity, ConfirmTrayIdentity
    client, engine, archive, user = pg_settlement
    state_id = 'a' * 32
    with engine.begin() as db:
        db.execute(text('UPDATE filament_slots SET tray_observation=:state WHERE printer_id=991240'),
                   {'state': json.dumps(dict(id=state_id, pending=True, hold_id=state_id, observed={'type': 'PLA'}))})
        db.execute(text("UPDATE print_archives SET accounting_reason='spool_change_pending',consumption_json=:entries WHERE id=:id"),
                   {'id': archive, 'entries': json.dumps([dict(identity_state=state_id, slot_number=1, grams_used=40)])})
    with Session(engine) as tray:
        execute = tray.execute
        def after_candidates(statement, *args, **kwargs):
            if str(statement).startswith('SELECT a.id,a.print_job_id,a.job_id,a.status,a.consumption_json'):
                candidates = execute(statement, *args, **kwargs).all()
                # The HTTP request opens another real connection while tray holds its spool lock.
                response = client.post(f'/archives/{archive}/settle-filament', json={'no_deduction': 'nothing_extruded'})
                assert response.status_code == 200, response.text
                class Rows:
                    def all(self): return candidates
                return Rows()
            return execute(statement, *args, **kwargs)
        monkeypatch.setattr(tray, 'execute', after_candidates)
        assert confirm_tray_identity(991240, 1, ConfirmTrayIdentity(observed_state=state_id, decision='same'), current_user=user, db=tray)['success']
    with engine.connect() as db:
        assert db.execute(text('SELECT accounting_reason FROM print_archives WHERE id=:id'), {'id': archive}).scalar_one() == 'operator_settled_no_deduction'
        assert db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == 0
