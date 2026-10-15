"""Owned PostgreSQL probe for the cert-F fixes whose DB semantics matter.

Covers: the setup_complete flag on the JSON system_config column (written by
POST /setup/complete, read by the lock, legacy '"true"' text still read as
complete), the GDPR erasure scrub of audit_logs.details (JSON column,
CAST(details AS TEXT) search, rewrite stays valid JSON) and login_attempts
renaming, and quota charging on completion through both the SQLAlchemy path
(POST /jobs/{id}/complete) and the qmark DB-API adapter used by monitor
completions (core.db_utils.get_db). Real routers and the real bootstrap
schema; the authenticated principal is injected at get_current_user.
"""
import json, os, secrets, stat
from pathlib import Path
private = Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
assert os.getuid() == 10001 and stat.S_IMODE(private.stat().st_mode) == 0o600
print('runtime-private-file uid=10001 mode=600', flush=True)
os.environ['JWT_SECRET_KEY'] = secrets.token_urlsafe(48)
os.environ['DATABASE_URL'] = 'postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE'] = str(private)
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
from modules.system import routes_setup
from modules.organizations import routes_sessions

registry._providers['EducationPolicyProvider'] = EducationPolicyService()

def one(sql, **p):
    with engine.connect() as c:
        return c.execute(text(sql), p).one()

principal = {'id': None, 'username': None, 'role': 'admin', 'group_id': None, 'is_active': True}
app = FastAPI()
app.include_router(routes_setup.router)
app.include_router(routes_sessions.router)
app.include_router(jobs_router)
def database():
    with Session(engine) as db:
        yield db
app.dependency_overrides[get_db] = database
app.dependency_overrides[get_current_user] = lambda: dict(principal)
client = TestClient(app)

# 1. Setup: first admin via the real wizard route (session recorded, farm name kept), then the lock flag.
r = client.post('/setup/admin', json={'username': 'pgadmin', 'email': 'pgadmin@example.test',
                                      'password': secrets.token_urlsafe(24) + 'aA1', 'site_name': 'PG Print Lab'})
assert r.status_code == 200, r.text
admin_id = one("SELECT id FROM users WHERE username='pgadmin'")[0]
assert one('SELECT COUNT(*) FROM active_sessions WHERE user_id=:u', u=admin_id)[0] == 1
assert one('SELECT app_name FROM branding WHERE id=1')[0] == 'PG Print Lab'
principal.update({'id': admin_id, 'username': 'pgadmin'})
with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(2,'pgadmin2','pgadmin2@example.test','x','admin',TRUE)"))
    c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,quota_period) VALUES(3,'pg_operator','pg_operator@example.test','x','operator',TRUE,'weekly')"))
    c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(4,'pg_viewer','pg_viewer@example.test','x','viewer',TRUE)"))
assert client.get('/setup/status').json()['is_complete'] is False
principal.update({'id': 4, 'username': 'pg_viewer', 'role': 'viewer'})
assert client.post('/setup/complete').status_code == 403            # viewer cannot complete setup
assert client.post('/setup/printer', json={'name': 'PG viewer printer', 'model': 'X1C'}).status_code == 403
principal.update({'id': admin_id, 'username': 'pgadmin', 'role': 'admin'})
r = client.post('/setup/complete')
assert r.status_code == 200, r.text
raw = one("SELECT value::text, json_typeof(value::json) FROM system_config WHERE key='setup_complete'")
assert tuple(raw) == ('true', 'boolean'), raw
assert client.get('/setup/status').json() == {'needs_setup': False, 'has_users': True, 'is_complete': True}
assert client.post('/setup/complete').status_code == 403
assert client.post('/setup/printer', json={'name': 'PG late printer', 'model': 'X1C'}).status_code == 403
principal.update({'id': 4, 'username': 'pg_viewer', 'role': 'viewer'})
assert client.post('/setup/printer', json={'name': 'PG viewer printer', 'model': 'X1C'}).status_code == 403
principal.update({'id': admin_id, 'username': 'pgadmin', 'role': 'admin'})
assert one('SELECT COUNT(*) FROM printers')[0] == 0
with engine.begin() as c:   # a row written by the old code path ('"true"' JSON string) still locks
    c.execute(text("UPDATE system_config SET value = CAST(:v AS JSON) WHERE key='setup_complete'"), {'v': '"true"'})
assert client.get('/setup/status').json()['is_complete'] is True
with engine.begin() as c:
    c.execute(text("UPDATE system_config SET value = CAST('false' AS JSON) WHERE key='setup_complete'"))
assert client.get('/setup/status').json()['is_complete'] is False
with engine.begin() as c:
    c.execute(text("UPDATE system_config SET value = CAST('true' AS JSON) WHERE key='setup_complete'"))
# Zero users: a completed install never reopens /setup/admin (checked inside a rolled-back transaction).
with engine.connect() as c:
    tx = c.begin()
    c.execute(text('DELETE FROM active_sessions')); c.execute(text('DELETE FROM users WHERE id=4'))
    from modules.system.routes_setup import _setup_is_complete, _validate_setup_phase
    from fastapi import HTTPException
    db = Session(bind=c)
    c.execute(text('DELETE FROM users'))
    assert _setup_is_complete(db)
    try:
        _validate_setup_phase(db); raise AssertionError('setup reopened with zero users')
    except HTTPException as exc:
        assert exc.status_code == 403 and exc.detail == 'Setup already completed.'
    db.close(); tx.rollback()
print('postgres-setup-lock-flag-pass', flush=True)

# 2. Quota charging on completion.
with engine.begin() as c:
    mid = c.execute(text("INSERT INTO models(name,color_requirements) VALUES('PG bracket', CAST(:r AS JSON)) RETURNING id"),
                    {'r': json.dumps({'1': {'color': 'Black', 'grams': 42.5}})}).scalar_one()
principal.update({'id': 3, 'username': 'pg_operator', 'role': 'operator'})
created = client.post('/jobs', json={'item_name': 'PG quota job', 'model_id': mid, 'duration_hours': 2.0})
assert created.status_code in (200, 201), created.text
jid = created.json()['id']
assert client.post(f'/jobs/{jid}/start').status_code == 200
done = client.post(f'/jobs/{jid}/complete')
assert done.status_code == 200 and done.json()['status'] == 'completed', done.text
from core.quota import _get_period_key, record_print_consumption, qmark_runner
key = _get_period_key('weekly')
grams, hours = one('SELECT grams_used, hours_used FROM quota_usage WHERE user_id=3 AND period_key=:k', k=key)
assert abs(grams - 42.5) < 1e-9 and hours > 0, (grams, hours)
from core.db_utils import get_db as raw_db
with raw_db() as conn:
    cur = conn.cursor()
    record_print_consumption(qmark_runner(cur), jid, grams=10.0, measured_hours=1.5)
    conn.commit()
grams2, hours2 = one('SELECT grams_used, hours_used FROM quota_usage WHERE user_id=3 AND period_key=:k', k=key)
assert abs(grams2 - 52.5) < 1e-9 and abs(hours2 - hours - 1.5) < 1e-4  # REAL (float4) column, (grams2, hours2, hours)
print('postgres-quota-charging-pass', flush=True)

# 3. GDPR erasure scrubs audit details (JSON) and login attempts.
principal.update({'id': admin_id, 'username': 'pgadmin', 'role': 'admin'})
with engine.begin() as c:
    c.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details,ip_address) VALUES "
                   "('login_failed','user',NULL,CAST(:a AS JSON),'192.0.2.5'),"
                   "('auth.login_failed','user',NULL,CAST(:b AS JSON),'192.0.2.5'),"
                   "('printer.created','printer',7,CAST(:c AS JSON),'192.0.2.5')"),
              {'a': json.dumps('Failed login: pg_operator from 192.0.2.5'),
               'b': json.dumps({'username': 'pg_operator'}),
               'c': json.dumps({'name': 'Lab', 'by': 'pg_operator@example.test'})})
    c.execute(text("INSERT INTO login_attempts(ip,username,attempted_at,success) VALUES('192.0.2.5','PG_Operator',1.0,0)"))
r = client.delete('/users/3/erase')
assert r.status_code == 200, r.text
left = one("SELECT COUNT(*) FROM audit_logs WHERE CAST(details AS TEXT) LIKE '%pg_operator%'")[0]
assert left == 0, left
with engine.connect() as c:
    for (d,) in c.execute(text("SELECT CAST(details AS TEXT) FROM audit_logs WHERE details IS NOT NULL")):
        json.loads(d)
    assert c.execute(text("SELECT COUNT(*) FROM audit_logs WHERE CAST(details AS TEXT) LIKE '%[deleted-user]%'")).scalar_one() == 3
    assert [r[0] for r in c.execute(text("SELECT username FROM login_attempts"))] == ['[deleted-3]']
assert one('SELECT username FROM users WHERE id=3')[0] == '[deleted-3]'
assert abs(one('SELECT grams_used FROM quota_usage WHERE user_id=3 AND period_key=:k', k=key)[0] - 52.5) < 1e-9
print('postgres-gdpr-scrub-pass', flush=True)
print('certf-fixes-native-pass', flush=True)
engine.dispose()
