"""Delete jobs through the API with current and pre-cascade foreign keys."""
from contextlib import contextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
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
    with engine.begin() as db:
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
        with Session(engine) as db:
            yield db

    app = FastAPI()
    for router in (jobs_crud.router, routes.router, routes_users.router, routes_config.router):
        app.include_router(router, prefix="/api")
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "admin", "group_id": None}
    with TestClient(app) as client:
        yield client


def assert_links(engine, *, deleted):
    with engine.connect() as db:
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
@pytest.mark.parametrize("deferred", [False, True], ids=["at-delete", "at-commit"])
def test_unknown_constraint_rolls_back(review_db, monkeypatch, enterprise_license, caplog, path, deferred):
    engine = review_db.get_bind()
    with client_for(engine, monkeypatch, legacy=True) as client:
        with engine.begin() as db:
            if deferred:
                db.exec_driver_sql("CREATE TABLE retained_job (job_id INTEGER REFERENCES jobs(id) "
                                   "DEFERRABLE INITIALLY DEFERRED)")
                db.exec_driver_sql("INSERT INTO retained_job VALUES (81)")
            else:
                db.exec_driver_sql("""CREATE TRIGGER hold_job BEFORE DELETE ON jobs WHEN OLD.id=81
                    BEGIN SELECT RAISE(ABORT, 'test hold on job'); END""")
        response = delete_via(client, path)
        assert response.status_code == 409, response.text
        assert response.json() == {"code": "job_has_linked_records",
                                   "message": "This job has linked records and cannot be deleted."}
        reason = "FOREIGN KEY constraint failed" if deferred else "test hold on job"
        assert reason in caplog.text
    assert_links(engine, deleted=False)
    with engine.connect() as db:
        assert db.execute(text("SELECT id FROM groups WHERE id=81")).scalar_one() == 81
        assert not db.execute(text("SELECT id FROM audit_logs WHERE action='job.deleted' AND entity_id=81")).first()
