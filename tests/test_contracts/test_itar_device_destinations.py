"""Under ITAR mode no printer, camera or smart plug client connects outside the local network.

Found by review 2026-11-13: the device clients (Bambu MQTT and FTPS, Anycubic LAN, Elegoo,
the connection test, the camera relay) take their address from the device destination
functions in core/webhook_utils.py. Those enforced the general device policy only, which
allows public addresses, so a saved printer on a public address was contacted, with its
credentials, under ITAR mode. device_destination_ips now also requires, under ITAR mode,
that every resolved address is on the local network.

Nothing here opens a connection: the resolver and every transport are fakes.
"""
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core import itar, webhook_utils
from core.webhook_utils import ITAR_DEVICE_REFUSAL, DeviceDestinationBlocked

BACKEND = Path(__file__).resolve().parents[2] / "backend"
PRIVATE = "192.168.50.20"
PUBLIC = "8.8.8.8"
NAMES = {
    "printer-local.test": [PRIVATE],
    "printer-public.test": [PUBLIC],
    "printer-mixed.test": [PRIVATE, PUBLIC],
    "printer-v6-public.test": ["2001:4860:4860::8888"],
}

# (ITAR mode on, host, the address the client may connect to or None when refused)
CASES = [
    pytest.param(True, PRIVATE, PRIVATE, id="itar-private-literal-allowed"),
    pytest.param(True, "printer-local.test", PRIVATE, id="itar-name-to-private-allowed"),
    pytest.param(True, PUBLIC, None, id="itar-public-literal-refused"),
    pytest.param(True, "printer-public.test", None, id="itar-name-to-public-refused"),
    pytest.param(True, "printer-mixed.test", None, id="itar-name-to-private-and-public-refused"),
    pytest.param(False, PUBLIC, PUBLIC, id="no-itar-public-literal-allowed"),
    pytest.param(False, "printer-public.test", PUBLIC, id="no-itar-name-to-public-allowed"),
]


class FakeTransportUsed(OSError):
    """Raised by every fake transport after it records the call, so the client stops there."""


@pytest.fixture
def lan(monkeypatch):
    """A fake resolver, and a guard that fails the test on any real connection."""
    def resolve(host, port, *args, **kwargs):
        name = webhook_utils.normalize_hostname(host)
        addresses = NAMES.get(name)
        if addresses is None:
            try:
                addresses = [str(__import__("ipaddress").ip_address(name))]
            except ValueError:
                raise socket.gaierror(socket.EAI_NONAME, "unknown test name")
        return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (a, port or 0)) for a in addresses]

    def no_real_connection(*args, **kwargs):
        raise AssertionError("a real connection was attempted")
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", resolve)
    # ODIN's own addresses are read from /proc and the database; not under test here.
    monkeypatch.setattr(webhook_utils, "is_own_service_endpoint", lambda ip, port: False)
    monkeypatch.setattr(socket.socket, "connect", no_real_connection)
    monkeypatch.setattr(socket, "create_connection", no_real_connection)

    def mode(on):
        if on:
            monkeypatch.setenv("ODIN_ITAR_MODE", "1")
        else:
            monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    return mode


# ---- the guard itself: core.webhook_utils ----

@pytest.mark.parametrize("on, host, address", CASES)
def test_device_destination_ips(lan, on, host, address):
    lan(on)
    if address is None:
        with pytest.raises(DeviceDestinationBlocked) as refused:
            webhook_utils.device_destination_ips(host, 8883)
        assert str(refused.value) == ITAR_DEVICE_REFUSAL
    else:
        assert webhook_utils.device_destination_ips(host, 8883) == [address]


def test_the_refusal_is_plain_and_names_no_address():
    assert ITAR_DEVICE_REFUSAL == "ITAR mode blocks printer and device addresses outside the local network."


@pytest.mark.parametrize("host", ["2001:4860:4860::8888", "[2001:4860:4860::8888]:8883", "printer-v6-public.test",
                                  "::ffff:8.8.8.8", "64:ff9b::808:808", "100.64.0.1", "198.18.0.1"])
def test_itar_refuses_public_ipv6_embedded_ipv4_and_special_ranges(lan, host):
    lan(True)
    with pytest.raises(DeviceDestinationBlocked):
        webhook_utils.device_destination_ips(host)


@pytest.mark.parametrize("host", ["10.1.2.3", "172.16.9.9", "192.168.1.1", "fd12:3456::7"])
def test_itar_allows_every_local_network_range(lan, host):
    lan(True)
    assert webhook_utils.device_destination_ips(host) == [host]


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.parametrize("host", ["127.0.0.1", "169.254.169.254", "0.0.0.0"])
def test_the_general_device_policy_still_applies_first(lan, on, host):
    lan(on)
    with pytest.raises(DeviceDestinationBlocked) as refused:
        webhook_utils.device_destination_ips(host)
    assert str(refused.value) != ITAR_DEVICE_REFUSAL


def test_with_itar_off_the_local_network_rule_is_never_evaluated(lan, monkeypatch):
    lan(False)

    def never(host):
        raise AssertionError("the ITAR rule ran with ITAR mode off")
    monkeypatch.setattr(itar, "is_private_destination", never)
    assert webhook_utils.device_destination_ips(PUBLIC, 80) == [PUBLIC]
    assert webhook_utils.device_connect_ip("printer-public.test", 8883) == PUBLIC
    with webhook_utils.pinned_device_destination(f"http://{PUBLIC}/x") as ips:
        assert ips == [PUBLIC]


@pytest.mark.parametrize("on, host, address", CASES)
def test_every_function_that_returns_or_pins_a_device_address(lan, monkeypatch, on, host, address):
    """device_connect_ip, pinned_device_destination, device_request and device_urlopen all
    resolve through device_destination_ips; none has a second path."""
    import requests
    lan(on)
    sent = []

    def request(self, method, url, **kwargs):
        sent.append(("requests", url))
        raise FakeTransportUsed()

    class Opener:
        def open(self, req, timeout=None):
            sent.append(("urllib", req))
            raise FakeTransportUsed()
    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(webhook_utils._urllib_request, "build_opener", lambda *handlers: Opener())
    url = f"http://{host}:7125/printer/info"
    if address is None:
        with pytest.raises(DeviceDestinationBlocked):
            webhook_utils.device_connect_ip(host, 8883)
        with pytest.raises(DeviceDestinationBlocked):
            with webhook_utils.pinned_device_destination(url):
                raise AssertionError("the block was entered")
        with pytest.raises(DeviceDestinationBlocked):
            webhook_utils.device_request("GET", url)
        with pytest.raises(DeviceDestinationBlocked):
            webhook_utils.device_urlopen(url)
        assert sent == []
    else:
        assert webhook_utils.device_connect_ip(host, 8883) == address
        with webhook_utils.pinned_device_destination(url) as ips:
            assert ips == [address]
        with pytest.raises(FakeTransportUsed):
            webhook_utils.device_request("GET", url)
        with pytest.raises(FakeTransportUsed):
            webhook_utils.device_urlopen(url)
        assert sent == [("requests", url), ("urllib", url)]


def test_device_destination_ips_is_the_only_resolver_in_the_device_section():
    import ast
    source = (BACKEND / "core" / "webhook_utils.py").read_text(encoding="utf-8")
    functions = {node.name: {call.func.id for call in ast.walk(node)
                             if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
                 for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    # One lookup for device hosts, followed by the ITAR check ...
    assert {"_real_getaddrinfo", "_refuse_public_device_addresses_under_itar"} <= functions["device_destination_ips"]
    # ... and every other device function gets its address from it.
    assert "device_destination_ips" in functions["pinned_device_destination"]
    assert "device_destination_ips" in functions["device_connect_ip"]
    assert "pinned_device_destination" in functions["device_urlopen"]
    assert "pinned_device_destination" in functions["device_request"]
    for name in ("pinned_device_destination", "device_connect_ip", "device_urlopen", "device_request"):
        assert "_real_getaddrinfo" not in functions[name] and "getaddrinfo" not in functions[name]


# ---- the clients ----

@pytest.fixture
def mqtt_connects(monkeypatch):
    import paho.mqtt.client as mqtt
    calls = []

    def connect(self, host, port=1883, *args, **kwargs):
        calls.append((host, port))
        raise FakeTransportUsed()
    monkeypatch.setattr(mqtt.Client, "connect", connect)
    monkeypatch.setattr(mqtt.Client, "loop_start", lambda self: None)
    return calls


@pytest.mark.parametrize("on, host, address", CASES)
def test_bambu_mqtt_connect(lan, mqtt_connects, monkeypatch, on, host, address):
    from modules.printers.adapters.bambu import BambuPrinter
    lan(on)
    monkeypatch.setattr(BambuPrinter, "MQTT_TIMEOUT", 0)
    printer = BambuPrinter(ip=host, serial="SYNTH", access_code="synthetic")
    assert printer.connect() is False
    assert mqtt_connects == ([] if address is None else [(address, BambuPrinter.MQTT_PORT)])


@pytest.mark.parametrize("on, host, address", CASES)
def test_bambu_telemetry_and_command_mqtt_connect(lan, mqtt_connects, on, host, address):
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig, BambuTelemetryAdapter
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    lan(on)
    config = BambuAdapterConfig("p", "SYNTH", host, "synthetic")
    for adapter in (BambuTelemetryAdapter(config, emitter=lambda event: None), BambuCommandAdapter(config)):
        with pytest.raises(DeviceDestinationBlocked if address is None else FakeTransportUsed):
            adapter.start()
    assert mqtt_connects == ([] if address is None else [(address, config.port)] * 2)


@pytest.mark.parametrize("on, host, address", CASES)
def test_bambu_integration_mqtt_connect(lan, mqtt_connects, on, host, address):
    from modules.printers.bambu_integration import BambuMQTTClient, BambuPrinterConfig
    lan(on)
    client = BambuMQTTClient(BambuPrinterConfig(ip_address=host, serial_number="SYNTH", access_code="synthetic"))
    with pytest.raises(ConnectionError):
        client.connect()
    assert mqtt_connects == ([] if address is None else [(address, 8883)])


@pytest.mark.parametrize("on, host, address", CASES)
def test_bambu_ftps_upload(lan, monkeypatch, tmp_path, on, host, address):
    import ftplib
    from modules.printers.telemetry.bambu import ftp_upload
    lan(on)
    opened = []

    def create_connection(target, timeout=None, **kwargs):
        opened.append(target)
        raise FakeTransportUsed()

    def login(self, *args, **kwargs):
        raise AssertionError("the access code was used")
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(ftplib.FTP, "login", login)
    part = tmp_path / "part.3mf"
    part.write_bytes(b"synthetic")
    assert ftp_upload._upload_once(host, "synthetic", str(part), None, 1) == (False, "connect")
    assert opened == ([] if address is None else [(address, ftp_upload.FTPS_PORT)])


@pytest.mark.parametrize("on, host, address", CASES)
def test_anycubic_lan_handshake_and_connect(lan, mqtt_connects, monkeypatch, on, host, address):
    import requests
    from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
    lan(on)
    sent = []

    def request(self, method, url, **kwargs):
        sent.append((method, url))
        raise FakeTransportUsed()
    monkeypatch.setattr(requests.Session, "request", request)
    with pytest.raises(DeviceDestinationBlocked if address is None else FakeTransportUsed):
        AnycubicLANPrinter(host).handshake()
    assert AnycubicLANPrinter(host).connect() is False
    assert sent == ([] if address is None else [("GET", f"http://{host}:18910/info")] * 2)
    assert mqtt_connects == []


@pytest.mark.parametrize("on, host, address", CASES)
def test_elegoo_websocket_and_camera_probe(lan, monkeypatch, on, host, address):
    import requests
    import websocket
    from modules.printers.adapters.elegoo import ElegooPrinter
    lan(on)
    opened = []

    def app(url, **kwargs):
        opened.append(url)
        raise FakeTransportUsed()

    def request(self, method, url, **kwargs):
        opened.append(url)
        raise FakeTransportUsed()
    monkeypatch.setattr(websocket, "WebSocketApp", app)
    monkeypatch.setattr(requests.Session, "request", request)
    printer = ElegooPrinter(host)
    assert printer.connect() is False
    assert printer.get_webcam_url() is None
    if address is None:
        assert opened == []
    else:
        assert opened == [f"ws://{address}:3030/websocket", f"http://{host}:8080/?action=stream",
                          f"http://{host}:8080/webcam/?action=stream"]


@pytest.mark.parametrize("api_type", ["moonraker", "prusalink", "elegoo", "bambu", "anycubic_lan"])
@pytest.mark.parametrize("host", [PUBLIC, "printer-public.test", "printer-mixed.test"])
def test_the_connection_test_route_refuses_before_any_client_is_built(lan, monkeypatch, api_type, host):
    import httpx
    from modules.printers import routes_crud
    from modules.printers.route_utils import TestConnectionRequest
    lan(True)

    def used(*args, **kwargs):
        raise AssertionError("a client was used")
    monkeypatch.setattr(httpx, "get", used)
    request = TestConnectionRequest(api_type=api_type, api_host=host, serial="SYNTH",
                                    access_code="synthetic", api_key="synthetic")
    with pytest.raises(HTTPException) as refused:
        routes_crud.test_printer_connection(request, current_user={})
    assert (refused.value.status_code, refused.value.detail) == (400, ITAR_DEVICE_REFUSAL)


@pytest.mark.parametrize("on, host, address", CASES)
def test_the_connection_test_route_for_moonraker(lan, monkeypatch, on, host, address):
    import httpx
    from modules.printers import routes_crud
    from modules.printers.route_utils import TestConnectionRequest
    lan(on)
    fetched = []

    def get(url, **kwargs):
        fetched.append(url)
        return SimpleNamespace(status_code=503)
    monkeypatch.setattr(httpx, "get", get)
    request = TestConnectionRequest(api_type="moonraker", api_host=host)
    if address is None:
        with pytest.raises(HTTPException) as refused:
            routes_crud.test_printer_connection(request, current_user={})
        assert refused.value.detail == ITAR_DEVICE_REFUSAL
        assert fetched == []
    else:
        assert routes_crud.test_printer_connection(request, current_user={}) == {
            "success": False, "error": "Moonraker returned HTTP 503"}
        assert fetched == [f"http://{host}/printer/info"]


def test_the_setup_connection_test_uses_the_same_check():
    source = (BACKEND / "modules/system/routes_setup.py").read_text(encoding="utf-8")
    start = source.index("_validate_setup_phase(db)\n\n    from modules.printers.route_utils import _check_ssrf_blocklist")
    assert "_check_ssrf_blocklist(request.api_host)" in source[start:start + 200]


def test_a_refusal_that_is_not_about_itar_keeps_the_generic_message(lan):
    from modules.printers.route_utils import _check_ssrf_blocklist
    lan(True)
    with pytest.raises(HTTPException) as refused:
        _check_ssrf_blocklist("127.0.0.1")
    assert refused.value.detail == "Invalid printer host"


@pytest.mark.parametrize("on, host, address", CASES)
def test_the_camera_relay(lan, monkeypatch, on, host, address):
    import requests
    from modules.printers import camera_relay
    lan(on)
    fetched = []

    def request(self, method, url, **kwargs):
        fetched.append(url)
        raise requests.ConnectionError("fake transport")
    url = f"http://viewer:synthetic@{host}:8080/?action=stream"
    monkeypatch.setattr(requests.Session, "request", request)
    monkeypatch.setattr(camera_relay, "_is_local_peer", lambda request: True)
    monkeypatch.setattr(camera_relay, "relay_token", lambda printer_id: "token")
    monkeypatch.setattr(camera_relay, "_stored_http_camera_url", lambda printer_id: url)
    with pytest.raises(HTTPException) as failed:
        camera_relay.camera_source(1, "token", SimpleNamespace())
    assert failed.value.status_code == 502
    assert fetched == ([] if address is None else [url])


@pytest.mark.parametrize("on, host, address", CASES)
def test_go2rtc_is_never_given_a_camera_outside_the_local_network(lan, monkeypatch, on, host, address):
    from modules.printers import camera_relay, route_utils
    lan(on)
    monkeypatch.setattr(camera_relay, "relay_url", lambda printer_id: "relay")
    rtsp = route_utils.go2rtc_stream_source(f"rtsp://viewer:synthetic@{host}:554/stream", 1)
    http = route_utils.go2rtc_stream_source(f"http://{host}:8080/?action=stream", 1)
    if address is None:
        assert rtsp is None and http is None
    else:
        assert rtsp == f"rtsp://viewer:synthetic@{address}:554/stream" and http == "relay"


# ---- start-up report ----

class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement):
        assert "api_key" not in str(statement) and "camera_url" not in str(statement)
        return SimpleNamespace(fetchall=lambda: self.rows)

    def close(self):
        pass


def _saved(monkeypatch, rows):
    import core.db
    monkeypatch.setattr(core.db, "SessionLocal", lambda: _Rows(rows))

    def no_lookup(*args, **kwargs):
        raise AssertionError("a name was looked up at start-up")
    monkeypatch.setattr(socket, "getaddrinfo", no_lookup)


ROWS = [(1, "Shop A1", PRIVATE), (2, "Remote X1C", PUBLIC), (3, "By name", "printer-public.test"),
        (4, "With port", f"{PUBLIC}:7125"), (5, "V6", "[2001:4860:4860::8888]:80"), (6, "Empty", None),
        (7, "Short form", "8.8"), (8, "ULA", "fd12:3456::7")]


def test_saved_printers_with_a_public_address_are_reported_by_id_and_name(monkeypatch):
    _saved(monkeypatch, ROWS)
    assert itar.saved_printers_outside_local_network() == [
        "printer 2 (Remote X1C)", "printer 4 (With port)", "printer 5 (V6)", "printer 7 (Short form)"]


def test_start_up_is_refused_under_itar_and_the_message_has_no_address(monkeypatch):
    _saved(monkeypatch, ROWS)
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    with pytest.raises(RuntimeError) as refused:
        itar.enforce_boot_printer_addresses()
    message = str(refused.value)
    assert "printer 2 (Remote X1C)" in message and "outside the local network" in message
    assert PUBLIC not in message and "2001:" not in message


def test_start_up_passes_under_itar_when_every_saved_address_is_local(monkeypatch):
    _saved(monkeypatch, [ROWS[0], ROWS[2], ROWS[5], ROWS[7]])
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    itar.enforce_boot_printer_addresses()


def test_start_up_does_not_read_printers_with_itar_off(monkeypatch):
    import core.db
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)

    def never():
        raise AssertionError("the printers were read with ITAR mode off")
    monkeypatch.setattr(core.db, "SessionLocal", never)
    itar.enforce_boot_printer_addresses()


def test_a_missing_printers_table_does_not_stop_start_up(monkeypatch):
    import core.db

    class Broken(_Rows):
        def execute(self, statement):
            raise RuntimeError("no such table")
    monkeypatch.setattr(core.db, "SessionLocal", lambda: Broken([]))
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    itar.enforce_boot_printer_addresses()


def test_the_app_runs_the_printer_audit_at_start_up():
    source = (BACKEND / "core" / "app.py").read_text(encoding="utf-8")
    block = source[source.index("db_urls = collect_db_configured_urls()"):]
    assert "enforce_boot_printer_addresses()" in block[:700]
