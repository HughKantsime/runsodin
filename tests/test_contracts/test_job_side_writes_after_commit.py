"""Job create and approval hold the database writer only for their own writes.

The alert a create or an approval raises (alert rows, preference seeding, deferred
delivery records, then the WebSocket event and external delivery) is staged after the
commit, in a transaction of its own (alert_dispatcher.dispatch_alert_after_commit).
* a failure in that side write leaves the committed job / approval and its audit intact;
* the audit row stays atomic with the change it records;
* the quota check-and-charge stays one conditional upsert, so concurrent creates against
  one allowance neither overspend nor fail with SQLite busy/snapshot errors.
Runs the real routes on the EDU load fixture in a fresh process (its engine is fixed at import).
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
import json, sys, tempfile, threading
from pathlib import Path
from ops.edu_readiness.api_load import _prepare_database
from starlette.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
app, tokens = _prepare_database(Path(tempfile.mkdtemp()) / "odin.db")
from core.db import engine
from modules.notifications import alert_dispatcher
from modules.jobs.routes import jobs_crud
out = {}
def count(sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar()
viewer = {"Authorization": "Bearer " + tokens["viewer"][0]}
operator = {"Authorization": "Bearer " + tokens["operator"][0]}
real_stage = alert_dispatcher.stage_alert
def failing_stage(*a, **k):
    raise RuntimeError("synthetic side-write failure")
with TestClient(app, raise_server_exceptions=False) as client:
    # (a) side-write failure after commit: the job and its audit stand, no alert row.
    alert_dispatcher.stage_alert = failing_stage
    created = client.post("/api/jobs", headers=viewer, json={"item_name": "side-fail", "quantity": 1, "duration_hours": 0.25})
    job_id = created.json().get("id")
    approved = client.post("/api/jobs/10290/approve", headers=operator)
    alert_dispatcher.stage_alert = real_stage
    out["create_status"] = created.status_code
    out["create_job"] = count("SELECT count(*) FROM jobs WHERE id=:id", id=job_id)
    out["create_audit"] = count("SELECT count(*) FROM audit_logs WHERE action='job.created' AND entity_id=:id", id=job_id)
    out["create_alerts"] = count("SELECT count(*) FROM alerts WHERE job_id=:id", id=job_id)
    out["approve_status"] = approved.status_code
    out["approve_state"] = count("SELECT count(*) FROM jobs WHERE id=10290 AND status='pending' AND approved_by IS NOT NULL")
    out["approve_audit"] = count("SELECT count(*) FROM audit_logs WHERE action='job.approved' AND entity_id=10290")
    out["approve_alerts"] = count("SELECT count(*) FROM alerts WHERE job_id=10290 AND alert_type='job_approved'")
    # Without the failure the side write does land after the commit.
    ok = client.post("/api/jobs/10291/approve", headers=operator)
    out["ok_approve_alerts"] = (ok.status_code, count("SELECT count(*) FROM alerts WHERE job_id=10291 AND alert_type='job_approved'"))
    # (b) audit is atomic with the change: a failure before the commit leaves neither.
    jobs_before = count("SELECT count(*) FROM jobs")
    audits_before = count("SELECT count(*) FROM audit_logs WHERE action='job.created'")
    real_commit = Session.commit
    def failing_commit(self):
        if self.new or self.dirty:
            raise RuntimeError("synthetic failure before commit")
        return real_commit(self)
    Session.commit = failing_commit
    failed = client.post("/api/jobs", headers=viewer, json={"item_name": "commit-fail", "quantity": 1, "duration_hours": 0.25})
    Session.commit = real_commit
    out["atomic"] = (failed.status_code, count("SELECT count(*) FROM jobs") - jobs_before,
                     count("SELECT count(*) FROM audit_logs WHERE action='job.created'") - audits_before)
# (c) concurrent creates against one allowance through the real route.
from modules.jobs.schemas import JobCreate
from starlette.requests import Request
from core.errors import OdinError
with engine.begin() as c:
    c.execute(text("UPDATE users SET quota_jobs=NULL"))
    used = c.execute(text("SELECT COALESCE(SUM(jobs_used),0) FROM quota_usage WHERE user_id=3")).scalar()
actor = {"id": 3, "username": "load-user-3@school.test", "role": "operator", "group_id": 1,
         "quota_jobs": used + 3, "quota_period": "monthly"}
barrier, results = threading.Barrier(8), []
def create(index):
    with Session(engine) as db:
        db.execute(text("SELECT count(*) FROM jobs")).scalar()  # a read before the first write
        barrier.wait()
        try:
            jobs_crud.create_job(JobCreate(item_name=f"race-{index}", quantity=1),
                                 Request({"type": "http", "headers": [], "query_string": b""}), db, actor)
            results.append("accepted")
        except OdinError as exc:
            db.rollback()
            results.append(str(exc.status))
        except Exception as exc:
            db.rollback()
            results.append(type(exc).__name__ + ":" + str(exc)[:80])
threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
out["race"] = sorted(results)
out["race_used"] = count("SELECT COALESCE(SUM(jobs_used),0) FROM quota_usage WHERE user_id=3") - used
print(json.dumps(out))
'''


def _probe(tmp_path):
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, capture_output=True, text=True, timeout=300,
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{ROOT / 'backend'}:{ROOT}",
                                 "HOME": str(tmp_path), "TMPDIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_side_writes_are_after_commit_and_audit_stays_atomic(tmp_path):
    out = _probe(tmp_path)
    # (a) side-write failure: committed job/approval and audit intact, no alert.
    assert (out["create_status"], out["create_job"], out["create_audit"], out["create_alerts"]) == (201, 1, 1, 0)
    assert (out["approve_status"], out["approve_state"], out["approve_audit"], out["approve_alerts"]) == (200, 1, 1, 0)
    assert out["ok_approve_alerts"] == [200, 1]
    # (b) audit atomic with the job: a failure before commit leaves neither.
    assert out["atomic"][0] == 500 and out["atomic"][1:] == [0, 0]
    # (c) concurrent creates on one allowance: exactly the allowance, no busy/snapshot error.
    assert out["race"] == ["429"] * 5 + ["accepted"] * 3, out["race"]
    assert out["race_used"] == 3
