"""A Bambu test/sync session always disconnects, even if reading status raises.

A leaked paho session keeps a TLS socket and loop thread open, and Bambu
printers have few MQTT slots (11-08: stray clients starved the X1C).
"""
import ast
import inspect
from types import SimpleNamespace as NS

import pytest


def _fake_printer(events):
    def make(**kwargs):
        def get_status():
            events.append("status")
            raise RuntimeError("synthetic status failure")
        return NS(connect=lambda: events.append("connect") or True, get_status=get_status,
                  disconnect=lambda: events.append("disconnect"))
    return make


def test_crud_test_connection_disconnects_when_status_raises(monkeypatch):
    from modules.printers import routes_crud, route_utils
    from modules.printers.adapters import bambu
    from modules.printers.telemetry import feature_flag
    events = []
    monkeypatch.setattr(feature_flag, "is_v2_enabled", lambda: False)
    monkeypatch.setattr(bambu, "BambuPrinter", _fake_printer(events))
    monkeypatch.setattr(routes_crud.time, "sleep", lambda s: None)
    request = route_utils.TestConnectionRequest(api_type="bambu", api_host="192.168.1.44", serial="s", access_code="c")
    result = routes_crud.test_printer_connection(request, {"id": 1, "role": "admin"})
    assert result["success"] is False
    assert events == ["connect", "status", "disconnect"]


def test_setup_test_connection_disconnects_when_status_raises(monkeypatch):
    from modules.system import routes_setup
    from modules.printers.adapters import bambu
    import time
    events = []
    monkeypatch.setattr(bambu, "BambuPrinter", _fake_printer(events))
    monkeypatch.setattr(time, "sleep", lambda s: None)
    result = routes_setup._test_bambu_legacy(NS(api_host="192.168.1.44", serial="s", access_code="c"))
    assert result["success"] is False
    assert events == ["connect", "status", "disconnect"]


def _disconnects_in_finally(function, name):
    tree = ast.parse(inspect.getsource(function).lstrip())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for stmt in node.finalbody:
                for call in ast.walk(stmt):
                    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "disconnect" and getattr(call.func.value, "id", None) == name):
                        found.append(call)
    return found


@pytest.mark.parametrize("name", ["bambu", "mk"])
def test_ams_sync_disconnects_in_finally(name):
    from modules.printers import routes_ams
    assert _disconnects_in_finally(routes_ams.sync_ams_state, name)
