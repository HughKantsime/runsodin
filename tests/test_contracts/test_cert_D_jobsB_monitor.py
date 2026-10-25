"""cert-D jobsB: real Bambu monitor job matching and completion paths.

Claims: F04-4.2-03 (time-window job matching), F04-4.2-04 (reactive per-printer stale
cleanup on print start), F04-4.2-05 (auto-create jobs on Bambu print completion for
reprint tracking). Only the MQTT transport (paho client) is stubbed; persisted effects
are read from a bootstrapped SQLite schema. Python clocks are pinned.
"""
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime.now(timezone.utc).replace(microsecond=0)

MATCHED_DEFECT = (
    "DEFECT: backend/modules/printers/monitors/mqtt_job_lifecycle.py:242-251 links a name/window-matched "
    "scheduled job (jobs.status='printing') but never records print_jobs.scheduled_job_id; at FINISH "
    "mqtt_job_lifecycle.py:409 re-reads linkage from print_jobs (None), so :452 auto-creates a duplicate "
    "completed job and the matched job stays 'printing' forever")


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)


@pytest.fixture
def lifecycle(review_db, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers.monitors import mqtt_job_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    monkeypatch.setattr(mqtt_job_lifecycle, 'datetime', FixedClock)
    engine = review_db.get_bind()

    @contextmanager
    def isolated_db():
        connection = engine.raw_connection()
        try:
            yield _ConnectionAdapter(connection, False)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', isolated_db)
    alerts, reschedules = [], []

    def start(printer_id=9, name='Unmatched synthetic file'):
        return mqtt_job_lifecycle.record_job_started(
            printer_id, 'Synthetic', {'subtask_name': name, 'gcode_file': 'Metadata/plate_1.gcode',
                                      'job_id': 'synthetic-task', 'total_layer_num': 0},
            lambda **kwargs: alerts.append(kwargs), lambda: reschedules.append(True))
    return review_db, start, alerts, reschedules


def scheduled(db, job_id, printer_id, start):
    db.execute(text(
        "INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,duration_hours,hold,priority,printer_id,"
        "scheduled_start,scheduled_end) VALUES(:id,:name,'scheduled',1,1,1,FALSE,3,:printer,:start,:end)"),
        {'id': job_id, 'name': f'Queued part {job_id}', 'printer': printer_id,
         'start': start.replace(tzinfo=None), 'end': (start + timedelta(hours=1)).replace(tzinfo=None)})
    db.commit()


def status(db, ids):
    db.expire_all()
    return {r[0]: (r[1], r[2]) for r in db.execute(text(
        f"SELECT id,status,printer_id FROM jobs WHERE id IN ({','.join(map(str, ids))})")).all()}


# ---------------- F04-4.2-03 time-window matching ----------------

@pytest.mark.parametrize('offset', [timedelta(minutes=90), timedelta(minutes=-90), timedelta(seconds=7200)],
                         ids=['plus90m', 'minus90m', 'plus2h_boundary'])
def test_sole_scheduled_job_inside_two_hour_window_is_linked(lifecycle, offset):
    db, start, alerts, reschedules = lifecycle
    scheduled(db, 801, 9, NOW + offset)
    print_job, linked, _ = start()
    assert print_job is not None and linked == 801
    assert status(db, [801])[801] == ('printing', 9)
    assert not alerts and not reschedules


@pytest.mark.parametrize('offset', [timedelta(seconds=7201), timedelta(hours=3)], ids=['plus2h1s', 'plus3h'])
def test_scheduled_job_outside_window_is_not_linked_and_is_bumped(lifecycle, offset):
    db, start, alerts, reschedules = lifecycle
    scheduled(db, 801, 9, NOW + offset)
    print_job, linked, _ = start()
    assert print_job is not None and linked is None
    assert status(db, [801])[801] == ('pending', None)
    assert [a['metadata']['reason'] for a in alerts] == ['ad_hoc_print'] and reschedules == [True]


def test_two_scheduled_jobs_inside_window_are_ambiguous_and_not_linked(lifecycle):
    db, start, alerts, _ = lifecycle
    scheduled(db, 801, 9, NOW + timedelta(minutes=30))
    scheduled(db, 802, 9, NOW + timedelta(minutes=60))
    _, linked, _ = start()
    assert linked is None
    assert status(db, [801, 802]) == {801: ('pending', None), 802: ('pending', None)}
    assert alerts[0]['metadata'] == {'bumped_job_ids': [801, 802], 'reason': 'ad_hoc_print',
                                     'ad_hoc_file': 'Unmatched synthetic file'}


def test_window_only_considers_jobs_scheduled_on_the_reporting_printer(lifecycle):
    db, start, _, _ = lifecycle
    scheduled(db, 801, 10, NOW + timedelta(minutes=30))
    _, linked, _ = start(printer_id=9)
    assert linked is None
    assert status(db, [801])[801] == ('scheduled', 10)


def test_window_matched_job_is_completed_by_its_print_end(lifecycle):
    """Window match must survive to the real job-end handler for the same print attempt."""
    from modules.printers.monitors import mqtt_job_lifecycle
    db, start, _, _ = lifecycle
    scheduled(db, 801, 9, NOW + timedelta(minutes=30))
    print_job, linked, weights = start()
    assert linked == 801
    final = mqtt_job_lifecycle.record_job_ended(9, 'Synthetic', print_job, linked, 'completed',
                                                {'subtask_name': 'Unmatched synthetic file'}, weights, lambda **kw: None)
    observed = (final, status(db, [801])[801], db.execute(text('SELECT COUNT(*) FROM jobs WHERE printer_id=9')).scalar_one())
    assert observed == (801, ('completed', 9), 1)


# ---------------- F04-4.2-04 reactive per-printer stale cleanup ----------------

def test_print_start_sweeps_only_this_printers_stale_schedules_then_links_window_job(lifecycle):
    db, start, alerts, reschedules = lifecycle
    scheduled(db, 701, 9, NOW - timedelta(hours=3))      # stale on reporting printer
    scheduled(db, 702, 10, NOW - timedelta(hours=3))     # stale on another printer: untouched here
    scheduled(db, 703, 9, NOW - timedelta(minutes=60))   # inside window on reporting printer
    _, linked, _ = start(printer_id=9)
    assert status(db, [701, 702, 703]) == {701: ('pending', None), 702: ('scheduled', 10), 703: ('printing', 9)}
    assert linked == 703
    assert alerts == [{'alert_type': 'queue_skipped', 'severity': 'info',
                       'title': 'Stale schedule swept on Synthetic',
                       'message': 'Jobs reset to pending (past 2hr window): Queued part 701',
                       'metadata': {'bumped_job_ids': [701], 'reason': 'stale_schedule'}}]
    assert reschedules == [True]
    row = db.execute(text('SELECT scheduled_start,scheduled_end,match_score FROM jobs WHERE id=701')).one()
    assert tuple(row) == (None, None, None)


# ---------------- F04-4.2-05 auto-create jobs on Bambu completion ----------------

SCENARIO = r'''
import json, os, secrets, socket, sys, threading, time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from cryptography.fernet import Fernet
os.environ.setdefault('JWT_SECRET_KEY', secrets.token_urlsafe(48))
os.environ.setdefault('ENCRYPTION_KEY', Fernet.generate_key().decode())
_socket = socket.socket
class LocalOnly(_socket):
    def __init__(self, family=socket.AF_INET, *a, **k):
        if family in (socket.AF_INET, socket.AF_INET6):
            raise OSError('network refused in owned scenario')
        super().__init__(family, *a, **k)
socket.socket = LocalOnly

from sqlalchemy import text
from core.db import engine
from core.schema import bootstrap_database
bootstrap_database(engine, Path('backend').resolve())
from core.registry import registry
from modules.jobs.queue_creation_provider import QueueCreationService
from modules.organizations.services import EducationPolicyService
registry.register_provider('QueueCreationProvider', QueueCreationService())
registry.register_provider('EducationPolicyProvider', EducationPolicyService())

UTC = timezone.utc
CLOCK = {'now': datetime(2026, 10, 14, 10, 0, tzinfo=UTC)}
class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return CLOCK['now'] if tz is None else CLOCK['now'].astimezone(tz)
from modules.printers.monitors import mqtt_job_lifecycle, mqtt_printer
mqtt_job_lifecycle.datetime = Clock
mqtt_printer.ws_push = lambda *a, **k: None
mqtt_printer.mqtt_republish = None

# Transport boundary: paho MQTT client replaced by an in-memory broker connection.
import modules.printers.adapters.bambu as bambu
CLIENTS = []
class FakeMQTTClient:
    def __init__(self, client_id=None, protocol=None, **kw):
        self.client_id = client_id; self.published = []; self.subscribed = []; CLIENTS.append(self)
    def username_pw_set(self, username, password): self.username = username
    def tls_set(self, **kw): pass
    def tls_insecure_set(self, value): pass
    def connect(self, host, port, keepalive=60): self.endpoint = (host, port)
    def loop_start(self): self.on_connect(self, None, {}, 0)
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, topic): self.subscribed.append(topic)
    def publish(self, topic, payload, qos=0):
        self.published.append((topic, json.loads(payload)))
        return SimpleNamespace(rc=0, wait_for_publish=lambda timeout=None: None, is_published=lambda: True)
bambu.mqtt.Client = FakeMQTTClient

def deliver(client, serial, section, when):
    CLOCK['now'] = when
    client.on_message(client, None, SimpleNamespace(topic=f'device/{serial}/report', retain=False,
                      payload=json.dumps({'print': section}).encode()))

def settle():
    time.sleep(0.3)
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon and t.name != 'MainThread':
            t.join(timeout=1)

def q(sql, **p):
    with engine.connect() as c:
        return [dict(r) for r in c.execute(text(sql), p).mappings()]

def push(state, name, percent, layer, **extra):
    section = {'command': 'push_status', 'msg': 0, 'sequence_id': str(percent), 'gcode_state': state,
               'subtask_name': name, 'gcode_file': '/data/Metadata/plate_1.gcode', 'job_id': '418273',
               'task_id': '418272', 'print_type': 'local', 'mc_percent': percent,
               'mc_remaining_time': 0 if state == 'FINISH' else 95, 'layer_num': layer, 'total_layer_num': 210,
               'bed_temper': 60.0, 'bed_target_temper': 60.0, 'nozzle_temper': 220.0,
               'nozzle_target_temper': 220.0, 'print_error': 0, 'hms': [], 'stg_cur': 2}
    section.update(extra)
    return section

def run_print(pid, serial, name, terminal='FINISH'):
    with engine.begin() as c:
        c.execute(text("INSERT INTO printers(id,name,model,machine_type,api_type,slot_count,is_active,tags,"
                       "timelapse_enabled,shared,org_id) VALUES(:id,:n,'Bambu Lab P1S','P1S','bambu',1,1,'[]',0,0,NULL)"),
                  {'id': pid, 'n': f'Owned P1S {pid}'})
    monitor = mqtt_printer.PrinterMonitor(pid, f'Owned P1S {pid}', '192.0.2.10', serial, 'synthetic-code')
    monitor._camera_discovered = True
    assert monitor.connect(), 'monitor did not connect through the stubbed transport'
    client = CLIENTS[-1]
    deliver(client, serial, push('RUNNING', name, 1, 1), datetime(2026, 10, 14, 10, 0, tzinfo=UTC))
    settle()
    deliver(client, serial, push('RUNNING', name, 55, 120), datetime(2026, 10, 14, 10, 50, tzinfo=UTC))
    deliver(client, serial, push(terminal, name, 100, 210), datetime(2026, 10, 14, 11, 30, tzinfo=UTC))
    settle()
    return {
        'subscribed': client.subscribed, 'endpoint': list(client.endpoint), 'username': client.username,
        'print_jobs': q('SELECT id,status,job_name,started_at,ended_at,scheduled_job_id FROM print_jobs WHERE printer_id=:p', p=pid),
        'jobs': q('SELECT id,item_name,printer_id,status,actual_start,actual_end,duration_hours,quantity,quantity_on_bed,'
                  'hold,is_locked,priority,model_id FROM jobs WHERE printer_id=:p ORDER BY id', p=pid),
        'alerts': q("SELECT alert_type,job_id FROM alerts WHERE printer_id=:p ORDER BY id", p=pid),
        'tracked_after': monitor._current_job_id,
    }

results = {}
results['unlinked'] = run_print(601, 'OWNEDSERIAL601', 'Reprint Bracket v2')
# Linked: a scheduled job on the printer whose name matches the print is completed in place.
real_now = datetime.now(UTC).replace(tzinfo=None)
def linked():
    with engine.begin() as c:
        c.execute(text("INSERT INTO printers(id,name,model,machine_type,api_type,slot_count,is_active,tags,"
                       "timelapse_enabled,shared) VALUES(602,'Owned P1S 602','Bambu Lab P1S','P1S','bambu',1,1,'[]',0,0)"))
        c.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,quantity,duration_hours,hold,priority,"
                       "scheduled_start) VALUES(950,'Linked Gear','scheduled',602,1,1,FALSE,3,:s)"), {'s': real_now})
    monitor = mqtt_printer.PrinterMonitor(602, 'Owned P1S 602', '192.0.2.11', 'OWNEDSERIAL602', 'synthetic-code')
    monitor._camera_discovered = True
    assert monitor.connect()
    client = CLIENTS[-1]
    deliver(client, 'OWNEDSERIAL602', push('RUNNING', 'Linked Gear', 1, 1), datetime(2026, 10, 14, 12, 0, tzinfo=UTC))
    settle()
    deliver(client, 'OWNEDSERIAL602', push('FINISH', 'Linked Gear', 100, 210), datetime(2026, 10, 14, 13, 0, tzinfo=UTC))
    settle()
    return {'jobs': q('SELECT id,item_name,status,duration_hours FROM jobs WHERE printer_id=602 ORDER BY id'),
            'print_jobs': q('SELECT status,scheduled_job_id FROM print_jobs WHERE printer_id=602')}
results['linked'] = linked()

# Reprint tracking: the auto-created job is visible through the real Jobs API and can be repeated.
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.dependencies import get_current_user
from modules.jobs.routes import jobs_crud
from modules.notifications import alert_dispatcher
alert_dispatcher.should_suppress_notification = lambda **_: True
app = FastAPI(); app.include_router(jobs_crud.router, prefix='/api/v1')
with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(7,'operator','op@example.test','fixture','operator',1)"))
app.dependency_overrides[get_current_user] = lambda: {'id': 7, 'username': 'operator', 'role': 'operator', 'group_id': None, 'is_active': True}
api = TestClient(app)
created = results['unlinked']['jobs'][0]['id']
listing = api.get('/api/v1/jobs', params={'status': 'completed'})
results['api_list_status'] = listing.status_code
results['api_list'] = [{'id': j['id'], 'item_name': j['item_name'], 'status': j['status'], 'printer_id': j['printer_id']}
                       for j in listing.json()] if listing.status_code == 200 else listing.text
repeat = api.post(f'/api/v1/jobs/{created}/repeat')
results['repeat_status'] = repeat.status_code
results['repeat_body'] = repeat.json() if repeat.headers.get('content-type', '').startswith('application/json') else repeat.text
if repeat.status_code == 200:
    results['repeat_job'] = q('SELECT item_name,status,printer_id,quantity,notes FROM jobs WHERE id=:id', id=repeat.json()['new_job_id'])[0]
print('SCENARIO_JSON=' + json.dumps(results, default=str))
engine.dispose()
'''


@pytest.fixture(scope='module')
def bambu_completion(tmp_path_factory):
    directory = tmp_path_factory.mktemp('certD-jobsB-bambu')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + str(directory / 'app.db'), LICENSE_DIR=str(directory),
               API_KEY='', ODIN_ITAR_MODE='0', ODIN_TELEMETRY_V2='0', COOKIE_SECURE='false',
               PYTHONPATH=f"{ROOT / 'backend'}:{ROOT}")
    result = subprocess.run([sys.executable, '-c', SCENARIO], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-4000:]
    line = [l for l in result.stdout.splitlines() if l.startswith('SCENARIO_JSON=')][-1]
    return json.loads(line[len('SCENARIO_JSON='):])


def test_monitor_uses_bambu_report_topic_through_stubbed_transport(bambu_completion):
    s = bambu_completion['unlinked']
    assert s['subscribed'] == ['device/OWNEDSERIAL601/report']
    assert s['endpoint'] == ['192.0.2.10', 8883] and s['username'] == 'bblp'


def test_unmatched_bambu_finish_auto_creates_completed_job_linked_to_print(bambu_completion):
    s = bambu_completion['unlinked']
    assert s['tracked_after'] is None
    assert len(s['print_jobs']) == 1 and len(s['jobs']) == 1
    print_job, job = s['print_jobs'][0], s['jobs'][0]
    assert print_job['status'] == 'completed' and print_job['job_name'] == 'Reprint Bracket v2'
    assert print_job['scheduled_job_id'] == job['id']
    assert (job['item_name'], job['printer_id'], job['status']) == ('Reprint Bracket v2', 601, 'completed')
    assert job['actual_start'].startswith('2026-10-14 10:00:00') and job['actual_end'].startswith('2026-10-14 11:30:00')
    assert job['duration_hours'] == pytest.approx(1.5)
    assert (job['quantity'], job['quantity_on_bed'], job['hold'], job['is_locked']) == (1, 1, 0, 0)


def test_matched_bambu_finish_completes_existing_job_without_duplicate(bambu_completion):
    s = bambu_completion['linked']
    assert [(j['id'], j['item_name'], j['status']) for j in s['jobs']] == [(950, 'Linked Gear', 'completed')]
    assert s['jobs'][0]['duration_hours'] == pytest.approx(1.0)
    assert s['print_jobs'] == [{'status': 'completed', 'scheduled_job_id': 950}]


def test_auto_created_job_is_listed_and_repeatable_for_reprint(bambu_completion):
    created = bambu_completion['unlinked']['jobs'][0]['id']
    assert bambu_completion['api_list_status'] == 200
    assert {'id': created, 'item_name': 'Reprint Bracket v2', 'status': 'completed', 'printer_id': 601} in bambu_completion['api_list']
    assert bambu_completion['repeat_status'] == 200, bambu_completion['repeat_body']
    assert bambu_completion['repeat_body']['original_job_id'] == created
    assert bambu_completion['repeat_job'] == {'item_name': 'Reprint Bracket v2', 'status': 'pending', 'printer_id': 601,
                                              'quantity': 1, 'notes': f'Repeat of job #{created}'}


@pytest.mark.parametrize('offset', [timedelta(hours=3), timedelta(hours=-3)], ids=['adhoc_bump', 'stale_sweep'])
def test_monitor_bump_and_stale_sweep_keep_the_explicit_pin(lifecycle, offset):
    """mqtt_job_lifecycle resets printer_id on bumped/stale jobs; the explicit pin (target) must survive."""
    db, start, alerts, reschedules = lifecycle
    scheduled(db, 801, 9, NOW + offset)
    db.execute(text("UPDATE jobs SET target_type='specific', target_filter='9' WHERE id=801")); db.commit()
    start()
    assert status(db, [801])[801] == ('pending', None)
    assert tuple(db.execute(text('SELECT target_type,target_filter FROM jobs WHERE id=801')).one()) == ('specific', '9')
