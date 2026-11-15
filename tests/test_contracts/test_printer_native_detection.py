"""A Bambu printer's own camera reports: one detection and one alert, no job outcome.

The reviewed table is checked against the bundled vendor lists by keyword only; no vendor
text is copied here. Monitor tests drive the real status handler over the real schema.
"""
import asyncio
import json
import re
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_hms_observation_boundaries import (
    hms_boundary, monitor_db, _running_packet_monitor,
)
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_vision_decision_behavior import vision

SPAGHETTI_HMS = [{'attr': 0x0C000300, 'code': 0x00030008}]
CHUTE_HMS = [{'attr': 0x0C000300, 'code': 0x00030006}]
CAMERA_HEALTH_HMS = [{'attr': 0x0C000100, 'code': 0x00020008}]
SPAGHETTI_ERROR = 0x0C00C004
CLUMP_ERROR = 0x0C008043


def bundles():
    from modules.printers.bambu_hms_bundle import BUNDLE_DIR, load_bundle
    families = {path.stem: load_bundle(path.stem) for path in sorted(BUNDLE_DIR.glob('???.json'))}
    assert len(families) >= 14 and all(families.values())
    return families


def texts(families, code):
    table = 'device_hms' if len(code) == 16 else 'device_error'
    return [family[table][code] for family in families.values() if family[table].get(code)]


def test_every_table_code_is_bundled_and_its_text_matches_its_kind():
    from modules.vision import printer_native as native
    families = bundles()
    assert set(native.PRINTER_DETECTION_CODES.values()) == set(native.KIND_KEYWORDS)
    assert not set(native.PRINTER_DETECTION_CODES) & native.NOT_DETECTION_CODES
    for code, kind in native.PRINTER_DETECTION_CODES.items():
        assert re.fullmatch(r'[0-9A-F]{16}|[0-9A-F]{8}', code)
        found = texts(families, code)
        assert found, f'{code} is in no bundled family'
        for sentence in found:
            assert any(word in sentence.lower() for word in native.KIND_KEYWORDS[kind]), (code, kind)
        # The alert and the title exist for every kind.
        assert native._ALERTS[kind] and native.alert_title('Xena', kind).startswith("Xena: the printer's camera saw ")


def test_ratchet_every_bundled_detection_sentence_is_reviewed():
    """A refreshed vendor list that adds a camera-detection code fails here until it is classified."""
    from modules.vision import printer_native as native
    subject = re.compile(r'AI (?:print )?monitor|spaghetti|first layer defect|clump|air-?printing|collapse|pile-?up|piled up|pileup', re.I)
    reviewed = set(native.PRINTER_DETECTION_CODES) | native.NOT_DETECTION_CODES
    unreviewed = sorted({code for family in bundles().values() for table in family.values()
                         for code, sentence in table.items() if subject.search(sentence)} - reviewed)
    assert unreviewed == []
    for code in native.NOT_DETECTION_CODES:
        assert texts(bundles(), code), f'{code} is no longer bundled: remove it from NOT_DETECTION_CODES'


@pytest.fixture
def native(hms_boundary, monkeypatch):
    """Real monitor + real schema; the worker runs inline, no camera, alerts captured."""
    from modules.notifications import alert_dispatch
    from modules.vision import printer_native as module
    db, handler, _, hms_alerts = hms_boundary
    alerts = []
    real_snapshot, real_dispatch = module._snapshot, alert_dispatch.dispatch_alert
    real_enqueue = module._enqueue_observation
    monkeypatch.setattr(module, 'get_db', handler.get_db)
    monkeypatch.setattr(module, '_enqueue_observation', lambda monitor, observation: module._observe(monitor, *observation))
    monkeypatch.setattr(module, '_start_worker', lambda target, *args: target(*args))
    monkeypatch.setattr(module, '_snapshot', lambda printer_id, kind: None)
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kwargs: alerts.append(kwargs))

    def monitor():
        created = _running_packet_monitor(hms_boundary, monkeypatch)
        created.serial = '094SYNTHETIC'
        return created
    return SimpleNamespace(db=db, module=module, alerts=alerts, hms_alerts=hms_alerts, monitor=monitor,
                           real_snapshot=real_snapshot, real_dispatch=real_dispatch, real_enqueue=real_enqueue)


def send(monitor, *, retained=False, **packet):
    monitor._on_status(SimpleNamespace(raw_data={'print': packet}, mqtt_retained=retained, report_age_seconds=None))


def detections(db):
    db.expire_all()
    rows = db.execute(text('SELECT id,printer_id,print_job_id,detection_type,confidence,status,frame_path,metadata_json '
                           'FROM vision_detections ORDER BY id')).mappings().all()
    return [dict(row, meta=json.loads(row['metadata_json'])) for row in rows]


def job_status(db):
    db.expire_all()
    return db.execute(text('SELECT status FROM print_jobs WHERE id=201')).scalar_one()


def test_hms_item_records_one_detection_and_one_alert(native):
    monitor = native.monitor()
    for _ in range(3):   # repeat packets
        send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    send(monitor, mc_percent=40)   # no hms key: the set stands
    send(monitor, hms=None)
    [row] = detections(native.db)
    assert (row['printer_id'], row['print_job_id'], row['detection_type'], row['confidence']) == (9, 201, 'spaghetti', 1.0)
    assert row['status'] == 'informational' and row['frame_path'] is None
    assert row['meta']['source'] == 'printer' and row['meta']['code'] == '0C00030000030008'
    assert 'spaghetti' in row['meta']['text'].lower()
    [alert] = native.alerts
    assert alert['alert_type'] == 'spaghetti_detected' and alert['severity'] == 'critical'
    assert alert['printer_id'] == 9 and alert['job_id'] is None
    assert alert['title'] == "P1S-09: the printer's camera saw possible spaghetti"
    assert alert['message'] == row['meta']['text']
    assert alert['metadata']['detection_id'] == row['id'] and alert['metadata']['source'] == 'printer'
    # Common notice: the existing HMS path still raises nothing, and nothing ended the print.
    assert native.hms_alerts == [] and job_status(native.db) == 'running'


def test_print_error_records_one_detection_and_one_alert(native):
    monitor = native.monitor()
    for value in (CLUMP_ERROR, CLUMP_ERROR, str(CLUMP_ERROR)):
        send(monitor, gcode_state='PAUSE', gcode_file='current.3mf', print_error=value)
    [row] = detections(native.db)
    assert (row['detection_type'], row['print_job_id'], row['meta']['code']) == ('nozzle_clump', 201, '0C008043')
    assert 'clumping' in row['meta']['text'].lower()
    [alert] = native.alerts
    assert alert['title'] == "P1S-09: the printer's camera saw possible nozzle clumping"
    # A pause with a detection code never fails, cancels or completes the print.
    assert job_status(native.db) == 'running'


def test_reconnect_and_restart_do_not_repeat(native):
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS, print_error=0)
    monitor._connection_generation += 1   # reconnect: the printer pushes everything again
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS, print_error=0)
    restarted = native.monitor()          # restart: no memory, only the stored identities
    assert getattr(restarted, '_native_detection_codes', None) is None
    send(restarted, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    assert restarted._native_detection_codes == {'0C00030000030008'}
    assert len(detections(native.db)) == 1 and len(native.alerts) == 1


def test_clear_then_recurrence_is_a_second_detection(native):
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    send(monitor, hms=[])
    assert native.db.execute(text("SELECT value FROM system_config WHERE key='printer.native_detection_codes.9'")).scalar_one() in ('[]', [])
    send(monitor, hms=SPAGHETTI_HMS)
    send(monitor, print_error=SPAGHETTI_ERROR)   # same problem under a second code: not a third
    send(monitor, print_error=0)
    assert [row['detection_type'] for row in detections(native.db)] == ['spaghetti', 'spaghetti']
    assert len(native.alerts) == 2


def test_two_kinds_in_one_print_are_two_detections(native):
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS + CHUTE_HMS)
    rows = detections(native.db)
    assert sorted((row['detection_type'], row['print_job_id']) for row in rows) == [('chute_pileup', 201), ('spaghetti', 201)]
    by_kind = {alert['metadata']['detection_type']: alert for alert in native.alerts}
    assert by_kind['chute_pileup']['severity'] == 'warning' and by_kind['spaghetti']['severity'] == 'critical'
    assert by_kind['chute_pileup']['title'] == "P1S-09: the printer's camera saw a possible full waste chute"


def test_no_running_job_records_a_detection_without_a_job(native):
    native.db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=201"))
    native.db.commit()
    monitor = native.monitor()
    monitor._state = {'gcode_state': 'IDLE'}
    monitor._last_gcode_state = 'IDLE'
    monitor._current_job_id = None
    send(monitor, hms=CHUTE_HMS)
    [row] = detections(native.db)
    assert row['print_job_id'] is None and row['detection_type'] == 'chute_pileup'
    assert len(native.alerts) == 1


def test_camera_health_marker_and_unknown_codes_record_nothing(native):
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=CAMERA_HEALTH_HMS + [{'attr': 0x0C000300, 'code': 0x0002000C}],
         print_error=0x0C008034)
    send(monitor, print_error=0x0C008009)
    send(monitor, print_error='not a number', hms=['malformed', {'attr': -1, 'code': 3}])
    assert detections(native.db) == [] and native.alerts == []
    assert job_status(native.db) == 'running'


def test_retained_report_records_nothing(native):
    monitor = native.monitor()
    send(monitor, retained=True, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS, print_error=SPAGHETTI_ERROR)
    assert detections(native.db) == [] and native.alerts == []


def test_failed_write_is_retried_and_never_reaches_the_monitor(native, monkeypatch):
    monitor = native.monitor()
    real = native.module.execute_insert_returning_id
    monkeypatch.setattr(native.module, 'execute_insert_returning_id',
                        lambda *args: (_ for _ in ()).throw(RuntimeError('Synthetic write failure')))
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)   # does not raise
    assert detections(native.db) == [] and native.alerts == []
    monkeypatch.setattr(native.module, 'execute_insert_returning_id', real)
    send(monitor, hms=SPAGHETTI_HMS)
    assert len(detections(native.db)) == 1 and len(native.alerts) == 1


def test_education_print_is_recorded_but_not_alerted(native):
    filename = 'odin-' + 'a' * 32 + '.3mf'
    native.db.execute(text('UPDATE print_jobs SET filename=:name WHERE id=201'), {'name': filename})
    native.db.commit()
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file=filename, hms=CHUTE_HMS)
    assert len(detections(native.db)) == 1 and native.alerts == []


def jpeg():
    import cv2
    import numpy as np
    ok, encoded = cv2.imencode('.jpg', np.full((24, 32, 3), 90, dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def test_camera_picture_is_attached_and_detection_is_pending(native, monkeypatch, tmp_path):
    import httpx
    from modules.vision import frame_storage
    native.db.execute(text("UPDATE printers SET camera_enabled=1,camera_url='rtsp://192.0.2.9/stream' WHERE id=9"))
    native.db.commit()
    monkeypatch.setattr(frame_storage, 'VISION_FRAMES_DIR', str(tmp_path / 'frames'))
    requests = []

    def respond(request):
        requests.append((request.method, str(request.url).split('?')[0],
                         dict(request.url.params), request.extensions['timeout']['read']))
        return httpx.Response(200, content=jpeg())
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    saved_training = []
    monkeypatch.setattr(frame_storage, 'save_training_frame', lambda *args: saved_training.append(args))
    # The fixture replaced _snapshot with "no camera"; this test runs the real one.
    monkeypatch.setattr(native.module, '_snapshot', native.real_snapshot)
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    [row] = detections(native.db)
    assert row['status'] == 'pending'
    assert re.fullmatch(r'9/\d{8}_\d{6}_\d{6}_spaghetti_[0-9a-f]{32}\.jpg', row['frame_path'])
    assert (tmp_path / 'frames' / row['frame_path']).read_bytes().startswith(b'\xff\xd8\xff')
    assert requests[0][0] == 'GET' and requests[0][1].endswith('/api/frame.jpeg')
    assert requests[0][2] == {'src': 'printer_9'} and requests[0][3] == 3.0
    # Never the training folder.
    assert saved_training == [] and not (tmp_path / 'frames' / '9' / 'training').exists()
    assert len(native.alerts) == 1


@pytest.mark.parametrize('reason', ['no_camera', 'bad_status', 'not_an_image', 'transport_error'])
def test_no_camera_or_no_frame_is_informational(native, monkeypatch, tmp_path, reason):
    import httpx
    from modules.vision import frame_storage
    monkeypatch.setattr(frame_storage, 'VISION_FRAMES_DIR', str(tmp_path / 'frames'))
    if reason != 'no_camera':
        native.db.execute(text("UPDATE printers SET camera_enabled=1,camera_url='rtsp://192.0.2.9/stream' WHERE id=9"))
        native.db.commit()

    def respond(request):
        if reason == 'no_camera':
            raise AssertionError('No request without a camera')
        if reason == 'transport_error':
            raise httpx.ConnectTimeout('Synthetic timeout')
        return httpx.Response(200 if reason == 'not_an_image' else 503, content=b'not a jpeg')
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(native.module, '_snapshot', native.real_snapshot)
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    [row] = detections(native.db)
    assert row['status'] == 'informational' and row['frame_path'] is None
    assert len(native.alerts) == 1   # the alert does not depend on the picture


def test_slow_snapshot_does_not_delay_the_monitor(native, monkeypatch):
    release = threading.Event()
    workers = []

    def slow(printer_id, kind):
        release.wait(10)
        return None
    monkeypatch.setattr(native.module, '_snapshot', slow)
    real_thread = threading.Thread

    def start(target, *args):
        worker = real_thread(target=target, args=args, daemon=True)
        workers.append(worker)
        worker.start()
    monkeypatch.setattr(native.module, '_start_worker', start)
    monitor = native.monitor()
    started = time.monotonic()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    send(monitor, mc_percent=41)
    elapsed = time.monotonic() - started
    try:
        # The status handler returned while the picture is still outstanding;
        # the detection is already stored and the alert waits for the worker.
        assert elapsed < 2 and workers[0].is_alive()
        assert [row['status'] for row in detections(native.db)] == ['informational']
        assert native.alerts == []
    finally:
        release.set()
        workers[0].join(10)
    assert len(native.alerts) == 1


def test_default_worker_is_a_daemon_thread():
    from modules.vision import printer_native as module
    seen = []
    module._start_worker(lambda *args: seen.append((threading.current_thread().daemon, args)), 1, 2)
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)
    assert seen == [(True, (1, 2))]
    assert module.SNAPSHOT_SECONDS == 3.0


@pytest.mark.parametrize('controlled', [False, True])
def test_controlled_print_alert_reaches_only_its_audience(native, monkeypatch, controlled):
    """Real recipient selection: the originating print's audience is checked."""
    from modules.notifications import alert_dispatch
    db = native.db
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,quantity,status,printer_id,duration_hours,hold,priority,charged_to_org_id) "
                    "VALUES(203,12,'Synthetic generic',1,'printing',9,1,0,3,1)"))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=203 WHERE id=201'))
    if controlled:
        db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
        db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,2)'))
        db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                        "VALUES(203,'CUI',1,'Secure',30,'fixture',CURRENT_TIMESTAMP)"))
    db.commit()
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', native.real_dispatch)
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    db.expire_all()
    rows = db.execute(text("SELECT user_id,alert_type,severity,title,printer_id FROM alerts ORDER BY user_id")).all()
    assert [row[0] for row in rows] == ([2] if controlled else [1, 2])
    assert {tuple(row[1:]) for row in rows} == {
        ('spaghetti_detected', 'critical', "P1S-09: the printer's camera saw possible spaghetti", 9)}
    assert len(detections(db)) == 1


def test_uncontrolled_job_keeps_printer_alert_audience(native, monkeypatch):
    from modules.notifications import alert_dispatch
    db = native.db
    db.execute(text("UPDATE education_submissions SET approved_printer_id=NULL WHERE approved_printer_id=9"))
    db.execute(text("DELETE FROM education_cost_center_printers WHERE printer_id=9"))
    db.execute(text("UPDATE printers SET org_id=NULL WHERE id=9"))
    db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(2,'Other organization',1)"))
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) "
                    "VALUES(3,'other-viewer','fixture','viewer',1,2)"))
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,quantity,status,printer_id,duration_hours,hold,priority,charged_to_org_id) "
                    "VALUES(203,12,'Synthetic generic',1,'printing',9,1,0,3,1)"))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=203 WHERE id=201'))
    db.commit()
    from core.recipient_policy import resolve_alert_scope
    assert resolve_alert_scope(db, printer_id=9)[0] == {1, 2, 3}
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', native.real_dispatch)
    send(native.monitor(), gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    assert db.execute(text('SELECT user_id,job_id FROM alerts ORDER BY user_id')).all() == [
        (1, None), (2, None), (3, None)]
    [row] = detections(db)
    assert row['print_job_id'] == 201


def test_policy_lookup_failure_preserves_detections_without_alerting(native, monkeypatch):
    from core import controlled_parts
    db = native.db
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,quantity,status,printer_id,duration_hours,hold,priority,charged_to_org_id) "
                    "VALUES(203,12,'Synthetic generic',1,'printing',9,1,0,3,1)"))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=203 WHERE id=201'))
    db.commit()
    lookups = []
    snapshots = []

    def unavailable(conn, kind, identifier):
        lookups.append((kind, identifier))
        raise RuntimeError('Synthetic policy lookup failure')

    monkeypatch.setattr(controlled_parts, 'policies', unavailable)
    monkeypatch.setattr(native.module, '_snapshot', lambda *args: snapshots.append(args))
    monitor = native.monitor()
    for _ in range(2):
        send(monitor, hms=SPAGHETTI_HMS + CHUTE_HMS)
    rows = detections(db)
    assert len(rows) == 2
    assert {row['print_job_id'] for row in rows} == {201}
    assert {row['status'] for row in rows} == {'informational'}
    assert len(snapshots) == 2
    assert lookups == [('job', 203), ('job', 203)]
    assert native.alerts == []  # Unknown audience must never become printer-wide.


def test_repeated_queued_reports_coalesce_without_losing_transitions(native, monkeypatch):
    workers = []
    monkeypatch.setattr(native.module, '_enqueue_observation', native.real_enqueue)
    monkeypatch.setattr(native.module, '_start_worker', lambda target, *args: workers.append((target, args)))
    monitor = native.monitor()
    with monitor._lock:
        for hms in (SPAGHETTI_HMS, [], SPAGHETTI_HMS):
            for _ in range(1000):
                native.module.observe(monitor, {'hms': hms})
    assert len(workers) == 1
    assert len(monitor._native_detection_pending) == 3
    monkeypatch.setattr(native.module, '_start_worker', lambda target, *args: target(*args))
    target, args = workers[0]
    target(*args)
    assert monitor._native_detection_pending is None
    assert len(detections(native.db)) == 2 and len(native.alerts) == 2


@pytest.mark.parametrize('failure', ['load', 'insert', 'save', 'clear'])
def test_database_failure_retries_queued_transitions_in_order(native, monkeypatch, failure):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError

    workers = []
    monkeypatch.setattr(native.module, '_enqueue_observation', native.real_enqueue)
    monkeypatch.setattr(native.module, '_start_worker', lambda target, *args: workers.append((target, args)))
    monitor = native.monitor()
    if failure == 'clear':
        # The clear must also survive a failed write, or the recurrence is lost.
        native.module._observe(monitor, {'0C00030000030008'}, None, 201, 'current.3mf')
        target, args = workers.pop(0)
        target(*args)
    reports = ([], SPAGHETTI_HMS) if failure == 'clear' else (SPAGHETTI_HMS, [], SPAGHETTI_HMS)
    with monitor._lock:
        for hms in reports:
            native.module.observe(monitor, {'hms': hms})
    pending = list(monitor._native_detection_pending)
    engine = native.db.get_bind()
    statements = {'load': 'SELECT value FROM system_config', 'insert': 'INSERT INTO vision_detections',
                  'save': 'INSERT INTO system_config', 'clear': 'INSERT INTO system_config'}
    failures = []

    def fail_once(conn, cursor, statement, parameters, context, executemany):
        if not failures and statement.startswith(statements[failure]):
            failures.append(statement)
            raise OperationalError(statement, parameters, RuntimeError('Synthetic transient database failure'))

    event.listen(engine, 'before_cursor_execute', fail_once)
    try:
        target, args = workers.pop(0)
        target(*args)
    finally:
        event.remove(engine, 'before_cursor_execute', fail_once)
    assert len(failures) == 1
    assert monitor._native_detection_running is False
    assert list(monitor._native_detection_pending or []) == pending
    assert len(detections(native.db)) == (1 if failure == 'clear' else 0)
    assert len(native.alerts) == (1 if failure == 'clear' else 0)  # failed transactions never dispatch alerts
    # A later clear restarts the worker even though the original error is gone.
    with monitor._lock:
        native.module.observe(monitor, {'hms': []})
    target, args = workers.pop(0)
    target(*args)
    assert monitor._native_detection_pending is None
    assert monitor._native_detection_running is False
    assert monitor._native_detection_codes == frozenset()
    rows = detections(native.db)
    assert len(rows) == 2 and [row['print_job_id'] for row in rows] == [201, 201]
    for target, args in workers:
        target(*args)
    assert len(native.alerts) == 2


def test_aborted_drain_restarts_on_next_report_without_losing_queue(native, monkeypatch):
    workers = []
    monkeypatch.setattr(native.module, '_enqueue_observation', native.real_enqueue)
    monkeypatch.setattr(native.module, '_start_worker', lambda target, *args: workers.append((target, args)))
    monitor = native.monitor()
    with monitor._lock:
        native.module.observe(monitor, {'hms': SPAGHETTI_HMS})
        native.module.observe(monitor, {'hms': []})
    real_observe = native.module._observe

    def abort(*args):
        raise SystemExit('Synthetic worker exit')

    monkeypatch.setattr(native.module, '_observe', abort)
    target, args = workers[0]
    with pytest.raises(SystemExit, match='Synthetic worker exit'):
        target(*args)
    monkeypatch.setattr(native.module, '_observe', real_observe)
    with monitor._lock:
        native.module.observe(monitor, {'hms': SPAGHETTI_HMS})
    assert len(workers) == 2, 'A terminated worker must not strand later reports'
    monkeypatch.setattr(native.module, '_start_worker', lambda target, *args: target(*args))
    target, args = workers[1]
    target(*args)
    assert monitor._native_detection_pending is None
    assert len(detections(native.db)) == 2 and len(native.alerts) == 2


@pytest.mark.parametrize('error', [RuntimeError, MemoryError])
def test_finish_thread_start_failure_still_alerts_each_detection(native, monkeypatch, error):
    workers = []
    finishing_threads = []

    def start(target, *args):
        if target is native.module._finish:
            raise error("can't start new thread")
        worker = threading.Thread(target=target, args=args, daemon=True)
        workers.append(worker)
        worker.start()

    def snapshot(*args):
        finishing_threads.append(threading.current_thread())
        return None

    monkeypatch.setattr(native.module, '_enqueue_observation', native.real_enqueue)
    monkeypatch.setattr(native.module, '_start_worker', start)
    monkeypatch.setattr(native.module, '_snapshot', snapshot)
    monitor = native.monitor()
    with monitor._lock:
        native.module.observe(monitor, {'hms': SPAGHETTI_HMS + CHUTE_HMS})
    for worker in workers:
        worker.join(5)
    assert all(not worker.is_alive() for worker in workers)
    assert len(detections(native.db)) == 2
    assert len(native.alerts) == 2
    assert finishing_threads == [workers[0], workers[0]]
    with monitor._lock:
        native.module.observe(monitor, {'hms': SPAGHETTI_HMS + CHUTE_HMS})
    for worker in workers:
        worker.join(5)
    assert len(detections(native.db)) == 2 and len(native.alerts) == 2


@pytest.mark.parametrize('error', [SystemExit, KeyboardInterrupt])
def test_finish_thread_start_exit_finishes_batch_before_propagating(native, monkeypatch, error):
    workers = []

    def start(target, *args):
        if target is native.module._finish:
            raise error('Synthetic worker exit')
        workers.append((target, args))

    monkeypatch.setattr(native.module, '_enqueue_observation', native.real_enqueue)
    monkeypatch.setattr(native.module, '_start_worker', start)
    monitor = native.monitor()
    with monitor._lock:
        native.module.observe(monitor, {'hms': SPAGHETTI_HMS + CHUTE_HMS})
    target, args = workers[0]
    with pytest.raises(error, match='Synthetic worker exit'):
        target(*args)
    rows = detections(native.db)
    assert len(rows) == 2
    assert {alert['metadata']['detection_id'] for alert in native.alerts} == {row['id'] for row in rows}
    assert len(native.alerts) == 2
    assert monitor._native_detection_running is False
    with monitor._lock:
        native.module.observe(monitor, {'hms': SPAGHETTI_HMS + CHUTE_HMS})
    target, args = workers[1]
    target(*args)
    assert monitor._native_detection_pending is None
    assert len(detections(native.db)) == 2 and len(native.alerts) == 2


def test_inline_alert_failure_does_not_strand_sibling(native, monkeypatch):
    from modules.notifications import alert_dispatch
    attempted = []
    snapshots = []

    def start(target, *args):
        if target is native.module._finish:
            raise RuntimeError("can't start new thread")
        target(*args)

    def dispatch(**kwargs):
        attempted.append(kwargs['metadata']['detection_id'])
        if len(attempted) == 1:
            raise RuntimeError('Synthetic dispatch failure')
        native.alerts.append(kwargs)

    monkeypatch.setattr(native.module, '_start_worker', start)
    monkeypatch.setattr(native.module, '_snapshot', lambda *args: snapshots.append(args))
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', dispatch)
    monitor = native.monitor()
    for _ in range(2):
        send(monitor, hms=SPAGHETTI_HMS + CHUTE_HMS)
    rows = detections(native.db)
    assert len(rows) == 2 and len(snapshots) == 2
    assert attempted == [row['id'] for row in rows]
    assert [alert['metadata']['detection_id'] for alert in native.alerts] == [rows[1]['id']]


def test_controlled_alert_keeps_originating_job_after_snapshot(native, monkeypatch):
    from modules.notifications import alert_dispatch
    db = native.db
    db.execute(text("INSERT INTO jobs(id,model_id,item_name,quantity,status,printer_id,duration_hours,hold,priority,charged_to_org_id) "
                    "VALUES(203,12,'Synthetic controlled',1,'printing',9,1,0,3,1)"))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=203 WHERE id=201'))
    db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
    db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,2)'))
    db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                    "VALUES(203,'CUI',1,'Secure',30,'fixture',CURRENT_TIMESTAMP)"))
    db.commit()
    monitor = native.monitor()

    def finish_print_during_snapshot(*args):
        db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=201"))
        db.execute(text("UPDATE jobs SET status='completed' WHERE id=203"))
        db.execute(text("UPDATE printers SET gcode_state='IDLE' WHERE id=9"))
        db.commit()
        monitor._current_job_id = None
        monitor._linked_job_id = None
        return None

    monkeypatch.setattr(native.module, '_snapshot', finish_print_during_snapshot)
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', native.real_dispatch)
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    assert db.execute(text('SELECT user_id,job_id FROM alerts ORDER BY user_id')).all() == [(2, 203)]


def test_writer_contention_does_not_hold_monitor_lock_and_preserves_order(native, monkeypatch):
    from sqlalchemy import event
    engine = native.db.get_bind()
    monitor = native.monitor()
    attempted = threading.Event()
    returned = threading.Event()
    workers = []

    def start(target, *args):
        worker = threading.Thread(target=target, args=args, daemon=True)
        workers.append(worker)
        worker.start()

    def before_execute(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('INSERT INTO vision_detections'):
            attempted.set()

    def report():
        with monitor._lock:
            native.module.observe(monitor, {'hms': SPAGHETTI_HMS})
        returned.set()

    monkeypatch.setattr(native.module, '_start_worker', start)
    monkeypatch.setattr(native.module, '_enqueue_observation', native.real_enqueue)
    event.listen(engine, 'before_cursor_execute', before_execute)
    blocker = engine.raw_connection()
    blocker.execute('BEGIN IMMEDIATE')
    reporter = threading.Thread(target=report, daemon=True)
    reporter.start()
    try:
        assert attempted.wait(2), 'Detection insert was never attempted'
        assert returned.wait(0.5), 'Detection persistence blocked the monitor under its lock'
        # A clear and recurrence must retain order while the first insert waits.
        with monitor._lock:
            monitor._current_job_id = None
            native.module.observe(monitor, {'hms': []})
            native.module.observe(monitor, {'hms': SPAGHETTI_HMS})
            native.module.observe(monitor, {'hms': SPAGHETTI_HMS})
    finally:
        blocker.rollback()
        blocker.close()
        reporter.join(5)
        for worker in workers:
            worker.join(5)
        event.remove(engine, 'before_cursor_execute', before_execute)
    assert not reporter.is_alive() and all(not worker.is_alive() for worker in workers)
    assert [row['print_job_id'] for row in detections(native.db)] == [201, None]
    assert len(native.alerts) == 2


def test_snapshot_total_deadline_covers_headers_and_body(native, monkeypatch):
    import httpx
    native.db.execute(text("UPDATE printers SET camera_enabled=1,camera_url='rtsp://192.0.2.9/stream' WHERE id=9"))
    native.db.commit()
    monkeypatch.setattr(native.module, 'SNAPSHOT_SECONDS', 0.4)
    body_finished = []
    body_started = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            body_started.append(True)
            await asyncio.sleep(0.3)
            body_finished.append(True)
            yield jpeg()

    async def respond(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, stream=Body())

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setattr(native.module, '_snapshot', native.real_snapshot)
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS)
    assert body_started == [True], 'The request must reach the async body before timing out'
    assert body_finished == [], 'The body consumed a second per-phase timeout after slow headers'
    assert [row['status'] for row in detections(native.db)] == ['informational']
    assert len(native.alerts) == 1


def test_stats_and_list_report_the_source(native):
    from modules.vision.routes import detections as routes
    monitor = native.monitor()
    send(monitor, gcode_state='RUNNING', gcode_file='current.3mf', hms=SPAGHETTI_HMS + CHUTE_HMS)
    db = native.db
    db.execute(text("INSERT INTO vision_detections(printer_id,detection_type,confidence,status,created_at) "
                    "VALUES(9,'spaghetti',0.8,'confirmed',CURRENT_TIMESTAMP),(9,'detachment',0.7,'pending',CURRENT_TIMESTAMP),"
                    "(9,'training_sample',0,'training',CURRENT_TIMESTAMP)"))
    db.execute(text("UPDATE vision_detections SET status='dismissed' WHERE detection_type='chute_pileup'"))
    db.commit()
    admin = {'id': 99, 'role': 'admin', 'group_id': None}
    viewer = {'id': 1, 'role': 'viewer', 'group_id': 1}

    def listed(user, source=None):
        return asyncio.run(routes.list_vision_detections(
            printer_id=None, detection_type=None, status_filter=None, source=source,
            limit=50, offset=0, current_user=user, db=db))
    everything = listed(admin)
    assert everything['total'] == 4
    assert sorted(item['source'] for item in everything['items']) == ['printer', 'printer', 'vigil', 'vigil']
    assert {item['detection_type'] for item in listed(admin, 'printer')['items']} == {'spaghetti', 'chute_pileup'}
    assert listed(admin, 'printer')['total'] == 2
    assert {item['detection_type'] for item in listed(admin, 'vigil')['items']} == {'spaghetti', 'detachment'}
    assert all(item['source'] == 'vigil' for item in listed(admin, 'vigil')['items'])
    # Detections stay platform-maintenance only, whatever their source.
    assert listed(viewer) == {'items': [], 'total': 0}
    stats = asyncio.run(routes.get_vision_stats(days=7, current_user=admin, db=db))
    assert stats['by_source'] == {
        'printer': {'total': 2, 'confirmed': 0, 'dismissed': 1},
        'vigil': {'total': 2, 'confirmed': 1, 'dismissed': 0},
    }
    assert stats['by_status'] == {'informational': 1, 'dismissed': 1, 'confirmed': 1, 'pending': 1}
    assert asyncio.run(routes.get_vision_stats(days=7, current_user=viewer, db=db))['by_source'] == {}


def test_vigil_auto_pause_never_claims_a_printer_source_detection(vision, review_db, monkeypatch):
    from modules.printers.adapters import moonraker
    thread, _, module = vision
    monkeypatch.setattr(module, 'engine', review_db.get_bind())
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='synthetic.invalid',gcode_state='RUNNING' WHERE id=9"))
    review_db.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status,metadata_json) VALUES"
                           "(101,9,'spaghetti',0.9,'pending',NULL),"
                           "(102,9,'spaghetti',1.0,'pending',:spaced),(103,9,'chute_pileup',1.0,'pending',:compact)"),
                      {'spaced': json.dumps({'source': 'printer', 'code': '0C00030000030008', 'text': 'Synthetic'}),
                       'compact': '{"source":"printer","code":"0C00C006"}'})
    review_db.commit()

    class Adapter:
        def __init__(self, host): pass
        def pause_print(self): return True
    monkeypatch.setattr(moonraker, 'MoonrakerPrinter', Adapter)
    thread._analyzed_frames = 3
    thread._first_analysis_at = module.time.monotonic() - 60
    monkeypatch.setattr(thread, '_live_printing', lambda: True)
    thread._auto_pause()
    review_db.expire_all()
    assert dict(review_db.execute(text('SELECT id,status FROM vision_detections ORDER BY id')).all()) == {
        101: 'auto_paused', 102: 'pending', 103: 'pending'}
