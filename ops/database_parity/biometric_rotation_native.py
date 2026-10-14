"""Owned PostgreSQL biometric rotation: actual HTTP, contention and COMMIT."""
import os
import stat
from pathlib import Path
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session

os.environ['JWT_SECRET_KEY'] = 'owned-biometric-native-signing-key-only'

private = Path(os.environ['BIOMETRIC_NATIVE_PASSWORD_FILE'])
os.environ['DATABASE_URL'] = 'postgresql+psycopg://postgres@pg/postgres'
os.environ['DATABASE_PASSWORD_FILE'] = str(private)
assert os.getuid() == 10001 and stat.S_IMODE(private.stat().st_mode) == 0o600
print('runtime-private-file uid=10001 mode=600', flush=True)
engine = create_engine(URL.create('postgresql+psycopg', username='postgres', password=private.read_text(), host='pg', database='postgres'), hide_parameters=True)
from core.schema import bootstrap_database
bootstrap_database(engine, Path('/source/backend'))
from core import auth
auth.SECRET_KEY = 'owned-biometric-native-signing-key-only'
from core.db import get_db
from core import db as configured_db
from core.db_compat import sql
assert configured_db.IS_POSTGRES and sql.is_postgres
assert engine.dialect.name == configured_db.engine.dialect.name == 'postgresql'
with engine.connect() as actual, configured_db.engine.connect() as configured:
    assert actual.execute(text('SELECT current_database()')).scalar_one() == configured.execute(text('SELECT current_database()')).scalar_one() == 'postgres'
print('biometric-native-actual-configured-postgres-parity-pass', flush=True)
from modules.push.models import BiometricToken
from modules.push.routes import biometric
BiometricToken.__table__.create(engine, checkfirst=True)
with engine.begin() as connection:
    connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(8001,'owned-bio-native','fixture','viewer',TRUE)"))
app = FastAPI()
app.include_router(biometric.router)
def database():
    with Session(engine) as db:
        yield db
app.dependency_overrides[get_db] = database

def seed(device):
    raw = 'owned-fixture-' + device
    with Session(engine) as db:
        db.add(BiometricToken(user_id=8001, device_id=device, token_hash=biometric._hash_token(raw), expires_at=(datetime.now(timezone.utc)+timedelta(days=1)).replace(microsecond=0)))
        db.commit()
    return raw

def refresh(raw, device):
    with TestClient(app) as client:
        return client.post('/auth/biometric-refresh', json={'refresh_token':raw,'device_id':device})

raw = seed('sequence')
first = refresh(raw, 'sequence')
assert first.status_code == 200
assert refresh(raw, 'sequence').status_code == 401
second = refresh(first.json()['refresh_token'], 'sequence')
assert second.status_code == 200
with engine.connect() as connection:
    assert connection.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=8001')).scalar_one() == 2
print('biometric-native-sequential-rotation-session-pass', flush=True)

raw = seed('locked')
with engine.connect() as held:
    transaction = held.begin()
    held.execute(text('SELECT id FROM users WHERE id=8001 FOR UPDATE'))
    response = refresh(raw, 'locked')
    assert response.status_code == 503
    transaction.rollback()
assert refresh(raw, 'locked').status_code == 200
print('biometric-native-parent-nowait-release-pass', flush=True)

raw = seed('race')
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
barrier = Barrier(2)
original_execute = Session.execute
def synchronized_parent(self, statement, *args, **kwargs):
    if str(statement).startswith('SELECT * FROM users WHERE id='):
        barrier.wait(timeout=10)
    return original_execute(self, statement, *args, **kwargs)
Session.execute = synchronized_parent
try:
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: refresh(raw, 'race'), range(2)))
    assert sorted(r.status_code for r in responses) in ([200, 401], [200, 503])
finally:
    Session.execute = original_execute
with engine.connect() as connection:
    assert connection.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=8001')).scalar_one() == 4
print('biometric-native-simultaneous-replay-single-issuance-pass', flush=True)

raw = seed('commit-fault')
with engine.begin() as connection:
    connection.exec_driver_sql("CREATE FUNCTION owned_bio_commit_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'owned deferred fault' USING ERRCODE='23514'; END $$")
    connection.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_bio_commit_fault AFTER INSERT ON active_sessions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_bio_commit_fault()')
response = refresh(raw, 'commit-fault')
assert response.status_code == 503 and 'access_token' not in response.json()
with engine.connect() as connection:
    assert connection.execute(text("SELECT token_hash FROM biometric_tokens WHERE user_id=8001 AND device_id='commit-fault'")).scalar_one() == biometric._hash_token(raw)
    assert connection.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=8001')).scalar_one() == 4
with engine.begin() as connection:
    connection.exec_driver_sql('DROP TRIGGER owned_bio_commit_fault ON active_sessions')
    connection.exec_driver_sql('DROP FUNCTION owned_bio_commit_fault()')
assert refresh(raw, 'commit-fault').status_code == 200
print('biometric-native-deferred-commit-rollback-pass', flush=True)
engine.dispose()
print('biometric-rotation-native-pass', flush=True)
