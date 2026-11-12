"""Vigil Stage 1: training capture sampling, burst, bed crop, storage guard and exclusion.

Also pins that Stage 0 (no training frame for controlled work) still holds for every new path.
"""
import json
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_C_vision import camera_printers, daemon, vision_http  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

GIB = 1024 ** 3
CROP = {'x': 0.25, 'y': 0.5, 'w': 0.5, 'h': 0.5}   # on a 200 x 100 frame: 100 x 50 pixels


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def env(review_db, tmp_path, monkeypatch):
    from modules.vision import detection_thread, frame_storage, training_capture
    from modules.vision.detection_thread import PrinterVisionThread
    from modules.vision.inference_engine import VisionInferenceEngine
    monkeypatch.setattr(frame_storage, 'VISION_FRAMES_DIR', str(tmp_path / 'frames'))
    monkeypatch.setattr(detection_thread, 'engine', review_db.get_bind())
    monkeypatch.setattr(detection_thread, 'ws_push', lambda *a, **k: None)
    free = {'bytes': 100 * GIB}
    monkeypatch.setattr(training_capture, 'STORAGE_GUARD',
                        training_capture.StorageGuard(lambda path: free['bytes']))

    def make(job_id=None, printer=None, glob=None):
        clock = Clock()
        thread = PrinterVisionThread(
            9, 'Owned', VisionInferenceEngine(),
            {'collect_training_data': 1, 'training_capture': training_capture.resolve_config(glob, printer)},
            0, job_id, 'IDLE')
        thread.clock = clock
        thread._sampler = training_capture.TrainingSampler(clock)
        thread.frame = np.full((100, 200, 3), 120, dtype=np.uint8)
        monkeypatch.setattr(thread, '_capture_frame', lambda: thread.frame)
        return thread

    def training():
        return sorted((tmp_path / 'frames').rglob('training/*.jpg'))

    return SimpleNamespace(make=make, free=free, training=training, root=tmp_path / 'frames', db=review_db)


def _scalar(db, sql, **params):
    db.commit()
    return db.execute(text(sql), params).scalar()


def _print(db, print_id, job_id, category=None, tags=None):
    model_id = None
    if category is not None:
        model_id = 100000 + job_id
        db.execute(text("INSERT INTO models(id,name,category) VALUES(:m,'part',:c)"), {'m': model_id, 'c': category})
    db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,model_id,required_tags) "
                    "VALUES(:j,'part','printing',9,:m,:t)"),
               {'j': job_id, 'm': model_id, 't': json.dumps(tags or [])})
    db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,scheduled_job_id) "
                    "VALUES(:p,9,'part','running','2026-11-12 00:00:00',:j)"), {'p': print_id, 'j': job_id})
    db.commit()


def _mark_controlled(db, job_id):
    db.execute(text("INSERT OR IGNORE INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
    db.execute(text("INSERT INTO controlled_jobs(job_id,audience_id,label,printer_tag,retention_days,marked_at,reason) "
                    "VALUES(:j,1,'CUI','Secure',7,'2026-01-01T00:00:00+00:00','test')"), {'j': job_id})
    db.commit()


def _platform_admin(db, user_id=20):
    db.execute(text("INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) "
                    "VALUES (:i,'root','root@example.test','fixture','admin',1,NULL)"), {'i': user_id})
    db.commit()


def _alerts(db, user_id):
    return _scalar(db, "SELECT COUNT(*) FROM alerts WHERE user_id=:u", u=user_id)


# ------------------------------------------------------------------ sampling

def test_training_frames_follow_the_interval_not_the_detection_tick(env):
    thread = env.make()
    thread._capture_and_analyze()
    assert len(env.training()) == 1                     # the first frame of a print is taken at once
    for _ in range(5):
        thread.clock.now += 10                          # ten-second detection ticks
        thread._capture_and_analyze()
    assert len(env.training()) == 1
    thread.clock.now += 70                              # 120 s since the first frame
    thread._capture_and_analyze()
    assert len(env.training()) == 2


def test_per_printer_interval_overrides_the_global_one(env):
    from modules.vision.training_capture import resolve_config
    assert resolve_config(None, None)['interval_sec'] == 120
    assert resolve_config({'training_capture_interval_sec': 600}, None)['interval_sec'] == 600
    assert resolve_config({'training_capture_interval_sec': 600}, {'interval_sec': 30})['interval_sec'] == 30
    assert resolve_config({'training_capture_interval_sec': 600}, {'interval_sec': 0})['interval_sec'] == 600
    thread = env.make(printer={'interval_sec': 30})
    thread._capture_and_analyze()
    thread.clock.now += 29
    thread._capture_and_analyze()
    assert len(env.training()) == 1
    thread.clock.now += 2
    thread._capture_and_analyze()
    assert len(env.training()) == 2


def test_a_new_job_is_sampled_at_once(env):
    thread = env.make(9001)
    thread._capture_and_analyze()
    thread.update_layer(0, 9002, 'IDLE')
    thread._capture_and_analyze()
    assert len(env.training()) == 2


# ------------------------------------------------------------------ burst

def test_a_detection_saves_three_frames_around_it(env):
    # When the tick before was NOT a scheduled frame it is saved too: see the next-but-one test.
    thread = env.make()
    thread._capture_and_analyze()                        # scheduled frame
    thread.clock.now += 10
    thread._capture_and_analyze()                        # not due; becomes "the tick before" for the next call
    assert len(env.training()) == 1
    thread._detection_burst(thread.frame)                # this tick; the tick before is the scheduled frame, already saved
    assert len(env.training()) == 2
    thread.clock.now += 10
    thread._capture_and_analyze()                        # the tick after
    assert len(env.training()) == 3                      # three distinct frames around the detection, no duplicate
    thread.clock.now += 10
    thread._capture_and_analyze()                        # burst is over; interval not reached
    assert len(env.training()) == 3


def test_burst_does_not_save_a_frame_the_sampler_already_took(env):
    thread = env.make()
    thread._capture_and_analyze()
    thread._detection_burst(thread.frame)
    assert len(env.training()) == 1
    thread.clock.now += 10
    thread._capture_and_analyze()
    assert len(env.training()) == 2


def test_confirmed_detection_saves_cropped_burst_and_a_full_detection_frame(env, monkeypatch):
    import modules.notifications.event_dispatcher as printer_events
    monkeypatch.setattr(printer_events, 'dispatch_alert', lambda **kwargs: None)
    thread = env.make(printer={'crop': CROP})
    thread._capture_and_analyze()
    thread.clock.now += 10
    thread._capture_and_analyze()
    thread._on_detection('spaghetti', {'confidence': 0.9, 'bbox': []}, thread.frame)
    assert len(env.training()) == 2
    thread.clock.now += 10
    thread._capture_and_analyze()
    assert len(env.training()) == 3
    for path in env.training():
        assert cv2.imread(str(path)).shape == (50, 100, 3)          # training frames are cropped
    detection_frames = list((env.root / '9').glob('*.jpg'))
    assert len(detection_frames) == 1
    assert cv2.imread(str(detection_frames[0])).shape == (100, 200, 3)   # detection frame stays full


# ------------------------------------------------------------------ crop

def test_crop_box_math_and_bounds():
    from modules.vision.training_capture import crop_box, crop_frame
    assert crop_box((100, 200, 3), CROP) == (50, 50, 150, 100)
    assert crop_box((100, 200, 3), {'x': 0, 'y': 0, 'w': 1, 'h': 1}) == (0, 0, 200, 100)
    assert crop_box((10, 10, 3), {'x': 0.9999, 'y': 0.9999, 'w': 0.0001, 'h': 0.0001}) == (9, 9, 10, 10)
    frame = np.arange(100 * 200 * 3, dtype=np.uint32).reshape(100, 200, 3).astype(np.uint8)
    cropped = crop_frame(frame, CROP)
    assert cropped.shape == (50, 100, 3)
    assert np.array_equal(cropped, frame[50:100, 50:150])
    assert crop_frame(frame, None) is frame


@pytest.mark.parametrize('good', [
    {'x': 0, 'y': 0, 'w': 1, 'h': 1},
    {'x': 0.5, 'y': 0.5, 'w': 0.5, 'h': 0.5},
    {'x': 0.3333, 'y': 0.3333, 'w': 0.6667, 'h': 0.6667},
    {'x': 0, 'y': 0, 'w': 0.05, 'h': 0.05},
])
def test_validate_crop_accepts(good):
    from modules.vision.training_capture import validate_crop
    assert validate_crop(good) == good


@pytest.mark.parametrize('bad', [
    None, [], {}, 'full',
    {'x': 0, 'y': 0, 'w': 1}, {'x': 0, 'y': 0, 'w': 1, 'h': 1, 'z': 0},
    {'x': -0.1, 'y': 0, 'w': 0.5, 'h': 0.5}, {'x': 1, 'y': 0, 'w': 0.5, 'h': 0.5},
    {'x': 0.6, 'y': 0, 'w': 0.5, 'h': 0.5}, {'x': 0, 'y': 0.6, 'w': 0.5, 'h': 0.5},
    {'x': 0, 'y': 0, 'w': 0, 'h': 0.5}, {'x': 0, 'y': 0, 'w': 0.04, 'h': 0.5},
    {'x': 0, 'y': 0, 'w': 1.5, 'h': 0.5},
    {'x': True, 'y': 0, 'w': 1, 'h': 1}, {'x': '0', 'y': 0, 'w': 1, 'h': 1},
    {'x': float('nan'), 'y': 0, 'w': 1, 'h': 1}, {'x': 0, 'y': 0, 'w': float('inf'), 'h': 1},
])
def test_validate_crop_rejects(bad):
    from modules.vision.training_capture import validate_crop
    with pytest.raises(ValueError):
        validate_crop(bad)


def test_training_frame_is_cropped_and_an_unusable_crop_saves_nothing(env):
    thread = env.make(printer={'crop': CROP})
    thread._capture_and_analyze()
    assert [cv2.imread(str(p)).shape for p in env.training()] == [(50, 100, 3)]

    from modules.vision.training_capture import resolve_config
    assert resolve_config(None, {'crop': {'x': 2, 'y': 0, 'w': 1, 'h': 1}})['crop_invalid'] is True
    assert resolve_config(None, None, failed=True)['crop_invalid'] is True
    broken = env.make(printer={'crop': {'x': 2, 'y': 0, 'w': 1, 'h': 1}})
    before = len(env.training())
    broken._capture_and_analyze()
    assert len(env.training()) == before


# ------------------------------------------------------------------ storage guard

def test_guard_alerts_once_per_low_period_and_resumes():
    from modules.vision.training_capture import StorageGuard
    state = {'free': 1}
    calls = []
    guard = StorageGuard(lambda path: state['free'])
    for _ in range(3):
        assert guard.has_room('/tmp', 10, lambda f, m: calls.append((f, m))) is False
    assert calls == [(1, 10)] and guard.low
    state['free'] = 10
    assert guard.has_room('/tmp', 10, lambda f, m: calls.append((f, m))) is True
    assert not guard.low
    state['free'] = 9
    assert guard.has_room('/tmp', 10, lambda f, m: calls.append((f, m))) is False
    assert calls == [(1, 10), (9, 10)]


def test_guard_off_when_minimum_is_zero_and_closed_when_unreadable(tmp_path):
    from modules.vision.training_capture import StorageGuard

    def broken(path):
        raise OSError('no such volume')
    assert StorageGuard(broken).has_room(str(tmp_path), 0) is True
    calls = []
    assert StorageGuard(broken).has_room(str(tmp_path), 10, lambda f, m: calls.append(1)) is False
    assert calls == []


def test_guard_measures_the_nearest_existing_directory(tmp_path):
    from modules.vision.training_capture import StorageGuard
    seen = []
    guard = StorageGuard(lambda path: seen.append(path) or 100)
    assert guard.has_room(str(tmp_path / 'not' / 'yet'), 10) is True
    assert seen == [str(tmp_path)]


def test_thread_stops_below_the_minimum_alerts_admins_once_and_resumes(env):
    _platform_admin(env.db)
    thread = env.make()
    env.free['bytes'] = 1 * GIB                          # default minimum is 2 GB
    for _ in range(3):
        thread._capture_and_analyze()
    assert len(env.training()) == 0
    assert _alerts(env.db, 20) == 1                      # one alert, not one per tick
    assert _alerts(env.db, 3) == 0                       # organisation admin is not a platform admin
    assert _scalar(env.db, "SELECT COUNT(*) FROM vision_detections WHERE status='training'") == 0
    env.free['bytes'] = 5 * GIB                          # space freed: resumes by itself
    thread._capture_and_analyze()
    assert len(env.training()) == 1
    env.free['bytes'] = 1 * GIB                          # a new low period alerts again
    thread.clock.now += 200
    thread._capture_and_analyze()
    assert len(env.training()) == 1
    assert _alerts(env.db, 20) == 2


def test_guard_minimum_is_configurable_and_zero_turns_it_off(env):
    env.free['bytes'] = 1 * GIB
    off = env.make(glob={'training_min_free_gb': 0})
    off._capture_and_analyze()
    assert len(env.training()) == 1
    strict = env.make(glob={'training_min_free_gb': 1.5})
    strict.clock.now += 0
    strict._capture_and_analyze()
    assert len(env.training()) == 1                      # 1 GB free < 1.5 GB: nothing new


def test_global_setting_validation():
    from modules.vision.training_capture import global_setting_error, min_free_bytes
    assert global_setting_error({'training_capture_interval_sec': 300, 'training_min_free_gb': 2.5}) is None
    assert global_setting_error({'training_min_free_gb': 0}) is None
    for bad in ({'training_capture_interval_sec': 0}, {'training_capture_interval_sec': True},
                {'training_capture_interval_sec': 86401}, {'training_capture_interval_sec': '5'},
                {'training_min_free_gb': -1}, {'training_min_free_gb': True},
                {'training_min_free_gb': float('nan')}, {'training_min_free_gb': 'x'},
                {'training_min_free_gb': 20000}):
        assert global_setting_error(bad), bad
    assert min_free_bytes(None) == 2 * GIB and min_free_bytes(0) == 0 and min_free_bytes(0.5) == GIB // 2
    assert min_free_bytes(-1) == 2 * GIB


# ------------------------------------------------------------------ exclusion

@pytest.mark.parametrize('category,expected', [
    ('no-training', True), ('No-Training', True), ('  no-training ', True),
    ('Mini Critters, no-training', True), ('a;NO-TRAINING', True),
    ('no-training-x', False), ('Mini Critters', False), ('', False), (None, False), (5, False),
])
def test_excluded_category(category, expected):
    from modules.vision.training_capture import excluded_category
    assert excluded_category(category) is expected


def test_a_job_whose_model_is_no_training_is_not_collected(env):
    _print(env.db, 9001, 8001, category=' No-Training ')
    thread = env.make(9001)
    thread._capture_and_analyze()
    thread._detection_burst(thread.frame)
    thread.clock.now += 10
    thread._capture_and_analyze()
    assert len(env.training()) == 0
    assert _scalar(env.db, "SELECT COUNT(*) FROM vision_detections WHERE status='training'") == 0


def test_other_categories_and_job_required_tags_do_not_exclude(env):
    # required_tags is a scheduling constraint; it is deliberately not the exclusion field.
    _print(env.db, 9001, 8001, category='Mini Critters', tags=['no-training'])
    env.make(9001)._capture_and_analyze()
    assert len(env.training()) == 1


def test_a_failed_exclusion_check_saves_nothing(env, monkeypatch):
    from modules.vision import training_capture
    _print(env.db, 9001, 8001, category='Mini Critters')

    def broken(category):
        raise RuntimeError('boom')
    monkeypatch.setattr(training_capture, 'excluded_category', broken)
    env.make(9001)._capture_and_analyze()
    assert len(env.training()) == 0


# ------------------------------------------------------------------ Stage 0 still holds

def test_controlled_work_is_never_collected_by_any_new_path(env):
    _platform_admin(env.db)
    env.free['bytes'] = 1 * GIB                          # would alert if the guard were reached
    _print(env.db, 9001, 8001)
    _mark_controlled(env.db, 8001)
    thread = env.make(9001)
    thread._capture_and_analyze()
    thread.clock.now += 10
    thread._capture_and_analyze()
    thread._detection_burst(thread.frame)
    thread.clock.now += 10
    thread._capture_and_analyze()
    assert len(env.training()) == 0
    assert _scalar(env.db, "SELECT COUNT(*) FROM vision_detections WHERE status='training'") == 0
    assert _alerts(env.db, 20) == 0                      # refused before the storage guard ran


def test_uncontrolled_work_next_to_controlled_work_is_still_collected(env):
    _print(env.db, 9001, 8001)
    _print(env.db, 9002, 8002)
    _mark_controlled(env.db, 8002)
    env.make(9001)._capture_and_analyze()
    assert len(env.training()) == 1


# ------------------------------------------------------------------ stored config and API

def test_printer_config_round_trip_through_system_config(review_db):
    from modules.vision import training_capture as tc
    training = tc.validate_printer_training({'training_crop': CROP, 'training_interval_sec': 45, 'junk': 1})
    assert training == {'training_crop': CROP, 'training_interval_sec': 45}
    tc.save_printer_training(review_db, 9, training)
    review_db.commit()
    assert tc.printer_training_view(review_db, 9) == {'training_crop': CROP, 'training_interval_sec': 45}
    tc.save_printer_training(review_db, 9, tc.validate_printer_training({'training_crop': None}))
    review_db.commit()
    assert tc.printer_training_view(review_db, 9) == {'training_crop': None, 'training_interval_sec': 45}
    assert tc.printer_training_view(review_db, 10) == {'training_crop': None, 'training_interval_sec': None}
    with review_db.get_bind().connect() as conn:
        assert tc.load_printer_configs(conn) == {9: {'crop': None, 'interval_sec': 45}}


@pytest.mark.parametrize('body', [
    {'training_interval_sec': 0}, {'training_interval_sec': True}, {'training_interval_sec': 86401},
    {'training_interval_sec': '5'}, {'training_crop': {'x': 0.6, 'y': 0, 'w': 0.6, 'h': 1}},
])
def test_validate_printer_training_rejects(body):
    from modules.vision.training_capture import validate_printer_training
    with pytest.raises(ValueError):
        validate_printer_training(body)


def test_validate_printer_training_ignores_other_fields_and_allows_null():
    from modules.vision.training_capture import validate_printer_training
    assert validate_printer_training({'enabled': 1}) == {}
    assert validate_printer_training({'training_crop': None, 'training_interval_sec': None}) == {
        'training_crop': None, 'training_interval_sec': None}


def test_settings_api_stores_and_returns_the_training_fields(vision_http):
    client, _ = vision_http
    assert client.get('/api/printers/10/vision').json()['training_crop'] is None      # defaults path
    response = client.patch('/api/printers/9/vision', json={'training_crop': CROP, 'training_interval_sec': 45})
    assert response.status_code == 200 and response.json()['training_crop'] == CROP
    stored = client.get('/api/printers/9/vision').json()
    assert (stored['training_crop'], stored['training_interval_sec']) == (CROP, 45)
    assert client.patch('/api/printers/9/vision',
                        json={'training_crop': {'x': 0.6, 'y': 0, 'w': 0.6, 'h': 1}}).status_code == 422
    assert client.patch('/api/printers/9/vision', json={'training_interval_sec': 0}).status_code == 422
    assert client.patch('/api/printers/9/vision', json={'bogus': 1}).status_code == 400
    assert client.patch('/api/printers/9/vision', json={'training_crop': None}).status_code == 200
    assert client.get('/api/printers/9/vision').json()['training_crop'] is None
    assert client.patch('/api/vision/settings', json={'training_min_free_gb': -1}).status_code == 422
    assert client.patch('/api/vision/settings', json={'training_capture_interval_sec': 0}).status_code == 422
    assert client.patch('/api/vision/settings',
                        json={'training_min_free_gb': 5, 'training_capture_interval_sec': 300}).status_code == 200


def test_monitor_hands_the_training_config_to_the_thread(daemon, camera_printers, vision_http):
    instance, _ = daemon
    client, _ = vision_http
    assert client.patch('/api/printers/9/vision', json={'training_crop': CROP, 'training_interval_sec': 45}).status_code == 200
    assert client.patch('/api/vision/settings', json={'training_min_free_gb': 5}).status_code == 200
    instance._scan_printers()
    config = instance._threads[9].settings['training_capture']
    assert (config['crop'], config['interval_sec'], config['min_free_bytes']) == (CROP, 45, 5 * GIB)
    assert config['crop_invalid'] is False
    assert client.patch('/api/printers/9/vision', json={'training_crop': None}).status_code == 200
    instance._scan_printers()
    assert instance._threads[9].settings['training_capture']['crop'] is None


def test_burst_saves_the_unsaved_tick_before_the_detection(env):
    thread = env.make()
    thread._capture_and_analyze()                        # scheduled frame (1)
    thread.clock.now += 10
    thread._capture_and_analyze()                        # not due, not saved
    thread.clock.now += 10
    thread._capture_and_analyze()                        # not due; the tick before it is unsaved
    thread._detection_burst(thread.frame)
    assert len(env.training()) == 3                      # scheduled + the tick before + this tick


def test_detections_on_back_to_back_ticks_never_save_a_frame_twice(env):
    thread = env.make()
    thread._capture_and_analyze()                        # scheduled frame (1)
    thread.clock.now += 10
    thread._capture_and_analyze()                        # not due, not saved
    thread.clock.now += 10
    thread._capture_and_analyze()
    thread._detection_burst(thread.frame)                # saves the tick before and this tick (3)
    thread.clock.now += 10
    thread._capture_and_analyze()                        # the frame after the detection (4)
    thread._detection_burst(thread.frame)                # the tick before was saved by the first burst
    assert len(env.training()) == 4
