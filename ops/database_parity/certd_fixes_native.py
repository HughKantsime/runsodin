"""Owned PostgreSQL probe for the cert-D fixes whose DB semantics changed.

Covers: job lifecycle guard (enum NOT IN inside the atomic completion UPDATE),
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
assert listed == [ids['B'], ids['A'], ids['C']], listed
from modules.jobs.scheduler import Scheduler, SchedulerConfig
with engine.begin() as c:
    c.execute(text("UPDATE printers SET is_active=(id=9)"))
with Session(engine) as db:
    Scheduler(SchedulerConfig(horizon_days=2)).run(db, start_date=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    db.commit()
starts = {k: one('SELECT scheduled_start FROM jobs WHERE id=:id', id=v)[0] for k, v in ids.items()}
assert all(starts.values()) and starts['B'] < starts['A'] < starts['C'], starts
print('postgres-named-priority-and-queue-order-pass', flush=True)

# 3. Specific-printer pin through POST /jobs.
with engine.begin() as c:
    c.execute(text("UPDATE printers SET is_active=TRUE"))
    c.execute(text("UPDATE jobs SET status='completed' WHERE status IN ('pending','scheduled')"))
pinned = client.post('/jobs', json={'item_name': 'PG pinned', 'duration_hours': 1, 'target_type': 'specific', 'printer_id': 12})
assert pinned.status_code == 201 and pinned.json()['printer_id'] == 12, pinned.text
with Session(engine) as db:
    Scheduler(SchedulerConfig(horizon_days=2)).run(db, start_date=datetime(2026, 10, 22, 12, tzinfo=timezone.utc))
    db.commit()
assert tuple(one('SELECT status, printer_id FROM jobs WHERE id=:id', id=pinned.json()['id'])) == ('scheduled', 12)
print('postgres-specific-printer-pin-pass', flush=True)

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
print('certd-fixes-native-pass', flush=True)
engine.dispose()
