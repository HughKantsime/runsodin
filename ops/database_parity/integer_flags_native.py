"""Integer flag columns (0/1 INTEGER, not BOOLEAN) on PostgreSQL and SQLite.

vision_models.is_active, vision_settings.{enabled, *_enabled, auto_pause,
collect_training_data} and printer_profiles.{is_shared, is_default} are INTEGER in the
bootstrap schema on both dialects (post-027 rules: no schema change). PostgreSQL refuses
"integer IS TRUE" and a boolean bound into an integer column, which SQLite accepts. This
probe drives the real code paths over the real bootstrap schema:
* the Vigil AI model registry load (inference_engine.reload_models),
* model activation and retirement (PATCH /vision/models/{id}/activate, DELETE /vision/models/{id}),
* per-printer vision settings with JSON booleans (PATCH /printers/{id}/vision),
* printer profiles with JSON booleans (POST /profiles, PUT /profiles/{id}, with and without raw_content).
Principal injected at get_current_user (auth is not the semantic under test).

Dialect: INTF_PG_HOST/INTF_PG_PORT/INTF_PG_PASSWORD (PostgreSQL) or INTF_SQLITE_PATH.
Prints 'integer-flags-native-pass' on success.
"""
import os
import secrets
import tempfile
from pathlib import Path

os.environ['JWT_SECRET_KEY'] = secrets.token_urlsafe(48)
_private = Path(tempfile.mkdtemp(prefix='intflags-')).resolve()  # no symlinked ancestors (restore gate)
os.chmod(_private, 0o700)
(_private / 'data').mkdir(mode=0o700)
os.environ['ODIN_DATA_DIR'] = str(_private / 'data')
PG = 'INTF_PG_HOST' in os.environ
if PG:
    password = os.environ.pop('INTF_PG_PASSWORD')
    (_private / 'password').write_text(password)
    os.chmod(_private / 'password', 0o600)
    os.environ['DATABASE_URL'] = 'postgresql+psycopg://postgres@{}:{}/postgres'.format(
        os.environ['INTF_PG_HOST'], os.environ['INTF_PG_PORT'])
    os.environ['DATABASE_PASSWORD_FILE'] = str(_private / 'password')
else:
    os.environ['DATABASE_URL'] = 'sqlite:///' + os.environ['INTF_SQLITE_PATH']

from sqlalchemy import text  # noqa: E402
from core.db import engine  # noqa: E402  (the app's engine: dialect-aware helpers follow DATABASE_URL)
from core.schema import bootstrap_database  # noqa: E402

bootstrap_database(engine, Path(os.environ.get('INTF_BACKEND', 'backend')))
print('bootstrap ok', 'postgresql' if PG else 'sqlite', flush=True)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from core.dependencies import get_current_user  # noqa: E402
from modules.system import profile_routes  # noqa: E402
from modules.vision.routes import router as vision_router  # noqa: E402

with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'root','x','admin',TRUE)"))
    c.execute(text("INSERT INTO printers(id,name,model,is_active,camera_enabled) VALUES(9,'Flag printer','X1C',TRUE,TRUE)"))
    c.execute(text("INSERT INTO vision_models(id,name,detection_type,filename,input_size,is_active) VALUES"
                   "(41,'old','spaghetti','old.onnx',640,1),(42,'new','spaghetti','new.onnx',640,0),"
                   "(43,'other','first_layer','fl.onnx',640,0)"))

app = FastAPI()
app.include_router(vision_router, prefix='/api')
app.include_router(profile_routes.router, prefix='/api')
app.dependency_overrides[get_current_user] = lambda: {
    'id': 1, 'username': 'root', 'role': 'admin', 'group_id': None, 'is_active': True}
client = TestClient(app, raise_server_exceptions=os.environ.get('INTF_RAISE') == '1')  # 1: show the server error


def active():
    with engine.connect() as c:
        return dict(c.execute(text('SELECT id,is_active FROM vision_models ORDER BY id')).fetchall())


# Registry load: exactly the active model per type.
from modules.vision.inference_engine import VisionInferenceEngine  # noqa: E402
inference = VisionInferenceEngine()
inference.reload_models()
assert inference._registry_available is True, 'vision model registry did not load'
assert set(inference._snapshots) == {'spaghetti'}, dict(inference._snapshots)
print('vision registry load ok', flush=True)

# Activation switches the active model of its type, stored as 1/0.
response = client.patch('/api/vision/models/42/activate')
assert response.status_code == 200, (response.status_code, response.text)
assert active() == {41: 0, 42: 1, 43: 0}, active()
inference.reload_models()
assert inference._registry_available is True and set(inference._snapshots) == {'spaghetti'}
print('vision model activation ok', flush=True)

# Retirement of an inactive model; an active one is refused.
assert client.delete('/api/vision/models/42').status_code == 400
response = client.delete('/api/vision/models/41')
assert response.status_code == 200, (response.status_code, response.text)
assert 41 not in active(), active()
print('vision model retirement ok', flush=True)

# Per-printer vision settings: JSON booleans are stored as 1/0 (create, then update).
response = client.patch('/api/printers/9/vision', json={'enabled': True, 'auto_pause': False, 'collect_training_data': True})
assert response.status_code == 200, (response.status_code, response.text)
response = client.patch('/api/printers/9/vision', json={'spaghetti_enabled': False, 'build_plate_empty_enabled': True,
                                                        'first_layer_enabled': 1, 'detachment_enabled': 0})
assert response.status_code == 200, (response.status_code, response.text)
with engine.connect() as c:
    row = c.execute(text('SELECT enabled,auto_pause,collect_training_data,spaghetti_enabled,build_plate_empty_enabled,'
                         'first_layer_enabled,detachment_enabled FROM vision_settings WHERE printer_id=9')).one()
assert tuple(row) == (1, 0, 1, 0, 1, 1, 0), tuple(row)
assert client.patch('/api/printers/9/vision', json={'enabled': 'yes'}).status_code == 422
assert client.patch('/api/printers/9/vision', json={'auto_pause': 2}).status_code == 422
print('per-printer vision flags ok', flush=True)

# Printer profiles: JSON booleans for is_shared / is_default.
response = client.post('/api/profiles', json={'name': 'Flags', 'slicer': 'orca', 'category': 'process',
                                              'raw_content': '{}', 'is_shared': False, 'is_default': True})
assert response.status_code == 201, (response.status_code, response.text)
profile_id = response.json()['id']
with engine.connect() as c:
    assert tuple(c.execute(text('SELECT is_shared,is_default FROM printer_profiles WHERE id=:i'), {'i': profile_id}).one()) == (0, 1)
response = client.put(f'/api/profiles/{profile_id}', json={'is_shared': True})
assert response.status_code == 200, (response.status_code, response.text)
response = client.put(f'/api/profiles/{profile_id}', json={'is_shared': False, 'raw_content': '{"v": 2}'})
assert response.status_code == 200, (response.status_code, response.text)
with engine.connect() as c:
    assert c.execute(text('SELECT is_shared FROM printer_profiles WHERE id=:i'), {'i': profile_id}).scalar_one() == 0
assert client.put(f'/api/profiles/{profile_id}', json={'is_shared': 'no'}).status_code == 422
print('printer profile flags ok', flush=True)
print('integer-flags-native-pass', flush=True)
