"""Certification group C — Vigil AI (F03) behavior evidence.

Real FastAPI routers + real bootstrapped SQLite schema + real monitor/thread code.
Only the outermost boundary is substituted: the go2rtc camera HTTP endpoint and
printer adapters. Defects are pinned as strict xfail with file:line and scenario.
"""
import configparser
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db

REPO = Path(__file__).resolve().parents[2]
ADMIN = {'id': 3, 'username': 'cert-admin', 'role': 'admin', 'group_id': None, 'is_active': True, '_auth_kind': 'session_jwt'}


@pytest.fixture
def vision_http(review_db):
    """Real detections router mounted at /api with the real schema."""
    from modules.vision.routes import detections
    from core.db import get_db
    from core.dependencies import get_current_user
    app = FastAPI()
    app.include_router(detections.router, prefix='/api')
    principal = dict(ADMIN)
    app.dependency_overrides[get_current_user] = lambda: principal

    def db():
        with Session(review_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = db
    with TestClient(app) as client:
        yield client, principal


@pytest.fixture
def camera_printers(review_db):
    review_db.execute(text("UPDATE printers SET camera_enabled=1, camera_url='rtsp://cert.invalid/stream', gcode_state='RUNNING', is_active=1 WHERE id IN (9,10)"))
    review_db.execute(text("INSERT INTO vision_settings(printer_id,enabled,spaghetti_threshold,capture_interval_sec) VALUES(9,1,0.65,1),(10,1,0.8,1)"))
    review_db.commit()
    return review_db


@pytest.fixture
def daemon(camera_printers, monkeypatch):
    """Real VisionMonitorDaemon scanning the real DB; go2rtc HTTP is the stubbed boundary."""
    import httpx
    from modules.vision import monitor
    calls = []

    def go2rtc(url, **kwargs):
        calls.append(url)
        return SimpleNamespace(status_code=404, content=b'')
    monkeypatch.setattr(httpx, 'get', go2rtc)
    monkeypatch.setattr(monitor, 'engine', camera_printers.get_bind())
    instance = monitor.VisionMonitorDaemon()
    yield instance, calls
    for thread in list(instance._threads.values()):
        thread.stop()
    for thread in list(instance._threads.values()):
        thread.join(timeout=3)


def _wait(predicate, seconds=6.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------- F03-3.1-01

def _program(conf):
    parser = configparser.RawConfigParser(strict=False)
    parser.read(REPO / 'docker' / conf)
    return parser['program:vision_monitor']


@pytest.mark.parametrize('conf', ['supervisord.conf', 'supervisord.vision.conf'])
def test_supervisord_program_launches_vision_monitor_module(conf):
    program = _program(conf)
    assert program['command'] == 'python3 -m modules.vision.monitor'
    assert program['directory'] == '/app/backend'
    assert program['autostart'] == 'true' and program['autorestart'] == 'true'
    # The supervised module is the real daemon entrypoint; no vision_monitor.py exists.
    assert (REPO / 'backend/modules/vision/monitor.py').is_file()
    assert not list(REPO.joinpath('backend').rglob('vision_monitor.py'))


def test_supervised_command_runs_daemon_until_sigterm(tmp_path):
    """Run the exact supervised command line from its working directory against a real DB."""
    from sqlalchemy import create_engine
    from core.schema import bootstrap_database
    database = tmp_path / 'vision-daemon.db'
    bootstrap_database(create_engine(f'sqlite:///{database}'), REPO / 'backend')
    env = dict(os.environ, PYTHONUNBUFFERED='1', ODIN_DB_ROLE='vision', DATABASE_URL=f'sqlite:///{database}',
               BACKEND_PATH=str(REPO / 'backend'), JWT_SECRET_KEY='cert-c-only', PYTHONPATH=str(REPO / 'backend'))
    command = _program('supervisord.conf')['command'].split()
    command[0] = sys.executable
    process = subprocess.Popen(command, cwd=REPO / 'backend', env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        time.sleep(4)
        assert process.poll() is None, process.stdout.read()
        process.terminate()
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
    assert 'Vision Monitor starting' in output
    assert 'Vision Monitor stopped' in output
    assert process.returncode == 0


# ------------------------------------------------- F03-3.1-03 / F03-3.1-04

def test_daemon_starts_one_capture_thread_per_running_camera_printer(daemon, camera_printers):
    instance, calls = daemon
    # A third printer without a camera and an idle printer must not get threads.
    camera_printers.execute(text("INSERT INTO printers(id,name,model,is_active,api_type,camera_enabled,camera_url,gcode_state) VALUES(11,'No camera','X',1,'bambu',0,NULL,'RUNNING'),(12,'Idle cam','X',1,'bambu',1,'rtsp://cert.invalid/idle','IDLE')"))
    camera_printers.commit()
    instance._scan_printers()
    assert set(instance._threads) == {9, 10}
    assert all(t.is_alive() for t in instance._threads.values())
    assert _wait(lambda: {'http://127.0.0.1:1984/api/frame.jpeg?src=printer_9',
                          'http://127.0.0.1:1984/api/frame.jpeg?src=printer_10'} <= set(calls))
    # Per-type thresholds come from the persisted per-printer settings rows.
    assert instance._threads[9].settings['spaghetti_threshold'] == 0.65
    assert instance._threads[10].settings['spaghetti_threshold'] == 0.8
    camera_printers.execute(text("UPDATE printers SET gcode_state='IDLE' WHERE id=10"))
    camera_printers.commit()
    stopped = instance._threads[10]
    instance._scan_printers()
    assert set(instance._threads) == {9}
    stopped.join(timeout=3)
    assert not stopped.is_alive()


def test_disabling_printer_vision_stops_running_thread(daemon, camera_printers, vision_http):
    instance, _ = daemon
    client, _ = vision_http
    instance._scan_printers()
    assert 9 in instance._threads
    assert client.patch('/api/printers/9/vision', json={'enabled': 0}).status_code == 200
    instance._scan_printers()
    assert 9 not in instance._threads


def test_sensitivity_change_applies_to_running_thread(daemon, camera_printers, vision_http):
    instance, _ = daemon
    client, _ = vision_http
    instance._scan_printers()
    assert client.patch('/api/printers/9/vision', json={'spaghetti_threshold': 0.95, 'auto_pause': 1}).status_code == 200
    instance._scan_printers()
    assert instance._threads[9].settings['spaghetti_threshold'] == 0.95
    assert instance._threads[9].settings['auto_pause'] == 1


def test_global_disable_stops_monitoring(daemon, camera_printers, vision_http):
    instance, _ = daemon
    client, _ = vision_http
    assert client.patch('/api/vision/settings', json={'enabled': False}).status_code == 200
    instance._scan_printers()
    assert instance._threads == {}


# ---------------------------------------------------------- F03-3.3-02

def test_printer_vision_settings_defaults_patch_persist_and_authority(vision_http, review_db):
    client, principal = vision_http
    defaults = client.get('/api/printers/9/vision').json()
    assert (defaults['enabled'], defaults['spaghetti_threshold'], defaults['first_layer_threshold'],
            defaults['detachment_threshold'], defaults['build_plate_empty_enabled']) == (1, 0.65, 0.60, 0.70, 0)
    response = client.patch('/api/printers/9/vision', json={'enabled': 0, 'spaghetti_threshold': 0.8, 'detachment_threshold': 0.55, 'bogus': 1})
    assert response.status_code == 200 and 'bogus' not in response.json()
    stored = client.get('/api/printers/9/vision').json()
    assert (stored['enabled'], stored['spaghetti_threshold'], stored['detachment_threshold'], stored['first_layer_threshold']) == (0, 0.8, 0.55, 0.6)
    assert client.patch('/api/printers/9/vision', json={'bogus': 1}).status_code == 400
    assert client.get('/api/printers/999/vision').status_code == 404
    principal.update(role='operator', group_id=1)
    assert client.patch('/api/printers/9/vision', json={'enabled': 1}).status_code == 403
    assert client.get('/api/printers/9/vision').json()['enabled'] == 0


@pytest.mark.parametrize('value', ['high', 7.5])
def test_invalid_sensitivity_is_rejected(vision_http, value):
    client, _ = vision_http
    response = client.patch('/api/printers/9/vision', json={'spaghetti_threshold': value})
    assert response.status_code in (400, 422)
    assert client.get('/api/printers/9/vision').json()['spaghetti_threshold'] == 0.65


# ---------------------------------------------------------- F03-3.3-03

def test_global_vision_settings_roundtrip_and_admin_only(vision_http):
    client, principal = vision_http
    assert client.get('/api/vision/settings').json() == {'enabled': True, 'retention_days': 30}
    assert client.patch('/api/vision/settings', json={'retention_days': 14, 'enabled': False, 'ignored': 1}).json() == {'retention_days': 14, 'enabled': False}
    assert client.get('/api/vision/settings').json() == {'enabled': False, 'retention_days': 14}
    principal.update(role='operator')
    assert client.get('/api/vision/settings').status_code == 403
    assert client.patch('/api/vision/settings', json={'enabled': True}).status_code == 403


def test_second_global_settings_change_persists(vision_http):
    client, _ = vision_http
    assert client.patch('/api/vision/settings', json={'retention_days': 14}).status_code == 200
    assert client.patch('/api/vision/settings', json={'enabled': False}).json() == {'enabled': False}
    assert client.get('/api/vision/settings').json() == {'enabled': False, 'retention_days': 14}


def _aged_frame(review_db, tmp_path, monkeypatch):
    from modules.vision import frame_storage
    monkeypatch.setattr(frame_storage, 'engine', review_db.get_bind())
    folder = tmp_path / 'frames' / '9'
    folder.mkdir(parents=True)
    frame = folder / 'aged.jpg'
    frame.write_bytes(b'\xff\xd8cert\xff\xd9')
    old = (datetime.now(timezone.utc) - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
    review_db.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status,frame_path,created_at) VALUES(7701,9,'spaghetti',0.9,'pending','9/aged.jpg',:old)"), {'old': old})
    review_db.commit()
    return frame_storage, frame


def test_frame_cleanup_honours_retention_key_it_reads(review_db, tmp_path, monkeypatch):
    """Positive control: the cleanup mechanism works when its own key is set."""
    frame_storage, frame = _aged_frame(review_db, tmp_path, monkeypatch)
    from core.models import SystemConfig
    review_db.add(SystemConfig(key='vision_retention_days', value='1'))
    review_db.commit()
    frame_storage.cleanup_old_frames(str(tmp_path / 'frames'))
    assert not frame.exists()


def test_global_retention_setting_drives_frame_cleanup(vision_http, review_db, tmp_path, monkeypatch):
    client, _ = vision_http
    frame_storage, frame = _aged_frame(review_db, tmp_path, monkeypatch)
    assert client.patch('/api/vision/settings', json={'retention_days': 1}).status_code == 200
    frame_storage.cleanup_old_frames(str(tmp_path / 'frames'))
    assert not frame.exists()


# ----------------------------------------------- F03-3.3-01 / F03-3.2-02

@pytest.fixture
def seeded_detections(review_db):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = [(8801, 9, 'spaghetti', 'pending'), (8802, 9, 'detachment', 'confirmed'), (8803, 10, 'detachment', 'dismissed'),
            (8804, 10, 'spaghetti', 'auto_paused'), (8805, 9, 'first_layer', 'pending'), (8806, 9, 'training_sample', 'training')]
    for index, (identity, printer, kind, status) in enumerate(rows):
        review_db.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status,frame_path,bbox_json,created_at) VALUES(:id,:p,:k,0.9,:s,:f,'[1,2,3,4]',:t)"),
                          {'id': identity, 'p': printer, 'k': kind, 's': status, 'f': f'{printer}/{identity}.jpg', 't': now - timedelta(minutes=index)})
    review_db.commit()


def _ids(response):
    assert response.status_code == 200, response.text
    return [row['id'] for row in response.json()['items']]


def test_detection_list_filters_by_printer_type_and_each_review_status(vision_http, seeded_detections):
    client, _ = vision_http
    everything = client.get('/api/vision/detections')
    assert _ids(everything) == [8801, 8802, 8803, 8804, 8805] and everything.json()['total'] == 5
    assert everything.json()['items'][0]['printer_name'] == 'P1S-09'
    assert _ids(client.get('/api/vision/detections', params={'printer_id': 10})) == [8803, 8804]
    assert _ids(client.get('/api/vision/detections', params={'detection_type': 'detachment'})) == [8802, 8803]
    for status, expected in [('pending', [8801, 8805]), ('confirmed', [8802]), ('dismissed', [8803]), ('auto_paused', [8804])]:
        assert _ids(client.get('/api/vision/detections', params={'status': status})) == expected
    combined = client.get('/api/vision/detections', params={'printer_id': 9, 'detection_type': 'spaghetti', 'status': 'pending'})
    assert _ids(combined) == [8801] and combined.json()['total'] == 1
    page = client.get('/api/vision/detections', params={'limit': 2, 'offset': 2})
    assert _ids(page) == [8803, 8804] and page.json()['total'] == 5


def test_detection_review_patch_persists_confirm_and_dismiss(vision_http, seeded_detections, review_db):
    client, principal = vision_http
    assert client.patch('/api/vision/detections/8801', json={'status': 'confirmed'}).json() == {'id': 8801, 'status': 'confirmed'}
    assert client.patch('/api/vision/detections/8805', json={'status': 'dismissed'}).status_code == 200
    detail = client.get('/api/vision/detections/8801').json()
    assert detail['status'] == 'confirmed' and detail['reviewed_by'] == 3 and detail['reviewed_at']
    assert client.get('/api/vision/detections/8805').json()['status'] == 'dismissed'
    assert client.patch('/api/vision/detections/8802', json={'status': 'auto_paused'}).status_code == 400
    principal.update(role='viewer')
    assert client.patch('/api/vision/detections/8802', json={'status': 'dismissed'}).status_code == 403


def test_capture_wire_fixture_for_detections_ui(vision_http, seeded_detections):
    """Producer fixture for frontend/src/pages/vision/Detections.cert.test.tsx."""
    client, _ = vision_http
    listing = client.get('/api/vision/detections', params={'limit': 30, 'offset': 0}).json()
    stats = client.get('/api/vision/stats', params={'days': 7}).json()
    fixture = {'scope': 'Owned real /api/vision/detections and /api/vision/stats HTTP responses (cert C)',
               'detections': listing, 'stats': stats}
    for row in fixture['detections']['items']:
        row['created_at'] = '2026-10-14T12:00:00'
    path = REPO / 'frontend/src/test-fixtures/cert-C-vision-detections-wire.json'
    path.write_text(json.dumps(fixture, indent=2) + '\n')
    assert [r['status'] for r in listing['items']] == ['pending', 'confirmed', 'dismissed', 'auto_paused', 'pending']


# ---------------------------------------------------------- F03-3.3-05

def test_stats_have_no_time_series_trend_output(vision_http, seeded_detections):
    """Records the exact stats contract: windowed aggregates only, no per-period trend series."""
    client, _ = vision_http
    body = client.get('/api/vision/stats', params={'days': 7}).json()
    assert set(body) == {'days', 'by_type', 'by_status', 'by_printer', 'accuracy_pct', 'total_reviewed'}
    assert body['by_type'] == {'spaghetti': 2, 'detachment': 2, 'first_layer': 1}
    assert body['accuracy_pct'] == 50.0 and body['total_reviewed'] == 2


# ---------------------------------------------------------- F03-3.3-04

def _real_model():
    candidates = [os.environ.get('ODIN_REAL_VISION_MODEL'), str(REPO / 'backend/vision_models_default/obico_spaghetti.onnx')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and Path(candidate).stat().st_size > 1_000_000:
            return Path(candidate)
    pytest.skip('hydrated Obico ONNX model not available')


def test_http_upload_list_activate_real_onnx_model(review_db, tmp_path, monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.vision.routes import vision_models
    from modules.vision import inference_engine
    model = _real_model()
    monkeypatch.setattr(vision_models, 'VISION_MODELS_DIR', str(tmp_path))
    monkeypatch.setattr(inference_engine, 'VISION_MODELS_DIR', str(tmp_path))
    monkeypatch.setattr(inference_engine, 'engine', review_db.get_bind())
    app = FastAPI()
    app.include_router(vision_models.router, prefix='/api')
    app.dependency_overrides[get_current_user] = lambda: dict(ADMIN)

    def db():
        with Session(review_db.get_bind()) as session:
            yield session
    app.dependency_overrides[get_db] = db
    client = TestClient(app)
    with model.open('rb') as handle:
        uploaded = client.post('/api/vision/models', params={'name': 'Cert detachment', 'detection_type': 'detachment', 'version': 'c1', 'input_size': 416},
                               files={'file': ('cert.onnx', handle, 'application/octet-stream')})
    assert uploaded.status_code == 200, uploaded.text
    identity = uploaded.json()['id']
    assert (tmp_path / uploaded.json()['filename']).stat().st_size == model.stat().st_size
    listed = {row['id']: row for row in client.get('/api/vision/models').json()}
    assert listed[identity]['is_active'] in (False, 0) and listed[identity]['detection_type'] == 'detachment'
    assert client.patch(f'/api/vision/models/{identity}/activate').json() == {'id': identity, 'detection_type': 'detachment', 'is_active': True}
    engine = inference_engine.VisionInferenceEngine()
    engine.reload_models()
    assert engine.model_state('detachment') == 'ready'
    assert client.post('/api/vision/models', params={'name': 'x', 'detection_type': 'nozzle'}, files={'file': ('x.onnx', b'x')}).status_code == 400
    assert client.post('/api/vision/models', params={'name': 'x', 'detection_type': 'spaghetti'}, files={'file': ('x.bin', b'x')}).status_code == 400


# ------------------------------------------------- F03-3.4-01..04 storage

def test_storage_roots_are_data_paths_and_frames_land_under_printer_directory(tmp_path, monkeypatch):
    from modules.vision import frame_storage, inference_engine
    from modules.vision.routes import vision_models
    assert frame_storage.VISION_FRAMES_DIR == '/data/vision_frames'
    assert vision_models.VISION_MODELS_DIR == inference_engine.VISION_MODELS_DIR == '/data/vision_models'
    monkeypatch.setattr(frame_storage, 'VISION_FRAMES_DIR', str(tmp_path))
    relative = frame_storage.save_detection_frame(42, np.full((24, 32, 3), 90, dtype=np.uint8), 'detachment')
    assert relative.split('/')[0] == '42'
    assert cv2.imread(str(tmp_path / relative)).shape == (24, 32, 3)


def test_onnx_models_are_git_lfs_tracked_and_default_model_exceeds_100mb():
    path = 'backend/vision_models_default/obico_spaghetti.onnx'
    attr = subprocess.run(['git', 'check-attr', 'filter', path], cwd=REPO, capture_output=True, text=True, check=True).stdout
    assert attr.strip().endswith('filter: lfs')
    pointer = subprocess.run(['git', 'show', f'HEAD:{path}'], cwd=REPO, capture_output=True, check=True).stdout
    assert pointer.startswith(b'version https://git-lfs.github.com/spec/v1')
    size = int(next(line.split()[1] for line in pointer.decode().splitlines() if line.startswith('size ')))
    assert size > 100 * 1024 * 1024


# ------------------------------------------- regression: cert-fix-C vision settings

def test_threshold_boundaries_accepted_and_global_settings_types_enforced(vision_http):
    client, _ = vision_http
    assert client.patch('/api/printers/9/vision', json={'spaghetti_threshold': 1, 'detachment_threshold': 0.0}).status_code == 200
    stored = client.get('/api/printers/9/vision').json()
    assert (stored['spaghetti_threshold'], stored['detachment_threshold']) == (1, 0.0)
    assert client.patch('/api/printers/9/vision', json={'first_layer_threshold': True}).status_code == 422
    for bad in ({'retention_days': 0}, {'retention_days': 'week'}, {'enabled': 'no'}):
        assert client.patch('/api/vision/settings', json=bad).status_code == 422, bad
    assert client.get('/api/vision/settings').json() == {'enabled': True, 'retention_days': 30}


def test_longer_global_retention_keeps_frame_and_overrides_legacy_key(vision_http, review_db, tmp_path, monkeypatch):
    client, _ = vision_http
    frame_storage, frame = _aged_frame(review_db, tmp_path, monkeypatch)
    from core.models import SystemConfig
    review_db.add(SystemConfig(key='vision_retention_days', value='1'))
    review_db.commit()
    assert client.patch('/api/vision/settings', json={'retention_days': 7}).status_code == 200
    frame_storage.cleanup_old_frames(str(tmp_path / 'frames'))
    assert frame.exists()


def test_reenabling_printer_vision_restarts_thread(daemon, camera_printers, vision_http):
    instance, _ = daemon
    client, _ = vision_http
    instance._scan_printers()
    first = instance._threads[9]
    assert client.patch('/api/printers/9/vision', json={'enabled': 0}).status_code == 200
    instance._scan_printers()
    first.join(timeout=3)
    assert not first.is_alive()
    assert client.patch('/api/printers/9/vision', json={'enabled': 1}).status_code == 200
    assert client.patch('/api/vision/settings', json={'enabled': True}).status_code == 200
    instance._scan_printers()
    assert instance._threads[9] is not first and instance._threads[9].is_alive()


def test_global_vision_settings_write_requires_superadmin(vision_http, review_db):
    """vision_settings is one install-wide row: an org-scoped admin cannot change it (review HIGH-4)."""
    client, principal = vision_http
    principal.update(role='admin', group_id=1)
    assert client.patch('/api/vision/settings', json={'enabled': False}).status_code == 403
    # Per-printer settings stay with the org admin for printers of their own org (printer 9 is org 1).
    assert client.patch('/api/printers/9/vision', json={'spaghetti_threshold': 0.7}).status_code == 200
    principal.update(group_id=2)
    assert client.patch('/api/printers/9/vision', json={'spaghetti_threshold': 0.2}).status_code == 404
    assert client.patch('/api/vision/settings', json={'enabled': False}).status_code == 403
    principal.update(group_id=None)
    assert client.get('/api/vision/settings').json()['enabled'] is True
    assert client.patch('/api/vision/settings', json={'enabled': False}).status_code == 200


# ------------------------------------------- review MEDIUM-6/7, item 11: monitor lifecycle

def test_zero_threshold_reaches_running_thread_unchanged(daemon, camera_printers, vision_http):
    instance, _ = daemon
    client, _ = vision_http
    assert client.patch('/api/printers/9/vision', json={'spaghetti_threshold': 0.0, 'build_plate_empty_threshold': 0}).status_code == 200
    instance._scan_printers()
    assert instance._threads[9].settings['spaghetti_threshold'] == 0.0
    assert instance._threads[9].settings['build_plate_empty_threshold'] == 0


@pytest.mark.parametrize('value', [0, -5, 0.5, 'fast', True, 86401])
def test_capture_interval_must_be_bounded_positive_int(vision_http, value):
    client, _ = vision_http
    assert client.patch('/api/printers/9/vision', json={'capture_interval_sec': value}).status_code == 422
    assert client.patch('/api/printers/9/vision', json={'capture_interval_sec': 30}).status_code == 200


def test_running_thread_picks_up_shorter_capture_interval(daemon, camera_printers, vision_http):
    instance, calls = daemon
    client, _ = vision_http
    assert client.patch('/api/printers/9/vision', json={'capture_interval_sec': 3600}).status_code == 200
    instance._scan_printers()
    url = 'http://127.0.0.1:1984/api/frame.jpeg?src=printer_9'
    assert _wait(lambda: calls.count(url) >= 1)
    time.sleep(0.3)
    assert calls.count(url) == 1  # sleeping for an hour
    assert client.patch('/api/printers/9/vision', json={'capture_interval_sec': 1}).status_code == 200
    instance._scan_printers()
    assert _wait(lambda: calls.count(url) >= 3, seconds=5)


def test_sleep_error_does_not_kill_thread_and_dead_threads_are_restarted(daemon, camera_printers, monkeypatch):
    instance, _ = daemon
    instance._scan_printers()
    thread = instance._threads[9]
    failures = []
    original = thread._wake.wait

    def flaky(timeout=None):
        if not failures:
            failures.append(timeout)
            raise OSError('cert: sleep interrupted')
        return original(timeout)
    monkeypatch.setattr(thread._wake, 'wait', flaky)
    assert _wait(lambda: failures)
    time.sleep(0.2)
    assert thread.is_alive()
    # A thread that died anyway is detected on the next scan and replaced.
    dead = instance._threads[10]
    dead.stop()
    dead.join(timeout=3)
    assert not dead.is_alive()
    instance._scan_printers()
    assert instance._threads[10] is not dead and instance._threads[10].is_alive()


def test_disable_joins_thread_and_reenable_never_duplicates_a_live_one(daemon, camera_printers, vision_http, monkeypatch):
    import threading
    from modules.vision import monitor
    instance, _ = daemon
    client, _ = vision_http
    instance._scan_printers()
    old = instance._threads[9]
    assert client.patch('/api/printers/9/vision', json={'enabled': 0}).status_code == 200
    instance._scan_printers()
    assert not old.is_alive()  # joined, not merely signalled
    # A thread stuck in a long capture outlives the join timeout: re-enable must wait for it.
    assert client.patch('/api/printers/9/vision', json={'enabled': 1}).status_code == 200
    instance._scan_printers()
    stuck = instance._threads[9]
    release, entered = threading.Event(), threading.Event()

    def blocking_capture():
        entered.set()
        release.wait(10)
    monkeypatch.setattr(stuck, '_capture_and_analyze', blocking_capture)
    stuck._wake.set()
    assert entered.wait(5)
    monkeypatch.setattr(monitor, 'THREAD_JOIN_TIMEOUT', 0.2)
    client.patch('/api/printers/9/vision', json={'enabled': 0})
    instance._scan_printers()
    assert stuck.is_alive() and 9 not in instance._threads
    client.patch('/api/printers/9/vision', json={'enabled': 1})
    instance._scan_printers()
    assert 9 not in instance._threads
    alive = [t for t in threading.enumerate() if getattr(t, 'printer_id', None) == 9 and t.is_alive()]
    assert alive == [stuck]
    release.set()
    stuck.join(timeout=5)
    instance._scan_printers()
    assert instance._threads[9] is not stuck and instance._threads[9].is_alive()


def test_api_and_thread_share_the_capture_interval_bound(vision_http):
    from modules.vision.detection_thread import MAX_CAPTURE_INTERVAL, capture_interval
    client, _ = vision_http
    assert client.patch('/api/printers/9/vision', json={'capture_interval_sec': MAX_CAPTURE_INTERVAL}).status_code == 200
    assert capture_interval(MAX_CAPTURE_INTERVAL) == MAX_CAPTURE_INTERVAL and capture_interval(MAX_CAPTURE_INTERVAL + 1) == 10


# ------------------------------------------- cert-B gap: auto-configured Bambu RTSP cameras

def test_auto_configured_bambu_camera_without_stored_url_is_monitored(daemon, camera_printers, monkeypatch):
    """RTSPS and chamber cameras derived from credentials must both be watched."""
    from cryptography.fernet import Fernet
    from core import crypto
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    instance, calls = daemon
    key = crypto.encrypt('01P00CERT0001|12345678')
    camera_printers.execute(text(
        "INSERT INTO printers(id,name,model,is_active,api_type,api_host,api_key,camera_enabled,camera_url,gcode_state) VALUES"
        "(21,'X1C auto','X1C',1,'bambu','192.0.2.21',:k,1,NULL,'RUNNING'),"
        "(22,'H2D auto','H2D',1,'bambu','192.0.2.22',:k,1,NULL,'RUNNING'),"
        "(23,'P1S no camera url','P1S',1,'bambu','192.0.2.23',:k,1,NULL,'RUNNING'),"
        "(24,'X1C camera off','X1C',1,'bambu','192.0.2.24',:k,0,NULL,'RUNNING')"), {'k': key})
    camera_printers.commit()
    instance._scan_printers()
    assert set(instance._threads) == {9, 10, 21, 22, 23}
    assert _wait(lambda: 'http://127.0.0.1:1984/api/frame.jpeg?src=printer_21' in calls)


def test_stop_arriving_while_interval_is_read_is_not_lost(daemon, camera_printers, vision_http, monkeypatch):
    """Final review LOW-4: a stop/interval wake between reading the interval and waiting must not be cleared."""
    from modules.vision import detection_thread
    instance, _ = daemon
    client, _ = vision_http
    assert client.patch('/api/printers/9/vision', json={'capture_interval_sec': 3600}).status_code == 200
    camera_printers.execute(text("UPDATE printers SET gcode_state='IDLE' WHERE id=10"))
    camera_printers.commit()
    instance._scan_printers()
    thread = instance._threads[9]
    real = detection_thread.capture_interval

    def stop_while_reading(value):
        if threading.current_thread() is thread:
            thread.stop()  # lands after the loop's _running check, before the wait
        return real(value)
    monkeypatch.setattr(detection_thread, 'capture_interval', stop_while_reading)
    thread._wake.set()  # finish the current wait so the loop reaches the interval read
    thread.join(timeout=3)
    assert not thread.is_alive()


# ------------------------------------------- final review MEDIUM-3 / LOW-5: thread cap, shared join deadline

def _more_camera_printers(db, ids):
    for pid in ids:
        db.execute(text("INSERT INTO printers(id,name,model,is_active,api_type,camera_enabled,camera_url,gcode_state) "
                        "VALUES(:id,:n,'P1S',1,'bambu',1,'rtsp://cert.invalid/s','RUNNING')"), {'id': pid, 'n': f'Cam {pid}'})
    db.commit()


def test_vision_threads_are_capped_logged_and_reported(daemon, camera_printers, vision_http, caplog):
    instance, _ = daemon
    client, _ = vision_http
    _more_camera_printers(camera_printers, [31, 32])
    assert client.get('/api/vision/monitor-status').json()['max_threads'] == 16
    for bad in (0, 257, 'many', True):
        assert client.patch('/api/vision/settings', json={'max_threads': bad}).status_code == 422
    assert client.patch('/api/vision/settings', json={'max_threads': 3}).status_code == 200
    with caplog.at_level('WARNING', logger='vision_monitor'):
        instance._scan_printers()
    assert set(instance._threads) == {9, 10, 31}
    assert 'Vision thread cap 3 reached' in caplog.text and '[32]' in caplog.text
    status = client.get('/api/vision/monitor-status').json()
    assert status == {'max_threads': 3, 'reported': True, 'active_threads': 3, 'capped': True, 'capped_count': 1, 'capped_printer_ids': [32]}
    # A freed slot goes to the waiting printer on the next scan.
    camera_printers.execute(text("UPDATE printers SET gcode_state='IDLE' WHERE id=10"))
    camera_printers.commit()
    instance._scan_printers()
    assert set(instance._threads) == {9, 31, 32}
    assert client.get('/api/vision/monitor-status').json()['capped'] is False


def test_global_disable_joins_all_threads_against_one_deadline(daemon, camera_printers, vision_http, monkeypatch):
    import threading
    from modules.vision import monitor
    instance, _ = daemon
    client, _ = vision_http
    _more_camera_printers(camera_printers, [33, 34])
    instance._scan_printers()
    stuck = [instance._threads[pid] for pid in (10, 33, 34)]
    release = threading.Event()
    entered = []
    for thread in stuck:
        def blocking(thread=thread):
            entered.append(thread.printer_id)
            release.wait(10)
        monkeypatch.setattr(thread, '_capture_and_analyze', blocking)
        thread._wake.set()
    assert _wait(lambda: len(entered) >= 3)
    monkeypatch.setattr(monitor, 'THREAD_JOIN_TIMEOUT', 0.5)
    assert client.patch('/api/vision/settings', json={'enabled': False}).status_code == 200
    started = time.monotonic()
    instance._scan_printers()
    elapsed = time.monotonic() - started
    release.set()
    assert instance._threads == {} and elapsed < 1.2, elapsed  # sequential joins would take >= 1.5 s
    assert set(instance._stopping) == {10, 33, 34}


def test_vision_printer_list_includes_auto_cameras_with_the_monitor_rule(vision_http, camera_printers, monkeypatch):
    """Settings gap: per-printer tuning lists the same printers the monitor would watch."""
    from cryptography.fernet import Fernet
    from core import crypto
    from modules.vision.monitor import VisionMonitorDaemon
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    client, principal = vision_http
    key = crypto.encrypt('01P00CERT0001|12345678')
    camera_printers.execute(text(
        "INSERT INTO printers(id,name,model,is_active,api_type,api_host,api_key,camera_enabled,camera_url,gcode_state) VALUES"
        "(21,'X1C auto','X1C',1,'bambu','192.0.2.21',:k,1,NULL,'IDLE'),"
        "(23,'P1S no camera url','P1S',1,'bambu','192.0.2.23',:k,1,NULL,'IDLE'),"
        "(24,'X1C camera off','X1C',1,'bambu','192.0.2.24',:k,0,NULL,'IDLE')"), {'k': key})
    camera_printers.commit()
    listed = client.get('/api/vision/printers').json()
    assert listed == [{'printer_id': 9, 'camera_source': 'configured'}, {'printer_id': 10, 'camera_source': 'configured'},
                      {'printer_id': 21, 'camera_source': 'auto'}, {'printer_id': 23, 'camera_source': 'auto'}]
    rows = camera_printers.execute(text('SELECT id,camera_url,api_type,api_key,api_host,model FROM printers WHERE id IN (21,23)')).mappings().all()
    assert {r['id']: VisionMonitorDaemon._has_effective_camera(r) for r in rows} == {21: True, 23: True}
    assert client.patch('/api/printers/21/vision', json={'spaghetti_threshold': 0.8}).status_code == 200
    principal.update(group_id=2)
    assert [p['printer_id'] for p in client.get('/api/vision/printers').json()] == [21, 23]  # org-less printers stay visible; org 1 printers do not
    principal.update(role='operator', group_id=None)
    assert client.get('/api/vision/printers').status_code == 403
