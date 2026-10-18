"""Job create and approval: short writer hold, alerts never lost (transactional outbox).

Inside its transaction a create or an approval writes its job change, its audit and
one alert_outbox row (migration 034). The alert fan-out runs after the commit
(modules.notifications.alert_outbox.dispatch), claiming the row so it is staged once.
* (a) a crash between commit and dispatch: the next start replays the row -> one alert;
* (b) a failed dispatch: the job/approval and audit stand, the sweep later delivers one
  alert, and repeated sweeps never duplicate it;
* audit stays atomic with the change (a failure before commit leaves neither, nor an
  outbox row);
* the quota check-and-charge stays one conditional upsert: concurrent creates against
  one allowance neither overspend nor fail with SQLite busy/snapshot errors.
Runs the real routes on the EDU load fixture in a fresh process (its engine is fixed at import).
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PROBE = r'''
import json, sys, tempfile, threading, time
from pathlib import Path
from ops.edu_readiness.api_load import _prepare_database
from starlette.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
app, tokens = _prepare_database(Path(tempfile.mkdtemp()) / "odin.db")
from core.db import engine
from modules.notifications import alert_dispatcher, alert_outbox
from modules.jobs.routes import jobs_crud
out = {}
def count(sql, **params):
    with engine.connect() as c:
        return c.execute(text(sql), params).scalar()
def alerts(job, kind):
    return count("SELECT count(*) FROM alerts WHERE job_id=:j AND alert_type=:k", j=job, k=kind)
viewer = {"Authorization": "Bearer " + tokens["viewer"][0]}
operator = {"Authorization": "Bearer " + tokens["operator"][0]}
real_dispatch, real_stage = alert_outbox.dispatch, alert_dispatcher.stage_alert

# (a) crash between commit and dispatch, then a restart.
client = TestClient(app, raise_server_exceptions=False)
alert_outbox.dispatch = lambda engine, outbox_id: False  # the process dies before dispatching
created = client.post("/api/jobs", headers=viewer, json={"item_name": "crash", "quantity": 1, "duration_hours": 0.25})
approved = client.post("/api/jobs/10290/approve", headers=operator)
alert_outbox.dispatch = real_dispatch
crash_job = created.json()["id"]
out["crash_before_restart"] = (created.status_code, approved.status_code, count("SELECT count(*) FROM alert_outbox"),
                               alerts(crash_job, "queue_added") + alerts(crash_job, "job_submitted"),
                               alerts(10290, "job_approved"))
with TestClient(app):  # restart: the lifespan sweeper replays at startup
    deadline = time.monotonic() + 10
    while count("SELECT count(*) FROM alert_outbox") and time.monotonic() < deadline:
        time.sleep(0.05)
out["crash_after_restart"] = (count("SELECT count(*) FROM alert_outbox"),
                              alerts(crash_job, "queue_added") + alerts(crash_job, "job_submitted"),
                              alerts(10290, "job_approved"))
alert_outbox.sweep(engine, older_than_seconds=0)
out["crash_after_second_sweep"] = (alerts(crash_job, "queue_added") + alerts(crash_job, "job_submitted"),
                                   alerts(10290, "job_approved"))

# (b) dispatch failure, then the sweep.
def failing_stage(*a, **k):
    raise RuntimeError("synthetic side-write failure")
alert_dispatcher.stage_alert = failing_stage
created = client.post("/api/jobs", headers=viewer, json={"item_name": "side-fail", "quantity": 1, "duration_hours": 0.25})
approved = client.post("/api/jobs/10291/approve", headers=operator)
alert_dispatcher.stage_alert = real_stage
job_id = created.json().get("id")
kinds = lambda j: alerts(j, "queue_added") + alerts(j, "job_submitted")
out["failure"] = (created.status_code, count("SELECT count(*) FROM jobs WHERE id=:id", id=job_id),
                  count("SELECT count(*) FROM audit_logs WHERE action='job.created' AND entity_id=:id", id=job_id), kinds(job_id),
                  approved.status_code, count("SELECT count(*) FROM jobs WHERE id=10291 AND status='pending' AND approved_by IS NOT NULL"),
                  count("SELECT count(*) FROM audit_logs WHERE action='job.approved' AND entity_id=10291"), alerts(10291, "job_approved"),
                  count("SELECT max(attempts) FROM alert_outbox"))
replayed = [alert_outbox.sweep(engine, older_than_seconds=0) for _ in range(3)]
out["after_sweeps"] = (replayed, kinds(job_id), alerts(10291, "job_approved"), count("SELECT count(*) FROM alert_outbox"))
# Two dispatchers racing for one row stage it once.
alert_outbox.dispatch = lambda engine, outbox_id: False
raced = client.post("/api/jobs", headers=viewer, json={"item_name": "raced", "quantity": 1, "duration_hours": 0.25}).json()["id"]
alert_outbox.dispatch = real_dispatch
row = count("SELECT id FROM alert_outbox WHERE job_id=:id", id=raced)
barrier, wins = threading.Barrier(4), []
def race():
    barrier.wait(); wins.append(alert_outbox.dispatch(engine, row))
threads = [threading.Thread(target=race) for _ in range(4)]
[t.start() for t in threads]; [t.join() for t in threads]
out["race"] = (sorted(wins), kinds(raced))

# Audit (and the outbox row) are atomic with the job: a failure before commit leaves none.
jobs_before, audits_before = count("SELECT count(*) FROM jobs"), count("SELECT count(*) FROM audit_logs WHERE action='job.created'")
outbox_before = count("SELECT count(*) FROM alert_outbox")
real_commit = Session.commit
def failing_commit(self):
    if self.new or self.dirty:
        raise RuntimeError("synthetic failure before commit")
    return real_commit(self)
Session.commit = failing_commit
failed = client.post("/api/jobs", headers=viewer, json={"item_name": "commit-fail", "quantity": 1, "duration_hours": 0.25})
Session.commit = real_commit
out["atomic"] = (failed.status_code, count("SELECT count(*) FROM jobs") - jobs_before,
                 count("SELECT count(*) FROM audit_logs WHERE action='job.created'") - audits_before,
                 count("SELECT count(*) FROM alert_outbox") - outbox_before)

# Concurrent creates against one allowance through the real route.
from modules.jobs.schemas import JobCreate
from starlette.requests import Request
from core.errors import OdinError
with engine.connect() as c:
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
            db.rollback(); results.append(str(exc.status))
        except Exception as exc:
            db.rollback(); results.append(type(exc).__name__ + ":" + str(exc)[:80])
threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
[t.start() for t in threads]; [t.join() for t in threads]
out["quota_race"] = sorted(results)
out["quota_used"] = count("SELECT COALESCE(SUM(jobs_used),0) FROM quota_usage WHERE user_id=3") - used
print(json.dumps(out))
'''


def _probe(tmp_path):
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, capture_output=True, text=True, timeout=300,
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{ROOT / 'backend'}:{ROOT}",
                                 "HOME": str(tmp_path), "TMPDIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_alerts_survive_crash_and_failure_exactly_once_with_audit_atomic(tmp_path):
    out = _probe(tmp_path)
    # (a) crash: committed job/approval with a pending row and no alert; restart -> exactly one.
    assert out["crash_before_restart"] == [201, 200, 2, 0, 0]
    assert out["crash_after_restart"] == [0, 1, 1]
    assert out["crash_after_second_sweep"] == [1, 1]
    # (b) failed dispatch: committed job, audit and approval stand; rows wait with an attempt counted.
    assert out["failure"] == [201, 1, 1, 0, 200, 1, 1, 0, 1]
    # The sweep delivers each once; further sweeps have nothing to replay.
    assert out["after_sweeps"] == [[2, 0, 0], 1, 1, 0]
    assert out["race"] == [[False, False, False, True], 1]
    # Audit and outbox row are atomic with the job.
    assert out["atomic"][0] == 500 and out["atomic"][1:] == [0, 0, 0]
    # Concurrent creates on one allowance: exactly the allowance, no busy/snapshot error.
    assert out["quota_race"] == ["429"] * 5 + ["accepted"] * 3, out["quota_race"]
    assert out["quota_used"] == 3
