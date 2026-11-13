"""Elegoo SDCP job sending: upload, start, controls and the admission status read, replayed against a
scripted fake printer built from Elegoo's public SDCP documentation (not verified on hardware)."""
import hashlib
import json
import time

import pytest
import requests

from modules.printers import dispatch_readiness
from modules.printers.adapters import elegoo as adapter_module
from modules.printers.adapters.elegoo import ElegooOutcomeUnknown, ElegooPrinter
from modules.printers.parsing import elegoo as parsing
from tests.support.device_http import route_device_requests
from tests.support.octoprint_http import Octo, Reply
from tests.support.sdcp_fake import (UPLOAD_OK, FakeConnection, answer, attach, response, status_frame)

HOST = '192.0.2.70'
BOARD = 'MB1'
UPLOAD = ('POST', '/uploadFile/upload')


@pytest.fixture(autouse=True)
def _no_itar(monkeypatch):
    monkeypatch.setattr('core.itar.enforce_request_destination', lambda url: None)


@pytest.fixture
def octo(monkeypatch):
    fake = Octo()
    route_device_requests(monkeypatch, get=fake.verb('GET'), post=fake.verb('POST'),
                          put=fake.verb('PUT'), delete=fake.verb('DELETE'), head=fake.verb('HEAD'))
    return fake


@pytest.fixture
def gcode(tmp_path):
    path = tmp_path / 'stored-1234.gcode'
    path.write_bytes(b'G28\nG1 X10\n')
    return path


def printer():
    return ElegooPrinter(HOST, mainboard_id=BOARD)


# ---- upload ----

def test_upload_success_sends_the_documented_request(octo, gcode):
    sent = []

    def upload(request):
        name, content, mime = request.kwargs['files']['File']
        sent.append((name, content, mime, dict(request.kwargs['data'])))
        return Reply(200, UPLOAD_OK)
    octo.routes[UPLOAD] = upload
    assert printer().upload_file(str(gcode), 'gear.gcode') is True
    assert octo.requests[0].url == f'http://{HOST}:3030/uploadFile/upload'
    name, content, mime, data = sent[0]
    assert (name, content, mime) == ('gear.gcode', b'G28\nG1 X10\n', 'application/octet-stream')
    assert data['Check'] == '1' and data['Offset'] == '0' and data['TotalSize'] == '11'
    assert data['S-File-MD5'] == hashlib.md5(b'G28\nG1 X10\n').hexdigest()
    assert len(data['Uuid']) == 32


def test_upload_is_chunked_with_offsets_and_one_checksum(octo, gcode, monkeypatch):
    monkeypatch.setattr(adapter_module, 'UPLOAD_CHUNK_BYTES', 4)
    seen = []
    octo.routes[UPLOAD] = lambda request: seen.append((request.kwargs['data'], request.kwargs['files']['File'][1])) or Reply(200, UPLOAD_OK)
    assert printer().upload_file(str(gcode), 'gear.gcode') is True
    assert [d['Offset'] for d, _ in seen] == ['0', '4', '8']
    assert b''.join(chunk for _, chunk in seen) == b'G28\nG1 X10\n'
    assert len({d['Uuid'] for d, _ in seen}) == 1 and len({d['S-File-MD5'] for d, _ in seen}) == 1


@pytest.mark.parametrize('reply', [
    Reply(200, {**UPLOAD_OK, 'success': False}), Reply(200, {**UPLOAD_OK, 'code': '000001'}),
    Reply(200, {**UPLOAD_OK, 'success': 'true'}), Reply(200, {'code': '000000'}), Reply(200, {}),
    Reply(200, raw=b'{'), Reply(200, raw=b'[]'), Reply(201, UPLOAD_OK), Reply(204, raw=b''),
    Reply(400, {}), Reply(500, UPLOAD_OK), Reply(404, {'error': 'not found'}),
])
def test_upload_refused_unless_the_documented_success(octo, gcode, reply):
    octo.routes[UPLOAD] = reply
    assert printer().upload_file(str(gcode), 'gear.gcode') is False
    assert len(octo.requests) == 1


def test_a_refused_later_chunk_stops_the_upload(octo, gcode, monkeypatch):
    monkeypatch.setattr(adapter_module, 'UPLOAD_CHUNK_BYTES', 4)
    replies = iter([Reply(200, UPLOAD_OK), Reply(200, {**UPLOAD_OK, 'success': False})])
    octo.routes[UPLOAD] = lambda request: next(replies)
    assert printer().upload_file(str(gcode), 'gear.gcode') is False
    assert len(octo.requests) == 2


@pytest.mark.parametrize('failure', [requests.exceptions.Timeout('slow'), requests.exceptions.ConnectionError('reset')])
def test_upload_with_no_answer_is_unknown_not_refused(octo, gcode, failure):
    octo.routes[UPLOAD] = failure
    with pytest.raises(ElegooOutcomeUnknown):
        printer().upload_file(str(gcode), 'gear.gcode')


def test_a_timeout_on_a_later_chunk_is_unknown(octo, gcode, monkeypatch):
    monkeypatch.setattr(adapter_module, 'UPLOAD_CHUNK_BYTES', 4)
    outcomes = iter([Reply(200, UPLOAD_OK), requests.exceptions.Timeout('slow')])

    def upload(request):
        return next(outcomes)
    octo.routes[UPLOAD] = upload
    with pytest.raises(ElegooOutcomeUnknown):
        printer().upload_file(str(gcode), 'gear.gcode')


@pytest.mark.parametrize('name', ['a.3mf', 'a.bgcode', 'a', 'a\nb.gcode', ''])
def test_upload_refuses_non_gcode_and_bad_names_before_any_request(octo, gcode, name):
    assert printer().upload_file(str(gcode), name) is False
    assert octo.requests == []


def test_upload_name_is_a_basename_and_unreadable_or_empty_files_are_refused(octo, gcode, tmp_path):
    octo.routes[UPLOAD] = lambda request: Reply(200, UPLOAD_OK)
    assert printer().upload_file(str(gcode), '../../etc/x.gcode') is True
    assert octo.requests[0].kwargs['files']['File'][0] == 'x.gcode'
    octo.requests.clear()
    empty = tmp_path / 'empty.gcode'
    empty.write_bytes(b'')
    assert printer().upload_file(str(empty), 'a.gcode') is False
    assert printer().upload_file(str(gcode) + '.missing', 'a.gcode') is False
    assert octo.requests == []


def test_upload_refuses_an_itar_blocked_destination(octo, gcode, monkeypatch):
    from core.itar import ItarOutboundBlocked

    def blocked(url):
        raise ItarOutboundBlocked('blocked')
    monkeypatch.setattr('core.itar.enforce_request_destination', blocked)
    assert printer().upload_file(str(gcode), 'a.gcode') is False
    assert octo.requests == []


# ---- start ----

START_DATA = {'Filename': 'gear.gcode', 'StartLayer': 0, 'Calibration_switch': 0, 'PrintPlatformType': 0, 'Tlp_Switch': 0}


def test_start_accepted_on_ack_zero_with_the_documented_frame():
    p = printer()
    ws = attach(p, answer(0))
    assert p.start_print('gear.gcode') is True
    frame = ws.sent[0]
    assert frame['Topic'] == f'sdcp/request/{BOARD}'
    assert frame['Data']['Cmd'] == 128 and frame['Data']['MainboardID'] == BOARD and frame['Data']['Data'] == START_DATA


@pytest.mark.parametrize('ack', [1, 2, 3, 7, -1])
def test_start_refused_on_any_nonzero_ack(ack):
    p = printer()
    attach(p, answer(ack))
    assert p.start_print('gear.gcode') is False


def test_start_with_no_answer_is_unknown_not_refused():
    p = printer()
    p.command_timeout = 0.05
    ws = attach(p, lambda frame: [])
    with pytest.raises(ElegooOutcomeUnknown):
        p.start_print('gear.gcode')
    assert len(ws.sent) == 1


@pytest.mark.parametrize('reply', [
    lambda f: response(f, 0, RequestID='someone-else'),
    lambda f: response(f, 0, Cmd=129),
    lambda f: {'Topic': f"sdcp/response/{BOARD}", 'Data': {'Cmd': 128, 'RequestID': f['Data']['RequestID'], 'Data': {}}},
    lambda f: {'Topic': f"sdcp/response/{BOARD}", 'Data': 'x'},
    lambda f: response(f, True),
])
def test_an_unmatched_or_malformed_answer_is_unknown(reply):
    p = printer()
    p.command_timeout = 0.05
    attach(p, lambda frame: [reply(frame)])
    with pytest.raises(ElegooOutcomeUnknown):
        p.start_print('gear.gcode')


def test_a_write_that_fails_part_way_is_unknown():
    p = printer()
    attach(p, lambda frame: OSError('broken pipe'))
    with pytest.raises(ElegooOutcomeUnknown):
        p.start_print('gear.gcode')


def test_start_sends_nothing_when_not_connected_or_unnamed_or_not_gcode():
    p = printer()
    assert p.start_print('gear.gcode') is False  # not connected
    no_board = ElegooPrinter(HOST)
    ws = attach(no_board, answer(0))
    assert no_board.start_print('gear.gcode') is False and ws.sent == []
    p = printer()
    ws = attach(p, answer(0))
    for name in ('a.3mf', 'a.bgcode', 'a', 'a\nb.gcode'):
        assert p.start_print(name) is False
    assert ws.sent == []


def test_response_frames_do_not_reach_the_status_parser():
    p = printer()
    seen = []
    p.on_status(lambda status: seen.append(status))
    ws = attach(p, answer(0))
    p.start_print('gear.gcode')
    assert seen == []
    ws.deliver(ws, json.dumps(status_frame(BOARD, current=1, print_status=8)))
    assert seen and seen[0].internal_state == 'PRINTING'


# ---- pause, resume, stop, speed: existing command ids [repo], send-only as before ----

@pytest.mark.parametrize('method,args,cmd,data', [
    ('pause_print', (), 129, {}), ('resume_print', (), 131, {}), ('stop_print', (), 130, {}),
    ('cancel_print', (), 130, {}), ('set_print_speed', (80,), 403, {'PrintSpeedPct': 80}),
])
def test_controls_send_their_command_frames(method, args, cmd, data):
    p = printer()
    ws = attach(p)
    assert getattr(p, method)(*args) is True
    assert ws.sent[0]['Data']['Cmd'] == cmd and ws.sent[0]['Data']['Data'] == data
    assert ws.sent[0]['Topic'] == f'sdcp/request/{BOARD}'


def test_controls_send_nothing_when_not_connected():
    p = printer()
    assert p.pause_print() is False and p.stop_print() is False


# ---- parsing ----

@pytest.mark.parametrize('body,expected', [
    ({'code': '000000', 'success': True}, True), (UPLOAD_OK, True),
    ({'code': '000000', 'success': 'true'}, False), ({'code': '1', 'success': True}, False),
    ({'success': True}, False), ({}, False), (None, False), ([], False), ('ok', False),
])
def test_upload_reply_parser(body, expected):
    assert parsing.parse_upload_reply(body) is expected


@pytest.mark.parametrize('bad', [None, [], 'x', {}, {'Data': None}, {'Data': {'Cmd': '128', 'RequestID': 'r', 'Data': {'Ack': 0}}},
                                 {'Data': {'Cmd': 128, 'RequestID': '', 'Data': {'Ack': 0}}},
                                 {'Data': {'Cmd': 128, 'RequestID': 'r', 'Data': {'Ack': '0'}}},
                                 {'Data': {'Cmd': 128, 'RequestID': 'r', 'Data': {'Ack': True}}},
                                 {'Data': {'Cmd': True, 'RequestID': 'r', 'Data': {'Ack': 0}}}])
def test_malformed_responses_parse_to_none(bad):
    assert parsing.parse_response(bad) is None


def test_well_formed_response_parses():
    assert parsing.parse_response({'Data': {'Cmd': 128, 'RequestID': 'r', 'Data': {'Ack': 3}}}) == {'cmd': 128, 'request_id': 'r', 'ack': 3}


# ---- admission status read ----

@pytest.fixture
def sdcp_socket(monkeypatch):
    holder = {}

    def connect(frames, url_sink=None):
        conn = FakeConnection(frames)
        holder['conn'] = conn
        return conn
    monkeypatch.setattr('core.webhook_utils.device_connect_ip', lambda host, port=None: host)

    def install(frames):
        conn = FakeConnection(frames)
        urls = []
        monkeypatch.setattr('websocket.create_connection', lambda url, timeout=None, **kw: urls.append(url) or conn)
        return conn, urls
    return install


def creds(**over):
    return {'api_type': 'elegoo', 'ip': HOST, 'port': 3030, 'api_key': BOARD, **over}


def soon():
    return time.monotonic() + 5


def test_status_read_is_idle_only_for_the_configured_board_in_idle(sdcp_socket, monkeypatch):
    monkeypatch.setattr(requests.Session, 'request', lambda *a, **k: pytest.fail('SDCP readiness must not use HTTP'))
    conn, urls = sdcp_socket([{'Topic': f'sdcp/notice/{BOARD}'}, status_frame(BOARD)])
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), soon()) is True
    assert urls == [f'ws://{HOST}:3030/websocket'] and conn.closed
    assert conn.sent[0]['Data']['Cmd'] == 0 and conn.sent[0]['Data']['MainboardID'] == BOARD


@pytest.mark.parametrize('frames', [
    [status_frame(BOARD, current=1, print_status=8)],        # printing
    [status_frame(BOARD, current=0, print_status=5)],        # paused
    [status_frame(BOARD, current=0, print_status=16)],       # finished, not reset
    [status_frame(BOARD, current=3)],                        # unknown state
    [status_frame('OTHER', topic=f'sdcp/status/{BOARD}')],   # another board answering
    [status_frame('OTHER')],                                 # not our topic: ignored, then silence
    ['not json'], [],                                        # garbage, silence
])
def test_status_read_fails_closed(sdcp_socket, frames):
    sdcp_socket(frames)
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), time.monotonic() + 1) is False


@pytest.mark.parametrize('bad', [{'api_key': ''}, {'api_key': 'a\nb'}, {'api_key': None}, {'port': True}, {'port': 0}, {'ip': ''}, {'ip': 'a b'}])
def test_status_read_refuses_bad_inputs_without_connecting(monkeypatch, bad):
    monkeypatch.setattr('websocket.create_connection', lambda *a, **k: pytest.fail('must not connect'))
    assert dispatch_readiness.fresh_http_status_is_idle(creds(**bad), soon()) is False


def test_status_read_respects_the_deadline_and_a_blocked_destination(sdcp_socket, monkeypatch):
    sdcp_socket([status_frame(BOARD)])
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), time.monotonic() - 1) is False

    def blocked(host, port=None):
        raise RuntimeError('blocked')
    monkeypatch.setattr('core.webhook_utils.device_connect_ip', blocked)
    monkeypatch.setattr('websocket.create_connection', lambda *a, **k: pytest.fail('must not connect'))
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), soon()) is False


def test_the_whole_upload_has_a_time_limit(octo, gcode, monkeypatch):
    monkeypatch.setattr(adapter_module, 'UPLOAD_CHUNK_BYTES', 4)
    clock = [0.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])
    seen = []

    def upload(request):
        seen.append(request.kwargs['data']['Offset'])
        clock[0] += 700
        return Reply(200, UPLOAD_OK)
    octo.routes[UPLOAD] = upload
    assert printer().upload_file(str(gcode), 'gear.gcode') is False   # stopped, never reported as sent
    assert seen == ['0', '4']


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('status', [
    {'TempOfNozzle': 220}, {'CurrentStatus': [0]}, {'PrintInfo': {'Status': 0}},
    {'CurrentStatus': [0], 'PrintInfo': {}},
    *[{'CurrentStatus': value, 'PrintInfo': {'Status': 0}}
      for value in (False, 0.0, [False], [0.0], [], [0, 1], '0', None)],
    *[{'CurrentStatus': [0], 'PrintInfo': {'Status': value}}
      for value in (False, 0.0, '0', None)],
])
def test_readiness_requires_explicit_valid_idle_states(sdcp_socket, nested, status):
    frame = status_frame(BOARD)
    frame.pop('Status')
    frame.update({'Data': {'Status': status}} if nested else {'Status': status})
    conn, _ = sdcp_socket([frame])
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), soon()) is False
    assert conn.closed


@pytest.mark.parametrize('nested', [False, True])
@pytest.mark.parametrize('current', [0, [0]])
def test_readiness_accepts_explicit_idle_states(sdcp_socket, nested, current):
    frame = status_frame(BOARD)
    frame['Status']['CurrentStatus'] = current
    if nested:
        frame['Data'] = {'Status': frame.pop('Status')}
    sdcp_socket([frame])
    assert dispatch_readiness.fresh_http_status_is_idle(creds(), soon()) is True


@pytest.mark.parametrize('topic,board', [
    ('sdcp/response/OTHER', 'OTHER'), ('sdcp/response/OTHER', BOARD),
    (f'sdcp/response/{BOARD}', 'OTHER'), (f'sdcp/response/{BOARD}', None),
    (f'prefix/sdcp/response/{BOARD}', BOARD),
])
def test_start_requires_both_response_board_identifiers(topic, board):
    p = printer()
    p.command_timeout = 0.001

    def wrong_board(frame):
        reply = response(frame, 0, MainboardID=board)
        reply['Topic'] = topic
        return [reply]
    ws = attach(p, wrong_board)
    with pytest.raises(ElegooOutcomeUnknown):
        p.start_print('gear.gcode')
    assert len(ws.sent) == 1 and not p._replies


@pytest.mark.parametrize('elapsed', [1200, 1201])
@pytest.mark.parametrize('stage', ['headers', 'body', 'eof'])
def test_upload_rejects_a_late_final_response(octo, gcode, monkeypatch, elapsed, stage):
    clock = [0.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])
    closed = []

    class SlowReply(Reply):
        def iter_content(self, chunk_size=4096):
            assert chunk_size == 1  # a trickle must not block waiting to fill a large buffer
            if stage == 'body':
                clock[0] = elapsed
            yield self.content
            if stage == 'body':
                pytest.fail('must stop consuming an expired response')
            if stage == 'eof':
                clock[0] = elapsed

        def __exit__(self, *exc):
            closed.append(True)
            return False

    def upload(request):
        if stage == 'headers':
            clock[0] = elapsed
        return SlowReply(200, UPLOAD_OK)
    octo.routes[UPLOAD] = upload
    assert printer().upload_file(str(gcode), 'gear.gcode') is False
    assert len(octo.requests) == 1 and closed == [True]
    assert octo.requests[0].kwargs['stream'] is True


def test_upload_body_read_failure_is_unknown(octo, gcode):
    class BrokenReply(Reply):
        def iter_content(self, chunk_size=4096):
            yield b'{'
            raise requests.exceptions.ConnectionError('interrupted response')
    octo.routes[UPLOAD] = BrokenReply(200, UPLOAD_OK)
    with pytest.raises(ElegooOutcomeUnknown):
        printer().upload_file(str(gcode), 'gear.gcode')


def test_upload_bounds_each_request_by_the_remaining_time(octo, gcode, monkeypatch):
    monkeypatch.setattr(adapter_module, 'UPLOAD_CHUNK_BYTES', 6)
    clock = [0.0]
    monkeypatch.setattr(adapter_module.time, 'monotonic', lambda: clock[0])

    def upload(request):
        clock[0] = 1180
        return Reply(200, UPLOAD_OK)
    octo.routes[UPLOAD] = upload
    assert printer().upload_file(str(gcode), 'gear.gcode') is True
    assert [r.kwargs['timeout'] for r in octo.requests] == [60, 20]


def test_upload_bounds_the_streamed_response_size(octo, gcode, monkeypatch):
    monkeypatch.setattr(adapter_module, '_MAX_BODY', 16)
    octo.routes[UPLOAD] = Reply(200, UPLOAD_OK)
    assert printer().upload_file(str(gcode), 'gear.gcode') is False
