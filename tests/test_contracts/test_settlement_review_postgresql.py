"""Native JSON and real two-connection regressions; run on the owned PostgreSQL harness."""
import json
from contextlib import contextmanager
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_classes_migration import pg_engine  # noqa: F401

pytestmark = pytest.mark.postgresql


def test_postgresql_061_exact_shape_claim_and_immutability(pg_settlement):
    import importlib
    from sqlalchemy.exc import DBAPIError
    migration = importlib.import_module('core.schema.migrations.061_print_filament_evidence')
    engine = pg_settlement[1]
    with engine.begin() as db:
        migration.validate(db)
        db.execute(text("INSERT INTO print_filament_evidence(print_job_id,kind,claim,source) VALUES(991240,'closed',1,'settlement')"))
        assert db.execute(text("INSERT INTO print_filament_evidence(print_job_id,kind,claim,source) VALUES(991240,'closed',1,'archive') ON CONFLICT DO NOTHING")).rowcount == 0
    for sql in ('UPDATE print_filament_evidence SET grams=4', 'DELETE FROM print_filament_evidence', 'TRUNCATE print_filament_evidence'):
        with engine.begin() as db, pytest.raises(DBAPIError, match='immutable print filament evidence'):
            db.execute(text(sql))


def test_two_deducers_one_print_one_claim_postgresql(pg_settlement, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from core import filament_ledger
    from core.print_attempts import sqlalchemy_runner
    engine = pg_settlement[1]
    with engine.begin() as db:
        db.execute(text('INSERT INTO spools(id,filament_id,remaining_weight_g) VALUES(991241,991240,1000)'))
    entered, release, second_started = Event(), Event(), Event()
    original = filament_ledger._append_evidence
    def hold(run, key, **kwargs):
        if kwargs.get('source') == 'archive':
            result = original(run, key, **kwargs)
            entered.set()
            assert release.wait(30)
            return result
        second_started.set()
        return original(run, key, **kwargs)
    monkeypatch.setattr(filament_ledger, '_append_evidence', hold)
    def deduct(source, spool):
        with engine.begin() as db:
            db.execute(text("SET LOCAL lock_timeout='30s'"))
            return filament_ledger.record_print_usage(sqlalchemy_runner(db), 991240,
                source=source, grams_by_spool={spool: 40})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(deduct, 'archive', 991240)
        try:
            assert entered.wait(30)
            second = pool.submit(deduct, 'moonraker', 991241)
            assert second_started.wait(30)
        finally:
            release.set()
        assert first.result(timeout=30).deducted
        assert second.result(timeout=30).state == 'already_deducted'
    with engine.connect() as db:
        assert db.execute(text('SELECT remaining_weight_g FROM spools ORDER BY id')).scalars().all() == [960, 1000]
        assert db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == 1
        assert db.execute(text('SELECT COUNT(*) FROM print_filament_evidence WHERE claim=1')).scalar_one() == 1


@pytest.fixture
def pg_settlement(pg_engine, monkeypatch):
    from core.schema import bootstrap_database
    with pg_engine.begin() as db:
        db.exec_driver_sql('DROP SCHEMA public CASCADE')
        db.exec_driver_sql('CREATE SCHEMA public')
    bootstrap_database(pg_engine)
    with settlement_client(pg_engine, monkeypatch) as case:
        yield case


@contextmanager
def settlement_client(engine, monkeypatch):
    """Use the same bare app and rows for native races and local fixture checks."""
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.archives.routes import router
    from modules.inventory.routes.spools import router as spools
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    with engine.begin() as db:
        db.execute(text("INSERT INTO users(id,username,role,is_active,password_hash) VALUES(991240,'settle-test','admin',TRUE,'fixture')"))
        db.execute(text("INSERT INTO printers(id,name,is_active) VALUES(991240,'Settlement test',TRUE)"))
        db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(991240,'Fixture','PLA')"))
        db.execute(text("INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,spool_weight_g,status,created_at) VALUES(991240,991240,1000,1000,250,'active','2026-10-01')"))
        db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,assigned_spool_id) VALUES(991240,1,991240)"))
        db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at,ended_at) VALUES(991240,991240,'failed','2026-10-11 12:00:00','2026-10-11 13:00:00')"))
        archive = db.execute(text("INSERT INTO print_archives(print_job_id,printer_id,print_name,ownership_state,accounting_state,accounting_reason,completed_at) VALUES(991240,991240,'Fixture','known','unresolved','no_bound_spool','2026-10-11 13:00:00') RETURNING id")).scalar_one()
    user = dict(id=991240, username='settle-test', role='admin', group_id=None, is_superadmin=True)
    app = FastAPI()
    app.include_router(router)
    app.include_router(spools)
    def session():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: user
    with TestClient(app) as client:
        yield client, engine, archive, user


def seed_completion(engine, shared_spool=True):
    with engine.begin() as db:
        db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status,quantity,hold,ams_mapping,created_at) VALUES(991240,'Fixture',991240,'failed',1,FALSE,'[0]','2026-10-01')"))
        db.execute(text("INSERT INTO print_files(id,filename,job_id,filaments_json) VALUES(991240,'fixture.3mf',991240,:materials)"),
                   {'materials': json.dumps([{'slot': 0, 'used_grams': 40}])})
        db.execute(text('UPDATE jobs SET print_file_id=991240 WHERE id=991240'))
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=991240 WHERE id=991240'))
        if not shared_spool:
            db.execute(text("INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,spool_weight_g,status,created_at) VALUES(991241,991240,1000,1000,250,'active','2026-10-01')"))
            db.execute(text('UPDATE filament_slots SET assigned_spool_id=991241 WHERE printer_id=991240'))


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


def test_postgresql_busy_archive_uses_real_nowait_and_rolls_back(pg_settlement):
    client, engine, archive, _ = pg_settlement
    with engine.connect() as holder, holder.begin():
        holder.execute(text('SELECT id FROM print_archives WHERE id=:id FOR UPDATE'), {'id': archive})
        response = client.post(f'/archives/{archive}/settle-filament', json={'entries': [{'spool_id': 991240, 'grams': 40}]})
        assert response.status_code == 409, response.text
        assert response.json()['detail']['code'] == 'busy'
    with engine.connect() as db:
        assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=991240')).scalar_one() == 1000
        assert db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == 0
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'quota_attempt:filament:%' OR key LIKE 'filament_settlement:print:%'")).scalar_one() == 0
        assert db.execute(text('SELECT accounting_state FROM print_archives WHERE id=:id'), {'id': archive}).scalar_one() == 'unresolved'


@pytest.mark.parametrize('first,second,shared_spool', [
    ('settle', 'complete', True), ('complete', 'settle', True), ('settle', 'settle', True),
    ('settle', 'complete', False), ('complete', 'settle', False),
])
def test_postgresql_concurrent_writers_deduct_once(pg_settlement, monkeypatch, first, second, shared_spool):
    """Keep a real deduction open until the other connection contends or refuses."""
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    from threading import Event
    from time import monotonic
    from core import filament_ledger
    from core.db import get_db
    from modules.jobs.routes.jobs_lifecycle import router
    from modules.inventory import services
    client, engine, archive, _ = pg_settlement
    client.app.include_router(router)
    monkeypatch.setattr(services, 'push_consumption_to_spoolman', lambda *_: [])
    seed_completion(engine, shared_spool)
    nowait = first != second and shared_spool
    pids = Queue()
    def session():
        with Session(engine) as db:
            db.execute(text("SET LOCAL lock_timeout = '120s'"))
            pids.put(db.execute(text('SELECT pg_backend_pid()')).scalar_one())
            yield db
    client.app.dependency_overrides[get_db] = session
    deducted, first_progress, release = Event(), Event(), Event()
    original = filament_ledger._deduct
    def hold_deduction(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result is not None, 'Fixture did not supply a deductible spool'
        if not deducted.is_set():
            deducted.set()
            first_progress.set()
            assert release.wait(90), 'Test did not release the first writer'
        return result
    monkeypatch.setattr(filament_ledger, '_deduct', hold_deduction)
    def request(kind):
        if kind == 'complete':
            return client.post('/jobs/991240/complete')
        return client.post(f'/archives/{archive}/settle-filament', json={'entries': [{'spool_id': 991240, 'grams': 40}]})
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(request, first)
        a.add_done_callback(lambda _: first_progress.set())
        try:
            assert first_progress.wait(30), 'First route did not reach accounting or return'
            if not deducted.is_set():
                response = a.result(timeout=30)
                pytest.fail(f'First route did not deduct: {response.status_code} {response.text}')
            first_pid = pids.get(timeout=30)
            b = pool.submit(request, second)
            second_pid = pids.get(timeout=30)
            assert first_pid != second_pid
            if nowait:
                # Both routes use NOWAIT for spools (completion via lock_slot_graph).
                # Require the actual refusal while the first transaction is open.
                response = b.result(timeout=30)
                assert response.status_code == 409, response.text
                if second == 'complete':
                    assert response.json()['detail'] == 'Spool assignments are busy; retry'
                else:
                    assert response.json()['detail']['code'] == 'busy'
                with engine.connect() as db:
                    assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=991240')).scalar_one() == 1000
                    assert db.execute(text('SELECT COUNT(*) FROM spool_usage')).scalar_one() == 0
            else:
                # Two settlements wait on the physical print row before NOWAIT;
                # writers using different spools can only meet at the claim key.
                # Observe the server's blocker, never infer contention from time.
                deadline = monotonic() + 30
                with engine.connect() as db:
                    while True:
                        blockers = db.execute(text('SELECT pg_blocking_pids(:pid)'), {'pid': second_pid}).scalar_one()
                        if first_pid in blockers:
                            break
                        if b.done():
                            response = b.result()
                            pytest.fail(f'Second route returned without contending: {response.status_code} {response.text}')
                        assert monotonic() < deadline, 'Second writer did not contend on the real first transaction'
                    statement = db.execute(text('SELECT query FROM pg_stat_activity WHERE pid=:pid'), {'pid': second_pid}).scalar_one()
                    assert ('UPDATE print_jobs' if shared_spool else 'INSERT INTO print_filament_evidence') in statement, statement
                assert not b.done()
        finally:
            release.set()
        first_response, second_response = a.result(timeout=30), b.result(timeout=30)
    assert first_response.status_code == 200, first_response.text
    if nowait:
        # Retry the busy route after commit: retained claims must prevent a second debit.
        second_response = request(second)
    if second == 'complete':
        assert second_response.status_code == 200, second_response.text
    else:
        assert second_response.status_code == 409, second_response.text
        assert second_response.json()['detail']['code'] == ('already_deducted' if first == 'complete' else 'not_unresolved')
    if first == 'complete':
        response = client.post(f'/archives/{archive}/settle-filament', json={'no_deduction': 'accept_existing'})
        assert response.status_code == 200, response.text
    with engine.connect() as db:
        winning_spool = 991241 if first == 'complete' and not shared_spool else 991240
        assert db.execute(text('SELECT id,remaining_weight_g FROM spools ORDER BY id')).all() == [
            (spool, 960 if spool == winning_spool else 1000)
            for spool in ([991240] if shared_spool else [991240, 991241])]
        assert db.execute(text('SELECT COUNT(*),SUM(weight_used_g) FROM spool_usage')).one() == (1, 40)
        assert db.execute(text('SELECT accounting_state FROM print_archives WHERE id=:id'), {'id': archive}).scalar_one() == 'accounted'
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='archive.filament_settled'")).scalar_one() == 1
