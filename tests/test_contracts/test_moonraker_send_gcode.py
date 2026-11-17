"""Exercise the real HTTP request builder without contacting a printer."""
import http.client
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from modules.printers.adapters import moonraker


@pytest.mark.parametrize('script', [
    'M117 hello', 'SET_PRESSURE_ADVANCE ADVANCE=0.04', 'M117 50%',
    'M117 a&b', 'M117 a+b', 'M117 #hello', 'G28\nM117 ready',
])
def test_moonraker_send_gcode_encodes_the_script(monkeypatch, script):
    from core import itar
    destinations, received = [], []
    monkeypatch.setattr(itar, 'enforce_request_destination', destinations.append)

    def server(request, timeout):
        url = urlsplit(request.full_url)
        target = url.path + ('?' + url.query if url.query else '')
        # Validate the wire request target as urllib's HTTP layer does; no socket.
        http.client.HTTPConnection('moonraker.test').putrequest(request.get_method(), target)
        assert request.get_method() == 'POST'
        assert url.path == '/printer/gcode/script'
        assert request.get_header('Content-type') == 'application/json'
        payload = json.loads(request.data) if request.data else parse_qs(url.query)
        received.append(payload['script'])
        return io.BytesIO(b'{"result": "ok"}')

    monkeypatch.setattr(moonraker, 'urlopen', server)
    assert moonraker.MoonrakerPrinter('moonraker.test').send_gcode(script) is True
    assert received == [script]
    assert len(destinations) == 1


@pytest.mark.parametrize('failure', ['http', 'connection', 'invalid_json', 'unexpected'])
def test_moonraker_send_gcode_failure_returns_false(monkeypatch, caplog, failure):
    from core import itar
    monkeypatch.setattr(itar, 'enforce_request_destination', lambda url: None)

    def server(request, timeout):
        if failure == 'http':
            raise HTTPError(request.full_url, 400, 'Bad request', {}, None)
        if failure == 'connection':
            raise URLError('private-argument')
        if failure == 'unexpected':
            raise RuntimeError('private-argument')
        return io.BytesIO(b'not json')

    monkeypatch.setattr(moonraker, 'urlopen', server)
    assert moonraker.MoonrakerPrinter('moonraker.test').send_gcode('M117 private-argument') is False
    assert caplog.records
    assert 'private-argument' not in caplog.text
