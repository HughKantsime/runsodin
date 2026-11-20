"""Delete jobs through the API with current and pre-cascade foreign keys."""
from contextlib import contextmanager, nullcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from core.registry import registry
from modules.jobs.routes import jobs_crud
from modules.organizations import routes, routes_users
from modules.organizations.services import EducationPolicyService
from modules.system import routes_config
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def legacy_links(db):
    # Build both child tables by hand, omitting the original job delete actions.
    db.exec_driver_sql("DROP TABLE alerts")
    db.exec_driver_sql("""CREATE TABLE alerts (
        id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, alert_type TEXT NOT NULL,
        severity TEXT NOT NULL, title TEXT NOT NULL, message TEXT, is_read BOOLEAN,
        is_dismissed BOOLEAN, printer_id INTEGER REFERENCES printers(id),
        job_id INTEGER REFERENCES jobs(id), spool_id INTEGER REFERENCES spools(id),
        metadata_json JSON, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    db.exec_driver_sql("DROP TABLE spool_usage")
    db.exec_driver_sql("""CREATE TABLE spool_usage (
        id INTEGER PRIMARY KEY, spool_id INTEGER NOT NULL REFERENCES spools(id),
        job_id INTEGER REFERENCES jobs(id), weight_used_g FLOAT NOT NULL,
        used_at DATETIME DEFAULT CURRENT_TIMESTAMP, notes TEXT, print_job_id INTEGER)""")


@contextmanager
def client_for(engine, monkeypatch, *, legacy=False):
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    with (nullcontext(engine) if isinstance(engine, Connection) else engine.begin()) as db:
        if engine.dialect.name == "sqlite":
            assert db.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            if legacy:
                legacy_links(db)
            actions = [next(row[6] for row in db.exec_driver_sql(sql) if row[2] == "jobs")
                       for sql in ("PRAGMA foreign_key_list(alerts)", "PRAGMA foreign_key_list(spool_usage)")]
            assert actions == (["NO ACTION", "NO ACTION"] if legacy else ["CASCADE", "SET NULL"])
        db.execute(text("INSERT INTO groups(id,name,is_org) VALUES (81,'Print room',TRUE)"))
        db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,updated_at) VALUES "
                        "(81,'First print','cancelled',81,'2000-01-01'),"
                        "(82,'Other print','printing',1,CURRENT_TIMESTAMP)"))
        db.execute(text("INSERT INTO filament_library(id,brand,name) VALUES (81,'Test','PLA')"))
        db.execute(text("INSERT INTO spools(id,filament_id,remaining_weight_g) VALUES (81,81,500)"))
        for jid in (81, 82):
            db.execute(text("INSERT INTO alerts(id,user_id,alert_type,severity,title,job_id) "
                            "VALUES (:id,3,'print_complete','info','Print finished',:id)"), {"id": jid})
            db.execute(text("INSERT INTO spool_usage(id,spool_id,job_id,weight_used_g,notes) "
                            "VALUES (:id,81,:id,15,'First layer')"), {"id": jid})

    def session():
        with Session(engine, join_transaction_mode="create_savepoint") as db:
            yield db

    app = FastAPI()
    for router in (jobs_crud.router, routes.router, routes_users.router, routes_config.router):
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "admin", "group_id": None}
    with TestClient(app) as client:
        yield client


def assert_links(engine, *, deleted):
    with (nullcontext(engine) if isinstance(engine, Connection) else engine.connect()) as db:
        ids = [82] if deleted else [81, 82]
        assert db.execute(text("SELECT id FROM jobs WHERE id IN (81,82) ORDER BY id")).scalars().all() == ids
        assert db.execute(text("SELECT job_id FROM alerts WHERE id IN (81,82) ORDER BY id")).scalars().all() == ids
        assert db.execute(text("SELECT id,job_id,spool_id,weight_used_g,notes FROM spool_usage "
                               "WHERE id IN (81,82) ORDER BY id")).all() == [
            (81, None if deleted else 81, 81, 15, "First layer"), (82, 82, 81, 15, "First layer")]
        assert db.execute(text("SELECT remaining_weight_g FROM spools WHERE id=81")).scalar_one() == 500


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy", "current"])
def test_single_delete(review_db, monkeypatch, legacy):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=legacy) as client:
        response = client.delete("/api/jobs/81")
        assert response.status_code == 204, response.text
        assert not response.content
    assert_links(engine, deleted=True)


@pytest.mark.postgresql
def test_single_delete_postgres(pg_engine, monkeypatch):
    with client_for(pg_engine, monkeypatch) as client:
        response = client.delete("/api/jobs/81")
        assert response.status_code == 204, response.text
    assert_links(pg_engine, deleted=True)


def test_delivered_inspection_keeps_existing_refusal(review_db, monkeypatch):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch) as client:
        with engine.begin() as db:
            db.execute(text("INSERT INTO traceability_records "
                            "(job_id,attempt,snapshot,snapshot_sha256,captured_at,delivered_at) "
                            "VALUES (81,1,'{}','digest','2000-01-01','2000-01-02')"))
        response = client.delete("/api/jobs/81")
        assert response.status_code == 409
        assert response.json()["detail"] == "Delivered traceability records must be retained"
    assert_links(engine, deleted=False)


def delete_via(client, path):
    if path == "single":
        return client.delete("/api/jobs/81")
    if path == "bulk":
        return client.post("/api/jobs/bulk-update", json={"action": "delete", "job_ids": [81, 82]})
    if path == "retention":
        response = client.put("/api/config/retention", json={"completed_jobs_days": 1, "alert_history_days": 0})
        assert response.status_code == 200, response.text
        return client.post("/api/admin/retention/cleanup")
    return client.delete(f"/api/{path}/81")


@pytest.mark.parametrize("path", ["bulk", "retention", "orgs", "groups"])
def test_other_deletes_on_legacy_schema(review_db, monkeypatch, enterprise_license, path):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        response = delete_via(client, path)
        assert response.status_code == 200, response.text
        if path == "bulk":
            assert response.json()["affected"] == 1
    assert_links(engine, deleted=True)


def test_bulk_rechecks_status_before_unlinking(review_db, monkeypatch):
    from modules.orders import services

    engine = review_db.get_bind()
    lock_and_check = services.assert_job_history_deletable

    def started_before_lock(db, job_id):
        if job_id == 81:
            db.execute(text("UPDATE jobs SET status='printing' WHERE id=81"))
        lock_and_check(db, job_id)

    monkeypatch.setattr(services, "assert_job_history_deletable", started_before_lock)
    with client_for(engine, monkeypatch, legacy=True) as client:
        response = delete_via(client, "bulk")
        assert response.status_code == 200, response.text
        assert response.json()["affected"] == 0
    assert_links(engine, deleted=False)


@pytest.mark.parametrize("path", ["single", "bulk", "retention", "orgs", "groups"])
def test_unknown_constraint_rolls_back(review_db, monkeypatch, enterprise_license, caplog, path):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        with engine.begin() as db:
            db.exec_driver_sql("""CREATE TRIGGER hold_job BEFORE DELETE ON jobs WHEN OLD.id=81
                BEGIN SELECT RAISE(ABORT, 'test hold on job'); END""")
        response = delete_via(client, path)
        assert response.status_code == 409, response.text
        assert response.json() == {"code": "job_has_linked_records",
                                   "message": "This job has linked records and cannot be deleted."}
        assert "test hold on job" in caplog.text
        assert f"path={path} job_ids=[81]" in caplog.text
    assert_links(engine, deleted=False)
    with engine.connect() as db:
        assert db.execute(text("SELECT id FROM groups WHERE id=81")).scalar_one() == 81
        assert not db.execute(text("SELECT id FROM audit_logs WHERE action='job.deleted' AND entity_id=81")).first()


@pytest.mark.parametrize("path", ["single", "bulk"])
def test_unrelated_reconcile_error_propagates(review_db, monkeypatch, path):
    def fail_reconciliation(db, _orders):
        db.execute(text("INSERT INTO groups(id,name) VALUES (81,'Duplicate')"))

    engine = review_db.get_bind()
    with client_for(engine, monkeypatch) as client:
        monkeypatch.setattr(jobs_crud, "reconcile_job_write", fail_reconciliation)
        with pytest.raises(IntegrityError, match="groups.id"):
            delete_via(client, path)
    assert_links(engine, deleted=False)


@pytest.mark.parametrize("path", ["single", "bulk", "orgs", "groups", "retention"])
def test_unrelated_deferred_constraint_is_not_job_conflict(review_db, monkeypatch, enterprise_license, path):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch) as client:
        with engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE audit_target (group_id INTEGER REFERENCES groups(id) "
                               "DEFERRABLE INITIALLY DEFERRED)")
            db.exec_driver_sql("""CREATE TRIGGER unrelated_write AFTER DELETE ON jobs
                BEGIN INSERT INTO audit_target VALUES (-1); END""")
        if path == "retention":
            response = delete_via(client, path)
            assert response.status_code == 503, response.text
        else:
            with pytest.raises(IntegrityError, match="FOREIGN KEY"):
                delete_via(client, path)
    assert_links(engine, deleted=False)


@pytest.mark.parametrize("path", ["single", "bulk", "orgs", "groups", "retention"])
def test_deferred_account_binding_failure_rolls_back_job_delete(review_db, monkeypatch, enterprise_license, path):
    from datetime import datetime, timezone
    from core import account_lifetime_schema
    from core.account_lifetime_creation import _account_creation_scope, _create_account

    engine = review_db.get_bind()
    with client_for(engine, monkeypatch) as client:
        with engine.begin() as db:
            db.exec_driver_sql("BEGIN")
            account_lifetime_schema.install(db)
            with _account_creation_scope(db) as scope:
                _create_account(db, scope, {"id": 81, "username": "binding-test", "password_hash": "fixture"},
                                born_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
            # Leave the binding in place when retiring its account in the delete transaction.
            db.exec_driver_sql("""CREATE TRIGGER incomplete_retirement AFTER DELETE ON jobs
                BEGIN UPDATE account_lifetimes SET current_user_id=NULL, state='retired',
                    retired_at='2026-01-02T00:00:00.000Z' WHERE current_user_id=81; END""")
        if path == "retention":
            response = delete_via(client, path)
            assert response.status_code == 503, response.text
        else:
            with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                delete_via(client, path)
    assert_links(engine, deleted=False)
    with engine.connect() as db:
        assert db.exec_driver_sql("SELECT state FROM account_lifetimes WHERE birth_user_id=81").scalar_one() == "active"


def test_retention_job_count_changed(review_db, monkeypatch):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        with engine.begin() as db:
            db.exec_driver_sql("""CREATE TRIGGER skip_job BEFORE DELETE ON jobs WHEN OLD.id=81
                BEGIN SELECT RAISE(IGNORE); END""")
        response = delete_via(client, "retention")
        assert response.status_code == 409, response.text
        assert response.json() == {"code": "retention_jobs_changed",
                                   "message": "Jobs changed during cleanup. Run it again."}
    assert_links(engine, deleted=False)


def test_foreign_org_cannot_unlink_job(review_db, monkeypatch):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        client.app.dependency_overrides[get_current_user] = lambda: {"id": 5, "role": "admin", "group_id": 2}
        assert client.delete("/api/jobs/81").status_code == 404
        response = client.post("/api/jobs/bulk-update", json={"action": "delete", "job_ids": [81]})
        assert response.status_code == 200, response.text
        assert response.json()["affected"] == 0
    assert_links(engine, deleted=False)


def test_bulk_late_refusal_restores_earlier_job(review_db, monkeypatch):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        with engine.begin() as db:
            db.execute(text("UPDATE jobs SET status='cancelled' WHERE id=82"))
            db.execute(text("INSERT INTO traceability_records "
                            "(job_id,attempt,snapshot,snapshot_sha256,captured_at,delivered_at) "
                            "VALUES (82,1,'{}','digest','2000-01-01','2000-01-02')"))
        deleted_ids = []

        def observe_delete(_conn, cursor, statement, params, _context, _many):
            if statement.startswith("DELETE FROM jobs WHERE id =") and cursor.rowcount == 1:
                deleted_ids.append(params[0])

        event.listen(engine, "after_cursor_execute", observe_delete)
        try:
            response = delete_via(client, "bulk")
        finally:
            event.remove(engine, "after_cursor_execute", observe_delete)
        assert deleted_ids == [81]
        assert response.status_code == 409
        assert response.json()["detail"] == "Delivered traceability records must be retained"
    assert_links(engine, deleted=False)


@pytest.mark.parametrize("size", [0, 501])
def test_org_job_link_chunk_boundary(review_db, monkeypatch, enterprise_license, size):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        with engine.begin() as db:
            db.execute(text("INSERT INTO groups(id,name,is_org) VALUES (99,'Test room',TRUE)"))
            if size:
                params = [{"id": 1000 + i} for i in range(size)]
                db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) "
                                "VALUES (:id,'Chunk','cancelled',99)"), params)
                db.execute(text("INSERT INTO alerts(id,user_id,alert_type,severity,title,job_id) "
                                "VALUES (:id,3,'print_complete','info','Done',:id)"), params)
                db.execute(text("INSERT INTO spool_usage(id,spool_id,job_id,weight_used_g) "
                                "VALUES (:id,81,:id,15)"), params)
        response = client.delete("/api/orgs/99")
        assert response.status_code == 200, response.text
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM groups WHERE id=99")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM jobs WHERE charged_to_org_id=99")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM alerts WHERE id>=1000")).scalar_one() == 0
        assert db.execute(text("SELECT count(*) FROM spool_usage WHERE id>=1000 AND job_id IS NULL")).scalar_one() == size
    assert_links(engine, deleted=False)


def test_conflict_log_limits_job_ids(review_db, monkeypatch, enterprise_license, caplog):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        with engine.begin() as db:
            db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) "
                            "VALUES (:id,'Print','cancelled',81)"), [{"id": i} for i in range(100, 121)])
            db.exec_driver_sql("""CREATE TRIGGER hold_job BEFORE DELETE ON jobs WHEN OLD.id=120
                BEGIN SELECT RAISE(ABORT, 'test hold on job'); END""")
        response = delete_via(client, "orgs")
        assert response.status_code == 409
        sample = [81, *range(100, 119)]
        assert f"path=orgs job_ids={sample}: test hold on job" in caplog.text
    assert_links(engine, deleted=False)
