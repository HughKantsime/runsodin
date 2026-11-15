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


def tls_error(reason, error_type=ssl.SSLError):
    error = error_type(1, reason)
    error.reason = reason  # OpenSSL supplies this structured attribute on real failures.
    return error


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
    (httpx.RemoteProtocolError("not http"), "unknown"),
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


@pytest.mark.parametrize("api_type", ["moonraker", "prusalink", "octoprint", "elegoo"])
def test_http_peer_disconnect_without_response_is_unknown(monkeypatch, api_type):
    from modules.printers import route_utils
    peer = raising(httpx.RemoteProtocolError("Server disconnected without sending a response."))
    for seam in ("pinned_device_get", "prusalink_test_get", "octoprint_test_get"):
        monkeypatch.setattr(route_utils, seam, peer)
    assert run(api_type=api_type, api_key=KEY)["reason"] == "unknown"


@pytest.mark.parametrize("api_type", ["moonraker", "prusalink", "octoprint", "elegoo"])
def test_http_non_http_status_line_is_not_this_type(monkeypatch, api_type):
    import h11
    from modules.printers import route_utils

    parser = h11.Connection(h11.CLIENT)
    parser.receive_data(b"SSH-2.0-OpenSSH_9.0\r\n\r\n")
    with pytest.raises(h11.RemoteProtocolError) as failure:
        parser.next_event()
    # httpcore/httpx preserve h11's message when translating the exception.
    error = httpx.RemoteProtocolError(str(failure.value))
    for seam in ("pinned_device_get", "prusalink_test_get", "octoprint_test_get"):
        monkeypatch.setattr(route_utils, seam, raising(error))
    assert run(api_type=api_type, api_key=KEY)["reason"] == "not_this_type"


@pytest.mark.parametrize("v2", [False, True])
def test_tls_internal_error_is_unknown(monkeypatch, v2):
    error = tls_error("TLSV1_ALERT_INTERNAL_ERROR")
    if v2:
        session = v2_adapter(monkeypatch, start_error=error)
        result = session.read_status_once(object(), timeout=0.05)
        assert result.success is False and result.reason == "unknown"
    else:
        fake_paho(monkeypatch, connect_error=error)
        assert run(api_type="bambu", serial=SERIAL, access_code=CODE)["reason"] == "unknown"


@pytest.mark.parametrize("error", [
    tls_error("CERTIFICATE_VERIFY_FAILED", ssl.SSLCertVerificationError),
    tls_error("TLSV1_ALERT_PROTOCOL_VERSION"),
])
@pytest.mark.parametrize("v2", [False, True])
def test_tls_certificate_and_version_failures_are_unknown(monkeypatch, error, v2):
    if v2:
        session = v2_adapter(monkeypatch, start_error=error)
        result = session.read_status_once(object(), timeout=0.05)
        assert result.success is False and result.reason == "unknown"
    else:
        fake_paho(monkeypatch, connect_error=error)
        assert run(api_type="bambu", serial=SERIAL, access_code=CODE)["reason"] == "unknown"


@pytest.mark.parametrize("payload", [
    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
    b"SSH-2.0-OpenSSH_9.0\r\n",
    b"GET / HTTP/1.1\r\n\r\n",
], ids=["http_response", "ssh_banner", "http_request"])
@pytest.mark.parametrize("v2", [False, True])
def test_runtime_tls_plaintext_peer_is_not_this_type(monkeypatch, payload, v2):
    """Use the installed OpenSSL parser without sockets or hand-set reason strings."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client = context.wrap_bio(incoming, outgoing, server_hostname="printer.test")
    with pytest.raises(ssl.SSLWantReadError):
        client.do_handshake()
    incoming.write(payload)
    with pytest.raises(ssl.SSLError) as failure:
        client.do_handshake()
    if v2:
        session = v2_adapter(monkeypatch, start_error=failure.value)
        result = session.read_status_once(object(), timeout=0.05)
        assert result.success is False and result.reason == "not_this_type"
    else:
        fake_paho(monkeypatch, connect_error=failure.value)
        assert run(api_type="bambu", serial=SERIAL, access_code=CODE)["reason"] == "not_this_type"


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
    monkeypatch.setattr(ultimaker.UltimakerPrinter, "_fetch", lambda self, path, **kwargs: answer)
    assert run(api_type="ultimaker")["reason"] == reason


@pytest.mark.parametrize("failure", ["timeout", "reset", "deadline", "deadline_at_eof"])
def test_ultimaker_unreadable_body_is_unknown(monkeypatch, failure):
    import requests
    from modules.printers.adapters import ultimaker

    class Body:
        status_code = 200
        closed = False

        def iter_content(self, chunk_size):
            yield b'{"variant": "Ultimaker S5"}'
            if failure == "timeout":
                raise requests.exceptions.ReadTimeout("synthetic body timeout")
            if failure == "reset":
                raise requests.exceptions.ConnectionError("synthetic body reset")

        def close(self):
            self.closed = True

    body = Body()
    monkeypatch.setattr(ultimaker.UltimakerPrinter, "_get", lambda self, path: body)
    causes = []
    read_body = ultimaker._json_object

    def record_read_failure(*args, **kwargs):
        try:
            return read_body(*args, **kwargs)
        except ultimaker._BodyReadError as exc:
            causes.append(exc.__cause__)
            raise

    monkeypatch.setattr(ultimaker, "_json_object", record_read_failure)
    if failure.startswith("deadline"):
        ticks = iter([0.0, 999.0] if failure == "deadline" else [0.0, 1.0, 999.0])
        monkeypatch.setattr(ultimaker, "time", NS(monotonic=lambda: next(ticks, 999.0)))
    result = run(api_type="ultimaker")
    assert body.closed
    assert result["reason"] == "unknown"
    expected = {"timeout": requests.exceptions.ReadTimeout, "reset": requests.exceptions.ConnectionError}
    assert len(causes) == 1 and isinstance(causes[0], expected.get(failure, TimeoutError))
    if failure.startswith("deadline"):
        # Additional timing observations must not turn into swallowed StopIteration.
        assert [ultimaker.time.monotonic() for _ in range(4)] == [999.0] * 4


@pytest.mark.parametrize("body", [b"<html>not a printer</html>", b"{}", b"[]"])
def test_ultimaker_inspected_wrong_body_is_not_this_type(monkeypatch, body):
    from modules.printers.adapters import ultimaker
    from tests.support.octoprint_http import Reply
    response = Reply(200, raw=body)
    monkeypatch.setattr(ultimaker.UltimakerPrinter, "_get", lambda self, path: response)
    assert run(api_type="ultimaker")["reason"] == "not_this_type"


@pytest.mark.parametrize("failure", ["bad_gzip", "deep_json"])
def test_ultimaker_malformed_encoded_body_is_not_this_type(monkeypatch, failure):
    import io
    import sys
    import requests
    from urllib3.response import HTTPResponse
    from modules.printers.adapters import ultimaker

    content = b"not a gzip stream" if failure == "bad_gzip" else (
        b'{"nested":' + b'[' * sys.getrecursionlimit() + b'0' + b']' * sys.getrecursionlimit() + b'}'
    )
    response = requests.Response()
    response.raw = HTTPResponse(
        body=io.BytesIO(content), preload_content=False,
        headers={"Content-Encoding": "gzip"} if failure == "bad_gzip" else {},
    )
    # Exercise real decoding over in-memory bytes; there is no socket to interrupt.
    body = NS(status_code=200, iter_content=response.iter_content, close=response.close)
    monkeypatch.setattr(ultimaker.UltimakerPrinter, "_get", lambda self, path: body)
    assert run(api_type="ultimaker")["reason"] == "not_this_type"
    assert response.raw.closed


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_ultimaker_system_body_size_limit(monkeypatch, extra_bytes):
    import json
    from modules.printers.adapters import ultimaker
    from tests.support.octoprint_http import Reply

    content = json.dumps({"variant": "Ultimaker S5", "pad": ""}).encode()
    content = content[:-2] + b'x' * (ultimaker._MAX_BODY - len(content) + extra_bytes) + content[-2:]
    body = Reply(200, raw=content)
    closed = []
    body.close = lambda: closed.append(True)
    monkeypatch.setattr(ultimaker.UltimakerPrinter, "_get", lambda self, path: body)
    result = run(api_type="ultimaker")
    assert closed == [True]
    if extra_bytes:
        assert result["success"] is False and result["reason"] == "unknown"
    else:
        assert result["success"] is True and result["variant"] == "Ultimaker S5"


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
    (tls_error("WRONG_VERSION_NUMBER"), "not_this_type"),
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
    (tls_error("WRONG_VERSION_NUMBER"), "not_this_type"),
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
