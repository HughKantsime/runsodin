"""Connections a job create opens, pinned (cv06 api_load).

On SQLite every new connection re-parses the 2.0 schema (about 1.4 ms), and the engine
opens one per session (NullPool). A job create opened five: the request session, the
org quiet-hours lookup, and three after commit (alert scope, the WebSocket event row and
browser-push delivery). The quiet-hours lookup now reuses the request session and the
after-commit alert delivery runs on one dispatch connection, so a create opens two.
Measured in steady state (configuration caches warm) in a fresh process, as the EDU
load fixture requires."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROBE = r'''
import json, sys, tempfile, traceback
from pathlib import Path
from ops.edu_readiness.api_load import _prepare_database
from starlette.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import Pool
app, tokens = _prepare_database(Path(tempfile.mkdtemp()) / "odin.db")
opened = []
@event.listens_for(Pool, "connect")
def connect(*_):
    opened.append(" <- ".join(f"{Path(f.filename).name}:{f.name}" for f in traceback.extract_stack()
                              if "/backend/" in f.filename)[-200:])
with TestClient(app) as client:
    headers = {"Authorization": "Bearer " + tokens["viewer"][0]}
    for index in range(3):  # warm the policy, quiet-hours, republish and VAPID caches
        client.post("/api/jobs", headers=headers, json={"item_name": f"warm-{index}", "quantity": 1, "duration_hours": 0.25})
    opened.clear()
    response = client.post("/api/jobs", headers=headers, json={"item_name": "measured", "quantity": 1, "duration_hours": 0.25})
    print(json.dumps({"status": response.status_code, "opened": opened}))
'''


def test_a_job_create_opens_two_database_connections(tmp_path):
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, capture_output=True, text=True, timeout=300,
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{ROOT / 'backend'}:{ROOT}",
                                 "HOME": str(tmp_path), "TMPDIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr[-3000:]
    measured = json.loads(result.stdout.strip().splitlines()[-1])
    assert measured["status"] == 201
    assert len(measured["opened"]) == 2, measured["opened"]
