"""POST /printers/test-connection says why it failed.

Every peer is simulated at the transport seam (no network). A reason is only
what the test observed; ambiguous outcomes are "unknown". No failure body
carries the printer address, serial, access code or API key.
"""
import errno
import socket
import ssl
from types import SimpleNamespace as NS

import httpx
import pytest

USER = {"id": 1, "role": "admin"}
HOST = "192.168.77.41"
SERIAL = "SERIALSECRET0001"
CODE = "accesssecret77"
KEY = "apikeysecret-55"
SECRETS = (HOST, SERIAL, CODE, KEY)


def run(**fields):
    from modules.printers import route_utils, routes_crud
    from modules.printers.connection_reasons import REASONS
    request = route_utils.TestConnectionRequest(**{"api_host": HOST, **fields})
    result = routes_crud.test_printer_connection(request, USER)
    if not result["success"]:
        assert result["reason"] in REASONS and isinstance(result["error"], str) and result["error"]
        assert not any(secret in str(result) for secret in SECRETS), result
    return result


def reply(status, body=b"{}"):
    return httpx.Response(status, content=body)


def raising(exc):
    def call(*args, **kwargs):
        raise exc
    return call


# ---- address policy ----

@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "[::1]", "0.0.0.0"])
def test_policy_refused_address_is_blocked_address(host):
    from modules.printers import route_utils, routes_crud
    for api_type in ("bambu", "moonraker", "elegoo"):
        request = route_utils.TestConnectionRequest(api_type=api_type, api_host=host, serial=SERIAL, access_code=CODE)
        result = routes_crud.test_printer_connection(request, USER)
        assert result["success"] is False and result["reason"] == "blocked_address"
        assert host not in result["error"] and SERIAL not in str(result) and CODE not in str(result)


def test_name_that_does_not_resolve_is_unreachable_not_blocked(monkeypatch):
    import core.webhook_utils as network
    from modules.printers import route_utils, routes_crud
    monkeypatch.setattr(network, "_real_getaddrinfo", raising(socket.gaierror(8, "nodename nor servname provided")))
    request = route_utils.TestConnectionRequest(api_type="moonraker", api_host="printer-typo.test")
    result = routes_crud.test_printer_connection(request, USER)
    assert result["success"] is False and result["reason"] == "unreachable"
    assert "printer-typo" not in str(result)


# ---- HTTP printers: Moonraker, PrusaLink, OctoPrint, Elegoo ----

HTTP_CASES = [
    (httpx.ConnectError("refused"), "unreachable"),
    (httpx.ConnectTimeout("slow"), "unreachable"),
    (httpx.ReadTimeout("silent"), "unreachable"),
    (httpx.RemoteProtocolError("not http"), "not_this_type"),
    (httpx.ReadError("reset"), "unknown"),
    (RuntimeError("anything else"), "unknown"),
    (reply(401), "auth"),
    (reply(403), "auth"),
    (reply(404), "not_this_type"),
    (reply(500), "unknown"),
    (reply(302), "unknown"),
]


def http_peer(outcome):
    return raising(outcome) if isinstance(outcome, Exception) else (lambda *a, **k: outcome)


@pytest.mark.parametrize("outcome,reason", HTTP_CASES + [(reply(200, b"<html>a web page</html>"), "not_this_type")])
def test_moonraker_reasons(monkeypatch, outcome, reason):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, "pinned_device_get", http_peer(outcome))
    assert run(api_type="moonraker")["reason"] == reason


@pytest.mark.parametrize("outcome,reason", HTTP_CASES)
def test_prusalink_reasons(monkeypatch, outcome, reason):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, "prusalink_test_get", http_peer(outcome))
    assert run(api_type="prusalink", api_key=KEY)["reason"] == reason


@pytest.mark.parametrize("outcome,reason", HTTP_CASES)
def test_octoprint_reasons(monkeypatch, outcome, reason):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, "octoprint_test_get", http_peer(outcome))
    assert run(api_type="octoprint", api_key=KEY)["reason"] == reason


def test_octoprint_without_a_key_sends_nothing_and_is_unknown(monkeypatch):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, "octoprint_test_get", raising(AssertionError("must not be called")))
    assert run(api_type="octoprint")["reason"] == "unknown"


@pytest.mark.parametrize("outcome,reason", [case for case in HTTP_CASES if isinstance(case[0], Exception)])
def test_elegoo_reasons(monkeypatch, outcome, reason):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, "pinned_device_get", http_peer(outcome))
    assert run(api_type="elegoo")["reason"] == reason


# ---- Ultimaker and Anycubic: adapters that hide the transport error ----

@pytest.mark.parametrize("answer,reason", [
    ((None, None), "unknown"),      # timeout, refused and blocked all look the same to the adapter
    ((404, None), "not_this_type"),
    ((200, {}), "not_this_type"),   # answered 200, but without an Ultimaker's fields
    ((200, None), "not_this_type"),
    ((401, None), "unknown"),       # the test sends no credential, so this is not "auth"
    ((500, None), "unknown"),
])
def test_ultimaker_reasons(monkeypatch, answer, reason):
    from modules.printers.adapters import ultimaker
    monkeypatch.setattr(ultimaker.UltimakerPrinter, "_fetch", lambda self, path: answer)
    assert run(api_type="ultimaker")["reason"] == reason


@pytest.mark.parametrize("connect,poll,info", [(False, True, 1), (True, False, 1), (True, True, 0)])
def test_anycubic_cannot_tell_and_says_unknown(monkeypatch, connect, poll, info):
    import modules.printers.adapters.anycubic_lan as adapter
    import modules.printers.route_utils as route_utils
    fake = NS(connect=lambda: connect, poll=lambda: poll, last_info=info, disconnect=lambda: None)
    monkeypatch.setattr(adapter, "AnycubicLANPrinter", lambda host: fake)
    monkeypatch.setattr(route_utils.time, "monotonic", iter(range(0, 1000, 10)).__next__)
    assert run(api_type="anycubic_lan")["reason"] == "unknown"


def test_unknown_printer_type_is_unknown():
    assert run(api_type="no-such-type")["reason"] == "unknown"


# ---- Bambu, legacy adapter: a fake paho client stands in for the printer ----

def fake_paho(monkeypatch, *, connect_error=None, connack=None):
    """paho client whose connect() raises, or whose loop delivers one CONNACK code (None: silence)."""
    from modules.printers.adapters import bambu

    class Client:
        def __init__(self, *args, **kwargs):
            self.on_connect = None
        def username_pw_set(self, *args): pass
        def tls_set(self, **kwargs): pass
        def tls_insecure_set(self, value): pass
        def connect(self, address, port, keepalive=60):
            assert address == HOST and port == 8883
            if connect_error is not None:
                raise connect_error
        def loop_start(self):
            if connack is not None:
                self.on_connect(self, None, {}, connack)
        def subscribe(self, topic): pass
        def publish(self, *args, **kwargs): return NS(rc=0, wait_for_publish=lambda timeout=None: None, is_published=lambda: True)
        def disconnect(self): pass
        def loop_stop(self): pass
        def socket(self): return None

    monkeypatch.setattr(bambu.mqtt, "Client", Client)
    monkeypatch.setattr(bambu.BambuPrinter, "MQTT_TIMEOUT", 0.2)
    from modules.printers.telemetry import feature_flag
    monkeypatch.setattr(feature_flag, "is_v2_enabled", lambda: False)


@pytest.mark.parametrize("error,reason", [
    (ConnectionRefusedError(61, "refused"), "unreachable"),
    (TimeoutError("timed out"), "unreachable"),
    (OSError(errno.EHOSTUNREACH, "No route to host"), "unreachable"),
    (OSError(errno.EPIPE, "Broken pipe"), "unknown"),
    (ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number"), "not_this_type"),
    (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), "unknown"),
    (ValueError("anything else"), "unknown"),
])
def test_bambu_legacy_connect_errors(monkeypatch, error, reason):
    fake_paho(monkeypatch, connect_error=error)
    assert run(api_type="bambu", serial=SERIAL, access_code=CODE)["reason"] == reason


@pytest.mark.parametrize("connack,reason", [
    (5, "auth"),       # not authorized: the access code was rejected
    (4, "auth"),       # bad user name or password
    (3, "unknown"),    # server unavailable
    (None, "unknown"), # TLS came up, the broker never answered: cannot tell why
])
def test_bambu_legacy_connack(monkeypatch, connack, reason):
    fake_paho(monkeypatch, connack=connack)
    assert run(api_type="bambu", serial=SERIAL, access_code=CODE)["reason"] == reason


def test_bambu_never_claims_control_disabled():
    """LAN Only / Developer Mode cannot be seen by a read-only MQTT login, so no path returns it."""
    import inspect
    from modules.printers import connection_reasons, routes_crud, route_utils
    from modules.printers.adapters import bambu
    from modules.printers.telemetry.bambu import session
    assert "control_disabled" in connection_reasons.REASONS
    for module in (routes_crud, route_utils, bambu, session):
        source = inspect.getsource(module)
        assert "CONTROL_DISABLED" not in source and "control_disabled" not in source


# ---- Bambu, V2 session ----

def v2_adapter(monkeypatch, *, start_error=None, refusal=None):
    from modules.printers.telemetry.bambu import session

    class Adapter:
        def __init__(self, config, emitter):
            self.connect_refusal = refusal
        def start(self):
            if start_error is not None:
                raise start_error
        def stop(self): pass

    monkeypatch.setattr(session, "BambuTelemetryAdapter", Adapter)
    return session


@pytest.mark.parametrize("error,reason", [
    (ConnectionRefusedError(61, "refused"), "unreachable"),
    (TimeoutError("timed out"), "unreachable"),
    (ssl.SSLError(1, "wrong version number"), "not_this_type"),
    (RuntimeError("anything else"), "unknown"),
])
def test_bambu_v2_start_errors(monkeypatch, error, reason):
    session = v2_adapter(monkeypatch, start_error=error)
    result = session.read_status_once(object(), timeout=0.05)
    assert result.success is False and result.reason == reason


@pytest.mark.parametrize("refusal,reason", [
    (5, "auth"), (4, "auth"), (NS(value=135), "auth"), (NS(value=134), "auth"),
    (NS(value=136), "unknown"), (None, "unknown"),
])
def test_bambu_v2_connack(monkeypatch, refusal, reason):
    session = v2_adapter(monkeypatch, refusal=refusal)
    result = session.read_status_once(object(), timeout=0.05)
    assert result.success is False and result.reason == reason


def test_bambu_v2_adapter_records_the_refusal_code():
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig, BambuTelemetryAdapter
    events = []
    adapter = BambuTelemetryAdapter(BambuAdapterConfig(printer_id="t", serial=SERIAL, host=HOST, access_code=CODE),
                                    emitter=events.append)
    assert adapter.connect_refusal is None
    adapter._on_connect(NS(subscribe=lambda *a, **k: None), None, {}, NS(value=135))
    assert adapter.connect_refusal.value == 135


def test_bambu_v2_route_passes_the_session_reason_through(monkeypatch):
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import session
    monkeypatch.setattr(feature_flag, "is_v2_enabled", lambda: True)
    monkeypatch.setattr(session, "read_status_once", lambda config, timeout: session.StatusReadResult(
        success=False, error="timeout waiting for CONNACK", reason="auth"))
    assert run(api_type="bambu", serial=SERIAL, access_code=CODE)["reason"] == "auth"
    monkeypatch.setattr(session, "read_status_once", raising(RuntimeError("synthetic")))
    assert run(api_type="bambu", serial=SERIAL, access_code=CODE)["reason"] == "unknown"


# ---- compatibility ----

def test_failure_keeps_success_and_error_and_success_has_no_reason(monkeypatch):
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, "pinned_device_get", http_peer(reply(404)))
    failed = run(api_type="moonraker")
    assert set(failed) == {"success", "error", "reason"} and failed["error"] == "Moonraker returned HTTP 404"
    monkeypatch.setattr(route_utils, "pinned_device_get", http_peer(reply(200, b'{"result": {"state": "ready"}}')))
    ok = run(api_type="moonraker")
    assert ok["success"] is True and ok["state"] == "ready" and "reason" not in ok
