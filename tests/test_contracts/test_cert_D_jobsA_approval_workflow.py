"""cert-D jobsA: generic job lifecycle + approval workflow through real routers, real schema, real JWT.

Only the outermost notification boundary (email / browser push transport) is substituted.
The wire-capture test writes frontend/src/test-fixtures/certD-jobsA-approval-wire.json, which the
frontend cert tests replay against the real Jobs page, permissions module and API client.
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_user_limits import signed_license  # noqa: F401

USERS = {  # username -> (id, role); ids/roles come from review_db, operator added here
    "student": (1, "viewer"), "manager": (2, "viewer"), "admin": (3, "admin"),
    "outsider": (4, "viewer"), "operator1": (6, "operator"),
}


@pytest.fixture
def cert(review_db, signed_license, monkeypatch):
    from core import auth
    from core.db import get_db
    from core.errors import OdinError
    from core.registry import registry
    from modules.jobs.routes import jobs_agent, jobs_crud, jobs_lifecycle, presets
    from modules.notifications import alert_dispatcher
    from modules.organizations import routes_auth, routes_permissions
    from modules.organizations.services import EducationPolicyService

    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    signed_license(tier="education", features=[])
    monkeypatch.setattr(auth, "SECRET_KEY", "certD-jobsA-approval-fixture-key")
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.setattr(alert_dispatcher, "should_suppress_notification", lambda **_: False)
    external = []
    monkeypatch.setattr(alert_dispatcher, "_deliver_email",
                        lambda db, uid, title, message, severity: external.append(("email", uid, title, message)))
    monkeypatch.setattr(alert_dispatcher, "_deliver_browser_push",
                        lambda db, uid, title, message, severity: external.append(("push", uid, title, message)))
    review_db.execute(text(
        "INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) "
        "VALUES (6,'operator1','operator1@example.test','fixture','operator',1,1)"))
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_job_approval','true')"))
    review_db.commit()

    app = FastAPI()
    for r in (jobs_crud.router, jobs_lifecycle.router, jobs_agent.router):
        app.include_router(r)
    app.include_router(presets.router)
    app.include_router(routes_permissions.router)
    app.include_router(routes_auth.router)

    @app.exception_handler(OdinError)
    async def odin_error(request, exc):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    def database():
        with Session(review_db.get_bind()) as db:
            yield db

    app.dependency_overrides[get_db] = database

    def as_user(username):
        return {"Authorization": "Bearer " + auth.create_access_token({"sub": username, "role": USERS[username][1]})}

    with TestClient(app) as client:
        yield client, as_user, external


def _status(db, job_id):
    db.rollback()
    return db.execute(text("SELECT status FROM jobs WHERE id=:id"), {"id": job_id}).scalar_one()


def _set_approval(db, enabled):
    db.execute(text("UPDATE system_config SET value=:v WHERE key='require_job_approval'"),
               {"v": "true" if enabled else "false"})
    db.commit()


def _alerts(db, alert_type=None):
    db.rollback()
    sql = "SELECT user_id, alert_type, job_id FROM alerts"
    if alert_type:
        return {tuple(r) for r in db.execute(text(sql + " WHERE alert_type=:t"), {"t": alert_type}).all()}
    return {tuple(r) for r in db.execute(text(sql)).all()}


def _rejected_job(db, submitted_by=1, reason="Synthetic: reduce infill"):
    db.execute(text(
        "INSERT INTO jobs(item_name,status,submitted_by,charged_to_user_id,charged_to_org_id,rejected_reason,quantity,priority,hold,is_locked) "
        "VALUES('Synthetic rejected',:s,:u,:u,1,:r,1,3,0,0)"), {"s": "rejected", "u": submitted_by, "r": reason})
    db.commit()
    return db.execute(text("SELECT MAX(id) FROM jobs")).scalar_one()


# ---------------------------------------------------------------- F04-4.1-01 lifecycle
def test_F04_4_1_01_generic_lifecycle_pending_printing_completed_or_failed(cert, review_db):
    client, as_user, _ = cert
    _set_approval(review_db, False)
    op = as_user("operator1")
    first = client.post("/jobs", json={"item_name": "Synthetic lifecycle A"}, headers=op)
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "pending"
    a = first.json()["id"]
    started = client.post(f"/jobs/{a}/start", headers=op)
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "printing" and started.json()["actual_start"]
    done = client.post(f"/jobs/{a}/complete", headers=op)
    assert done.status_code == 200 and done.json()["status"] == "completed" and done.json()["actual_end"]
    assert _status(review_db, a) == "completed"
    # A completed job cannot be restarted.
    assert client.post(f"/jobs/{a}/start", headers=op).status_code == 400
    assert _status(review_db, a) == "completed"

    b = client.post("/jobs", json={"item_name": "Synthetic lifecycle B"}, headers=op).json()["id"]
    assert client.post(f"/jobs/{b}/start", headers=op).json()["status"] == "printing"
    failed = client.post(f"/jobs/{b}/fail", headers=op)
    assert failed.status_code == 200 and failed.json()["status"] == "failed"
    assert _status(review_db, b) == "failed"
    # Viewers cannot drive lifecycle transitions.
    c = client.post("/jobs", json={"item_name": "Synthetic lifecycle C"}, headers=op).json()["id"]
    assert client.post(f"/jobs/{c}/start", headers=as_user("student")).status_code == 403
    assert _status(review_db, c) == "pending"


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/jobs/routes/jobs_lifecycle.py:134 (/complete conditional UPDATE only excludes "
    "status=completed) and :327 (/fail sets FAILED with no source-state guard): a cancelled job, or a submitted/rejected job "
    "that was never approved, is moved to completed/failed (with filament deduction on complete), bypassing "
    "Pending->Printing and the approval gate"))
@pytest.mark.parametrize("source,action", [("cancelled", "complete"), ("submitted", "complete"),
                                           ("submitted", "fail"), ("rejected", "complete")])
def test_F04_4_1_terminal_transition_requires_a_printing_lifecycle(cert, review_db, source, action):
    client, as_user, _ = cert
    review_db.execute(text(
        "INSERT INTO jobs(id,item_name,status,submitted_by,charged_to_user_id,charged_to_org_id,quantity,priority,hold,is_locked) "
        "VALUES(901,'Synthetic guarded',:s,1,1,1,1,3,0,0)"), {"s": source})
    review_db.commit()
    response = client.post(f"/jobs/901/{action}", headers=as_user("operator1"))
    assert response.status_code in (400, 409), response.text
    assert _status(review_db, 901) == source


# ---------------------------------------------------------------- F04-4.1-02 approval-mode sequence
def test_F04_4_1_02_approval_mode_sequence_has_no_persisted_approved_state(cert, review_db):
    client, as_user, _ = cert
    observed = []
    created = client.post("/jobs", json={"item_name": "Synthetic approval sequence"}, headers=as_user("student"))
    assert created.status_code == 201, created.text
    job = created.json()["id"]
    observed.append(_status(review_db, job))
    approve = client.post(f"/jobs/{job}/approve", headers=as_user("operator1"))
    assert approve.status_code == 200, approve.text
    assert approve.json()["status"] == "approved"  # response label only
    observed.append(_status(review_db, job))
    row = review_db.execute(text("SELECT approved_by, approved_at FROM jobs WHERE id=:id"), {"id": job}).one()
    assert row[0] == 6 and row[1] is not None
    # Approve is not repeatable once the job left submitted.
    assert client.post(f"/jobs/{job}/approve", headers=as_user("operator1")).status_code == 400
    assert client.post(f"/jobs/{job}/start", headers=as_user("operator1")).json()["status"] == "printing"
    observed.append(_status(review_db, job))
    assert client.post(f"/jobs/{job}/complete", headers=as_user("operator1")).json()["status"] == "completed"
    observed.append(_status(review_db, job))
    assert observed == ["submitted", "pending", "printing", "completed"]
    from core.base import JobStatus
    assert "approved" not in {s.value for s in JobStatus}


def test_F04_4_1_02_unapproved_submission_cannot_be_started(cert, review_db):
    client, as_user, _ = cert
    job = client.post("/jobs", json={"item_name": "Synthetic unapproved"}, headers=as_user("student")).json()["id"]
    assert client.post(f"/jobs/{job}/start", headers=as_user("operator1")).status_code == 400
    assert _status(review_db, job) == "submitted"


# ---------------------------------------------------------------- F04-4.1-03 resubmit by original submitter
@pytest.mark.parametrize("actor,expected", [("outsider", 403), ("operator1", 403), ("admin", 403), ("student", 200)])
def test_F04_4_1_03_only_original_submitter_resubmits_rejected_job(cert, review_db, actor, expected):
    client, as_user, _ = cert
    review_db.execute(text("UPDATE groups SET owner_id=2 WHERE id=1"))
    review_db.commit()
    job = _rejected_job(review_db)
    response = client.post(f"/jobs/{job}/resubmit", headers=as_user(actor))
    assert response.status_code == expected, response.text
    review_db.rollback()
    status, reason = review_db.execute(text("SELECT status, rejected_reason FROM jobs WHERE id=:id"), {"id": job}).one()
    if expected == 200:
        assert response.json() == {"status": "resubmitted", "job_id": job}
        assert (status, reason) == ("submitted", None)
        assert _alerts(review_db, "job_submitted") == {(2, "job_submitted", job)}
        # A resubmitted job is no longer rejected and cannot be resubmitted again.
        assert client.post(f"/jobs/{job}/resubmit", headers=as_user("student")).status_code == 400
    else:
        assert (status, reason) == ("rejected", "Synthetic: reduce infill")
        assert _alerts(review_db) == set()


def test_F04_4_1_03_only_rejected_jobs_can_be_resubmitted(cert, review_db):
    client, as_user, _ = cert
    job = client.post("/jobs", json={"item_name": "Synthetic not rejected"}, headers=as_user("student")).json()["id"]
    assert client.post(f"/jobs/{job}/resubmit", headers=as_user("student")).status_code == 400
    assert _status(review_db, job) == "submitted"


# ---------------------------------------------------------------- F04-4.6-01 toggle (admin-only)
def test_F04_4_6_01_require_job_approval_setting_is_admin_only_and_effective(cert, review_db, signed_license):
    client, as_user, _ = cert
    review_db.execute(text("DELETE FROM system_config WHERE key='require_job_approval'"))
    review_db.commit()
    assert client.get("/config/require-job-approval", headers=as_user("student")).json() == {"require_job_approval": False}
    for actor in ("student", "operator1"):
        assert client.put("/config/require-job-approval", json={"enabled": True}, headers=as_user(actor)).status_code == 403
    assert client.get("/config/require-job-approval", headers=as_user("admin")).json() == {"require_job_approval": False}
    assert client.post("/jobs", json={"item_name": "Synthetic off"}, headers=as_user("student")).json()["status"] == "pending"

    on = client.put("/config/require-job-approval", json={"enabled": True}, headers=as_user("admin"))
    assert on.status_code == 200 and on.json() == {"require_job_approval": True}
    assert client.get("/config/require-job-approval", headers=as_user("operator1")).json() == {"require_job_approval": True}
    assert client.post("/jobs", json={"item_name": "Synthetic on"}, headers=as_user("student")).json()["status"] == "submitted"

    off = client.put("/config/require-job-approval", json={"enabled": False}, headers=as_user("admin"))
    assert off.json() == {"require_job_approval": False}
    assert client.post("/jobs", json={"item_name": "Synthetic off again"}, headers=as_user("student")).json()["status"] == "pending"

    # Feature gate: an admin on a license without job_approval cannot enable it.
    signed_license(tier="community", features=[])
    assert client.put("/config/require-job-approval", json={"enabled": True}, headers=as_user("admin")).status_code == 403
    assert client.get("/config/require-job-approval", headers=as_user("admin")).json() == {"require_job_approval": False}


def test_F04_4_6_01_settings_page_is_admin_only_in_served_rbac(cert):
    client, _, _ = cert
    perms = client.get("/permissions").json()
    assert perms["page_access"]["settings"] == ["admin"]
    assert perms["action_access"]["settings.edit"] == ["admin"]


# ---------------------------------------------------------------- F04-4.6-02 viewer submission -> submitted
def test_F04_4_6_02_viewer_submission_is_submitted_and_owned(cert, review_db):
    client, as_user, _ = cert
    response = client.post("/jobs", json={"item_name": "Synthetic viewer submission"}, headers=as_user("student"))
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "submitted"
    job = response.json()["id"]
    assert review_db.execute(text("SELECT status, submitted_by FROM jobs WHERE id=:id"), {"id": job}).one() == ("submitted", 1)
    listed = client.get("/jobs", headers=as_user("operator1")).json()
    assert [j["status"] for j in listed if j["id"] == job] == ["submitted"]
    listed_by_viewer = client.get("/jobs", headers=as_user("student")).json()
    assert [j["status"] for j in listed_by_viewer if j["id"] == job] == ["submitted"]


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: frontend/src/pages/jobs/Upload.tsx:304,376 shows viewers 'Submit for Approval' and routes to "
    "/models?schedule=<id>, whose producer backend/modules/models_library/routes/models_crud.py:333 requires operator "
    "(viewer -> 403) and :418 hard-codes status 'pending'; the viewer upload->submit-for-approval path cannot create a "
    "submitted job"))
def test_F04_4_6_02_viewer_upload_submit_for_approval_path_creates_submitted_job(cert, review_db):
    from modules.models_library.routes import models_crud
    client, as_user, _ = cert
    client.app.include_router(models_crud.router)
    review_db.execute(text("INSERT INTO models(id,name,build_time_hours,org_id) VALUES(77,'Synthetic model',1.0,1)"))
    review_db.commit()
    response = client.post("/models/77/schedule", headers=as_user("student"))
    assert response.status_code in (200, 201), response.text
    assert response.json()["status"] == "submitted"


# ---------------------------------------------------------------- F04-4.6-04 approve/reject + notification dispatch
def test_F04_4_6_04_approve_and_reject_dispatch_notifications_to_submitter(cert, review_db):
    from modules.notifications.models import AlertPreference
    client, as_user, external = cert
    for typ in ("job_approved", "job_rejected"):
        review_db.execute(text("DELETE FROM alert_preferences WHERE alert_type=:t"), {"t": typ})
        review_db.add(AlertPreference(user_id=1, alert_type=typ, in_app=True, email=True, browser_push=False))
        review_db.add(AlertPreference(user_id=4, alert_type=typ, in_app=True, email=True, browser_push=False))
    review_db.commit()
    first = client.post("/jobs", json={"item_name": "Synthetic approve me"}, headers=as_user("student")).json()["id"]
    second = client.post("/jobs", json={"item_name": "Synthetic reject me"}, headers=as_user("student")).json()["id"]
    # Viewers cannot approve or reject.
    assert client.post(f"/jobs/{first}/approve", headers=as_user("student")).status_code == 403
    assert client.post(f"/jobs/{second}/reject", json={"reason": "x"}, headers=as_user("student")).status_code == 403
    assert _status(review_db, first) == "submitted" and _status(review_db, second) == "submitted"
    external.clear()

    assert client.post(f"/jobs/{first}/approve", headers=as_user("operator1")).status_code == 200
    assert _alerts(review_db, "job_approved") == {(1, "job_approved", first)}
    assert external == [("email", 1, "Job approved: Synthetic approve me", "Approved by operator1")]

    assert client.post(f"/jobs/{second}/reject", json={"reason": "   "}, headers=as_user("admin")).status_code == 400
    assert _status(review_db, second) == "submitted"
    rejected = client.post(f"/jobs/{second}/reject", json={"reason": " Too much filament "}, headers=as_user("admin"))
    assert rejected.status_code == 200 and rejected.json()["reason"] == "Too much filament"
    review_db.rollback()
    assert review_db.execute(text("SELECT status, rejected_reason FROM jobs WHERE id=:id"), {"id": second}).one() == ("rejected", "Too much filament")
    assert _alerts(review_db, "job_rejected") == {(1, "job_rejected", second)}
    assert review_db.execute(text("SELECT message FROM alerts WHERE alert_type='job_rejected'")).scalar_one() == "Reason: Too much filament"
    assert external[-1] == ("email", 1, "Job rejected: Synthetic reject me", "Reason: Too much filament")
    assert len(external) == 2


# ---------------------------------------------------------------- F04-4.6-06 operator/admin bypass
@pytest.mark.parametrize("actor", ["operator1", "admin"])
def test_F04_4_6_06_operators_and_admins_bypass_approval(cert, review_db, actor):
    client, as_user, _ = cert
    review_db.execute(text("UPDATE groups SET owner_id=2 WHERE id=1"))
    review_db.commit()
    response = client.post("/jobs", json={"item_name": f"Synthetic {actor} job"}, headers=as_user(actor))
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "pending"
    job = response.json()["id"]
    assert review_db.execute(text("SELECT status, submitted_by FROM jobs WHERE id=:id"), {"id": job}).one() == ("pending", None)
    assert _alerts(review_db, "job_submitted") == set()


# ---------------------------------------------------------------- F04-4.6-07 targeted to group owner (JWT path)
def test_F04_4_6_07_submission_alert_targets_group_owner_only(cert, review_db):
    client, as_user, _ = cert
    review_db.execute(text("UPDATE groups SET owner_id=2 WHERE id=1"))
    review_db.commit()
    job = client.post("/jobs", json={"item_name": "Synthetic owner routed"}, headers=as_user("student")).json()["id"]
    # Owner (2) only; admin 3, operator 6, outsider 4 and foreign admin 5 excluded.
    assert _alerts(review_db, "job_submitted") == {(2, "job_submitted", job)}


# ---------------------------------------------------------------- producer wire capture for the UI cert tests
def _scrub(value):
    if isinstance(value, dict):
        return {k: ("<ts>" if k in ("created_at", "updated_at", "approved_at") and v else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


def test_capture_certD_jobsA_approval_wire(cert, review_db):
    client, as_user, _ = cert
    review_db.execute(text("UPDATE groups SET owner_id=2 WHERE id=1"))
    review_db.commit()
    op, stu, adm = as_user("operator1"), as_user("student"), as_user("admin")
    pending = client.post("/jobs", json={"item_name": "Wire operator pending"}, headers=op).json()["id"]
    submitted = client.post("/jobs", json={"item_name": "Wire student submitted"}, headers=stu).json()["id"]
    to_reject = client.post("/jobs", json={"item_name": "Wire student rejected"}, headers=stu).json()["id"]
    foreign = client.post("/jobs", json={"item_name": "Wire outsider rejected"}, headers=as_user("outsider")).json()["id"]
    reject_req = {"reason": "Wire reason: reduce infill"}
    for jid in (to_reject, foreign):
        assert client.post(f"/jobs/{jid}/reject", json=reject_req, headers=op).status_code == 200
    wire = {
        "scope": "Real JWT HTTP responses from jobs_crud/jobs_lifecycle/jobs_agent/presets/permissions/auth routers; timestamps scrubbed",
        "ids": {"pending": pending, "submitted": submitted, "own_rejected": to_reject, "foreign_rejected": foreign},
        "permissions": client.get("/permissions").json(),
        "me": {u: client.get("/auth/me", headers=as_user(u)).json() for u in ("student", "operator1", "admin")},
        "approval_setting": client.get("/config/require-job-approval", headers=stu).json(),
        "jobs_operator": _scrub(client.get("/jobs", headers=op).json()),
        "jobs_viewer": _scrub(client.get("/jobs", headers=stu).json()),
    }
    approve = client.post(f"/jobs/{submitted}/approve", headers=op)
    wire["approve"] = {"method": "POST", "path": f"/jobs/{submitted}/approve", "status": approve.status_code, "body": _scrub(approve.json())}
    wire["jobs_operator_after_approve"] = _scrub(client.get("/jobs", headers=op).json())
    correction = {"item_name": "Wire student rejected", "quantity": 1, "notes": "Wire corrected", "due_date": None}
    patched = client.patch(f"/jobs/{to_reject}", json=correction, headers=stu)
    wire["correct"] = {"method": "PATCH", "path": f"/jobs/{to_reject}", "request": correction, "status": patched.status_code, "body": _scrub(patched.json())}
    resub = client.post(f"/jobs/{to_reject}/resubmit", headers=stu)
    wire["resubmit"] = {"method": "POST", "path": f"/jobs/{to_reject}/resubmit", "status": resub.status_code, "body": resub.json()}
    # A second submission to reject through the UI contract.
    reject_target = client.post("/jobs", json={"item_name": "Wire reject target"}, headers=stu).json()["id"]
    wire["ids"]["reject_target"] = reject_target
    wire["jobs_operator_reject_view"] = _scrub(client.get("/jobs", headers=op).json())
    rej = client.post(f"/jobs/{reject_target}/reject", json=reject_req, headers=op)
    wire["reject"] = {"method": "POST", "path": f"/jobs/{reject_target}/reject", "request": reject_req, "status": rej.status_code, "body": _scrub(rej.json())}
    put_req = {"enabled": False}
    put = client.put("/config/require-job-approval", json=put_req, headers=adm)
    wire["approval_put"] = {"method": "PUT", "path": "/config/require-job-approval", "request": put_req, "status": put.status_code, "body": put.json()}
    wire["approval_after_put"] = client.get("/config/require-job-approval", headers=adm).json()

    assert approve.status_code == 200 and patched.status_code == 200 and resub.status_code == 200 and rej.status_code == 200 and put.status_code == 200
    mine = {pending, submitted, to_reject, foreign}
    statuses = {j["id"]: j["status"] for j in wire["jobs_operator"] if j["id"] in mine}
    assert statuses == {pending: "pending", submitted: "submitted", to_reject: "rejected", foreign: "rejected"}
    # Producer contract: list rows carry no submitter / rejection / org fields.
    row = next(j for j in wire["jobs_operator"] if j["id"] == to_reject)
    assert not {"submitted_by", "rejected_reason", "charged_to_org_id"} & set(row)
    path = Path(__file__).resolve().parents[2] / "frontend/src/test-fixtures/certD-jobsA-approval-wire.json"
    path.write_text(json.dumps(wire, indent=2, sort_keys=True) + "\n")
