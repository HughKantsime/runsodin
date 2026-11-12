"""Ultimaker parser, adapter and refusals (Part 1: monitoring and camera only), replayed from
transcripts built from Ultimaker's public local API documentation. Not verified on hardware."""
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from modules.printers import support_levels
from modules.printers.adapters import ultimaker as adapter_module
from modules.printers.adapters.ultimaker import UltimakerPrinter, UltimakerState
from modules.printers.parsing import ultimaker as parsing
from tests.support.device_http import route_device_requests
from tests.support.octoprint_http import Octo, Reply
from tests.support.ultimaker_http import J, P, S, UUID, job_body, printer_body, system_body

HOST = '192.0.2.60'
ALL_VERBS = ('get', 'post', 'put', 'delete', 'head', 'patch')


@pytest.fixture(autouse=True)
def _no_itar(monkeypatch):
    monkeypatch.setattr('core.itar.enforce_request_destination', lambda url: None)


@pytest.fixture
def um(monkeypatch):
    fake = Octo()
    route_device_requests(monkeypatch, **{verb: fake.verb(verb.upper()) for verb in ALL_VERBS})
    return fake


def client():
    return UltimakerPrinter(HOST)


def serve(um, printer_status, job):
    """job: None = HTTP 404 (no job), a dict = 200 with that body, or a Reply/exception."""
    um.routes[('GET', P)] = Reply(200, printer_body(printer_status))
    if job is None:
        um.routes[('GET', J)] = Reply(404, {'message': 'No active print job'})
    elif isinstance(job, (Reply, BaseException)):
        um.routes[('GET', J)] = job
    else:
        um.routes[('GET', J)] = Reply(200, job)


# ---- state maps ----

@pytest.mark.parametrize('raw,expected', [
    ('none', 'IDLE'), ('pre_print', 'PRINTING'), ('printing', 'PRINTING'), ('post_print', 'PRINTING'),
    ('resuming', 'PRINTING'), ('pausing', 'PAUSED'), ('paused', 'PAUSED'), ('wait_user_action', 'PAUSED'),
    ('wait_cleanup', 'IDLE'),
    ('PRINTING', 'UNKNOWN'), ('', 'UNKNOWN'), ('aborting', 'UNKNOWN'), ('finished', 'UNKNOWN'),
    (None, 'UNKNOWN'), (5, 'UNKNOWN'), (['printing'], 'UNKNOWN'), ({}, 'UNKNOWN'),
])
def test_job_state_table(raw, expected):
    assert parsing.map_job_state(raw) == expected


@pytest.mark.parametrize('raw,expected', [
    ('idle', 'IDLE'), ('printing', 'PRINTING'), ('error', 'ERROR'),
    ('maintenance', 'UNKNOWN'), ('booting', 'UNKNOWN'),
    ('Idle', 'UNKNOWN'), ('', 'UNKNOWN'), (None, 'UNKNOWN'), (1, 'UNKNOWN'), ([], 'UNKNOWN'),
])
def test_printer_status_table(raw, expected):
    assert parsing.map_printer_status(raw) == expected


# (id, printer status, job, expected state, finished)
FINISHED_JOB = job_body(state='wait_cleanup', progress=1.0, elapsed=1440)
CASES = [
    ('idle_no_job', 'idle', None, 'IDLE', False),
    ('idle_state_none', 'idle', job_body(state='none', uuid=''), 'IDLE', False),
    ('printing', 'printing', job_body(), 'PRINTING', False),
    ('pre_print', 'printing', job_body(state='pre_print'), 'PRINTING', False),
    ('post_print', 'printing', job_body(state='post_print'), 'PRINTING', False),
    ('resuming', 'printing', job_body(state='resuming'), 'PRINTING', False),
    ('pausing', 'printing', job_body(state='pausing'), 'PAUSED', False),
    ('paused', 'printing', job_body(state='paused'), 'PAUSED', False),
    ('paused_idle_printer', 'idle', job_body(state='paused'), 'PAUSED', False),
    ('wait_user_action', 'printing', job_body(state='wait_user_action'), 'PAUSED', False),
    ('finished_wait_cleanup', 'idle', FINISHED_JOB, 'IDLE', True),
    ('error_no_job', 'error', None, 'ERROR', False),
    ('error_with_job', 'error', job_body(), 'ERROR', False),
    ('printing_but_no_job', 'printing', None, 'UNKNOWN', False),
    ('idle_but_job_printing', 'idle', job_body(), 'UNKNOWN', False),
    ('printing_job_unknown_state', 'printing', job_body(state='weird'), 'UNKNOWN', False),
    ('printing_job_no_uuid', 'printing', job_body(uuid=''), 'UNKNOWN', False),
    ('wait_cleanup_no_uuid', 'idle', job_body(state='wait_cleanup', uuid=''), 'UNKNOWN', False),
]


@pytest.mark.parametrize('name,printer_status,job,expected,finished', CASES, ids=[c[0] for c in CASES])
def test_status_table(um, name, printer_status, job, expected, finished):
    serve(um, printer_status, job)
    status = client().get_status()
    assert status.internal_state == expected and status.state is UltimakerState(expected)
    assert status.finished is finished


@pytest.mark.parametrize('printer_status', ['maintenance', 'booting', 'something_new'])
def test_maintenance_booting_and_unknown_printer_status_read_no_job(um, printer_status):
    serve(um, printer_status, job_body())
    status = client().get_status()
    assert status.state is UltimakerState.UNKNOWN
    assert [r.path for r in um.requests] == [P]


def test_printing_status_replay(um):
    serve(um, 'printing', job_body())
    status = client().get_status()
    assert status.state is UltimakerState.PRINTING and status.internal_state == 'PRINTING'
    assert (status.nozzle_temp, status.nozzle_target, status.bed_temp, status.bed_target) == (215.0, 215.0, 60.0, 60.0)
    assert (status.filename, status.time_printing, status.time_remaining) == ('gear', 600, 840)
    assert status.progress_percent == pytest.approx(42.0)
    assert (status.job_uuid, status.job_state, status.job_read) == (UUID, 'printing', True)
    assert [(r.method, r.path) for r in um.requests] == [('GET', P), ('GET', J)]


def test_404_on_print_job_is_no_job(um):
    serve(um, 'idle', None)
    status = client().get_status()
    assert status.state is UltimakerState.IDLE and status.job_read is True
    assert (status.filename, status.job_uuid, status.progress_percent, status.time_remaining) == ('', '', None, 0)


# ---- malformed, oversized, non-JSON ----

BIG = b'x' * 70_000


@pytest.mark.parametrize('reply', [
    Reply(500, {'message': 'x'}), Reply(404, {}), Reply(401, {}), Reply(403, {}), Reply(204, raw=b''),
    Reply(200, raw=b'{'), Reply(200, raw=b'[]'), Reply(200, raw=b'null'), Reply(200, raw=b'"idle"'),
    Reply(200, raw=b'<html>not json</html>'), Reply(200, raw=b'\xff\xfe'),
    Reply(200, raw=b'{"status": "idle", "pad": "' + BIG + b'"}'),   # over 64 KiB
    Reply(200, {}), Reply(200, {'status': 5}), Reply(200, {'status': None}), Reply(200, {'bed': {}}),
])
def test_bad_printer_bodies_are_unknown_and_read_no_job(um, reply):
    um.routes[('GET', P)] = reply
    status = client().get_status()
    assert status.state is UltimakerState.UNKNOWN and status.internal_state == 'UNKNOWN'
    assert [r.path for r in um.requests] == [P]


def test_a_body_just_under_the_limit_is_read(um):
    um.routes[('GET', P)] = Reply(200, dict(printer_body('idle'), pad='x' * 60_000))
    assert client().get_status().state is UltimakerState.IDLE


@pytest.mark.parametrize('job', [
    Reply(500, {}), Reply(403, {}), Reply(409, {}), Reply(200, raw=b'{'), Reply(200, raw=b'[]'), Reply(200, raw=b'null'),
    Reply(200, raw=b'{"state": "printing", "uuid": "' + BIG + b'"}'),
    requests.exceptions.Timeout('slow'), requests.exceptions.ConnectionError('refused'),
])
def test_unreadable_job_is_unknown_not_idle(um, job):
    serve(um, 'idle', job)
    status = client().get_status()
    assert status.state is UltimakerState.UNKNOWN and status.job_read is False


def test_error_status_survives_an_unreadable_job(um):
    serve(um, 'error', Reply(500, {}))
    assert client().get_status().state is UltimakerState.ERROR


@pytest.mark.parametrize('failure', [requests.exceptions.Timeout('slow'), requests.exceptions.ConnectionError('refused')])
def test_no_answer_is_disconnected(um, failure):
    um.routes[('GET', P)] = failure
    status = client().get_status()
    assert status.state is UltimakerState.DISCONNECTED and status.internal_state == 'OFFLINE'


# ---- progress and time maths ----

@pytest.mark.parametrize('progress,expected', [
    (0, 0.0), (0.42, 42.0), (0.5, 50.0), (1, 100.0), (1.0, 100.0),
    (1.5, 100.0), (-0.2, 0.0), (7, 100.0),                       # clamped
    ('0.5', None), (None, None), (True, None), ([], None), (float('nan'), None), (float('inf'), None),
])
def test_progress_zero_to_one_becomes_percent(progress, expected):
    parsed = parsing.parse_job({'state': 'printing', 'uuid': UUID, 'progress': progress})
    if expected is None:
        assert parsed['progress_percent'] is None
    else:
        assert parsed['progress_percent'] == pytest.approx(expected)


@pytest.mark.parametrize('elapsed,total,time_printing,remaining', [
    (600, 1440, 600, 840),
    (600, None, 600, 0),          # missing total
    (600, 0, 600, 0),
    (600, 'soon', 600, 0),
    (1500, 1440, 1500, 0),        # past the estimate: never negative
    (-5, 1440, 0, 1440),          # a negative elapsed is dropped
    (600.9, 1440.4, 600, 840),    # whole seconds
    (None, 1440, 0, 1440),
    ('x', 'y', 0, 0),
    (float('nan'), float('inf'), 0, 0),
])
def test_time_maths(elapsed, total, time_printing, remaining):
    parsed = parsing.parse_job({'state': 'printing', 'uuid': UUID, 'time_elapsed': elapsed, 'time_total': total})
    assert (parsed['time_printing'], parsed['time_remaining']) == (time_printing, remaining)


def test_job_name_is_cleaned_not_guessed():
    assert parsing.parse_job({'name': '  gear  '})['filename'] == 'gear'
    for bad in (None, 5, '', '   ', 'a' * 300, 'bad\nname', ['x']):
        assert parsing.parse_job({'name': bad})['filename'] == ''


# ---- extruders ----

def test_two_extruders_are_read_and_the_first_is_the_nozzle(um):
    um.routes[('GET', P)] = Reply(200, printer_body('printing', extruders=((215.0, 220.0), (190.5, 200.0))))
    um.routes[('GET', J)] = Reply(200, job_body())
    status = client().get_status()
    assert (status.nozzle_temp, status.nozzle_target) == (215.0, 220.0)
    assert status.extruders == [
        {'temp': 215.0, 'target': 220.0, 'material_guid': 'guid-0'},
        {'temp': 190.5, 'target': 200.0, 'material_guid': 'guid-1'},
    ]


def test_one_extruder_and_odd_extruder_shapes(um):
    assert len(parsing.parse_printer(printer_body())['extruders']) == 1
    for body in ({'status': 'idle'}, {'status': 'idle', 'heads': []}, {'status': 'idle', 'heads': 'x'},
                 {'status': 'idle', 'heads': [None]}, {'status': 'idle', 'heads': [{'extruders': 'x'}]},
                 {'status': 'idle', 'heads': [{'extruders': []}]}):
        parsed = parsing.parse_printer(body)
        assert parsed['extruders'] == [] and parsed['nozzle_temp'] is None and parsed['nozzle_target'] is None


def test_a_third_extruder_is_ignored():
    assert len(parsing.parse_printer(printer_body(extruders=((1.0, 1.0), (2.0, 2.0), (3.0, 3.0))))['extruders']) == 2


def test_odd_numbers_are_dropped_not_guessed():
    body = printer_body('idle', bed=(True, float('inf')), extruders=(('hot', None),))
    parsed = parsing.parse_printer(body)
    assert (parsed['nozzle_temp'], parsed['nozzle_target'], parsed['bed_temp'], parsed['bed_target']) == (None, None, None, None)


# ---- system / test connection ----

def test_system_identity(um):
    um.routes[('GET', S)] = Reply(200, system_body())
    info = client().get_system()
    assert (info['variant'], info['firmware']) == ('Ultimaker S5', '8.3.0.20240617')
    assert [(r.method, r.path) for r in um.requests] == [('GET', S)]


@pytest.mark.parametrize('reply', [
    Reply(404, {}), Reply(500, {}), Reply(403, {}), Reply(200, raw=b'{'), Reply(200, raw=b'[]'), Reply(200, {}),
    Reply(200, {'variant': 5, 'firmware': None}), Reply(200, raw=b'{"variant": "' + BIG + b'"}'),
    requests.exceptions.Timeout('slow'),
])
def test_system_that_is_not_an_ultimaker_answer_is_none(um, reply):
    um.routes[('GET', S)] = reply
    assert client().get_system() is None


def test_add_form_test_connection_reads_system_only(um, monkeypatch):
    from modules.printers import route_utils, routes_crud
    monkeypatch.setattr(routes_crud, '_check_ssrf_blocklist', lambda host: None)
    um.routes[('GET', S)] = Reply(200, system_body())
    request = route_utils.TestConnectionRequest(api_type='Ultimaker', api_host=HOST, api_key='ignored')
    result = routes_crud.test_printer_connection(request, {'id': 1, 'role': 'admin'})
    assert result['success'] is True and result['variant'] == 'Ultimaker S5' and result['firmware'] == '8.3.0.20240617'
    assert 'Ultimaker S5' in result['detail'] and '8.3.0.20240617' in result['detail']
    assert [(r.method, r.path) for r in um.requests] == [('GET', S)]
    assert 'ignored' not in str(result) and all('X-Api-Key' not in (r.headers or {}) for r in um.requests)

    um.routes[('GET', S)] = Reply(404, {})
    failed = routes_crud.test_printer_connection(request, {'id': 1, 'role': 'admin'})
    assert failed['success'] is False and 'error' in failed


# ---- GET only, pinned, ITAR ----

def test_no_request_other_than_get_is_ever_made(um):
    serve(um, 'printing', job_body())
    um.routes[('GET', S)] = Reply(200, system_body())
    printer = client()
    printer.get_status()
    printer.get_system()
    printer.default_camera_url()
    assert um.requests and {r.method for r in um.requests} == {'GET'}
    assert all(r.kwargs.get('timeout') == 5 and r.kwargs.get('stream') is True for r in um.requests)
    assert all('data' not in r.kwargs and 'json' not in r.kwargs and 'files' not in r.kwargs for r in um.requests)


def test_adapter_source_can_only_send_get():
    source = Path(adapter_module.__file__).read_text()
    assert re.findall(r'_http\.(\w+)', source) == ['get']
    assert 'requests.post' not in source and 'requests.put' not in source and 'requests.delete' not in source


def test_pinned_device_request_and_itar_check_are_used(um, monkeypatch):
    import core.webhook_utils as webhook_utils
    itar, pinned = [], []
    monkeypatch.setattr('core.itar.enforce_request_destination', lambda url: itar.append(url))
    original = webhook_utils.device_request

    def recording(method, url, **kwargs):
        pinned.append((method, url))
        return original(method, url, **kwargs)
    monkeypatch.setattr(webhook_utils, 'device_request', recording)
    serve(um, 'printing', job_body())
    client().get_status()
    urls = [f'http://{HOST}:80{P}', f'http://{HOST}:80{J}']
    assert itar == urls and pinned == [('GET', url) for url in urls]


def test_itar_block_stops_the_request(um, monkeypatch):
    from core.itar import ItarOutboundBlocked

    def block(url):
        raise ItarOutboundBlocked('blocked')
    monkeypatch.setattr('core.itar.enforce_request_destination', block)
    serve(um, 'idle', None)
    assert client().get_status().state is UltimakerState.DISCONNECTED
    assert client().get_system() is None
    assert um.requests == []


def test_bad_origin_makes_no_request(um):
    for printer in (UltimakerPrinter(''), UltimakerPrinter('user:pw@' + HOST), UltimakerPrinter(HOST, port=0),
                    UltimakerPrinter('bad host')):
        assert printer.get_status().state is UltimakerState.DISCONNECTED
        assert printer.get_system() is None and printer.default_camera_url() is None
    assert um.requests == []


# ---- camera default ----

@pytest.mark.parametrize('host,expected', [
    (HOST, 'http://192.0.2.60:8080/?action=stream'),
    ('192.0.2.60:8081', 'http://192.0.2.60:8080/?action=stream'),   # the printer's API port is not the camera's
    ('um-s5.local', 'http://um-s5.local:8080/?action=stream'),
    ('2001:db8::5', 'http://[2001:db8::5]:8080/?action=stream'),
])
def test_default_camera_url(um, host, expected):
    assert UltimakerPrinter(host).default_camera_url() == expected
    assert um.requests == []   # a documented default, never probed


# ---- support level, control and dispatch refusals ----

def test_support_level_entry():
    level, source = support_levels.SUPPORT_TABLE['ultimaker']
    assert level == support_levels.EXPERIMENTAL
    assert 'monitoring only for now' in source and 'not verified on hardware' in source
    assert support_levels.support_level(' Ultimaker ') == 'experimental'


def test_controls_and_dispatch_are_refused_even_with_a_forged_opt_in():
    # The opt-in store is never consulted for a monitoring-only type (db=None would raise if it were).
    assert support_levels.is_monitor_only('ultimaker') and not support_levels.is_monitor_only('octoprint')
    assert support_levels.experimental_enabled(None, 9, 'ultimaker') is False
    assert not support_levels.opt_in_applies('ultimaker') and support_levels.opt_in_applies('octoprint')
    refusal = support_levels.MONITOR_ONLY_REFUSAL
    assert 'not available for this printer type yet' in refusal
    printer = SimpleNamespace(id=9, api_type='ultimaker')
    assert support_levels.experimental_refusal(None, 9, 'ultimaker') == refusal
    assert support_levels.dispatch_refusal(None, 9, 'ultimaker') == refusal
    for action in ('resume', 'speed', 'fan', 'lights', 'pause', 'stop'):
        assert support_levels.control_refusal(None, 9, 'ultimaker', action) == refusal
    assert support_levels.command_refusal(printer) == refusal
    assert support_levels.command_refusal(printer, 'resume_print') == refusal
    assert support_levels.enabled_for_printer(printer) is False


def test_scheduler_and_admission_see_a_dispatch_unsupported_printer():
    from modules.printers.dispatch_support import dispatch_unsupported_reason
    reason = dispatch_unsupported_reason(SimpleNamespace(api_type=' Ultimaker ', model='Ultimaker S5', machine_type=None))
    assert reason.startswith('printer_dispatch_unsupported: ') and 'not available for this printer type yet' in reason
    assert dispatch_unsupported_reason({'api_type': 'ultimaker'}) == reason
    assert dispatch_unsupported_reason(SimpleNamespace(api_type='octoprint', model=None, machine_type=None)) is None


@pytest.mark.parametrize('action', ['pause_print', 'resume_print', 'stop_print', 'cancel_print'])
def test_every_command_is_refused_and_never_falls_through_to_bambu(um, monkeypatch, action):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, '_bambu_command', lambda *a, **k: pytest.fail('fell through to the Bambu path'))
    printer = SimpleNamespace(id=9, api_type='ultimaker', api_host=HOST, api_key=None)
    assert route_utils._send_printer_command(printer, action) is False
    assert um.requests == []


def test_a_body_that_dribbles_past_the_time_limit_is_dropped(monkeypatch):
    from modules.printers.adapters import ultimaker as adapter_module

    class Slow:
        closed = False

        def iter_content(self, chunk_size=8192):
            yield b'{"variant": '
            yield b'"Ultimaker S5"}'

        def close(self):
            self.closed = True

    ticks = iter([0.0, 1.0, 999.0, 1000.0])
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: next(ticks))
    reply = Slow()
    assert adapter_module._json_object(reply) is None and reply.closed
