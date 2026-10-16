"""cert-D F04-4.1-01 / F04-4.5-01: lifecycle corrections are idempotent and fail is atomic."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.fixture
def lifecycle_client(review_db, monkeypatch):
    from core import db as core_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    review_db.execute(text("INSERT INTO models(id,name,org_id,color_requirements) VALUES(111,'Fixture model',1,:req)"),
                      {"req": json.dumps({"slot1": {"grams": 10.0}})})
    review_db.execute(text("INSERT INTO jobs(id,item_name,model_id,printer_id,status,charged_to_org_id,quantity,hold) "
                           "VALUES(111,'Fixture print',111,9,'printing',1,1,0)"))
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(111,'Synthetic','Test','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,status,location_printer_id,location_slot,remaining_weight_g,"
                           "initial_weight_g,org_id) VALUES(111,111,'active',9,1,100,100,1)"))
    review_db.commit()
    binding = review_db.get_bind()
    app = FastAPI(); app.include_router(jobs_lifecycle.router)

    def request_db():
        with Session(binding) as session:
            yield session
    app.dependency_overrides[core_db.get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "role": "operator", "group_id": 1, "is_active": True}
    with TestClient(app) as client:
        yield client, review_db


def _spool(db):
    db.expire_all()
    return (db.execute(text("SELECT remaining_weight_g FROM spools WHERE id=111")).scalar_one(),
            db.execute(text("SELECT count(*) FROM spool_usage WHERE job_id=111")).scalar_one())


def test_failed_then_completed_correction_deducts_filament_once(lifecycle_client):
    client, db = lifecycle_client
    assert client.post("/jobs/111/complete").json()["status"] == "completed"
    assert _spool(db) == (90.0, 1)
    assert client.post("/jobs/111/fail").json()["status"] == "failed"      # inspection: it actually failed
    assert client.post("/jobs/111/complete").json()["status"] == "completed"  # correction back
    assert _spool(db) == (90.0, 1)


@pytest.mark.parametrize("status", ["cancelled", "submitted", "rejected"])
def test_fail_refusal_is_409_and_leaves_row_and_session_clean(lifecycle_client, status):
    client, db = lifecycle_client
    db.execute(text("UPDATE jobs SET status=:s, notes='before' WHERE id=111"), {"s": status}); db.commit()
    response = client.post("/jobs/111/fail", params={"notes": "operator note"})
    assert response.status_code == 409, response.text
    db.expire_all()
    assert tuple(db.execute(text("SELECT status,notes,actual_end FROM jobs WHERE id=111")).one()) == (status, "before", None)
    # The session was rolled back: a following valid request on the same app still works.
    db.execute(text("UPDATE jobs SET status='printing' WHERE id=111")); db.commit()
    assert client.post("/jobs/111/fail", params={"notes": "real"}).json()["status"] == "failed"


def test_fail_predicate_is_atomic_against_a_concurrent_cancel(lifecycle_client, monkeypatch):
    """The row turns cancelled after fail_job read it: the UPDATE predicate still refuses."""
    client, db = lifecycle_client
    from modules.jobs.routes import jobs_lifecycle
    original = jobs_lifecycle.prepare_job_write

    def cancel_between_read_and_write(session, ids, *a, **k):
        parents = original(session, ids, *a, **k)
        session.execute(text("UPDATE jobs SET status='cancelled' WHERE id=111"))
        return parents
    monkeypatch.setattr(jobs_lifecycle, "prepare_job_write", cancel_between_read_and_write)
    response = client.post("/jobs/111/fail")
    assert response.status_code == 409, response.text
    db.expire_all()
    # Refused by the UPDATE predicate; the request's transaction (including the injected cancel) rolled back.
    assert db.execute(text("SELECT status FROM jobs WHERE id=111")).scalar_one() == "printing"


def test_reset_then_reprint_deducts_again_and_markers_follow_the_attempt(lifecycle_client):
    """Per print attempt (same scheme as the quota attempt markers): correction = once, reset = new attempt."""
    from core.print_attempts import attempt_seq, sqlalchemy_runner
    client, db = lifecycle_client
    assert client.post("/jobs/111/complete").status_code == 200
    assert client.post("/jobs/111/fail").status_code == 200
    assert client.post("/jobs/111/complete").status_code == 200
    assert _spool(db) == (90.0, 1)
    assert client.post("/jobs/111/reset").status_code == 200          # a genuine new print
    assert attempt_seq(sqlalchemy_runner(db), 111) == 1
    db.execute(text("UPDATE jobs SET printer_id=9 WHERE id=111")); db.commit()  # re-assigned to the printer
    assert client.post("/jobs/111/start").status_code == 200
    assert client.post("/jobs/111/complete").status_code == 200
    assert _spool(db) == (80.0, 2)
    db.expire_all()
    keys = set(db.execute(text("SELECT key FROM system_config WHERE key LIKE 'quota_attempt:%'")).scalars())
    assert keys == {'quota_attempt:seq:111', 'quota_attempt:filament:job:111:0', 'quota_attempt:filament:job:111:1'}


def test_delete_clears_the_attempt_counter_and_markers(lifecycle_client):
    from fastapi import FastAPI
    from modules.jobs.routes import jobs_crud
    client, db = lifecycle_client
    client.app.include_router(jobs_crud.router)
    assert client.post("/jobs/111/complete").status_code == 200
    assert client.post("/jobs/111/reset").status_code == 200
    db.execute(text("UPDATE jobs SET status='cancelled' WHERE id=111")); db.commit()
    assert client.delete("/jobs/111").status_code == 204
    db.expire_all()
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'quota_attempt:%111%'")).scalar_one() == 0


# ---------------- round 4: deductions without a marker (pre-upgrade, Moonraker) ----------------

def test_pre_upgrade_deduction_without_a_marker_is_not_repeated(lifecycle_client):
    client, db = lifecycle_client
    # A pre-upgrade completion deducted 10 g and wrote spool_usage, but no attempt marker exists.
    db.execute(text("UPDATE spools SET remaining_weight_g=90 WHERE id=111"))
    db.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,notes) VALUES(111,111,10,'pre-upgrade')"))
    db.execute(text("UPDATE jobs SET status='failed' WHERE id=111")); db.commit()
    assert client.post("/jobs/111/complete").status_code == 200        # correction after upgrade
    assert _spool(db) == (90.0, 1)


def _moonraker(db, monkeypatch):
    import types
    from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor
    db.execute(text("UPDATE spools SET location_slot=NULL WHERE id=111"))
    db.commit()
    fake = types.SimpleNamespace(printer_id=9)
    engine = db.get_bind()

    def deduct():
        with engine.begin() as conn:
            MoonrakerMonitor._auto_deduct_filament(fake, conn, 111, observed_grams=10.0)
    return deduct


def test_moonraker_deduction_is_per_attempt_and_shared_with_operator_corrections(lifecycle_client, monkeypatch):
    client, db = lifecycle_client
    deduct = _moonraker(db, monkeypatch)
    deduct()
    assert _spool(db) == (90.0, 1)
    deduct()                                                             # duplicate end event
    assert _spool(db) == (90.0, 1)
    db.execute(text("UPDATE jobs SET status='failed' WHERE id=111")); db.commit()
    assert client.post("/jobs/111/complete").status_code == 200          # operator correction: same attempt
    assert _spool(db) == (90.0, 1)
    assert client.post("/jobs/111/reset").status_code == 200             # a new physical attempt
    db.execute(text("UPDATE jobs SET printer_id=9 WHERE id=111")); db.commit()  # rescheduled on the printer
    deduct()                                                             # Moonraker reprint deducts again
    assert _spool(db) == (80.0, 2)


def test_filament_claim_is_atomic_even_when_both_callers_pass_the_read_check(lifecycle_client, monkeypatch):
    """Two writers that both read 'not deducted' still produce exactly one winning claim."""
    from core import print_attempts
    client, db = lifecycle_client
    monkeypatch.setattr(print_attempts, 'filament_already_deducted', lambda run, job_id: False)
    engine = db.get_bind()
    with engine.begin() as first, engine.begin() as second:
        a = print_attempts.mark_filament_deducted(print_attempts.sqlalchemy_runner(first), 111)
    with engine.begin() as second:
        b = print_attempts.mark_filament_deducted(print_attempts.sqlalchemy_runner(second), 111)
    assert (a, b) == (True, False)
