"""Under ITAR mode every go2rtc request goes to the address that was checked, with no proxy,
and go2rtc is only given camera sources on the local network.

Found by review 2026-11-13:
1. go2rtc_api_url() checked the name in ODIN_GO2RTC_API_URL and returned the URL with the
   name still in it, so the client looked the name up again when it connected.
2. The go2rtc clients honoured HTTP_PROXY / HTTPS_PROXY / ALL_PROXY.
3. ODIN_CAMERA_RELAY_BASE_URL was written into go2rtc's configuration, with a camera token,
   with no check.

Every transport here is a fake. Nothing opens a connection.
"""
import ast
import asyncio
import contextlib
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import HTTPException

from core import itar
from modules.printers import camera_relay, go2rtc_access, route_utils

BACKEND = Path(__file__).resolve().parents[2] / "backend"
PRIVATE = "172.18.0.4"
PUBLIC = "8.8.8.8"
PROXY = "http://203.0.113.9:3128"
JPEG = b"\xff\xd8\xff" + b"0" * 32


def _answer(*addresses):
    return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 0))
            for a in addresses]


class Resolver:
    """Answers with `answers[0]` on the first lookup, `answers[1]` on the second, and so on;
    the last answer repeats. Each answer is a tuple of addresses, or an exception."""

    def __init__(self, monkeypatch, *answers):
        self.answers = answers
        self.calls = []
        monkeypatch.setattr(socket, "getaddrinfo", self)

    def __call__(self, host, *args, **kwargs):
        answer = self.answers[min(len(self.calls), len(self.answers) - 1)]
        self.calls.append(host)
        if isinstance(answer, Exception):
            raise answer
        return _answer(*answer)


def _rebinding(monkeypatch):
    """Local on the first lookup, public on every lookup after it."""
    return Resolver(monkeypatch, (PRIVATE,), (PUBLIC,))


def _no_lookup(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a name was looked up")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def secret(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    monkeypatch.delenv("ODIN_CAMERA_RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("ODIN_GO2RTC_API_URL", raising=False)
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    camera_relay.rotate_relay_secret()


def _proxies(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, PROXY)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


class _Frames(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"--frame\r\n"


class Wire:
    """Replaces every httpx entry point the go2rtc callers use with a fake transport.

    Records each request as it would leave, and the trust_env each client was built with.
    The real client under the fake is always built with trust_env=False, so the proxy
    variables set by these tests can never be used either.
    """

    def __init__(self, monkeypatch):
        self.requests = []
        self.trust_env = []
        real_async, real_sync = httpx.AsyncClient, httpx.Client

        def handler(request):
            self.requests.append(request)
            path = request.url.path
            if path == "/api/streams":
                return httpx.Response(200, json={"printer_9": {}, "printer_9_mjpeg": {}})
            if path == "/api/stream.mjpeg":
                return httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"},
                                      stream=_Frames())
            if path == "/api/frame.jpeg":
                return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=JPEG)
            return httpx.Response(200, content=b"ok")

        def build(real, kwargs):
            self.trust_env.append(kwargs.pop("trust_env", True))
            return real(transport=httpx.MockTransport(handler), trust_env=False, **kwargs)

        def client_options(kwargs):
            return {name: kwargs.pop(name) for name in ("trust_env",) if name in kwargs}

        def module_call(method):
            def call(url, **kwargs):
                with build(real_sync, client_options(kwargs)) as client:
                    return client.request(method, url, **kwargs)
            return call

        @contextlib.contextmanager
        def stream(method, url, **kwargs):
            with build(real_sync, client_options(kwargs)) as client:
                with client.stream(method, url, **kwargs) as response:
                    yield response

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: build(real_async, kwargs))
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: build(real_sync, kwargs))
        monkeypatch.setattr(httpx, "get", module_call("GET"))
        monkeypatch.setattr(httpx, "post", module_call("POST"))
        monkeypatch.setattr(httpx, "stream", stream)

    def sent(self):
        return [(r.url.host, r.url.port, r.headers["host"], r.url.path) for r in self.requests]


# ---- the callers: every place that sends a request to go2rtc ----

def _reload(monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("go2rtc is a separate service here"))
    assert route_utils._go2rtc_reload() is True


def _vision_frame(monkeypatch):
    from modules.vision import detection_thread
    detection_thread.PrinterVisionThread(9, "cam", None, {}, 2, 33, "RUNNING")._capture_frame()


def _timelapse_frame(monkeypatch):
    """capture_owned_frame's ownership checks need a database; the request it sends is
    _frame_request, called here with exactly what capture_frame hands capture_owned_frame."""
    from modules.archives import timelapse_capture, timelapse_frame_capture

    def capture(*, http, base, printer_id, auth=None, options=None, **ownership):
        with timelapse_frame_capture._frame_request(http, base, printer_id, auth, options) as response:
            return response.status_code == 200
    monkeypatch.setattr(timelapse_frame_capture, "capture_owned_frame", capture)
    assert timelapse_capture.capture_frame(9, 1, nonce="n", job_id=1) is True


class _Db:
    def query(self, *args):
        return self

    def filter(self, *args):
        return self

    def first(self):
        return SimpleNamespace(id=9, org_id=1, shared=True, is_active=True, camera_enabled=True)

    def expire_all(self):
        pass


def _webrtc(monkeypatch):
    from core import controlled_parts
    from modules.printers import camera_routes
    from starlette.requests import Request
    monkeypatch.setattr(controlled_parts, "assert_access", lambda *a, **k: None)
    monkeypatch.setattr(controlled_parts, "policies", lambda *a, **k: [])
    monkeypatch.setattr(camera_routes, "get_camera_url", lambda printer: "rtsp://192.168.1.40/s")
    monkeypatch.setattr(camera_routes, "sync_go2rtc_config", lambda db: pytest.fail("the stream is listed"))
    monkeypatch.setattr(camera_routes, "audit_view", lambda *a, **k: None)

    async def receive():
        return {"type": "http.request", "body": b"SDP", "more_body": False}
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    response = asyncio.run(camera_routes.camera_webrtc(9, request, _Db(), {"id": 1, "role": "admin", "group_id": None}))
    assert response.status_code == 200


def _snapshot(monkeypatch):
    from modules.printers import routes_camera_snapshot
    monkeypatch.setattr(routes_camera_snapshot, "get_camera_stream", lambda *a, **k: None)
    monkeypatch.setattr(routes_camera_snapshot, "audit_view", lambda *a, **k: None)
    response = asyncio.run(routes_camera_snapshot.camera_snapshot(9, _Db(), {"id": 1}))
    assert response.body == JPEG


def _mjpeg(monkeypatch):
    from modules.printers import mjpeg_relay

    async def run():
        entry = {"evicted": asyncio.Event(), "privileged": True}
        response = await mjpeg_relay.open_mjpeg(
            9, check=lambda: {"privileged": True}, sync=lambda: pytest.fail("the stream is listed"),
            claim=lambda viewer, printer_id: entry, release=lambda entry: None)
        chunks = [chunk async for chunk in response.body_iterator]
        assert chunks == [b"--frame\r\n"]
    asyncio.run(run())


def _purge(monkeypatch):
    """Start-up clean-up of a separate go2rtc (ITAR mode only in production; the function
    itself sends the same request in both modes, always without a proxy)."""
    from modules.printers import go2rtc_purge
    assert go2rtc_purge.remove_streams_not_in({"printer_9": "rtsp://192.168.1.40/s", "printer_9_mjpeg": "x"}) == []


# file -> (how to make it send, paths it requests, trust_env it asked for with ITAR mode off)
CALLERS = {
    "modules/printers/go2rtc_purge.py": (_purge, ["/api/streams"], [False]),
    "modules/printers/route_utils.py": (_reload, ["/api/restart"], [True]),
    "modules/vision/detection_thread.py": (_vision_frame, ["/api/frame.jpeg"], [True]),
    "modules/archives/timelapse_capture.py": (_timelapse_frame, ["/api/frame.jpeg"], [True]),
    "modules/printers/camera_routes.py": (_webrtc, ["/api/streams", "/api/webrtc"], [True, True]),
    "modules/printers/routes_camera_snapshot.py": (_snapshot, ["/api/frame.jpeg"], [False]),
    "modules/printers/mjpeg_relay.py": (_mjpeg, ["/api/streams", "/api/stream.mjpeg"], [False, False]),
}
ADDRESS_READERS = {"go2rtc_target", "go2rtc_api_url", "go2rtc_client_kwargs"}


def _go2rtc_address_imports():
    found = {}
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith(("core/schema/preserved", "tests/")) or relative == "modules/printers/go2rtc_access.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=relative)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("go2rtc_access"):
                names = {alias.name for alias in node.names} & ADDRESS_READERS
                if names:
                    found.setdefault(relative, set()).update(names)
            elif isinstance(node, ast.Attribute) and node.attr in ADDRESS_READERS:
                found.setdefault(relative, set()).add(node.attr)
    return found


def test_the_caller_list_is_every_file_that_reads_the_go2rtc_address():
    """A new go2rtc caller fails here until it is added to CALLERS, and so to the tests below.
    Callers take the address and the client settings together from go2rtc_target(): the two
    older readers each make their own lookup, so a caller must not combine them."""
    found = _go2rtc_address_imports()
    assert set(found) == set(CALLERS)
    assert all(names == {"go2rtc_target"} for names in found.values()), found


# ---- 1. the request goes to the address that was checked ----

@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_itar_sends_each_go2rtc_request_to_the_checked_address_without_a_second_lookup(monkeypatch, secret, caller):
    drive, paths, _ = CALLERS[caller]
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://go2rtc:1984")
    _proxies(monkeypatch)
    resolver = _rebinding(monkeypatch)
    wire = Wire(monkeypatch)
    drive(monkeypatch)
    # The address from the one lookup, the service name only in the Host header.
    assert wire.sent() == [(PRIVATE, 1984, "go2rtc:1984", path) for path in paths]
    assert resolver.calls == ["go2rtc"]
    assert all(r.headers["authorization"].startswith("Basic ") for r in wire.requests)
    # 2. no proxy from the environment
    assert wire.trust_env == [False] * len(paths)


@pytest.mark.parametrize("caller", sorted(CALLERS))
def test_without_itar_each_go2rtc_caller_is_unchanged(monkeypatch, secret, caller):
    drive, paths, trust_env = CALLERS[caller]
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://go2rtc:1984")
    _proxies(monkeypatch)
    _no_lookup(monkeypatch)
    wire = Wire(monkeypatch)
    drive(monkeypatch)
    assert wire.sent() == [("go2rtc", 1984, "go2rtc:1984", path) for path in paths]
    assert wire.trust_env == trust_env


# Beside the API go2rtc is restarted by supervisord, not over HTTP: route_utils sends nothing,
# and there is no separate go2rtc for go2rtc_purge to clean.
@pytest.mark.parametrize("caller", sorted(set(CALLERS) - {"modules/printers/route_utils.py",
                                                          "modules/printers/go2rtc_purge.py"}))
def test_itar_turns_proxies_off_for_the_default_loopback_go2rtc(monkeypatch, secret, caller):
    drive, paths, _ = CALLERS[caller]
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    _proxies(monkeypatch)
    _no_lookup(monkeypatch)
    wire = Wire(monkeypatch)
    drive(monkeypatch)
    assert wire.sent() == [("127.0.0.1", 1984, "127.0.0.1:1984", path) for path in paths]
    assert wire.trust_env == [False] * len(paths)
    assert not any("authorization" in r.headers for r in wire.requests)


def test_the_timelapse_capture_sends_the_request_it_is_given_the_settings_for():
    source = (BACKEND / "modules" / "archives" / "timelapse_frame_capture.py").read_text(encoding="utf-8")
    assert "with _frame_request(http,base,printer_id,auth,options) as response:" in source
    assert source.count(".stream(") == 1


def test_itar_target_for_a_name_is_the_checked_address_and_the_host_header(monkeypatch, secret):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://go2rtc:1984/")
    resolver = _rebinding(monkeypatch)
    target = go2rtc_access.go2rtc_target()
    assert target.url == f"http://{PRIVATE}:1984" and target.remote is True
    assert target.client_kwargs == {"auth": go2rtc_access.go2rtc_api_credentials(), "trust_env": False,
                                    "headers": {"Host": "go2rtc:1984"}}
    assert resolver.calls == ["go2rtc"]
    # A later use looks the name up again, sees the public answer and is refused.
    for reader in ("go2rtc_target", "go2rtc_api_url", "go2rtc_client_kwargs", "go2rtc_is_remote"):
        with pytest.raises(go2rtc_access.Go2rtcUrlRefused):
            getattr(go2rtc_access, reader)()


@pytest.mark.parametrize("url, expected", [
    ("http://10.0.0.5:1984", "http://10.0.0.5:1984"),
    ("http://[fd00::5]:1984", "http://[fd00::5]:1984"),
    ("http://127.0.0.1:1984", "http://127.0.0.1:1984"),
])
def test_itar_leaves_an_ip_address_as_it_is(monkeypatch, secret, url, expected):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", url)
    _no_lookup(monkeypatch)
    target = go2rtc_access.go2rtc_target()
    assert target.url == expected and "headers" not in target.client_kwargs
    assert target.client_kwargs["trust_env"] is False


def test_itar_sends_localhost_to_its_loopback_address(monkeypatch, secret):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://localhost:1984")
    resolver = Resolver(monkeypatch, ("::1", "127.0.0.1"))
    target = go2rtc_access.go2rtc_target()
    # go2rtc beside the API listens on 127.0.0.1, so the IPv4 address is the one used.
    assert target.url == "http://127.0.0.1:1984" and target.remote is False
    assert target.client_kwargs == {"trust_env": False, "headers": {"Host": "localhost:1984"}}
    assert resolver.calls == ["localhost"]


@pytest.mark.parametrize("answer", [(PRIVATE, PUBLIC), (PUBLIC,), socket.gaierror("no such host")])
def test_itar_refuses_a_mixed_public_or_unresolvable_go2rtc_name(monkeypatch, secret, answer):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://go2rtc:1984")
    Resolver(monkeypatch, answer)
    Wire(monkeypatch)
    with pytest.raises(go2rtc_access.Go2rtcUrlRefused):
        go2rtc_access.go2rtc_target()


def test_itar_refuses_https_to_a_go2rtc_name(monkeypatch, secret, caplog):
    """The certificate is for the name and the connection goes to the address; ODIN's go2rtc
    serves plain HTTP on the internal network, so this is refused instead of half-working."""
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "https://go2rtc:1984")
    Resolver(monkeypatch, (PRIVATE,))
    with caplog.at_level("ERROR", logger="odin.go2rtc"):
        with pytest.raises(go2rtc_access.Go2rtcUrlRefused):
            go2rtc_access.go2rtc_target()
    assert "https" in caplog.text
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "https://10.0.0.5:1984")
    assert go2rtc_access.go2rtc_target().url == "https://10.0.0.5:1984"


def test_without_itar_the_target_is_the_configured_url_and_the_old_settings(monkeypatch, secret):
    _no_lookup(monkeypatch)
    _proxies(monkeypatch)
    assert go2rtc_access.go2rtc_target() == ("http://127.0.0.1:1984", {}, False)
    for url in ("http://go2rtc:1984", f"https://{PUBLIC}:1984/"):
        monkeypatch.setenv("ODIN_GO2RTC_API_URL", url)
        assert go2rtc_access.go2rtc_target() == (
            url.rstrip("/"), {"auth": go2rtc_access.go2rtc_api_credentials()}, True)
        assert go2rtc_access.go2rtc_api_url() == url.rstrip("/")
        assert go2rtc_access.go2rtc_client_kwargs() == {"auth": go2rtc_access.go2rtc_api_credentials()}


# ---- the shared helper ----

@pytest.mark.parametrize("url, answer, expected", [
    ("http://api:8000", (PRIVATE,), (f"http://{PRIVATE}:8000", "api:8000")),
    ("http://api", (PRIVATE,), (f"http://{PRIVATE}", "api")),
    ("http://user:pw@api:8000/base?x=1", (PRIVATE,), (f"http://user:pw@{PRIVATE}:8000/base?x=1", "api:8000")),
    ("http://api:8000", ("fd00::7",), ("http://[fd00::7]:8000", "api:8000")),
    ("http://api:8000", ("fd00::7", "10.1.2.3"), ("http://10.1.2.3:8000", "api:8000")),
])
def test_pinned_literal_url_replaces_the_name_with_a_checked_address(monkeypatch, url, answer, expected):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    resolver = Resolver(monkeypatch, answer)
    assert itar.pinned_literal_url(url) == expected
    assert len(resolver.calls) == 1


def test_pinned_literal_url_refuses_what_pin_for_request_refuses_and_is_off_without_itar(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    for answer in ((PUBLIC,), (PRIVATE, PUBLIC), socket.gaierror("no such host")):
        Resolver(monkeypatch, answer)
        with pytest.raises(itar.ItarOutboundBlocked):
            itar.pinned_literal_url("http://api:8000")
    _no_lookup(monkeypatch)
    with pytest.raises(itar.ItarOutboundBlocked):
        itar.pinned_literal_url(f"http://{PUBLIC}:8000")
    assert itar.pinned_literal_url("http://10.0.0.5:8000") == ("http://10.0.0.5:8000", None)
    monkeypatch.delenv("ODIN_ITAR_MODE")
    assert itar.pinned_literal_url(f"http://{PUBLIC}:8000") == (f"http://{PUBLIC}:8000", None)
    assert itar.pinned_literal_url("http://api:8000") == ("http://api:8000", None)


# ---- 3. the camera relay address handed to go2rtc ----

HTTP_CAMERA = SimpleNamespace(id=7, api_type="generic", api_host=None, api_key=None,
                              camera_url="http://192.168.1.50/stream", model=None)
RTSP_CAMERA = SimpleNamespace(id=4, api_type="generic", api_host=None, api_key=None,
                              camera_url="rtsp://192.168.1.40:554/s", model=None)


def _relay(monkeypatch, url, itar_on=True):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1" if itar_on else "")
    monkeypatch.setenv("ODIN_CAMERA_RELAY_BASE_URL", url)


def _tokens():
    return [camera_relay.relay_token(printer.id) for printer in (HTTP_CAMERA, RTSP_CAMERA)]


@pytest.mark.parametrize("url, answer", [
    (f"http://{PUBLIC}:8000", None),
    ("http://relay.example:8000", (PUBLIC,)),
    ("http://relay.example:8000", (PRIVATE, PUBLIC)),
    ("http://relay.example:8000", socket.gaierror("no such host")),
    ("http://[2001:4860:4860::8888]:8000", None),
])
def test_itar_refuses_a_relay_address_outside_the_local_network_and_emits_no_token(monkeypatch, secret, caplog,
                                                                                url, answer):
    from modules.printers.route_utils import CHAMBER_CAMERA_SOURCE
    tokens = _tokens()
    _relay(monkeypatch, url)
    if answer is None:
        _no_lookup(monkeypatch)
    else:
        Resolver(monkeypatch, answer)
    monkeypatch.setattr(camera_relay, "relay_token", lambda printer_id: pytest.fail("a token was made"))
    with pytest.raises(camera_relay.RelayUrlRefused) as refused:
        camera_relay.relay_url(7)
    assert isinstance(refused.value, itar.ItarOutboundBlocked)
    assert str(refused.value) == camera_relay.ITAR_REFUSAL == (
        "Cameras that go through the ODIN relay are turned off: ITAR mode is on and "
        "ODIN_CAMERA_RELAY_BASE_URL points outside the local network.")
    with caplog.at_level("ERROR"):
        assert route_utils.go2rtc_stream_source(HTTP_CAMERA.camera_url, 7) is None
        assert route_utils.go2rtc_stream_source(CHAMBER_CAMERA_SOURCE, 7) is None
        config = route_utils.go2rtc_config_for([HTTP_CAMERA, RTSP_CAMERA], "192.168.1.10")
    assert camera_relay.ITAR_REFUSAL in caplog.text
    # The camera that does not use the relay still streams; nothing names the relay or a token.
    assert sorted(config["streams"]) == ["printer_4", "printer_4_mjpeg"]
    written = yaml.safe_dump(config) + caplog.text
    assert "internal/camera-source" not in written
    assert not any(token in written for token in tokens)


def test_itar_hands_go2rtc_the_checked_relay_address(monkeypatch, secret):
    _relay(monkeypatch, "http://api:8000/")
    resolver = _rebinding(monkeypatch)
    token = camera_relay.relay_token(7)
    assert resolver.calls == []
    assert camera_relay.relay_url(7) == f"http://{PRIVATE}:8000/internal/camera-source/7/{token}"
    assert resolver.calls == ["api"]
    # The name now answers with a public address: go2rtc is given nothing.
    with pytest.raises(camera_relay.RelayUrlRefused):
        camera_relay.relay_url(7)


def test_itar_builds_each_relay_stream_from_one_lookup(monkeypatch, secret):
    """The MJPEG variant reuses the checked source instead of looking the name up again."""
    _relay(monkeypatch, "http://api:8000")
    resolver = _rebinding(monkeypatch)
    config = route_utils.go2rtc_config_for([HTTP_CAMERA], "192.168.1.10")
    source = f"http://{PRIVATE}:8000/internal/camera-source/7/{camera_relay.relay_token(7)}"
    assert config["streams"] == {"printer_7": source, "printer_7_mjpeg": source}
    assert resolver.calls == ["api"]


@pytest.mark.parametrize("url", ["http://10.0.0.9:8000", "http://127.0.0.1:8000", "http://[fd00::9]:8000"])
def test_itar_keeps_a_local_relay_address(monkeypatch, secret, url):
    _relay(monkeypatch, url)
    _no_lookup(monkeypatch)
    assert camera_relay.relay_url(7) == f"{url}/internal/camera-source/7/{camera_relay.relay_token(7)}"


def test_itar_keeps_the_default_loopback_relay(monkeypatch, secret):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("PORT", "8000")
    _no_lookup(monkeypatch)
    assert camera_relay.relay_base_url() == "http://127.0.0.1:8000"
    assert itar.audit_boot_config(itar.env_configured_urls()) == []


@pytest.mark.parametrize("url", [f"http://{PUBLIC}:8000", "http://api:8000/", "http://relay.example:8000"])
def test_without_itar_the_relay_address_is_used_as_configured(monkeypatch, secret, url):
    _relay(monkeypatch, url, itar_on=False)
    _no_lookup(monkeypatch)
    assert camera_relay.relay_base_url() == url.rstrip("/")
    assert camera_relay.relay_url(7) == f"{url.rstrip('/')}/internal/camera-source/7/{camera_relay.relay_token(7)}"
    config = route_utils.go2rtc_config_for([HTTP_CAMERA], "192.168.1.10")
    assert config["streams"] == {"printer_7": camera_relay.relay_url(7), "printer_7_mjpeg": camera_relay.relay_url(7)}


def test_the_boot_audit_reports_a_public_relay_address(monkeypatch, secret):
    _no_lookup(monkeypatch)
    assert itar.env_configured_urls() == [""]
    _relay(monkeypatch, f"http://{PUBLIC}:8000")
    assert itar.env_configured_urls() == ["", f"http://{PUBLIC}:8000"]
    with pytest.raises(RuntimeError, match=f"http://{PUBLIC}:8000"):
        itar.enforce_boot_config(itar.env_configured_urls())
    _relay(monkeypatch, "http://10.0.0.9:8000")
    itar.enforce_boot_config(itar.env_configured_urls())


def test_only_the_relay_module_reads_the_relay_address_setting():
    readers = set()
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith(("core/schema/preserved", "tests/")):
            continue
        if '"ODIN_CAMERA_RELAY_BASE_URL"' in path.read_text(encoding="utf-8"):
            readers.add(relative)
    assert readers == {"modules/printers/camera_relay.py", "core/itar.py"}


# ---- 3b. every other source go2rtc is given ----

def test_every_go2rtc_stream_source_is_the_relay_a_checked_address_or_go2rtc_itself():
    """go2rtc_config_for is the only builder of the streams, and it takes each source from
    go2rtc_stream_source (the relay, or an RTSP address from device_connect_ip) or from
    go2rtc's own loopback RTSP server."""
    source = (BACKEND / "modules" / "printers" / "route_utils.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builder = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "go2rtc_config_for")
    assigned = [ast.unparse(n.value) for n in ast.walk(builder) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Subscript) and ast.unparse(t.value) == "streams" for t in n.targets)]
    assert sorted(assigned) == ["f\"ffmpeg:{go2rtc_rtsp_url(f'printer_{p.id}')}#video=mjpeg\"", "source", "source"]
    assert "source = go2rtc_stream_source(get_camera_url(p), p.id)" in ast.unparse(builder)
    assert route_utils.GO2RTC_RTSP_LISTEN == "127.0.0.1:8554"
    # No other file writes a streams section or adds a stream through go2rtc's API.
    writers = set()
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith(("core/schema/preserved", "tests/")):
            continue
        text = path.read_text(encoding="utf-8")
        if "go2rtc" in text and ('"streams"' in text or "'streams'" in text or "streams[" in text
                                 or "api/streams?" in text):
            writers.add(relative)
    # scripts/prepare_go2rtc_config.py reads the streams it has just built (to compare with a
    # running go2rtc) and, when the build fails under ITAR mode, writes an EMPTY streams section.
    assert writers == {"modules/printers/route_utils.py", "scripts/prepare_go2rtc_config.py"}
    prepare = (BACKEND / "scripts" / "prepare_go2rtc_config.py").read_text(encoding="utf-8")
    assert '"streams: {}\\n"' in prepare and "streams[" not in prepare
    # go2rtc_purge only lists and DELETEs streams; it never adds one.
    purge = (BACKEND / "modules" / "printers" / "go2rtc_purge.py").read_text(encoding="utf-8")
    assert '"PUT"' not in purge and '"POST"' not in purge and '"PATCH"' not in purge
    assert purge.count('"DELETE"') == 1 and purge.count('"GET"') == 2


@pytest.mark.parametrize("camera", ["rtsp://cam.example:554/s", "rtsps://user:pw@8.8.4.4:322/live",
                                    "http://cam.example/stream", "https://8.8.4.4/stream"])
def test_itar_gives_go2rtc_no_camera_source_outside_the_local_network(monkeypatch, secret, camera):
    from core import webhook_utils
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, *a, **k: _answer(PUBLIC))
    assert route_utils.go2rtc_stream_source(camera, 7) is None
    printer = SimpleNamespace(id=7, api_type="generic", api_host=None, api_key=None, camera_url=camera, model=None)
    assert route_utils.go2rtc_config_for([printer], "192.168.1.10")["streams"] == {}
    monkeypatch.delenv("ODIN_ITAR_MODE")
    assert route_utils.go2rtc_stream_source(camera, 7) is not None
