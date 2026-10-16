"""Owned PostgreSQL probe for the cert-D fixes whose DB semantics changed.

Covers: job lifecycle guard (enum NOT IN inside the atomic completion UPDATE);
the filament ledger (claims, pre-upgrade rows, archive qmark runner, a real
claim race); printer-delete pin marking; retention purge marker cleanup;
queue_position ordering (boolean NULLS-last ORDER BY) in GET /jobs and the
scheduler, named priority persisted as INTEGER, specific-printer pin, ship
guard on orders, and the maintenance care-counter reset. Real routers and the
real bootstrap schema; the authenticated principal is injected at the
get_current_user boundary (auth is not the DB semantic under test).
"""
import os, stat, secrets
from pathlib import Path
private = Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
assert os.getuid() == 10001 and stat.S_IMODE(private.stat().st_mode) == 0o600
print('runtime-private-file uid=10001 mode=600', flush=True)
os.environ['JWT_SECRET_KEY'] = secrets.token_urlsafe(48)
os.environ['DATABASE_URL'] = 'postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE'] = str(private)
from datetime import datetime, timezone
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
engine = create_engine(URL.create('postgresql+psycopg', username='postgres', password=private.read_text(), host='pg', database='postgres'), hide_parameters=True)
bootstrap_database(engine, Path('/source/backend'))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.db import get_db
from core.dependencies import get_current_user
from core.registry import registry
from modules.organizations.services import EducationPolicyService
from modules.jobs.routes import router as jobs_router
from modules.orders.routes import orders_crud
from modules.system import routes_maintenance

registry._providers['EducationPolicyProvider'] = EducationPolicyService()
import modules.orders as orders_module
orders_module.register_providers(registry)
with engine.begin() as c:
    c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned',TRUE)"))
    c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(3,'operator','x','operator',TRUE,1)"))
    for pid in (9, 12):
        c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared,org_id) "
                       "VALUES(:id,:n,'bambu',1,TRUE,'[]',FALSE,FALSE,1)"), {'id': pid, 'n': f'PG-{pid}'})
    c.execute(text("UPDATE printers SET prints_since_maintenance=7, hours_since_maintenance=12.5, total_print_hours=40 WHERE id=9"))

app = FastAPI()
app.include_router(jobs_router)
app.include_router(orders_crud.router)
app.include_router(routes_maintenance.router)
def database():
    with Session(engine) as db:
        yield db
app.dependency_overrides[get_db] = database
app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'username': 'operator', 'role': 'operator', 'group_id': 1, 'is_active': True}
client = TestClient(app)
def one(sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).one()

# 1. Lifecycle guard on PostgreSQL enum column.
for status, action in (('cancelled', 'complete'), ('submitted', 'complete'), ('submitted', 'fail'), ('rejected', 'complete'), ('cancelled', 'fail')):
    with engine.begin() as c:
        jid = c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,priority,hold,is_locked) "
                             "VALUES('PG guard',:s,1,1,3,FALSE,FALSE) RETURNING id"), {'s': status}).scalar_one()
    r = client.post(f'/jobs/{jid}/{action}')
    assert r.status_code == 409, (status, action, r.status_code, r.text)
    assert one('SELECT status FROM jobs WHERE id=:id', id=jid)[0] == status
live = client.post('/jobs', json={'item_name': 'PG live'}).json()['id']
assert client.post(f'/jobs/{live}/start').status_code == 200
done = client.post(f'/jobs/{live}/complete')
assert done.status_code == 200 and done.json()['status'] == 'completed'
assert client.post(f'/jobs/{live}/complete').status_code == 200  # idempotent retry
assert client.post(f'/jobs/{live}/fail').json()['status'] == 'failed'  # operator correction
print('postgres-lifecycle-guard-pass', flush=True)

# 2. Named priority persists as INTEGER; queue_position orders GET /jobs and the scheduler.
with engine.begin() as c:
    c.execute(text("UPDATE jobs SET status='completed' WHERE status IN ('pending','scheduled')"))
ids = {}
for name, prio in (('A', 'urgent'), ('B', 'low'), ('C', 'normal')):
    r = client.post('/jobs', json={'item_name': 'PG queue ' + name, 'priority': prio, 'duration_hours': 1})
    assert r.status_code == 201, r.text
    ids[name] = r.json()['id']
row = one("SELECT priority, pg_typeof(priority)::text FROM jobs WHERE id=:id", id=ids['A'])
assert tuple(row) == (1, 'integer'), row
listed = [j['id'] for j in client.get('/jobs', params={'status': 'pending'}).json()]
assert listed == [ids['A'], ids['C'], ids['B']], listed
assert client.patch('/jobs/reorder', json={'job_ids': [ids['B'], ids['A'], ids['C']]}).status_code == 200
listed = [j['id'] for j in client.get('/jobs', params={'status': 'pending'}).json()]
# Priority first; drag position only orders within a priority.
assert listed == [ids['A'], ids['C'], ids['B']], listed
with engine.begin() as c:
    c.execute(text("UPDATE jobs SET priority=3 WHERE id IN (:a,:b,:c)"), {'a': ids['A'], 'b': ids['B'], 'c': ids['C']})
    c.execute(text("UPDATE jobs SET queue_position=NULL WHERE id=:c"), {'c': ids['C']})
listed = [j['id'] for j in client.get('/jobs', params={'status': 'pending'}).json()]
assert listed == [ids['B'], ids['A'], ids['C']], listed  # placed by position, unplaced C last (NULLs last on PG)
from modules.jobs.scheduler import Scheduler, SchedulerConfig
with engine.begin() as c:
    c.execute(text("UPDATE printers SET is_active=(id=9)"))
with Session(engine) as db:
    Scheduler(SchedulerConfig(horizon_days=2)).run(db, start_date=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    db.commit()
starts = {k: one('SELECT scheduled_start FROM jobs WHERE id=:id', id=v)[0] for k, v in ids.items()}
assert all(starts.values()) and starts['B'] < starts['A'] < starts['C'], starts
# Terminal state clears queue_position.
assert client.post(f"/jobs/{ids['B']}/start").status_code == 200
assert client.post(f"/jobs/{ids['B']}/complete").status_code == 200
assert one('SELECT queue_position FROM jobs WHERE id=:id', id=ids['B'])[0] is None
print('postgres-named-priority-and-queue-order-pass', flush=True)

# 3. Specific-printer pin through POST /jobs.
with engine.begin() as c:
    c.execute(text("UPDATE printers SET is_active=TRUE"))
    c.execute(text("UPDATE jobs SET status='completed' WHERE status IN ('pending','scheduled')"))
pinned = client.post('/jobs', json={'item_name': 'PG pinned', 'duration_hours': 1, 'target_type': 'specific', 'printer_id': 12})
assert pinned.status_code == 201 and pinned.json()['pinned_printer_id'] == 12, pinned.text
with Session(engine) as db:
    Scheduler(SchedulerConfig(horizon_days=2)).run(db, start_date=datetime(2026, 10, 22, 12, tzinfo=timezone.utc))
    db.commit()
pid = pinned.json()['id']
assert tuple(one('SELECT status, printer_id FROM jobs WHERE id=:id', id=pid)) == ('scheduled', 12)
assert client.post(f'/jobs/{pid}/reset').status_code == 409          # reset only from a finished outcome
assert client.post(f'/jobs/{pid}/start').status_code == 200
assert client.post(f'/jobs/{pid}/fail').status_code == 200
assert client.post(f'/jobs/{pid}/reset').status_code == 200          # reset keeps the pin
assert tuple(one('SELECT status, printer_id, target_filter FROM jobs WHERE id=:id', id=pid)) == ('pending', None, '12')
with engine.begin() as c:
    c.execute(text("UPDATE printers SET is_active=FALSE WHERE id=12"))
with Session(engine) as db:
    Scheduler(SchedulerConfig(horizon_days=2)).run(db, start_date=datetime(2026, 10, 22, 12, tzinfo=timezone.utc))
    db.commit()
assert one('SELECT status FROM jobs WHERE id=:id', id=pid)[0] == 'pending'  # never placed elsewhere
assert client.get(f'/jobs/{pid}').json()['pinned_printer_issue'] == 'pinned_printer_inactive'
with engine.begin() as c:
    c.execute(text("UPDATE printers SET is_active=TRUE WHERE id=12"))
print('postgres-specific-printer-pin-pass', flush=True)

# 3b. PATCH cannot move status (approval/lifecycle bypass).
with engine.begin() as c:
    cid = c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,priority,hold,is_locked) "
                         "VALUES('PG patch','cancelled',1,1,3,FALSE,FALSE) RETURNING id")).scalar_one()
assert client.patch(f'/jobs/{cid}', json={'status': 'completed'}).status_code == 422
assert one('SELECT status FROM jobs WHERE id=:id', id=cid)[0] == 'cancelled'
print('postgres-pin-and-patch-guard-pass', flush=True)

# 4. Ship guard.
with engine.begin() as c:
    for oid, status in ((801, 'cancelled'), (802, 'pending'), (803, 'fulfilled'), (804, 'in_progress')):
        c.execute(text("INSERT INTO orders(id,order_number,status,org_id) VALUES(:id,:n,:s,1)"), {'id': oid, 'n': f'PG-{oid}', 's': status})
for oid, expected, final in ((801, 409, 'cancelled'), (802, 409, 'pending'), (803, 200, 'shipped'), (804, 200, 'shipped')):
    r = client.patch(f'/orders/{oid}/ship', json={'tracking_number': f'PG-T-{oid}'})
    assert r.status_code == expected, (oid, r.status_code, r.text)
    assert one('SELECT status FROM orders WHERE id=:id', id=oid)[0] == final
assert client.patch('/orders/803/ship', json={'tracking_number': 'again'}).status_code == 409
print('postgres-ship-guard-pass', flush=True)

# 5. Maintenance log resets care counters in the same transaction.
with engine.begin() as c:
    c.execute(text("INSERT INTO maintenance_tasks(id,name,interval_print_hours,is_active) VALUES(501,'PG service',10,TRUE)"))
r = client.post('/maintenance/logs', json={'printer_id': 9, 'task_id': 501, 'task_name': 'PG service'})
assert r.status_code == 200, r.text
assert tuple(one('SELECT prints_since_maintenance, hours_since_maintenance FROM printers WHERE id=9')) == (0, 0)
status = next(p for p in client.get('/maintenance/status').json() if p['printer_id'] == 9)
task = next(t for t in status['tasks'] if t['task_id'] == 501)
assert (status['prints_since_maintenance'], task['hours_until_due'], task['next_due_print_hours']) == (0, 10, 50), (status, task)
print('postgres-maintenance-counter-reset-pass', flush=True)
# 6. Job state machine on PostgreSQL: guarded UPDATE through SQLAlchemy and through the
#    monitors' DBAPI cursor adapter (named -> qmark -> %s), enum status column.
from modules.jobs.state_machine import transition_job, TransitionError
from core.db_utils import _ConnectionAdapter
with engine.begin() as c:
    sm = c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,priority,hold,is_locked,printer_id) "
                        "VALUES('PG sm','cancelled',1,1,3,FALSE,FALSE,9) RETURNING id")).scalar_one()
with Session(engine) as db:
    assert transition_job(db, sm, 'completed', 'monitor') is False          # print end never revives a cancelled job
    assert transition_job(db, sm, 'pending', 'operator') is True            # reset from a finished outcome
    db.commit()
raw = engine.raw_connection()
try:
    cursor = _ConnectionAdapter(raw, True).cursor()
    assert transition_job(cursor, sm, 'scheduled', 'scheduler', extra_fields={'printer_id': 9}) is True
    assert transition_job(cursor, sm, 'printing', 'monitor', where='printer_id=:pid', params={'pid': 9}) is True
    assert transition_job(cursor, sm, 'pending', 'operator') is False       # no reset from a live print
    raw.commit()
finally:
    raw.close()
assert one('SELECT status FROM jobs WHERE id=:id', id=sm)[0] == 'printing'
assert client.post(f'/jobs/{sm}/reset').status_code == 409
assert client.patch(f'/jobs/{sm}', json={'status': 'pending'}).status_code == 422
print('postgres-state-machine-pass', flush=True)

# 7. Filament once per print attempt (quota_attempt: markers in system_config).
with engine.begin() as c:
    c.execute(text("INSERT INTO models(id,name,org_id,color_requirements) VALUES(901,'PG grams',1,:req)"),
              {'req': '{"slot1": {"grams": 10.0}}'})
    c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(901,'PG','Test','PLA')"))
    c.execute(text("INSERT INTO spools(id,filament_id,status,location_printer_id,location_slot,remaining_weight_g,"
                   "initial_weight_g,org_id) VALUES(901,901,'active',9,1,100,100,1)"))
    fj = c.execute(text("INSERT INTO jobs(item_name,model_id,status,charged_to_org_id,quantity,priority,hold,is_locked,printer_id) "
                        "VALUES('PG grams',901,'printing',1,1,3,FALSE,FALSE,9) RETURNING id")).scalar_one()
for route in ('complete', 'fail', 'complete'):
    assert client.post(f'/jobs/{fj}/{route}').status_code == 200, route
assert one('SELECT remaining_weight_g FROM spools WHERE id=901')[0] == 90
assert client.post(f'/jobs/{fj}/reset').status_code == 200
with engine.begin() as c:
    c.execute(text('UPDATE jobs SET printer_id=9 WHERE id=:id'), {'id': fj})
assert client.post(f'/jobs/{fj}/start').status_code == 200
assert client.post(f'/jobs/{fj}/complete').status_code == 200
assert one('SELECT remaining_weight_g FROM spools WHERE id=901')[0] == 80
with engine.connect() as c:
    keys = set(c.execute(text("SELECT key FROM system_config WHERE key LIKE :p"), {'p': f'quota_attempt:%:{fj}%'}).scalars())
assert keys == {f'quota_attempt:seq:{fj}', f'quota_attempt:started:{fj}:1',
                f'quota_attempt:filament:job:{fj}:0', f'quota_attempt:filament:job:{fj}:1'}, keys
print('postgres-print-attempt-markers-pass', flush=True)

# 8. The filament ledger on PostgreSQL (JSON system_config values, timestamp used_at):
#    a marker-less pre-upgrade automatic row is this attempt's deduction; nothing
#    deducted claims nothing; a print-scoped deduction through the archive's qmark
#    runner covers a later operator correction; and two real connections racing
#    for one claim serialise on the unique key -- the loser deducts nothing.
import threading
from core import filament_ledger
from core.print_attempts import sqlalchemy_runner, clear_job_attempts
from modules.archives.attempt_accounting import _ledger_runner
with engine.begin() as c:
    legacy = c.execute(text("INSERT INTO jobs(item_name,model_id,status,charged_to_org_id,quantity,priority,hold,is_locked,printer_id) "
                            "VALUES('PG legacy',901,'printing',1,1,3,FALSE,FALSE,9) RETURNING id")).scalar_one()
    c.execute(text("INSERT INTO spool_usage(spool_id,job_id,weight_used_g,notes) VALUES(901,:j,5,:n)"),
              {'j': legacy, 'n': f'Auto-deducted on job #{legacy} complete (PG legacy)'})
assert client.post(f'/jobs/{legacy}/complete').status_code == 200          # applied completion, no second deduction
assert one('SELECT status FROM jobs WHERE id=:id', id=legacy)[0] == 'completed'
assert one('SELECT remaining_weight_g FROM spools WHERE id=901')[0] == 80
with engine.begin() as c:
    unknown = c.execute(text("INSERT INTO jobs(item_name,model_id,status,charged_to_org_id,quantity,priority,hold,is_locked,printer_id) "
                             "VALUES('PG unknown',901,'printing',1,1,3,FALSE,FALSE,9) RETURNING id")).scalar_one()
    c.execute(text("UPDATE spools SET remaining_weight_g=NULL WHERE id=901"))
assert client.post(f'/jobs/{unknown}/complete').status_code == 200
assert one("SELECT COUNT(*) FROM system_config WHERE key LIKE :p", p=f'quota_attempt:filament:job:{unknown}:%')[0] == 0
with engine.begin() as c:
    c.execute(text("UPDATE spools SET remaining_weight_g=80 WHERE id=901"))
    pj = c.execute(text("INSERT INTO print_jobs(printer_id,scheduled_job_id,job_name,status,started_at) "
                        "VALUES(9,:j,'PG print','completed',NOW()) RETURNING id"), {'j': unknown}).scalar_one()
with engine.begin() as c:                                                  # archive path: SQLAlchemy exec_driver_sql + qmark
    outcome = filament_ledger.record_job_filament(_ledger_runner(c), unknown, source='archive', print_job_id=pj,
                                                  grams_by_spool={901: 10})
    assert outcome.deducted and outcome.deductions[0]['remaining_g'] == 70, outcome
with engine.begin() as c:
    c.execute(text("UPDATE jobs SET status='failed' WHERE id=:j"), {'j': unknown})
assert client.post(f'/jobs/{unknown}/complete').status_code == 200          # operator correction: same print
assert one('SELECT remaining_weight_g FROM spools WHERE id=901')[0] == 70
with engine.begin() as c:
    race = c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,priority,hold,is_locked) "
                          "VALUES('PG race','completed',1,1,3,FALSE,FALSE) RETURNING id")).scalar_one()
first = engine.connect(); first_tx = first.begin()
assert filament_ledger.record_job_filament(sqlalchemy_runner(first), race, source='operator_complete',
                                           grams_by_spool={901: 10}).deducted
seen = {}
def contender():
    with engine.begin() as c:
        seen['state'] = filament_ledger.record_job_filament(sqlalchemy_runner(c), race, source='moonraker',
                                                           grams_by_spool={901: 10}).state
worker = threading.Thread(target=contender); worker.start(); worker.join(1.0)
assert worker.is_alive()                                                   # waits on the uncommitted claim
first_tx.commit(); first.close(); worker.join(30)
assert seen == {'state': 'already_deducted'}, seen
assert one('SELECT remaining_weight_g FROM spools WHERE id=901')[0] == 60
print('postgres-filament-ledger-pass', flush=True)

# 9. Printer delete: chunked expanding IN marks every pin to the printer (any status);
#    only the still-schedulable jobs are reported as live (their owners get the alert).
from modules.jobs.pinning import clear_pins_for_deleted_printer
with engine.begin() as c:
    c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared,org_id) "
                   "VALUES(14,'PG-14','bambu',1,TRUE,'[]',FALSE,FALSE,1)"))
    live_ids = [c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,priority,hold,is_locked,"
                               "target_type,target_filter) VALUES('PG pin',:s,1,1,3,FALSE,FALSE,'specific','14') RETURNING id"),
                          {'s': s}).scalar_one() for s in ('pending', 'scheduled', 'submitted', 'pending', 'pending')]
    history = c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,priority,hold,is_locked,"
                             "target_type,target_filter) VALUES('PG pin hist','completed',1,1,3,FALSE,FALSE,'specific','14') RETURNING id")).scalar_one()
with Session(engine) as db:
    pins = clear_pins_for_deleted_printer(db, 14, chunk=2)
    assert sorted(pins.live) == sorted(live_ids) and sorted(pins.marked) == sorted(live_ids + [history])
    db.commit()
with engine.connect() as c:
    filters = dict(c.execute(text("SELECT id,target_filter FROM jobs WHERE id IN (" + ",".join(map(str, live_ids + [history])) + ")")).all())
assert all(filters[j] == 'deleted:14' for j in live_ids + [history]), filters
print('postgres-printer-delete-pins-pass', flush=True)

# 10. Retention purge (DELETE ... RETURNING id) clears the purged jobs' attempt markers.
import asyncio
from modules.system.routes_config import run_retention_cleanup
with engine.begin() as c:
    old = c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,priority,hold,is_locked,updated_at) "
                         "VALUES('PG old','completed',1,1,3,FALSE,FALSE,NOW() - INTERVAL '90 days') RETURNING id")).scalar_one()
    for key in (f'quota_attempt:seq:{old}', f'quota_attempt:filament:job:{old}:0'):
        c.execute(text("INSERT INTO system_config(key,value) VALUES(:k,'1')"), {'k': key})
    c.execute(text("INSERT INTO system_config(key,value) VALUES('data_retention',:v) ON CONFLICT(key) DO UPDATE SET value=:v"),
              {'v': '{"completed_jobs_days": 30}'})
with Session(engine) as db:
    result = asyncio.run(run_retention_cleanup(current_user={'id': 3, 'role': 'admin'}, db=db))
assert result['deleted']['completed_jobs'] >= 1, result
with engine.connect() as c:
    assert c.execute(text("SELECT COUNT(*) FROM jobs WHERE id=:id"), {'id': old}).scalar_one() == 0
    assert c.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE :p"), {'p': f'quota_attempt:%:{old}%'}).scalar_one() == 0
print('postgres-retention-attempt-cleanup-pass', flush=True)
print('certd-fixes-native-pass', flush=True)
engine.dispose()
