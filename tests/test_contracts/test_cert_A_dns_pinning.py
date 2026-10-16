"""Group A: DNS pins fail closed and match case/IDNA/trailing-dot variants.

Simulated rebinding: the policy lookup sees an allowed address, every later
lookup of the same name answers 127.0.0.1 where a real listener waits. The
listener must never receive a connection, for urllib, requests, httpx and
the webhook safe_post path, with upper-case and internationalised names.
"""
import socket
import threading

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    cert_app, cert_client, fernet_env, review_db,
)


@pytest.fixture
def loopback():
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


def _norm(host):
    host = host.decode() if isinstance(host, bytes) else str(host)
    return host.strip("[]").rstrip(".").lower().encode("idna").decode()


@pytest.fixture
def rebinding(monkeypatch):
    """name -> first answer for the policy lookup, 127.0.0.1 for every later lookup."""
    import core.webhook_utils as webhook_utils
    real = webhook_utils._real_getaddrinfo
    seen = {}
    first = {}

    def fake(host, port, *args, **kwargs):
        key = _norm(host)
        if key not in first:
            return real(host, port, *args, **kwargs)
        count = seen.get(key, 0)
        seen[key] = count + 1
        ip = first[key] if count == 0 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0))]
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", fake)
    return first, seen


NAMES = ["REBIND-Device.Test", "rebind-device.test.", "rébind-device.test"]


def test_normalize_hostname_forms():
    from core.webhook_utils import normalize_hostname
    assert normalize_hostname("Printer.LAN.") == "printer.lan"
    assert normalize_hostname("rébind.test") == "xn--rbind-bsa.test"
    assert normalize_hostname("[FD12::5]") == "fd12::5"
    assert normalize_hostname(b"XN--RBIND-BSA.TEST") == "xn--rbind-bsa.test"


def test_pin_fails_closed_for_unrelated_names():
    import core.webhook_utils as webhook_utils
    with webhook_utils._pin_dns("printer.test", 80, ["192.0.2.60"]):
        assert socket.getaddrinfo("PRINTER.test.", 80)[0][4][0] == "192.0.2.60"
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("localhost", 80)
        with pytest.raises(socket.gaierror):
            socket.getaddrinfo("printer.test", 81)


@pytest.mark.parametrize("name", NAMES)
def test_urllib_plug_request_case_and_idn_variants_stay_pinned(rebinding, loopback, name):
    from modules.printers import plug_http_protocol
    first, _ = rebinding
    port, hits = loopback
    first[_norm(name)] = "192.0.2.60"
    status, _ = plug_http_protocol.http_request(f"http://{name}:{port}/cm?cmnd=Power", timeout=0.5)
    assert status == 0 and hits == []


@pytest.mark.parametrize("name", NAMES)
def test_requests_prusalink_case_and_idn_variants_stay_pinned(rebinding, loopback, name):
    from modules.printers.adapters.prusalink import PrusaLinkPrinter
    first, _ = rebinding
    port, hits = loopback
    first[_norm(name)] = "192.0.2.60"
    printer = PrusaLinkPrinter(name, port=port, api_key="synthetic")
    printer.timeout = 0.5
    assert printer._get("/api/v1/status") is None
    assert hits == []


@pytest.mark.parametrize("name", NAMES)
def test_httpx_test_connection_case_and_idn_variants_stay_pinned(rebinding, loopback, name):
    import httpx
    from modules.printers.route_utils import pinned_device_get
    first, _ = rebinding
    port, hits = loopback
    first[_norm(name)] = "192.0.2.60"
    with pytest.raises(Exception):
        pinned_device_get(httpx, f"http://{name}:{port}/printer/info", timeout=0.5)
    assert hits == []


@pytest.mark.parametrize("name", ["REBIND-Hook.Test", "rébind-hook.test"])
def test_safe_post_case_and_idn_variants_stay_pinned(rebinding, loopback, name):
    from core.webhook_utils import safe_post
    first, _ = rebinding
    port, hits = loopback
    first[_norm(name)] = "100.64.0.1"  # non-private, unroutable: the pin is used, connect times out
    with pytest.raises(Exception):
        safe_post(f"http://{name}:{port}/hook", json={}, timeout=0.5)
    assert hits == []


def test_api_host_and_plug_host_are_stored_normalized(cert_app, review_db, monkeypatch):
    import core.webhook_utils as webhook_utils
    real = webhook_utils._real_getaddrinfo
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, port, *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.40", port or 0))]
                        if _norm(host).endswith(".test") else real(host, port, *a, **k))
    operator = cert_app.roles["operator"]
    assert cert_app.patch("/printers/301", headers=operator, json={"api_host": "Printer-Room.TEST.:7125"}).status_code == 200
    assert cert_app.put("/printers/301/plug", headers=operator, json={"type": "tasmota", "host": "Plug.TEST"}).status_code == 200
    review_db.expire_all()
    assert tuple(review_db.execute(text("SELECT api_host,plug_host FROM printers WHERE id=301")).one()) == (
        "printer-room.test:7125", "plug.test")
    assert cert_app.put("/printers/301/plug", headers=operator,
                        json={"type": "homeassistant", "host": "HTTP://Hä.TEST:8123"}).status_code == 200
    review_db.expire_all()
    assert review_db.execute(text("SELECT plug_host FROM printers WHERE id=301")).scalar_one() == "http://xn--h-0fa.test:8123"
