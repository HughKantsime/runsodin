"""Claims F21-21.3-04 (PrusaLink HMS history), F21-21.4-04 (Elegoo failure detection),
F04-4.2-05 (auto-create jobs on completion).

Runs SCENARIO_CODE in an isolated subprocess (temp SQLite,
generated secrets, network refused) and asserts on the observed database effects.
Defects are recorded as strict xfails so they flip to failures once fixed.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / 'artifacts' / 'application-certification' / 'claims-protocols-backend'

SCENARIO_CODE = r'''
"""Owned monitor scenarios for backend claims (run as a subprocess with an isolated DATABASE_URL).

Drives the real PrusaLink / Elegoo monitor state machines with statuses produced by the
real protocol parsers, and the real Bambu job-end lifecycle, against a bootstrapped
SQLite store. Prints one JSON line of observed database effects.
"""
import copy
import json
import os
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.setdefault('JWT_SECRET_KEY', secrets.token_urlsafe(48))
os.environ.setdefault('ENCRYPTION_KEY', Fernet.generate_key().decode())
_original = socket.socket


class _LocalOnlySocket(_original):
    def __init__(self, family=socket.AF_INET, *args, **kwargs):
        if family in (socket.AF_INET, socket.AF_INET6):
            raise OSError('network refused in owned claim scenario')
        super().__init__(family, *args, **kwargs)


socket.socket = _LocalOnlySocket

from sqlalchemy import text  # noqa: E402
from core.db import engine  # noqa: E402
from core.schema import bootstrap_database  # noqa: E402

bootstrap_database(engine, Path('backend').resolve())

from modules.printers.adapters.prusalink import PrusaLinkPrinter, PrusaLinkStatus  # noqa: E402
from modules.printers.adapters.elegoo import ElegooPrinter  # noqa: E402
from modules.printers.parsing.prusalink import parse_v1_status  # noqa: E402
from modules.printers.monitors import prusalink_monitor, elegoo_monitor  # noqa: E402

for module in (prusalink_monitor, elegoo_monitor):
    module.ws_push = lambda *a, **kw: None
    module._mqtt_republish = None


def q(sql, **params):
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text(sql), params).fetchall()]


def add_printer(pid, api_type):
    with engine.begin() as c:
        c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared) "
                       "VALUES(:id,:name,:t,1,1,'[]',0,0)"), {'id': pid, 'name': f'claim-{api_type}-{pid}', 't': api_type})


def snapshot(pid):
    return {
        'print_jobs': q('SELECT id,status,job_name,scheduled_job_id FROM print_jobs WHERE printer_id=:p ORDER BY id', p=pid),
        'jobs': q('SELECT id,item_name,status FROM jobs WHERE printer_id=:p ORDER BY id', p=pid),
        'hms_error_history': q('SELECT code,message,severity,source FROM hms_error_history WHERE printer_id=:p', p=pid),
        'printer_last_error': q('SELECT last_error_code,last_error_message FROM printers WHERE id=:p', p=pid),
        'alerts': q('SELECT alert_type,severity FROM alerts WHERE printer_id=:p ORDER BY id', p=pid),
    }


def settle():
    time.sleep(0.3)
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(timeout=2)


def prusa_status(state, message=None):
    raw = {'printer': {'state': state, 'temp_bed': 60.0, 'temp_nozzle': 210.0},
           'job': {'id': 42, 'progress': 50.0 if state == 'PRINTING' else 0.0, 'time_printing': 300,
                   'time_remaining': 600, 'file': {'display': 'claim-prusa.gcode'}}}
    if message:
        raw['printer']['status_printer'] = {'message': message}
    status = PrusaLinkStatus()
    PrusaLinkPrinter._apply_parsed(status, parse_v1_status(raw))
    return status


def run_prusa(pid, states, message=None):
    add_printer(pid, 'prusalink')
    monitor = prusalink_monitor.PrusaLinkMonitorThread(pid, f'claim-{pid}', '127.0.0.1')
    monitor._try_dispatch = lambda: None
    for state in states:
        monitor._process_status(prusa_status(state, message if state == 'ERROR' else None))
        settle()
    return snapshot(pid)


SDCP = {  # (CurrentStatus, PrintInfo.Status) as decoded by parsing/elegoo.py
    'PRINTING': (1, 8), 'PAUSED': (0, 5), 'STOPPING': (0, 7), 'FINISHED': (0, 16), 'IDLE': (0, 0), 'HEATING': (8, 0),
}


def run_elegoo(pid, states):
    add_printer(pid, 'elegoo')
    monitor = elegoo_monitor.ElegooMonitorThread(pid, f'claim-{pid}', '127.0.0.1')
    monitor._try_dispatch = lambda: None
    adapter = ElegooPrinter('127.0.0.1')
    observed = []
    for name in states:
        current, print_status = SDCP[name]
        adapter._parse_status({'Topic': 'sdcp/status/x', 'Status': {'CurrentStatus': [current], 'PrintInfo': {
            'Status': print_status, 'Filename': 'claim-elegoo.gcode', 'CurrentTicks': 100, 'TotalTicks': 200}}})
        status = copy.copy(adapter._latest_status)
        observed.append(status.internal_state)
        monitor._process_status(status)
        settle()
    result = snapshot(pid)
    result['internal_states'] = observed
    return result


def run_bambu_unlinked_completion(pid):
    from modules.printers.monitors import mqtt_job_lifecycle
    add_printer(pid, 'bambu')
    with engine.begin() as c:
        c.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status) "
                       "VALUES(:id,:p,'claim.3mf','claim-bambu','2026-10-14 10:00:00','running')"), {'id': pid, 'p': pid})
    linked = mqtt_job_lifecycle.record_job_ended(pid, f'claim-{pid}', pid, None, 'completed',
                                                 {'subtask_name': 'claim-bambu'}, {}, lambda **kw: None)
    settle()
    result = snapshot(pid)
    result['returned_linked_job_id'] = linked
    return result


def run_prusa_unlinked_completion(pid):
    return run_prusa(pid, ['PRINTING', 'FINISHED'])


results = {
    'prusalink_error': run_prusa(301, ['IDLE', 'PRINTING', 'ERROR'], message='Thermal runaway (synthetic)'),
    'elegoo_printing_to_idle': run_elegoo(401, ['IDLE', 'PRINTING', 'IDLE']),
    'elegoo_printing_stopping_idle': run_elegoo(402, ['IDLE', 'PRINTING', 'STOPPING', 'IDLE']),
    'elegoo_printing_paused_idle': run_elegoo(403, ['IDLE', 'PRINTING', 'PAUSED', 'IDLE']),
    'elegoo_printing_finished': run_elegoo(404, ['IDLE', 'PRINTING', 'FINISHED', 'IDLE']),
    'bambu_unlinked_completion': run_bambu_unlinked_completion(501),
    'prusalink_unlinked_completion': run_prusa_unlinked_completion(302),
}
print('SCENARIO_JSON=' + json.dumps(results, default=str))
engine.dispose()
'''


@pytest.fixture(scope='module')
def scenarios(tmp_path_factory):
    directory = tmp_path_factory.mktemp('claims-monitor-owned')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + str(directory / 'app.db'), LICENSE_DIR=str(directory),
               API_KEY='', ODIN_ITAR_MODE='0', ODIN_TELEMETRY_V2='0', PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}")
    result = subprocess.run([sys.executable, '-c', SCENARIO_CODE],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-3000:]
    line = [l for l in result.stdout.splitlines() if l.startswith('SCENARIO_JSON=')][-1]
    data = json.loads(line[len('SCENARIO_JSON='):])
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / 'monitor-lifecycle-scenarios.json').write_text(json.dumps(data, indent=1))
    return data


# --- PrusaLink error recording -------------------------------------------------

def test_prusalink_error_transition_fails_job_and_sets_printer_last_error(scenarios):
    s = scenarios['prusalink_error']
    assert [r[1] for r in s['print_jobs']] == ['failed']
    assert s['printer_last_error'] == [['PRUSALINK_ERROR', 'Thermal runaway (synthetic)']]


def test_prusalink_error_transition_is_recorded_in_hms_error_history(scenarios):
    rows = scenarios['prusalink_error']['hms_error_history']
    assert any(r[0] == 'PRUSALINK_ERROR' and r[3] == 'prusalink' for r in rows)


# --- Elegoo failure detection ---------------------------------------------------

def test_elegoo_printing_to_idle_is_detected_as_failure(scenarios):
    s = scenarios['elegoo_printing_to_idle']
    assert s['internal_states'] == ['IDLE', 'PRINTING', 'IDLE']
    assert [r[1] for r in s['print_jobs']] == ['failed']
    assert s['printer_last_error'] == [['PRINT_FAILED', 'Print stopped unexpectedly']]


def test_elegoo_printing_to_finished_is_completed(scenarios):
    assert [r[1] for r in scenarios['elegoo_printing_finished']['print_jobs']] == ['completed']


def test_elegoo_stopped_print_is_not_recorded_as_success(scenarios):
    s = scenarios['elegoo_printing_stopping_idle']
    assert s['internal_states'] == ['IDLE', 'PRINTING', 'STOPPING', 'IDLE']
    assert [r[1] for r in s['print_jobs']] != ['completed']


def test_elegoo_print_ending_from_paused_reaches_terminal_state(scenarios):
    s = scenarios['elegoo_printing_paused_idle']
    assert [r[1] for r in s['print_jobs']] != ['running']


# --- Auto-create jobs on completion -----------------------------------------------

def test_bambu_unlinked_completion_auto_creates_and_links_job(scenarios):
    s = scenarios['bambu_unlinked_completion']
    assert s['jobs'] == [[1, 'claim-bambu', 'completed']]
    assert s['print_jobs'] == [[501, 'completed', 'claim-bambu', 1]]
    assert s['returned_linked_job_id'] == 1


# Documented limitation (FEATURES.md 4.2: "Auto-create jobs on Bambu print completion"): the
# generic job_events.job_completed path (PrusaLink/Elegoo) records the print but creates no
# jobs row for an unlinked print; only Bambu's mqtt_job_lifecycle does.
@pytest.mark.parametrize('scenario', ['prusalink_unlinked_completion', 'elegoo_printing_finished'])
def test_non_bambu_unlinked_completion_records_the_print_but_creates_no_job(scenarios, scenario):
    s = scenarios[scenario]
    assert [r[1] for r in s['print_jobs']] == ['completed']
    assert s['jobs'] == []
