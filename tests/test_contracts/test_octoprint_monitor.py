"""OctoPrint monitor: start, progress and end through the shared observation path, replayed from
transcripts built from OctoPrint's public REST API documentation (no OctoPrint code)."""
import threading
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import text

from tests.support.device_http import route_device_requests
from tests.support.octoprint_http import Octo, Reply, flags, job_body, printer_body
from tests.test_contracts.test_cert_A_harness import bind_engine, fernet_env, review_db  # noqa: F401

STREAM = 'http://192.0.2.60:8080/?action=stream'
PRINTING = flags(operational=True, printing=True)
PAUSED = flags(operational=True, paused=True)
IDLE = flags(operational=True, ready=True)
ERROR = flags(error=True)


@pytest.fixture
def world(review_db, fernet_env, monkeypatch):  # noqa: F811
    from modules.printers.monitors import octoprint_monitor as module
    import modules.notifications.job_events as job_events
    import modules.printers.route_utils as route_utils
    engine = review_db.get_bind()
    bind_engine(monkeypatch, engine)
    monkeypatch.setattr(module, 'engine', engine)
    monkeypatch.setenv('ODIN_HOST_IP', '192.0.2.250')
    monkeypatch.setattr('core.itar.enforce_request_destination', lambda url: None)
    review_db.execute(text(
        "INSERT INTO printers(id,name,model,is_active,api_type,api_host,org_id,shared,gcode_state,slot_count,"
        "tags,timelapse_enabled,camera_enabled) VALUES "
        "(501,'Cert Octo',NULL,1,'octoprint','192.0.2.60',1,0,'IDLE',1,'[]',0,1)"))
    review_db.commit()
    clock = [6_000_000.0]
    events, go2rtc = [], []
    monkeypatch.setattr(module, 'ws_push', lambda kind, payload=None, **kw: events.append((kind, payload)))
    monkeypatch.setattr(module, '_mqtt_republish', None, raising=False)
    monkeypatch.setattr(job_events, '_active_print_jobs', {})
    monkeypatch.setattr(route_utils, '_go2rtc_restart_with_lock', lambda config, **kw: go2rtc.append(config))
    http = Octo()
    http.routes[('GET', '/api/printer')] = Reply(200, printer_body(PRINTING))
    http.routes[('GET', '/api/job')] = Reply(200, job_body())
    http.routes[('GET', '/api/settings')] = Reply(200, {'webcam': {'streamUrl': STREAM}})
    route_device_requests(monkeypatch, get=http.verb('GET'), post=http.verb('POST'))
    monitor = module.OctoPrintMonitorThread(501, 'Cert Octo', '192.0.2.60', api_key='synthetic-key')
    monkeypatch.setattr(monitor, '_try_dispatch', lambda: None)
    monkeypatch.setattr(module, 'threading', SimpleNamespace(
        Thread=lambda *a, **kw: SimpleNamespace(start=lambda: None),
        RLock=threading.RLock, Lock=threading.Lock, Event=threading.Event))

    def run(polls, step=11, before_poll=None):
        count = [0]

        def sleep(seconds):
            count[0] += 1
            clock[0] += step
            if before_poll:
                before_poll(count[0])
            if count[0] >= polls:
                monitor._running = False
        monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock[0], sleep=sleep))
        monitor._running = True
        monitor.run()
    return SimpleNamespace(db=review_db, http=http, run=run, module=module, monitor=monitor,
                           clock=clock, events=events, go2rtc=go2rtc)


def row(db, columns):
    db.expire_all()
    return dict(db.execute(text(f'SELECT {columns} FROM printers WHERE id=501')).mappings().one())


def jobs(db):
    db.expire_all()
    return [tuple(r) for r in db.execute(text('SELECT status,progress_percent FROM print_jobs WHERE printer_id=501 ORDER BY id'))]


def script(world, *steps):
    """steps[i] is applied after poll i+1: (flags, job_body or None) or an exception/Reply for /api/printer."""
    def apply(poll):
        if poll > len(steps):
            return
        printer, job = steps[poll - 1]
        world.http.routes[('GET', '/api/printer')] = printer if isinstance(printer, (Reply, BaseException)) else Reply(200, printer_body(printer))
        if job is not None:
            world.http.routes[('GET', '/api/job')] = Reply(200, job)
    return apply


def test_start_progress_and_completion_through_the_shared_path(world):
    world.run(3, before_poll=script(world, (PRINTING, job_body(completion=80.0, left=300)), (IDLE, job_body(completion=100.0, left=0, state='Operational'))))
    assert jobs(world.db) == [('completed', 80)]
    r = row(world.db, 'gcode_state,last_seen')
    assert r['gcode_state'] == 'IDLE' and r['last_seen']
    printing = [p for kind, p in world.events if kind == 'printer_telemetry' and p['state'] == 'PRINTING']
    assert printing[0]['progress'] == 42.0 and printing[0]['remaining_min'] == 14
    assert [p for kind, p in world.events if p['state'] == 'IDLE']


def test_a_print_that_stops_short_is_cancelled_never_completed(world):
    world.run(2, before_poll=script(world, (IDLE, job_body(completion=60.0, state='Operational'))))
    assert [j[0] for j in jobs(world.db)] == ['cancelled']


def test_idle_without_any_progress_figure_is_not_a_success(world):
    world.run(2, before_poll=script(world, (IDLE, {'job': {'file': {'name': None}}, 'progress': {}, 'state': 'Operational'})))
    assert [j[0] for j in jobs(world.db)] == ['cancelled']


def test_error_state_records_history_and_fails_the_job(world):
    world.run(2, before_poll=script(world, (ERROR, None)))
    world.db.expire_all()
    history = world.db.execute(text('SELECT code,message,severity,source FROM hms_error_history WHERE printer_id=501')).all()
    assert [tuple(h) for h in history] == [('OCTOPRINT_ERROR', 'Printer reported error state', 'error', 'octoprint')]
    assert [j[0] for j in jobs(world.db)] == ['failed']


def test_pause_and_resume_keep_one_running_job(world):
    world.run(3, before_poll=script(world, (PAUSED, None), (PRINTING, None)))
    assert [j[0] for j in jobs(world.db)] == ['running']
    assert row(world.db, 'gcode_state')['gcode_state'] == 'PRINTING'


@pytest.mark.parametrize('initial_state', [PRINTING, PAUSED], ids=['printing', 'paused'])
@pytest.mark.parametrize('unread_job', [requests.exceptions.Timeout('slow'), Reply(200, job_body(name=None))],
                         ids=['timeout', 'missing-filename'])
def test_start_waits_for_identity_then_retries(world, initial_state, unread_job):
    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(initial_state))
    world.http.routes[('GET', '/api/job')] = unread_job
    world.run(2)
    assert jobs(world.db) == []
    assert row(world.db, 'last_seen')['last_seen']  # monitoring continues while identity is unavailable

    world.http.routes[('GET', '/api/job')] = Reply(200, job_body(name='recovered.gcode'))
    world.run(1)
    assert [j[0] for j in jobs(world.db)] == ['running']
    assert world.db.execute(text('SELECT filename FROM print_jobs WHERE printer_id=501')).scalar_one() == 'recovered.gcode'
    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(PRINTING))
    world.run(2)
    assert jobs(world.db) == [('running', 42)]


def test_initial_pause_establishes_attempt_before_resume(world):
    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(PAUSED))
    world.run(1)
    assert [j[0] for j in jobs(world.db)] == ['running']
    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(PRINTING))
    world.run(2)
    assert jobs(world.db) == [('running', 42)]
    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(IDLE))
    world.http.routes[('GET', '/api/job')] = Reply(200, job_body(completion=100.0, left=0))
    world.run(1)
    assert jobs(world.db) == [('completed', 42)]


def test_recovered_identity_reconciles_dispatched_job_and_claim(world, tmp_path, monkeypatch):
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    from modules.printers import dispatch, support_levels
    from modules.printers.models import Printer
    from tests.test_contracts.dispatch_test_support import configure_dispatch

    engine, _ = configure_dispatch(world.db, tmp_path, monkeypatch, protocol='octoprint')
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    support_levels.set_experimental_enabled(world.db, world.db.get(Printer, 59), True, 3)
    world.db.commit()
    names = []
    monkeypatch.setattr(dispatch, '_dispatch_octoprint',
                        lambda jid, path, name, creds: (names.append(name) or True, 'Synthetic accepted'))
    assert dispatch.dispatch_job(59, 53)[0]
    world.monitor.printer_id = 59
    world.http.routes[('GET', '/api/job')] = requests.exceptions.Timeout('slow')
    world.run(1)
    world.http.routes[('GET', '/api/job')] = Reply(200, job_body(name=names[0]))
    world.run(2)
    with engine.connect() as conn:
        attempt = conn.execute(text('SELECT id,scheduled_job_id,progress_percent FROM print_jobs WHERE printer_id=59')).one()
        assert tuple(attempt)[1:] == (53, 42)
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one() == attempt.id
        assert conn.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one() == 1
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'printing'
    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(IDLE))
    world.http.routes[('GET', '/api/job')] = Reply(200, job_body(name=names[0], completion=100.0, left=0))
    world.run(1)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'completed'
        assert conn.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one() == 0


def test_offline_and_unknown_are_recorded_without_last_seen_or_job_events(world):
    def after(poll):
        if poll == 1:
            world.db.execute(text("UPDATE printers SET last_seen='2026-01-01 00:00:00' WHERE id=501")); world.db.commit()
            world.http.routes[('GET', '/api/printer')] = Reply(409, {'error': 'Printer is not operational'})
        elif poll == 2:
            world.http.routes[('GET', '/api/printer')] = Reply(500, {})
    world.run(3, before_poll=after)
    r = row(world.db, 'gcode_state,print_stage,last_seen')
    assert (r['gcode_state'], r['print_stage']) == ('UNKNOWN', 'Unknown')
    assert str(r['last_seen']).startswith('2026-01-01')
    assert [j[0] for j in jobs(world.db)] == ['running']

    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(PRINTING))
    world.run(1)
    r = row(world.db, 'gcode_state,last_seen')
    assert r['gcode_state'] == 'PRINTING' and not str(r['last_seen']).startswith('2026-01-01')
    assert [j[0] for j in jobs(world.db)] == ['running']  # the gap did not start a second print


def test_no_answer_persists_offline_after_five_failed_polls(world):
    world.http.routes[('GET', '/api/printer')] = requests.exceptions.ConnectionError('refused')
    seen = []
    original = world.module.OctoPrintMonitorThread._persist_state
    world.monitor._persist_state = lambda gstate, stage: seen.append(gstate) or original(world.monitor, gstate, stage)
    world.run(5)
    assert seen == ['OFFLINE'] and row(world.db, 'gcode_state')['gcode_state'] == 'OFFLINE'


def test_webcam_url_is_discovered_only_from_settings(world):
    from core import crypto
    world.run(1)
    assert crypto.decrypt(row(world.db, 'camera_url')['camera_url']) == STREAM
    assert world.go2rtc
    assert [r.path for r in world.http.requests if 'webcam' in r.path] == []


@pytest.mark.parametrize('settings', [
    Reply(200, {'webcam': {}}), Reply(200, {'webcam': {'streamUrl': '/webcam/?action=stream'}}), Reply(403, {}),
])
def test_no_stream_url_means_no_camera(world, settings):
    world.http.routes[('GET', '/api/settings')] = settings
    world.run(1)
    assert not row(world.db, 'camera_url')['camera_url']
    assert {r.path for r in world.http.requests} <= {'/api/printer', '/api/job', '/api/settings'}


def test_progress_is_written_at_most_every_five_seconds(world, monkeypatch):
    import modules.notifications.event_dispatcher as printer_events
    writes = []
    original = printer_events.on_progress_update
    monkeypatch.setattr(printer_events, 'on_progress_update',
                        lambda *a, **k: writes.append(world.clock[0]) or original(*a, **k))

    def advance(poll):
        world.http.routes[('GET', '/api/job')] = Reply(200, job_body(completion=10.0 + 2 * poll))
    world.run(12, step=1, before_poll=advance)
    assert world.module.POLL_INTERVAL == 5 and world.module.PROGRESS_DB_INTERVAL == 5
    assert len(writes) >= 2 and all(b - a >= 5 for a, b in zip(writes, writes[1:]))


def test_live_state_evidence_only_for_real_readings(world, monkeypatch):
    from modules.printers.monitors import live_state
    seen = []
    monkeypatch.setattr(live_state, 'observe_status', lambda engine, pid, status, protocol, **kw: seen.append((pid, protocol, status.internal_state)))
    world.run(3, before_poll=script(world, (PRINTING, None), (Reply(500, {}), None)))
    assert seen and all(entry == (501, 'octoprint', 'PRINTING') for entry in seen)


def test_daemon_selects_only_active_octoprint_printers_and_decrypts_the_key(world):
    from core import crypto
    assert [r['id'] for r in world.module._rows()] == [501]
    assert world.module._credentials('') == ''
    assert world.module._credentials(crypto.encrypt('abc')) == 'abc'
    assert world.module._credentials('plain-key') == 'plain-key'


def test_idle_with_an_unreadable_job_waits_then_decides_from_the_real_reading(world):
    def after(poll):
        if poll == 1:
            world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(IDLE))
            world.http.routes[('GET', '/api/job')] = requests.exceptions.Timeout('slow')
    world.run(2, before_poll=after)
    assert jobs(world.db) == [('running', 42)] and world.monitor._last_state == 'PRINTING'
    world.http.routes[('GET', '/api/job')] = Reply(200, job_body(completion=100.0, left=0, state='Operational'))
    world.run(1)
    assert jobs(world.db) == [('completed', 42)]


def test_ten_unreadable_job_readings_record_cancelled_with_the_reason(world):
    def after(poll):
        if poll == 1:
            world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(IDLE))
            world.http.routes[('GET', '/api/job')] = Reply(500, {})
    world.run(10, before_poll=after)  # polls 2..10: nine failed readings
    assert jobs(world.db) == [('running', 42)]
    world.run(1)                      # the tenth
    assert jobs(world.db) == [('cancelled', 42)]
    world.db.expire_all()
    history = world.db.execute(text('SELECT code,message,severity,source FROM hms_error_history WHERE printer_id=501')).all()
    assert len(history) == 1 and history[0][0] == 'OCTOPRINT_RESULT_UNREADABLE'
    assert 'could not be read' in history[0][1] and tuple(history[0][2:]) == ('warning', 'octoprint')


def test_a_return_to_unknown_after_offline_is_written(world):
    world.http.routes[('GET', '/api/printer')] = Reply(500, {})
    world.run(1)
    assert row(world.db, 'gcode_state')['gcode_state'] == 'UNKNOWN'
    world.http.routes[('GET', '/api/printer')] = requests.exceptions.ConnectionError('refused')
    world.run(5)
    assert row(world.db, 'gcode_state')['gcode_state'] == 'OFFLINE'
    world.http.routes[('GET', '/api/printer')] = Reply(500, {})
    world.run(1)
    assert row(world.db, 'gcode_state')['gcode_state'] == 'UNKNOWN'


def test_error_with_no_print_running_fails_nothing_and_dispatches_nothing(world, monkeypatch):
    import modules.notifications.event_dispatcher as printer_events
    monkeypatch.setattr(printer_events, 'on_print_failed', lambda *a, **k: pytest.fail('no print was running'))
    started = []
    monkeypatch.setattr(world.module.threading, 'Thread',
                        lambda *a, **kw: started.append(kw.get('target')) or SimpleNamespace(start=lambda: None))
    world.http.routes[('GET', '/api/printer')] = Reply(200, printer_body(ERROR))
    world.run(2)
    assert jobs(world.db) == [] and started == []
    world.db.expire_all()
    history = world.db.execute(text('SELECT code FROM hms_error_history WHERE printer_id=501')).all()
    assert [h[0] for h in history] == ['OCTOPRINT_ERROR']
