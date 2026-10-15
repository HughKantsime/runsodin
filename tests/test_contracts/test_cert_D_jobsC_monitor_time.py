"""cert-D jobsC F04-4.7-01: actual print duration alongside the slicer estimate at the real monitor boundary.

Drives the real Bambu MQTT job-end lifecycle (modules.printers.monitors.mqtt_job_lifecycle.record_job_ended)
for a scheduled job that carries a slicer estimate, in an isolated subprocess (temp SQLite, generated
secrets, network refused). The physical printer is the only stubbed boundary.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

SCENARIO = r'''
import json, os, secrets, socket, time, threading
from pathlib import Path
from cryptography.fernet import Fernet
os.environ.setdefault('JWT_SECRET_KEY', secrets.token_urlsafe(48))
os.environ.setdefault('ENCRYPTION_KEY', Fernet.generate_key().decode())
_original = socket.socket
class _LocalOnly(_original):
    def __init__(self, family=socket.AF_INET, *a, **k):
        if family in (socket.AF_INET, socket.AF_INET6):
            raise OSError('network refused')
        super().__init__(family, *a, **k)
socket.socket = _LocalOnly
from sqlalchemy import text
from core.db import engine
from core.schema import bootstrap_database
bootstrap_database(engine, Path('backend').resolve())
from modules.printers.monitors import mqtt_job_lifecycle
with engine.begin() as c:
    c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared) VALUES(77,'certC-bambu','bambu',1,1,'[]',0,0)"))
    c.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,duration_hours,quantity,hold,priority,actual_start,scheduled_start,scheduled_end) "
                   "VALUES(770,'certC timed',:s,77,2.0,1,0,3,'2026-10-14 08:00:00','2026-10-14 08:00:00','2026-10-14 10:00:00')"), {'s': os.environ.get('CERTC_JOB_STATUS', 'printing')})
    c.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                   "VALUES(7700,77,'certc.3mf','certC timed','2026-10-14 08:00:00','running',770)"))
linked = mqtt_job_lifecycle.record_job_ended(77, 'certC-bambu', 7700, 770, 'completed', {'subtask_name': 'certC timed'}, {}, lambda **kw: None)
time.sleep(0.3)
for t in threading.enumerate():
    if t is not threading.current_thread() and t.daemon:
        t.join(timeout=2)
with engine.connect() as c:
    job = dict(c.execute(text('SELECT status,duration_hours,actual_start,actual_end FROM jobs WHERE id=770')).mappings().one())
    pj = dict(c.execute(text('SELECT status,print_duration_seconds,ended_at FROM print_jobs WHERE id=7700')).mappings().one())
print('SCENARIO_JSON=' + json.dumps({'linked': linked, 'job': job, 'print_job': pj}, default=str))
engine.dispose()
'''


def _run(tmp_path_factory, job_status='printing'):
    directory = tmp_path_factory.mktemp('certD-jobsC-monitor')
    env = dict(os.environ, CERTC_JOB_STATUS=job_status, DATABASE_URL='sqlite:///' + str(directory / 'app.db'), LICENSE_DIR=str(directory),
               API_KEY='', ODIN_ITAR_MODE='0', ODIN_TELEMETRY_V2='0', PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}")
    result = subprocess.run([sys.executable, '-c', SCENARIO], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-3000:]
    line = [l for l in result.stdout.splitlines() if l.startswith('SCENARIO_JSON=')][-1]
    return json.loads(line[len('SCENARIO_JSON='):])


@pytest.fixture(scope='module')
def ended(tmp_path_factory):
    return _run(tmp_path_factory)


def test_print_end_never_overwrites_an_operator_terminal_status(tmp_path_factory):
    """Operator pressed Mark failed while printing; the later FINISH must not flip the job to completed."""
    result = _run(tmp_path_factory, 'failed')
    assert result['job']['status'] == 'failed'
    assert result['print_job']['status'] == 'completed'  # the physical print outcome is still recorded


def test_monitor_end_records_actual_end_and_print_duration(ended):
    assert ended['linked'] == 770
    assert ended['job']['status'] == 'completed'
    assert ended['job']['actual_start'].startswith('2026-10-14 08:00:00') and ended['job']['actual_end']
    assert ended['print_job']['status'] == 'completed' and ended['print_job']['print_duration_seconds'] > 0


def test_monitor_end_keeps_slicer_estimate_alongside_actual(ended):
    assert ended['job']['duration_hours'] == 2.0
