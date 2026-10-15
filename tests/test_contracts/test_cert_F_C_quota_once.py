"""Security review MED 4: a job's grams/hours are charged to quota at most once.

A manual POST /jobs/{id}/complete followed by the monitor's observed completion
charged twice (job_events overwrote the status unconditionally, then charged).
Same rule as the filament deduction (cert-D): once per job across failed->completed
corrections and reset->reprint. Real jobs router and the real monitor completion
(job_events.job_completed) on a bootstrapped SQLite schema.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[2] / "backend"


@pytest.fixture
def farm(tmp_path, monkeypatch):
    import secrets
    monkeypatch.setenv("JWT_SECRET_KEY", secrets.token_urlsafe(48))
    from core.db import get_db
    from core.db_utils import _ConnectionAdapter
    from core.dependencies import get_current_user
    from core.registry import registry
    from core.schema import bootstrap_database
    from modules.jobs.routes import router as jobs_router
    from modules.notifications import job_events
    from modules.organizations.services import EducationPolicyService
    engine = create_engine("sqlite:///" + str(tmp_path / "quota.db"), connect_args={"check_same_thread": False})
    bootstrap_database(engine, BACKEND)
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,quota_period) "
                       "VALUES(3,'q_op','q_op@example.test','x','operator',1,'monthly')"))
        c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared) "
                       "VALUES(9,'Quota printer','moonraker',1,1,'[]',0,0)"))
        c.execute(text("INSERT INTO models(id,name,color_requirements) VALUES(41,'Quota part',:r)"),
                  {"r": json.dumps({"1": {"color": "black", "grams": 40}})})
    app = FastAPI()
    app.include_router(jobs_router)

    def database():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "username": "q_op", "role": "operator",
                                                          "group_id": None, "is_active": True}

    @contextmanager
    def monitor_db():
        connection = engine.raw_connection()
        try:
            yield _ConnectionAdapter(connection, False)
        except BaseException:
            connection.rollback(); raise
        finally:
            connection.close()
    monkeypatch.setattr(job_events, "get_db", monitor_db)
    monkeypatch.setattr(job_events, "dispatch_alert", lambda **kwargs: None)
    monkeypatch.setattr(job_events, "increment_care_counters", lambda *args: None)

    class QuietBus:
        def publish(self, event): pass
    monkeypatch.setattr(job_events, "get_event_bus", lambda: QuietBus())

    def monitor_completes(job_id, success=True, seconds=3600):
        with engine.begin() as c:
            pid = c.execute(text("INSERT INTO print_jobs(printer_id,job_name,started_at,status,scheduled_job_id) "
                                 "VALUES(9,'observed',:now,'running',:j) RETURNING id"),
                            {"now": datetime.now(timezone.utc).replace(tzinfo=None), "j": job_id}).scalar_one()
        job_events.job_completed(9, pid, success=success, duration_seconds=seconds)

    def usage():
        with engine.connect() as c:
            row = c.execute(text("SELECT COALESCE(SUM(grams_used),0), COALESCE(SUM(hours_used),0) FROM quota_usage WHERE user_id=3")).one()
        return float(row[0]), round(float(row[1]), 6)

    def status(job_id):
        with engine.connect() as c:
            return c.execute(text("SELECT status FROM jobs WHERE id=:j"), {"j": job_id}).scalar_one()

    with TestClient(app) as client:
        def new_job():
            r = client.post("/jobs", json={"item_name": "Quota once", "model_id": 41, "duration_hours": 2, "printer_id": 9})
            assert r.status_code == 201, r.text
            return r.json()["id"]
        yield client, new_job, monitor_completes, usage, status


def test_manual_completion_then_monitor_completion_charges_once(farm):
    client, new_job, monitor_completes, usage, status = farm
    jid = new_job()
    assert client.post(f"/jobs/{jid}/start").status_code == 200
    assert client.post(f"/jobs/{jid}/complete").status_code == 200
    charged = usage()
    assert charged[0] == 40
    monitor_completes(jid, success=True)
    assert usage() == charged
    assert status(jid) == "completed"


def test_monitor_does_not_overwrite_an_operator_outcome(farm):
    client, new_job, monitor_completes, usage, status = farm
    jid = new_job()
    assert client.post(f"/jobs/{jid}/start").status_code == 200
    assert client.post(f"/jobs/{jid}/fail").status_code == 200
    monitor_completes(jid, success=True)
    assert status(jid) == "failed" and usage() == (0.0, 0.0)


def test_failed_then_completed_charges_once(farm):
    client, new_job, monitor_completes, usage, status = farm
    jid = new_job()
    assert client.post(f"/jobs/{jid}/start").status_code == 200
    assert client.post(f"/jobs/{jid}/fail").status_code == 200
    assert usage() == (0.0, 0.0)
    assert client.post(f"/jobs/{jid}/complete").status_code == 200
    assert usage()[0] == 40
    assert client.post(f"/jobs/{jid}/complete").status_code == 200   # idempotent retry
    assert usage()[0] == 40


def test_reprint_after_reset_is_a_new_print_and_charges_again(farm):
    # Security review MED-D: a reset-and-reprint is a new print attempt. Idempotency
    # is keyed on the attempt, so the second completion charges again.
    client, new_job, monitor_completes, usage, status = farm
    jid = new_job()
    assert client.post(f"/jobs/{jid}/start").status_code == 200
    assert client.post(f"/jobs/{jid}/complete").status_code == 200
    first = usage()
    assert first[0] == 40
    assert client.post(f"/jobs/{jid}/reset").status_code == 200
    assert status(jid) == "pending"
    assert client.post(f"/jobs/{jid}/start").status_code == 200
    monitor_completes(jid, success=True)
    assert status(jid) == "completed"
    assert usage()[0] == 80  # charged a second time for the reprint


def test_monitor_only_completion_charges_once(farm):
    client, new_job, monitor_completes, usage, status = farm
    jid = new_job()
    assert client.post(f"/jobs/{jid}/start").status_code == 200
    monitor_completes(jid, success=True, seconds=5400)
    assert status(jid) == "completed" and usage() == (40.0, 1.5)
    monitor_completes(jid, success=True, seconds=5400)
    assert usage() == (40.0, 1.5)


def test_delete_clears_the_attempt_marker_so_a_reused_id_charges(farm):
    # Security review MED-D: the marker must not survive job delete (SQLite reuses ids).
    client, new_job, monitor_completes, usage, status = farm
    jid = new_job()
    assert client.post(f"/jobs/{jid}/start").status_code == 200
    assert client.post(f"/jobs/{jid}/complete").status_code == 200
    assert usage()[0] == 40
    assert client.delete(f"/jobs/{jid}").status_code == 204
    jid2 = new_job()
    assert client.post(f"/jobs/{jid2}/start").status_code == 200
    assert client.post(f"/jobs/{jid2}/complete").status_code == 200
    assert usage()[0] == 80  # the reused id does not inherit the deleted job's marker
