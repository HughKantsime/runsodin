"""Group A: non-HTTP device channels connect only to policy-validated IP literals.

Every name below "rebinds" to 127.0.0.1, where a real TCP listener (or UDP
socket) waits. Bambu MQTT (legacy, V2 telemetry, V2 commands, dispatch
readiness), Bambu FTPS, the Elegoo SDCP websocket and the SDCP UDP probe
must refuse before any packet reaches it.
"""
import socket
import threading
import time

import pytest

REBOUND = "rebound-printer.test"


@pytest.fixture
def rebound(monkeypatch):
    import core.webhook_utils as webhook_utils
    real = webhook_utils._real_getaddrinfo
    calls = []

    def fake(host, port, *args, **kwargs):
        if webhook_utils.normalize_hostname(host) == REBOUND:
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]
        return real(host, port, *args, **kwargs)
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", fake)
    # Any direct lookup outside the policy (a client resolving the name itself) also rebinds.
    real_socket = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: fake(host, port, *a, **k)
                        if str(host).lower() == REBOUND else real_socket(host, port, *a, **k))
    return calls


@pytest.fixture
def tcp_listener():
    hits = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    sock.settimeout(0.1)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = sock.accept()
            except (socket.timeout, OSError):
                continue
            hits.append(True)
            conn.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield sock.getsockname()[1], hits
    stop.set()
    thread.join(1)
    sock.close()


def test_legacy_bambu_mqtt(rebound, tcp_listener, monkeypatch):
    from modules.printers.adapters.bambu import BambuPrinter
    port, hits = tcp_listener
    monkeypatch.setattr(BambuPrinter, "MQTT_PORT", port)
    monkeypatch.setattr(BambuPrinter, "MQTT_TIMEOUT", 1)
    printer = BambuPrinter(ip=REBOUND, serial="SYNTH", access_code="synthetic")
    assert printer.connect() is False
    printer.disconnect()
    assert rebound and hits == []


def test_v2_telemetry_mqtt(rebound, tcp_listener):
    from core.webhook_utils import DeviceDestinationBlocked
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig, BambuTelemetryAdapter
    port, hits = tcp_listener
    adapter = BambuTelemetryAdapter(BambuAdapterConfig("p", "SYNTH", REBOUND, "synthetic", port=port), emitter=lambda e: None)
    with pytest.raises(DeviceDestinationBlocked):
        adapter.start()
    assert rebound and hits == []


def test_v2_command_mqtt(rebound, tcp_listener):
    from core.webhook_utils import DeviceDestinationBlocked
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.commands import BambuCommandAdapter
    port, hits = tcp_listener
    adapter = BambuCommandAdapter(BambuAdapterConfig("p", "SYNTH", REBOUND, "synthetic", port=port))
    with pytest.raises(DeviceDestinationBlocked):
        adapter.start()
    assert rebound and hits == []


def test_dispatch_bambu_readiness_mqtt(rebound, tcp_listener, monkeypatch):
    from modules.printers import dispatch_bambu_readiness
    from modules.printers.telemetry.bambu import broker_policy
    port, hits = tcp_listener
    monkeypatch.setattr(broker_policy, "resolve_bambu_broker_config",
                        lambda host, **kw: broker_policy.BrokerEndpoint(host, port, True))
    creds = {"ip": REBOUND, "serial": "SYNTH", "access_code": "synthetic"}
    assert dispatch_bambu_readiness.fresh_bambu_status_is_idle(creds, time.monotonic() + 2) is False
    assert rebound and hits == []


def test_bambu_ftps_upload(rebound, tcp_listener, monkeypatch, tmp_path):
    from modules.printers.telemetry.bambu import ftp_upload
    port, hits = tcp_listener
    monkeypatch.setattr(ftp_upload, "FTPS_PORT", port)
    part = tmp_path / "part.3mf"
    part.write_bytes(b"synthetic")
    assert ftp_upload.upload_file(REBOUND, "synthetic", str(part), timeout=1) is False
    assert rebound and hits == []


def test_elegoo_sdcp_websocket(rebound, tcp_listener):
    from modules.printers.adapters.elegoo import ElegooPrinter
    port, hits = tcp_listener
    assert ElegooPrinter(REBOUND, port=port).connect() is False
    assert rebound and hits == []


def test_elegoo_sdcp_udp_probe_uses_validated_ip(cert_app, monkeypatch, rebound):
    """The test-connection UDP probe sends to the validated literal, never the name."""
    sent = []

    class FakeSocket:
        def __init__(self, *a, **k):
            pass
        def settimeout(self, t):
            pass
        def sendto(self, data, address):
            sent.append(address)
        def recvfrom(self, n):
            raise socket.timeout()
        def close(self):
            pass
    import core.webhook_utils as webhook_utils
    real = webhook_utils._real_getaddrinfo
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, port, *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.7.30", port or 0))]
                        if webhook_utils.normalize_hostname(host) == "sdcp-printer.test" else real(host, port, *a, **k))
    real_socket = socket.socket

    def make_socket(family=socket.AF_INET, type=socket.SOCK_STREAM, *args, **kwargs):
        # Only the SDCP UDP probe is faked; every other socket is real.
        return FakeSocket() if type == socket.SOCK_DGRAM else real_socket(family, type, *args, **kwargs)
    monkeypatch.setattr(socket, "socket", make_socket)  # routes_crud imports socket at call time
    import modules.printers.route_utils as route_utils
    monkeypatch.setattr(route_utils, "pinned_device_get", lambda *a, **k: None)
    response = cert_app.post("/printers/test-connection", headers=cert_app.roles["operator"],
                             json={"api_type": "elegoo", "api_host": "SDCP-Printer.test"})
    assert response.status_code == 200, response.text
    assert sent == [("192.168.7.30", 3000)]


from tests.test_contracts.test_cert_A_harness import cert_app, cert_client, fernet_env, review_db  # noqa: E402,F401


def test_replay_broker_flag_only_allows_loopback_literals(rebound, tcp_listener):
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig, BambuTelemetryAdapter
    port, hits = tcp_listener
    adapter = BambuTelemetryAdapter(BambuAdapterConfig("p", "SYNTH", REBOUND, "", port=port, use_tls=False,
                                                       replay_broker=True), emitter=lambda e: None)
    with pytest.raises(ValueError):
        adapter.start()
    assert hits == []
