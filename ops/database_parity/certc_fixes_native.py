"""Owned PostgreSQL probe for the cert-C fixes whose DB semantics matter.

Covers: Spoolman-link uniqueness serialised by pg_advisory_xact_lock (parallel creates and
updates have exactly one winner; no index; 1.9.x duplicates kept, editable and reported), deterministic and
finite-only material rate lookup (ORDER BY id over double precision incl. Infinity),
system_config JSON round-trips for global Vigil AI settings (second PATCH persists,
monitor global switch, retention-driven frame cleanup), and per-piece filament COGS.
Real routers and the real bootstrap schema; the principal is injected at
get_current_user (auth is not the DB semantic under test).
"""
import importlib
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

password = os.environ.pop('CERTC_PG_PASSWORD')
os.environ['JWT_SECRET_KEY'] = secrets.token_urlsafe(48)
# Dialect-aware SQL helpers (core.db_compat) follow the configured DATABASE_URL, as in production.
_private = Path(tempfile.mkdtemp(prefix='certc-pg-')).resolve()  # no symlinked ancestors (restore gate)
os.chmod(_private, 0o700)
(_private / 'password').write_text(password)
os.chmod(_private / 'password', 0o600)
os.environ['DATABASE_URL'] = 'postgresql+psycopg://postgres@{}:{}/postgres'.format(os.environ.get('CERTC_PG_HOST', 'pg'), os.environ.get('CERTC_PG_PORT', '5432'))
os.environ['DATABASE_PASSWORD_FILE'] = str(_private / 'password')
(_private / 'data').mkdir(mode=0o700)
os.environ['ODIN_DATA_DIR'] = str(_private / 'data')  # empty data dir: no restore staged
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError

engine = create_engine(URL.create('postgresql+psycopg', username='postgres', password=password,
                                  host=os.environ.get('CERTC_PG_HOST', 'pg'), port=int(os.environ.get('CERTC_PG_PORT', '5432')),
                                  database='postgres'), hide_parameters=True)
bootstrap_database(engine, Path(os.environ.get('CERTC_BACKEND', '/source/backend')))
print('bootstrap ok', flush=True)

from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.db import get_db
from core.dependencies import get_current_user
from modules.inventory.routes import router as inventory_router
from modules.inventory.routes import spools as spool_routes
from modules.vision.routes import detections

SUPER = {'id': 1, 'username': 'root', 'role': 'admin', 'group_id': None, 'is_active': True}
with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'root','x','admin',TRUE)"))
    c.execute(text("INSERT INTO filament_library(id,brand,name,material,cost_per_gram) VALUES"
                   "(301,'C','Inf PETG','PETG',:inf),(305,'C','Neg PETG','PETG',-1),(310,'C','Later PETG','PETG',0.09),"
                   "(308,'C','First valid PETG','PETG',0.04),(990,'C','Spool PLA','PLA',NULL)"), {'inf': float('inf')})

app = FastAPI()
app.include_router(inventory_router, prefix='/api')
app.include_router(detections.router, prefix='/api')


def database():
    with Session(engine) as db:
        yield db


app.dependency_overrides[get_db] = database
app.dependency_overrides[get_current_user] = lambda: SUPER
from fastapi.exceptions import RequestValidationError
from core.errors import request_validation_response
app.add_exception_handler(RequestValidationError, lambda request, exc: request_validation_response(exc))
client = TestClient(app)

# --- Spoolman link uniqueness: advisory-lock serialisation, no index, duplicates kept
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor
with engine.connect() as c:
    assert c.execute(text("SELECT count(*) FROM pg_indexes WHERE tablename='spools' AND indexdef ILIKE '%spoolman_spool_id%'")).scalar_one() == 0


def spool(**extra):
    response = client.post('/api/spools', json={'filament_id': 990, 'initial_weight_g': 1000, **extra})
    assert response.status_code == 200, response.text
    return response.json()['id']


a, b, c3 = spool(spoolman_spool_id=42), spool(), spool()
refused = client.post('/api/spools', json={'filament_id': 990, 'spoolman_spool_id': 42})
assert refused.status_code == 409 and f'#{a}' in refused.json()['detail'], refused.text
assert client.patch(f'/api/spools/{b}', json={'spoolman_spool_id': 42}).status_code == 409
assert client.patch(f'/api/spools/{b}', json={'spoolman_spool_id': 2_147_483_648}).status_code == 422
assert client.patch(f'/api/spools/{b}', json={'spoolman_spool_id': 2_147_483_647}).status_code == 200
original = spool_routes._refuse_taken_spoolman_link


def slow_check(*args, **kwargs):
    original(*args, **kwargs)
    _time.sleep(0.4)  # widen the check-then-write window
spool_routes._refuse_taken_spoolman_link = slow_check


def race(requests):
    barrier = threading.Barrier(len(requests))

    def send(request):
        method, url, body = request
        local = TestClient(app)
        barrier.wait()
        return getattr(local, method)(url, json=body).status_code
    with ThreadPoolExecutor(len(requests)) as pool:
        return sorted(pool.map(send, requests))


try:
    assert race([('post', '/api/spools', {'filament_id': 990, 'spoolman_spool_id': 64})] * 4) == [200, 409, 409, 409]
    targets = [spool() for _ in range(3)]
    assert race([('patch', f'/api/spools/{sid}', {'spoolman_spool_id': 65}) for sid in targets]) == [200, 409, 409]
finally:
    spool_routes._refuse_taken_spoolman_link = original
with engine.connect() as c:
    assert c.execute(text('SELECT count(*) FROM spools WHERE spoolman_spool_id=64')).scalar_one() == 1
    assert c.execute(text('SELECT count(*) FROM spools WHERE spoolman_spool_id=65')).scalar_one() == 1
print('spoolman link 409 names holder, int32 bound, parallel creates/updates exactly one winner ok', flush=True)

with engine.begin() as c:
    c.execute(text('UPDATE spools SET spoolman_spool_id=42 WHERE id=:id'), {'id': c3})  # 1.9.x-style duplicate
assert client.patch(f'/api/spools/{c3}', json={'notes': 'still editable', 'spoolman_spool_id': 42}).status_code == 200
conflicts = client.get('/api/spools/spoolman-link-conflicts').json()
assert [(x['spoolman_spool_id'], [s['id'] for s in x['spools']]) for x in conflicts] == [(42, [a, c3])]
with engine.connect() as c:
    assert c.execute(text('SELECT count(*) FROM spools WHERE spoolman_spool_id=42')).scalar_one() == 2
print('existing duplicate kept, still editable, reported to admins ok', flush=True)

# --- filament rate: superadmin write, finite bound, deterministic lookup
assert client.post('/api/filaments', content='{"brand":"C","name":"Bad","material":"PLA","cost_per_gram":Infinity}',
                   headers={'Content-Type': 'application/json'}).status_code == 422
app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'admin', 'group_id': 7}
assert client.patch('/api/filaments/lib_308', json={'cost_per_gram': 0.5}).status_code == 403
app.dependency_overrides[get_current_user] = lambda: SUPER
from modules.models_library.services import material_cost_per_gram
with Session(engine) as db:
    for _ in range(3):
        assert material_cost_per_gram(db, 'PETG') == 0.04
print('material rate lowest-id finite lookup ok', flush=True)

# --- per-piece filament COGS over the JSON color_requirements column
from modules.models_library.models import Model
from modules.orders.product_costing import _filament_cost_per_item
with Session(engine) as db:
    model = Model(name='C plate', default_filament_type='PETG', color_requirements={'slot1': {'color': '#000', 'grams': 100}}, quantity_per_bed=4)
    db.add(model)
    db.commit()
    assert abs(_filament_cost_per_item(db.get(Model, model.id)) - 1.0) < 1e-9
print('per-piece filament cost ok', flush=True)

# --- global Vigil AI settings JSON on PostgreSQL
assert client.patch('/api/vision/settings', json={'retention_days': 14}).status_code == 200
assert client.patch('/api/vision/settings', json={'enabled': False}).status_code == 200
assert client.get('/api/vision/settings').json() == {'enabled': False, 'retention_days': 14}
from modules.vision import monitor, frame_storage
monitor.engine = engine
frame_storage.engine = engine
assert monitor.VisionMonitorDaemon._globally_enabled(None) is False
assert client.patch('/api/vision/settings', json={'enabled': True, 'retention_days': 1}).status_code == 200
assert monitor.VisionMonitorDaemon._globally_enabled(None) is True
with engine.begin() as c:
    c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared) VALUES(9,'PG-9','bambu',1,TRUE,'[]',FALSE,FALSE)"))
    c.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status,frame_path,created_at) VALUES(7701,9,'spaghetti',0.9,'pending','9/aged.jpg',:old)"),
              {'old': datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=3)})
frames = Path(tempfile.mkdtemp())
(frames / '9').mkdir()
(frames / '9' / 'aged.jpg').write_bytes(b'\xff\xd8c\xff\xd9')
frame_storage.cleanup_old_frames(str(frames))
assert not (frames / '9' / 'aged.jpg').exists()
with engine.connect() as c:
    assert c.execute(text('SELECT frame_path FROM vision_detections WHERE id=7701')).scalar_one() is None
app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'admin', 'group_id': 7}
assert client.patch('/api/vision/settings', json={'enabled': False}).status_code == 403
print('vision settings JSON persistence, global switch, retention cleanup, superadmin gate ok', flush=True)
import shutil
shutil.rmtree(_private, ignore_errors=True)
print('certc-fixes-native-pass', flush=True)
