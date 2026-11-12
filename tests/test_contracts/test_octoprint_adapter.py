"""OctoPrint parser, adapter, admission readiness and command routing, replayed from transcripts
built from OctoPrint's public REST API documentation (no OctoPrint code)."""
import time
from types import SimpleNamespace

import pytest
import requests

from modules.printers import dispatch_readiness
from modules.printers.adapters import octoprint as adapter_module
from modules.printers.adapters.octoprint import OctoPrintPrinter, OctoPrintState
from modules.printers.parsing import octoprint as parsing
from tests.support.device_http import route_device_requests
from tests.support.octoprint_http import Octo, Reply, flags, job_body, printer_body

HOST = '192.0.2.60'
KEY = 'synthetic-application-key'


@pytest.fixture(autouse=True)
def _no_itar(monkeypatch):
    monkeypatch.setattr('core.itar.enforce_request_destination', lambda url: None)


@pytest.fixture
def octo(monkeypatch):
    fake = Octo()
    route_device_requests(monkeypatch, get=fake.verb('GET'), post=fake.verb('POST'),
                          put=fake.verb('PUT'), delete=fake.verb('DELETE'), head=fake.verb('HEAD'))
    return fake


def client():
    return OctoPrintPrinter(HOST, api_key=KEY)


# ---- state map ----

STATE_TABLE = [
    ('printing', dict(operational=True, printing=True), 'PRINTING'),
    ('pausing', dict(operational=True, pausing=True), 'PRINTING'),
    ('cancelling', dict(operational=True, cancelling=True), 'PRINTING'),
    ('paused', dict(operational=True, paused=True), 'PAUSED'),
    ('idle', dict(operational=True, ready=True), 'IDLE'),
    ('error', dict(error=True), 'ERROR'),
    ('closed_or_error', dict(closedOrError=True), 'ERROR'),
    ('error_while_operational', dict(operational=True, error=True), 'ERROR'),
    ('operational_not_ready', dict(operational=True), 'UNKNOWN'),
    ('ready_not_operational', dict(ready=True), 'UNKNOWN'),
    ('nothing_set', {}, 'UNKNOWN'),
    ('printing_and_error', dict(printing=True, error=True), 'UNKNOWN'),
    ('paused_and_printing', dict(paused=True, printing=True), 'UNKNOWN'),
    ('paused_and_closed', dict(paused=True, closedOrError=True), 'UNKNOWN'),
]


@pytest.mark.parametrize('name,on,expected', STATE_TABLE, ids=[row[0] for row in STATE_TABLE])
def test_state_map_table(name, on, expected):
    assert parsing.map_flags(flags(**on)) == expected


@pytest.mark.parametrize('bad', [
    None, [], 'flags', {}, {'operational': 'true', 'ready': True}, {'printing': 1}, {'ready': None, 'operational': True},
])
def test_malformed_flags_are_unknown(bad):
    assert parsing.map_flags(bad) == 'UNKNOWN'


@pytest.mark.parametrize('payload', [None, [], 'x', {}, {'state': None}, {'state': {}}, {'state': {'flags': 'x'}}])
def test_malformed_printer_bodies_are_unknown(payload):
    assert parsing.printer_state(payload) == 'UNKNOWN'


def test_every_flag_combination_never_misreports():
    names = parsing.FLAG_NAMES
    for bits in range(2 ** len(names)):
        f = {name: bool(bits >> i & 1) for i, name in enumerate(names)}
        busy = f['printing'] or f['pausing'] or f['cancelling']
        halted = f['error'] or f['closedOrError']
        result = parsing.map_flags(f)
        if result == 'IDLE':
            assert f['operational'] and f['ready'] and not (busy or halted or f['paused']), f
        if result == 'PRINTING':
            assert busy and not (halted or f['paused']), f
        if result == 'PAUSED':
            assert f['paused'] and not (busy or halted), f
        if result == 'ERROR':
            assert halted and not (busy or f['paused']), f


# ---- status replay ----

def test_printing_status_replay(octo):
    octo.routes[('GET', '/api/printer')] = Reply(200, printer_body(flags(operational=True, printing=True)))
    octo.routes[('GET', '/api/job')] = Reply(200, job_body())
    status = client().get_status()
    assert status.state is OctoPrintState.PRINTING and status.internal_state == 'PRINTING'
    assert (status.nozzle_temp, status.nozzle_target, status.bed_temp, status.bed_target) == (214.9, 215.0, 60.0, 60.0)
    assert (status.filename, status.progress_percent, status.time_printing, status.time_remaining) == ('gear.gcode', 42.0, 600, 840)
    assert [(r.method, r.path) for r in octo.requests] == [('GET', '/api/printer'), ('GET', '/api/job')]
    assert all(r.headers == {'X-Api-Key': KEY} for r in octo.requests)


@pytest.mark.parametrize('on,expected', [
    (dict(operational=True, paused=True), OctoPrintState.PAUSED),
    (dict(operational=True, ready=True), OctoPrintState.IDLE),
    (dict(operational=True, cancelling=True), OctoPrintState.PRINTING),
    (dict(error=True), OctoPrintState.ERROR),
    (dict(closedOrError=True), OctoPrintState.ERROR),
])
def test_status_variants(octo, on, expected):
    octo.routes[('GET', '/api/printer')] = Reply(200, printer_body(flags(**on)))
    octo.routes[('GET', '/api/job')] = Reply(200, job_body(completion=100.0, left=0))
    assert client().get_status().state is expected


def test_finished_job_keeps_its_100_percent(octo):
    octo.routes[('GET', '/api/printer')] = Reply(200, printer_body(flags(operational=True, ready=True)))
    octo.routes[('GET', '/api/job')] = Reply(200, job_body(completion=100.0, left=0, state='Operational'))
    status = client().get_status()
    assert status.state is OctoPrintState.IDLE and status.progress_percent == 100.0


def test_not_operational_is_offline_and_reads_no_job(octo):
    octo.routes[('GET', '/api/printer')] = Reply(409, {'error': 'Printer is not operational'})
    status = client().get_status()
    assert status.state is OctoPrintState.OFFLINE and status.internal_state == 'OFFLINE'
    assert [r.path for r in octo.requests] == ['/api/printer']


@pytest.mark.parametrize('reply', [
    Reply(500, {'error': 'x'}), Reply(404, {}), Reply(401, {}), Reply(403, {}), Reply(204, raw=b''),
    Reply(200, raw=b'{'), Reply(200, raw=b'[]'), Reply(200, raw=b'null'),
    Reply(200, {'state': {'flags': {}}}), Reply(200, {'temperature': {}}),
    Reply(200, printer_body(flags(operational=True))),  # operational but not ready
])
def test_anything_else_is_unknown_and_never_reads_a_job(octo, reply):
    octo.routes[('GET', '/api/printer')] = reply
    status = client().get_status()
    assert status.state is OctoPrintState.UNKNOWN and status.internal_state == 'UNKNOWN'
    assert [r.path for r in octo.requests] == ['/api/printer']


@pytest.mark.parametrize('failure', [requests.exceptions.Timeout('slow'), requests.exceptions.ConnectionError('refused')])
def test_no_answer_is_disconnected(octo, failure):
    octo.routes[('GET', '/api/printer')] = failure
    assert client().get_status().state is OctoPrintState.DISCONNECTED


def test_job_failures_do_not_change_the_state(octo):
    octo.routes[('GET', '/api/printer')] = Reply(200, printer_body(flags(operational=True, ready=True)))
    for job in (Reply(500, {}), Reply(409, {}), Reply(200, raw=b'{'), requests.exceptions.Timeout('slow')):
        octo.routes[('GET', '/api/job')] = job
        status = client().get_status()
        assert status.state is OctoPrintState.IDLE and status.filename == '' and status.progress_percent is None


def test_odd_numbers_are_dropped_not_guessed(octo):
    body = printer_body(flags(operational=True, printing=True), tool=('hot', None), bed=(True, float('inf')))
    octo.routes[('GET', '/api/printer')] = Reply(200, raw=__import__('json').dumps(body).replace('Infinity', '1e999').encode())
    octo.routes[('GET', '/api/job')] = Reply(200, job_body(completion=150.0, print_time=-5, left='soon'))
    status = client().get_status()
    assert (status.nozzle_temp, status.nozzle_target, status.bed_temp) == (None, None, None)
    assert status.progress_percent is None and status.time_printing == 0 and status.time_remaining == 0


def test_version_identity(octo):
    octo.routes[('GET', '/api/version')] = Reply(200, {'api': '0.1', 'server': '1.10.0', 'text': 'OctoPrint 1.10.0'})
    assert client().get_version()['server'] == '1.10.0'
    octo.routes[('GET', '/api/version')] = Reply(403, {})
    assert client().get_version() is None
    octo.routes[('GET', '/api/version')] = Reply(200, raw=b'{')
    assert client().get_version() is None


def test_no_key_or_bad_origin_makes_no_request(octo, tmp_path):
    gcode = tmp_path / 'a.gcode'
    gcode.write_bytes(b'G28\n')
    for printer in (OctoPrintPrinter(HOST), OctoPrintPrinter(HOST, api_key='bad key'),
                    OctoPrintPrinter('', api_key=KEY), OctoPrintPrinter('user:pw@' + HOST, api_key=KEY)):
        assert printer.get_status().state is OctoPrintState.DISCONNECTED
        assert printer.upload_and_print(str(gcode)) is False and printer.pause_print() is False
    assert octo.requests == []


def test_itar_block_stops_the_request(octo, monkeypatch):
    from core.itar import ItarOutboundBlocked

    def block(url):
        raise ItarOutboundBlocked('blocked')
    monkeypatch.setattr('core.itar.enforce_request_destination', block)
    assert client().get_status().state is OctoPrintState.DISCONNECTED
    assert octo.requests == []


@pytest.mark.parametrize('host,port,expected', [
    ('192.0.2.60', 80, 'http://192.0.2.60:80'),
    ('192.0.2.60:5000', 80, 'http://192.0.2.60:5000'),
    ('octopi.local', 5000, 'http://octopi.local:5000'),
    ('fd00::5', 80, 'http://[fd00::5]:80'),
    ('[fd00::5]:5000', 80, 'http://[fd00::5]:5000'),
    ('192.0.2.60/path', 80, None), ('user:pw@192.0.2.60', 80, None), ('', 80, None),
    ('bad host', 80, None), ('192.0.2.60:99999', 80, None), ('192.0.2.60', 0, None),
])
def test_origin_parsing(host, port, expected):
    assert adapter_module._origin(host, port) == expected


# ---- upload ----

CREATED = {'files': {'local': {'name': 'gear.gcode', 'origin': 'local'}}, 'done': True}


@pytest.fixture
def gcode(tmp_path):
    path = tmp_path / 'stored-1234.gcode'
    path.write_bytes(b'G28\nG1 X10\n')
    return path


def test_upload_success_on_201_done_true_with_the_documented_request(octo, gcode):
    sent = []

    def upload(request):
        name, handle, mime = request.kwargs['files']['file']
        sent.append((name, handle.read(), mime))
        return Reply(201, CREATED)
    octo.routes[('POST', '/api/files/local')] = upload
    assert client().upload_and_print(str(gcode), 'gear.gcode') is True
    request = octo.requests[0]
    assert request.headers == {'X-Api-Key': KEY} and request.url == f'http://{HOST}:80/api/files/local'
    assert request.kwargs['data'] == {'select': 'true', 'print': 'true'}
    assert sent == [('gear.gcode', b'G28\nG1 X10\n', 'application/octet-stream')]


@pytest.mark.parametrize('reply', [
    Reply(200, CREATED), Reply(202, CREATED), Reply(204, raw=b''),
    Reply(201, {'files': {}, 'done': False}), Reply(201, {'files': {}}), Reply(201, {'done': 'true'}),
    Reply(201, {'done': 1}), Reply(201, raw=b'{'), Reply(201, raw=b'[]'),
    Reply(400, {'error': 'No file included'}), Reply(409, {'error': 'Printer is not operational'}),
    Reply(415, {'error': 'Unsupported media type'}), Reply(500, {}),
    requests.exceptions.Timeout('slow'), requests.exceptions.ConnectionError('refused'),
])
def test_upload_refused_unless_201_done_true(octo, gcode, reply):
    octo.routes[('POST', '/api/files/local')] = reply
    assert client().upload_and_print(str(gcode), 'gear.gcode') is False


def test_upload_name_is_a_basename_and_control_characters_are_refused(octo, gcode):
    octo.routes[('POST', '/api/files/local')] = lambda request: Reply(201, CREATED)
    assert client().upload_and_print(str(gcode), '../../etc/x.gcode') is True
    assert octo.requests[0].kwargs['files']['file'][0] == 'x.gcode'
    octo.requests.clear()
    assert client().upload_and_print(str(gcode), 'a\nb.gcode') is False
    assert client().upload_and_print(str(gcode), '') is False  # falls back to nothing usable only if empty path
    assert client().upload_and_print(str(gcode) + '.missing', 'a.gcode') is False
    assert [r.path for r in octo.requests] == ['/api/files/local'] * 1 or octo.requests == []


# ---- controls ----

CONTROLS = [
    ('pause_print', {'command': 'pause', 'action': 'pause'}),
    ('resume_print', {'command': 'pause', 'action': 'resume'}),
    ('stop_print', {'command': 'cancel'}),
    ('cancel_print', {'command': 'cancel'}),
]


@pytest.mark.parametrize('method,payload', CONTROLS)
def test_control_succeeds_only_on_204(octo, method, payload):
    octo.routes[('POST', '/api/job')] = Reply(204, raw=b'')
    assert getattr(client(), method)() is True
    request = octo.requests[0]
    assert (request.method, request.path, request.kwargs['json'], request.headers) == ('POST', '/api/job', payload, {'X-Api-Key': KEY})


@pytest.mark.parametrize('method,payload', CONTROLS)
@pytest.mark.parametrize('reply', [
    Reply(200, {}), Reply(201, {}), Reply(202, {}), Reply(400, {}), Reply(401, {}),
    Reply(409, {'error': 'Printer is not operational'}), Reply(500, {}),
    requests.exceptions.Timeout('slow'), requests.exceptions.ConnectionError('refused'),
])
def test_control_refused_on_anything_else(octo, method, payload, reply):
    octo.routes[('POST', '/api/job')] = reply
    assert getattr(client(), method)() is False


# ---- webcam: only what OctoPrint's own settings report ----

def settings(stream):
    return Reply(200, {'webcam': {'streamUrl': stream, 'webcamEnabled': True}})


def test_webcam_url_comes_only_from_settings(octo):
    octo.routes[('GET', '/api/settings')] = settings('http://192.0.2.60:8080/?action=stream')
    assert client().get_webcam_url() == 'http://192.0.2.60:8080/?action=stream'
    assert [(r.method, r.path) for r in octo.requests] == [('GET', '/api/settings')]


@pytest.mark.parametrize('reply', [
    settings('/webcam/?action=stream'), settings(''), settings(None), settings(5),
    settings('http://user:pw@192.0.2.60/stream'), settings('file:///etc/passwd'),
    settings('http://192.0.2.60/a b'), settings('http://'),
    Reply(200, {'webcam': {}}), Reply(200, {}), Reply(200, raw=b'{'), Reply(403, {}), Reply(404, {}),
    requests.exceptions.Timeout('slow'),
])
def test_webcam_url_is_never_invented(octo, reply):
    octo.routes[('GET', '/api/settings')] = reply
    assert client().get_webcam_url() is None
    assert {r.path for r in octo.requests} == {'/api/settings'}


# ---- admission readiness (shared dispatch_readiness path) ----

@pytest.mark.parametrize('on,expected', [
    (dict(operational=True, ready=True), True),
    (dict(operational=True, printing=True), False),
    (dict(operational=True, paused=True), False),
    (dict(operational=True, cancelling=True), False),
    (dict(error=True), False), (dict(closedOrError=True), False),
    (dict(operational=True), False), ({}, False),
])
def test_raw_status_is_idle(on, expected):
    assert dispatch_readiness.raw_status_is_idle('octoprint', printer_body(flags(**on))) is expected


@pytest.mark.parametrize('payload', [None, [], 'idle', {}, {'state': {'flags': 'x'}}])
def test_raw_status_is_idle_rejects_malformed_bodies(payload):
    assert dispatch_readiness.raw_status_is_idle('octoprint', payload) is False


def creds(**extra):
    return {'api_type': 'octoprint', 'ip': HOST, 'port': 5000, 'api_key': KEY, **extra}


def test_fresh_http_idle_reads_api_printer_with_the_key(octo):
    octo.routes[('GET', '/api/printer')] = Reply(200, printer_body(flags(operational=True, ready=True)))
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), time.monotonic() + 5) is True
    assert [(r.method, r.url, r.headers) for r in octo.requests] == [
        ('GET', f'http://{HOST}:5000/api/printer', {'X-Api-Key': KEY})]


@pytest.mark.parametrize('reply', [
    Reply(409, {'error': 'Printer is not operational'}), Reply(403, {}), Reply(500, {}),
    Reply(200, printer_body(flags(operational=True, printing=True))), Reply(200, raw=b'{'),
])
def test_fresh_http_not_idle_for_anything_but_ready(octo, reply):
    octo.routes[('GET', '/api/printer')] = reply
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), time.monotonic() + 5) is False


def test_fresh_http_requires_a_key_before_any_network(octo):
    assert dispatch_readiness.fresh_http_status_is_idle(creds(api_key=''), time.monotonic() + 5) is False
    assert octo.requests == []


# ---- shared dispatch plumbing knows the type ----

def test_identity_and_sender_protocol_accept_octoprint_and_nothing_else():
    from core import sender_operation_protocol as protocol
    from core.dispatch_identity import build_identity
    base = {'api_type': 'octoprint', 'ip': HOST, 'port': 5000, 'api_key': KEY, 'is_active': True}
    assert build_identity(base, b'k' * 32) is not None
    assert build_identity({**base, 'api_type': 'octoprint2'}, b'k' * 32) is None
    protocol._status_inputs({'api_type': 'octoprint', 'ip': HOST, 'port': 5000, 'api_key': KEY})
    with pytest.raises(Exception):
        protocol._status_inputs({'api_type': 'octoprint2', 'ip': HOST})


# ---- internal command helper: explicit route, never the Bambu fallback ----

def test_send_printer_command_routes_octoprint_and_never_falls_through(monkeypatch):
    from modules.printers import route_utils
    for name in ('_bambu_command', '_prusalink_command', '_elegoo_command'):
        monkeypatch.setattr(route_utils, name, lambda *a: pytest.fail('octoprint must not reach another adapter'))
    sent = []
    monkeypatch.setattr(route_utils, '_octoprint_command', lambda printer, action: sent.append(action) or True)
    printer = SimpleNamespace(id=7, api_type='octoprint', api_host=HOST, api_key=None)
    assert route_utils._send_printer_command(printer, 'pause_print') is True
    assert route_utils._send_printer_command(printer, 'stop_print') is True
    assert route_utils._send_printer_command(printer, 'resume_print') is False  # needs the opt-in
    assert sent == ['pause_print', 'stop_print']


def test_octoprint_command_uses_the_decrypted_key_and_only_allowed_commands(monkeypatch):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils.crypto, 'decrypt', lambda value: 'decrypted-' + value)
    seen = []
    monkeypatch.setattr(adapter_module.OctoPrintPrinter, 'pause_print',
                        lambda self: seen.append((self.base_url, self.api_key)) or True)
    printer = SimpleNamespace(id=7, api_type='octoprint', api_host=HOST + ':5000', api_key='enc')
    assert route_utils._octoprint_command(printer, 'pause_print') is True
    assert seen == [(f'http://{HOST}:5000', 'decrypted-enc')]
    assert route_utils._octoprint_command(printer, 'set_print_speed') is False
