"""Ultimaker monitor: start, progress and end through the shared observation path, offline and
recovery, replayed from transcripts built from Ultimaker's public local API documentation.
Monitoring only: the fake transport records every request and only GET may appear."""
from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import text

from tests.support.device_http import route_device_requests
from tests.support.octoprint_http import Octo, Reply
from tests.support.ultimaker_http import J, P, S, UUID, job_body, printer_body, system_body
from tests.test_contracts.test_cert_A_harness import bind_engine, fernet_env, review_db  # noqa: F401

CAMERA = 'http://192.0.2.60:8080/?action=stream'
NO_JOB = Reply(404, {'message': 'No active print job'})


@pytest.fixture
def world(review_db, fernet_env, monkeypatch):  # noqa: F811
    from modules.printers.monitors import ultimaker_monitor as module
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
        "(501,'Cert Um',NULL,1,'ultimaker','192.0.2.60',1,0,'IDLE',1,'[]',0,1)"))
    review_db.commit()
    clock = [6_000_000.0]
    events, go2rtc = [], []
    monkeypatch.setattr(module, 'ws_push', lambda kind, payload=None, **kw: events.append((kind, payload)))
    monkeypatch.setattr(module, '_mqtt_republish', None, raising=False)
    monkeypatch.setattr(job_events, '_active_print_jobs', {})
    monkeypatch.setattr(route_utils, '_go2rtc_restart_with_lock', lambda config, **kw: go2rtc.append(config))
    http = Octo()
    http.routes[('GET', P)] = Reply(200, printer_body('printing'))
    http.routes[('GET', J)] = Reply(200, job_body())
    http.routes[('GET', S)] = Reply(200, system_body())
    route_device_requests(monkeypatch, **{verb: http.verb(verb.upper())
                                          for verb in ('get', 'post', 'put', 'delete', 'head', 'patch')})
    monitor = module.UltimakerMonitorThread(501, 'Cert Um', '192.0.2.60')

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
    """steps[i] is applied after poll i+1: (printer status | body | Reply | exception, job body | Reply | None to keep)."""
    def apply(poll):
        if poll > len(steps):
            return
        printer, job = steps[poll - 1]
        if isinstance(printer, (Reply, BaseException)):
            world.http.routes[('GET', P)] = printer
        else:
            world.http.routes[('GET', P)] = Reply(200, printer if isinstance(printer, dict) else printer_body(printer))
        if job is not None:
            world.http.routes[('GET', J)] = job if isinstance(job, (Reply, BaseException)) else Reply(200, job)
    return apply


def test_polling_constants():
    from modules.printers.monitors import ultimaker_monitor as module
    assert module.POLL_INTERVAL == 10 and module.MAX_CONSECUTIVE_FAILURES == 3
    assert module.HEARTBEAT_INTERVAL == 10 and module.PROGRESS_DB_INTERVAL == 5


def test_start_progress_and_completion_through_the_shared_path(world):
    finished = job_body(state='wait_cleanup', progress=1.0, elapsed=1440)
    world.run(3, before_poll=script(world, ('printing', job_body(progress=0.8, elapsed=1100)), ('idle', finished)))
    assert jobs(world.db) == [('completed', 80)]
    r = row(world.db, 'gcode_state,print_stage,last_seen')
    assert r['gcode_state'] == 'IDLE' and r['print_stage'] == 'Finished' and r['last_seen']
    printing = [p for kind, p in world.events if kind == 'printer_telemetry' and p['state'] == 'PRINTING']
    assert printing[0]['progress'] == pytest.approx(42.0) and printing[0]['remaining_min'] == 14
    assert [p for kind, p in world.events if p['state'] == 'IDLE']


def test_telemetry_row_and_websocket_payload_carry_the_first_extruder(world):
    world.http.routes[('GET', P)] = Reply(200, printer_body('printing', extruders=((215.0, 220.0), (190.0, 200.0))))
    world.run(1)
    world.db.expire_all()
    telemetry = world.db.execute(text(
        'SELECT bed_temp,nozzle_temp,bed_target,nozzle_target FROM printer_telemetry WHERE printer_id=501')).all()
    assert [tuple(t) for t in telemetry] == [(60.0, 215.0, 60.0, 220.0)]
    assert row(world.db, 'nozzle_temp,nozzle_target_temp,bed_temp') == {
        'nozzle_temp': 215.0, 'nozzle_target_temp': 220.0, 'bed_temp': 60.0}
    payload = [p for kind, p in world.events if kind == 'printer_telemetry'][0]
    assert set(payload) == {'printer_id', 'bed_temp', 'bed_target', 'nozzle_temp', 'nozzle_target', 'state',
                            'progress', 'remaining_min', 'current_layer', 'total_layers', 'fan_speed'}
    assert payload['nozzle_temp'] == 215.0 and payload['state'] == 'PRINTING'


def test_an_aborted_print_is_cancelled_never_completed(world):
    world.run(2, before_poll=script(world, ('idle', job_body(state='wait_cleanup', progress=0.6))))
    assert [j[0] for j in jobs(world.db)] == ['cancelled']


def test_a_finished_job_without_a_progress_figure_is_not_a_success(world):
    world.run(2, before_poll=script(world, ('idle', job_body(state='wait_cleanup', progress=None))))
    assert [j[0] for j in jobs(world.db)] == ['cancelled']


def test_a_job_that_vanishes_is_cancelled_not_assumed_finished(world):
    world.run(2, before_poll=script(world, ('idle', NO_JOB)))
    assert [j[0] for j in jobs(world.db)] == ['cancelled']
    world.run(2)  # still no job (the fixture's later polls keep answering the same way): nothing more happens
    assert [j[0] for j in jobs(world.db)] == ['cancelled', 'running'] or [j[0] for j in jobs(world.db)] == ['cancelled']


def test_wait_cleanup_polls_after_the_end_do_not_end_it_twice(world):
    finished = job_body(state='wait_cleanup', progress=1.0, elapsed=1440)
    world.run(5, before_poll=script(world, ('idle', finished), ('idle', finished), ('idle', NO_JOB)))
    assert [j[0] for j in jobs(world.db)] == ['completed']


def test_error_state_records_history_and_fails_the_job(world):
    world.run(2, before_poll=script(world, ('error', None)))
    world.db.expire_all()
    history = world.db.execute(text('SELECT code,message,severity,source FROM hms_error_history WHERE printer_id=501')).all()
    assert [tuple(h) for h in history] == [('ULTIMAKER_ERROR', 'Printer reported error state', 'error', 'ultimaker')]
    assert [j[0] for j in jobs(world.db)] == ['failed']


def test_error_with_no_print_running_fails_nothing(world, monkeypatch):
    import modules.notifications.event_dispatcher as printer_events
    monkeypatch.setattr(printer_events, 'on_print_failed', lambda *a, **k: pytest.fail('no print was running'))
    world.http.routes[('GET', P)] = Reply(200, printer_body('error'))
    world.http.routes[('GET', J)] = NO_JOB
    world.run(2)
    assert jobs(world.db) == []
    world.db.expire_all()
    assert [h[0] for h in world.db.execute(text('SELECT code FROM hms_error_history WHERE printer_id=501')).all()] == ['ULTIMAKER_ERROR']


@pytest.mark.parametrize('pausing_state', ['paused', 'pausing', 'wait_user_action'])
def test_pause_and_resume_keep_one_running_job(world, pausing_state):
    world.run(3, before_poll=script(world, ('printing', job_body(state=pausing_state)), ('printing', job_body())))
    assert [j[0] for j in jobs(world.db)] == ['running']
    assert row(world.db, 'gcode_state')['gcode_state'] == 'PRINTING'


def test_a_new_job_uuid_ends_the_earlier_print_and_starts_the_new_one(world):
    world.run(2, before_poll=script(world, ('printing', job_body(uuid='second-job-uuid', name='second'))))
    assert [j[0] for j in jobs(world.db)] == ['cancelled', 'running']


def test_the_job_uuid_is_the_physical_job_id(world, monkeypatch):
    import modules.notifications.event_dispatcher as printer_events
    seen = []
    original = printer_events.on_print_start
    monkeypatch.setattr(printer_events, 'on_print_start',
                        lambda *a, **k: seen.append(k.get('physical_job_id')) or original(*a, **k))
    world.run(1)
    assert seen == [f'ultimaker_{UUID}']


def test_unknown_is_recorded_without_last_seen_or_job_events(world):
    def after(poll):
        if poll == 1:
            world.db.execute(text("UPDATE printers SET last_seen='2026-01-01 00:00:00' WHERE id=501")); world.db.commit()
            world.http.routes[('GET', P)] = Reply(200, printer_body('maintenance'))
        elif poll == 2:
            world.http.routes[('GET', P)] = Reply(500, {})
    world.run(3, before_poll=after)
    r = row(world.db, 'gcode_state,print_stage,last_seen')
    assert (r['gcode_state'], r['print_stage']) == ('UNKNOWN', 'Unknown')
    assert str(r['last_seen']).startswith('2026-01-01')
    assert [j[0] for j in jobs(world.db)] == ['running']

    world.http.routes[('GET', P)] = Reply(200, printer_body('printing'))
    world.run(1)
    r = row(world.db, 'gcode_state,last_seen')
    assert r['gcode_state'] == 'PRINTING' and not str(r['last_seen']).startswith('2026-01-01')
    assert [j[0] for j in jobs(world.db)] == ['running']  # the gap did not start a second print


def test_offline_after_three_failed_polls_and_back(world):
    world.http.routes[('GET', P)] = requests.exceptions.ConnectionError('refused')
    seen = []
    original = world.module.UltimakerMonitorThread._persist_state
    world.monitor._persist_state = lambda gstate, stage: seen.append(gstate) or original(world.monitor, gstate, stage)
    world.run(2)
    assert seen == [] and row(world.db, 'gcode_state')['gcode_state'] == 'IDLE'   # two failures are not offline
    world.run(1)
    assert seen == ['OFFLINE'] and row(world.db, 'gcode_state')['gcode_state'] == 'OFFLINE'
    world.run(3)
    assert seen == ['OFFLINE']                                                    # written once, not every poll

    world.http.routes[('GET', P)] = Reply(200, printer_body('printing'))
    world.run(1)
    r = row(world.db, 'gcode_state,print_stage,last_seen')
    assert r['gcode_state'] == 'PRINTING' and r['print_stage'] == 'Printing' and r['last_seen']
    assert [j[0] for j in jobs(world.db)] == ['running']


def test_a_timeout_after_an_answer_does_not_reset_the_count_wrongly(world):
    def after(poll):
        world.http.routes[('GET', P)] = (requests.exceptions.Timeout('slow') if poll % 3 else Reply(200, printer_body('printing')))
    world.run(7, before_poll=after)
    assert row(world.db, 'gcode_state')['gcode_state'] != 'OFFLINE'   # never three in a row


def test_only_get_requests_reach_the_printer(world):
    finished = job_body(state='wait_cleanup', progress=1.0, elapsed=1440)
    world.run(4, before_poll=script(world, ('printing', job_body(state='paused')), ('error', None), ('idle', finished)))
    assert world.http.requests and {r.method for r in world.http.requests} == {'GET'}
    assert {r.path for r in world.http.requests} <= {P, J, S}
    assert all('data' not in r.kwargs and 'json' not in r.kwargs and 'files' not in r.kwargs for r in world.http.requests)


def test_default_camera_url_is_registered_through_discovery(world):
    from core import crypto
    world.run(1)
    assert crypto.decrypt(row(world.db, 'camera_url')['camera_url']) == CAMERA
    assert world.go2rtc
    assert [r.path for r in world.http.requests if '8080' in r.url or 'stream' in r.url] == []   # never probed


def test_no_camera_when_the_printer_has_the_camera_off(world):
    world.db.execute(text('UPDATE printers SET camera_enabled=0 WHERE id=501')); world.db.commit()
    world.run(1)
    assert not row(world.db, 'camera_url')['camera_url']


def test_an_existing_camera_url_is_kept(world):
    from core import crypto
    own = 'http://192.0.2.60:8081/stream'
    world.db.execute(text('UPDATE printers SET camera_url=:u WHERE id=501'), {'u': crypto.encrypt(own)}); world.db.commit()
    world.run(1)
    assert crypto.decrypt(row(world.db, 'camera_url')['camera_url']) == own


def test_the_default_camera_url_must_pass_the_private_address_policy(world):
    world.db.execute(text("UPDATE printers SET api_host='127.0.0.1' WHERE id=501")); world.db.commit()
    monitor = world.module.UltimakerMonitorThread(501, 'Loopback', '127.0.0.1')
    monitor._register_camera()
    assert not row(world.db, 'camera_url')['camera_url']


def test_live_state_evidence_only_for_real_readings(world, monkeypatch):
    from modules.printers.monitors import live_state
    seen = []
    monkeypatch.setattr(live_state, 'observe_status', lambda engine, pid, status, protocol, **kw: seen.append((pid, protocol, status.internal_state)))
    world.run(3, before_poll=script(world, ('printing', None), (Reply(500, {}), None)))
    assert seen and all(entry == (501, 'ultimaker', 'PRINTING') for entry in seen)


def test_live_state_branch_needs_a_classified_state_and_a_printer_status():
    from modules.printers.monitors import live_state
    published = []

    class Publisher:
        def publish(self, begin, printer_id, state):
            published.append(state)
    owner = SimpleNamespace(_live_state_publishers={1: Publisher()})
    engine = SimpleNamespace(begin=None)
    good = SimpleNamespace(internal_state='PAUSED', raw_data={'status': 'printing'})
    live_state.observe_status(engine, 1, good, 'ultimaker', owner=owner)
    for bad in (SimpleNamespace(internal_state='UNKNOWN', raw_data={'status': 'maintenance'}),
                SimpleNamespace(internal_state='OFFLINE', raw_data={}),
                SimpleNamespace(internal_state='IDLE', raw_data={})):
        live_state.observe_status(engine, 1, bad, 'ultimaker', owner=owner)
    assert published == ['PAUSED']


def test_daemon_selects_only_active_ultimaker_printers(world):
    world.db.execute(text(
        "INSERT INTO printers(id,name,model,is_active,api_type,api_host,org_id,shared,gcode_state,slot_count,tags,timelapse_enabled,camera_enabled) VALUES "
        "(502,'Octo',NULL,1,'octoprint','192.0.2.61',1,0,'IDLE',1,'[]',0,0),"
        "(503,'Off',NULL,0,'ultimaker','192.0.2.62',1,0,'IDLE',1,'[]',0,0),"
        "(504,'NoHost',NULL,1,'ultimaker',NULL,1,0,'IDLE',1,'[]',0,0)"))
    world.db.commit()
    assert [r['id'] for r in world.module._rows()] == [501]
    inventory = world.module._inventory()
    assert list(inventory) == [501] and inventory[501][1] == ('Cert Um', '192.0.2.60')
    assert isinstance(world.module._session(501, 'Cert Um', '192.0.2.60'), world.module.UltimakerMonitorThread)
