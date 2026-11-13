"""TEST ONLY: a scripted OctoPrint REST server for adapter, readiness and monitor replays.

Transcripts are built from OctoPrint's public REST API documentation, never from OctoPrint code.
Route it with tests.support.device_http.route_device_requests(monkeypatch, get=fake.verb('GET'), ...).
"""
import json
from types import SimpleNamespace
from urllib.parse import urlsplit


class Reply:
    """The parts of requests.Response the adapters and readiness checks use."""

    def __init__(self, status=200, body=None, raw=None):
        self.status_code = status
        self.content = raw if raw is not None else json.dumps(body).encode()
        self.text = self.content.decode(errors='replace')

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=4096):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Octo:
    """routes[(METHOD, path)] -> Reply | exception instance | callable(request) -> either."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def verb(self, method):
        def call(url, **kwargs):
            parts = urlsplit(url)
            request = SimpleNamespace(method=method, url=url, path=parts.path,
                                      headers=kwargs.get('headers'), kwargs=kwargs)
            self.requests.append(request)
            outcome = self.routes.get((method, parts.path))
            if callable(outcome):
                outcome = outcome(request)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome if outcome is not None else Reply(404, {'error': 'not found'})
        return call


def flags(**on):
    base = dict(operational=False, paused=False, printing=False, cancelling=False,
                pausing=False, error=False, ready=False, closedOrError=False)
    base.update(on)
    return base


def printer_body(state_flags, tool=(214.9, 215.0), bed=(60.0, 60.0)):
    return {"temperature": {"tool0": {"actual": tool[0], "target": tool[1], "offset": 0},
                            "bed": {"actual": bed[0], "target": bed[1], "offset": 0}},
            "sd": {"ready": False},
            "state": {"text": "Printing", "flags": state_flags}}


def job_body(name='gear.gcode', completion=42.0, print_time=600, left=840, state='Printing'):
    return {"job": {"file": {"name": name, "origin": "local", "size": 1000}, "estimatedPrintTime": 1440},
            "progress": {"completion": completion, "filepos": 10, "printTime": print_time, "printTimeLeft": left},
            "state": state}
