"""Under ITAR mode the clients treated as local cannot be pointed outside the local network.

Found by review 2026-11-13: the troubleshooting printer check and the go2rtc clients were
classified local, but a saved printer on a public address, or a public ODIN_GO2RTC_API_URL,
was contacted (with credentials) with no ITAR check.
"""
import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core import itar
from modules.printers import bambu_chamber_camera, go2rtc_access
from modules.system import troubleshooting

BACKEND = Path(__file__).resolve().parents[2] / "backend"
PUBLIC = "8.8.8.8"


def _resolves_to(monkeypatch, address):
    monkeypatch.setattr(troubleshooting, "resolve_addresses",
                        lambda *a: [(None, None, None, None, (address, 0))])


def _no_request(monkeypatch):
    """Fail on any use of the printer credentials or of a network client."""
    import httpx
    from core import crypto

    def refuse(*args, **kwargs):
        raise AssertionError("the printer was contacted or its credentials were read")
    monkeypatch.setattr(crypto, "decrypt", refuse)
    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(troubleshooting, "ftps_checks", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# ---- 1. troubleshooting printer check ----

def test_safe_destination_alone_accepts_a_public_address(monkeypatch):
    # The reason the ITAR check is needed: this filter only refuses special addresses.
    _resolves_to(monkeypatch, PUBLIC)
    assert troubleshooting.safe_destination("printer.example:7125") == (PUBLIC, 7125)


@pytest.mark.parametrize("protocol", ["moonraker", "prusalink", "bambu"])
@pytest.mark.parametrize("address", [PUBLIC, "2001:4860:4860::8888"])
def test_itar_skips_the_printer_check_for_an_address_outside_the_local_network(monkeypatch, protocol, address):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    _resolves_to(monkeypatch, address)
    _no_request(monkeypatch)
    printer = SimpleNamespace(id=1, api_host="printer.example", api_type=protocol, api_key="stored")
    result = troubleshooting.printer_checks(printer)
    assert [(c["stage"], c["status"], c["message"]) for c in result["checks"]] == [
        ("destination", "skipped", troubleshooting.ITAR_SKIPPED)]
    assert troubleshooting.ITAR_SKIPPED == ("This check was skipped because ITAR mode blocks "
                                            "destinations outside the local network.")
    assert address not in str(result)


class _Client:
    sent = []

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, auth=None):
        _Client.sent.append((url, headers))
        return SimpleNamespace(status_code=200)


@pytest.mark.parametrize("itar_on, address", [(True, "192.168.40.20"), (False, "192.168.40.20"), (False, PUBLIC)])
def test_the_printer_check_still_runs_on_the_local_network_and_without_itar(monkeypatch, itar_on, address):
    import httpx
    from core import crypto
    if itar_on:
        monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    else:
        monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    _resolves_to(monkeypatch, address)
    monkeypatch.setattr(crypto, "decrypt", lambda value: "key")
    monkeypatch.setattr(httpx, "Client", _Client)
    _Client.sent = []
    printer = SimpleNamespace(id=1, api_host="printer.example", api_type="moonraker", api_key="stored")
    result = troubleshooting.printer_checks(printer)
    assert [c["status"] for c in result["checks"]] == ["pass", "pass"]
    assert _Client.sent == [(f"http://{address}:7125/printer/info", {"X-Api-Key": "key"})]


# ---- 2. go2rtc address ----

def test_the_default_go2rtc_address_is_unchanged_with_and_without_itar(monkeypatch):
    monkeypatch.delenv("ODIN_GO2RTC_API_URL", raising=False)
    for mode in ("", "1"):
        monkeypatch.setenv("ODIN_ITAR_MODE", mode)
        assert go2rtc_access.go2rtc_api_url() == "http://127.0.0.1:1984"
        assert go2rtc_access.go2rtc_client_kwargs() == {}
    assert itar.audit_boot_config(itar.env_configured_urls()) == []


@pytest.mark.parametrize("url", ["http://10.0.0.5:1984", "http://[fd00::5]:1984", "http://localhost:1984/"])
def test_itar_allows_a_loopback_or_local_go2rtc_address(monkeypatch, url):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", url)
    assert go2rtc_access.go2rtc_api_url() == url.rstrip("/")


def test_itar_allows_a_go2rtc_name_that_resolves_to_the_local_network(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://go2rtc:1984")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", ("172.18.0.4", 0))])
    assert go2rtc_access.go2rtc_api_url() == "http://go2rtc:1984"
    assert go2rtc_access.go2rtc_is_remote() is True


def _public_go2rtc(monkeypatch, url):
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", url)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: [(2, 1, 6, "", (PUBLIC, 0))])


@pytest.mark.parametrize("url", [f"http://{PUBLIC}:1984", "http://go2rtc.example:1984"])
@pytest.mark.parametrize("reader", ["go2rtc_api_url", "go2rtc_client_kwargs", "go2rtc_is_remote",
                                    "go2rtc_api_host", "go2rtc_api_port", "go2rtc_api_listen"])
def test_itar_refuses_a_public_go2rtc_address_for_every_reader(monkeypatch, caplog, url, reader):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    _public_go2rtc(monkeypatch, url)
    with caplog.at_level("ERROR", logger="odin.go2rtc"):
        with pytest.raises(itar.ItarOutboundBlocked) as refused:
            getattr(go2rtc_access, reader)()
    # Also an HTTPException, so a camera route answers with the plain reason.
    assert isinstance(refused.value, HTTPException)
    assert (refused.value.status_code, refused.value.detail) == (409, go2rtc_access.ITAR_REFUSAL)
    assert "go2rtc address refused; no request was sent" in caplog.text


def test_itar_refuses_a_go2rtc_name_that_does_not_resolve(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://go2rtc.example:1984")

    def unresolved(*args, **kwargs):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", unresolved)
    with pytest.raises(go2rtc_access.Go2rtcUrlRefused):
        go2rtc_access.go2rtc_api_url()


def test_without_itar_a_public_go2rtc_address_is_returned_as_before(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)

    def refuse(*args, **kwargs):
        raise AssertionError("no lookup is made without ITAR mode")
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", f"http://{PUBLIC}:1984/")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    assert go2rtc_access.go2rtc_api_url() == f"http://{PUBLIC}:1984"


def test_itar_stops_a_go2rtc_reload_before_any_request(monkeypatch):
    import httpx
    from modules.printers import route_utils
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    _public_go2rtc(monkeypatch, f"http://{PUBLIC}:1984")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("go2rtc was contacted"))
    with pytest.raises(go2rtc_access.Go2rtcUrlRefused):
        route_utils._go2rtc_reload()


def test_the_boot_audit_reports_a_public_go2rtc_address(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    _public_go2rtc(monkeypatch, f"http://{PUBLIC}:1984")
    assert itar.env_configured_urls() == [f"http://{PUBLIC}:1984"]
    with pytest.raises(RuntimeError, match=f"http://{PUBLIC}:1984"):
        itar.enforce_boot_config(itar.env_configured_urls())
    # The start-up audit is given the list.
    assert "*env_configured_urls()," in (BACKEND / "core" / "app.py").read_text(encoding="utf-8")


def test_every_reader_of_the_go2rtc_address_goes_through_the_validated_source():
    """Only go2rtc_access may read the setting to build a request; core/webhook_utils reads
    it to look up go2rtc's own addresses for the device policy and sends nothing to it."""
    readers = set()
    for path in BACKEND.rglob("*.py"):
        relative = path.relative_to(BACKEND).as_posix()
        if relative.startswith("core/schema/preserved") or relative.startswith("tests/"):
            continue
        source = path.read_text(encoding="utf-8")
        if '"ODIN_GO2RTC_API_URL"' in source or "'ODIN_GO2RTC_API_URL'" in source:
            readers.add(relative)
    assert readers == {"modules/printers/go2rtc_access.py", "core/webhook_utils.py", "core/itar.py"}


# ---- 3. Bambu chamber camera (found by the import inventory) ----

@pytest.mark.parametrize("itar_on", [True, False])
def test_the_chamber_camera_connects_outside_the_local_network_only_without_itar(monkeypatch, itar_on):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1" if itar_on else "")
    monkeypatch.setattr(bambu_chamber_camera, "device_connect_ip", lambda host, port: PUBLIC)

    class Reached(Exception):
        pass

    def connect(*args, **kwargs):
        raise Reached()
    monkeypatch.setattr(bambu_chamber_camera, "socket", SimpleNamespace(
        socket=connect, AF_INET=socket.AF_INET, AF_INET6=socket.AF_INET6, SOCK_STREAM=socket.SOCK_STREAM))

    async def first_frame():
        stream = bambu_chamber_camera.frames(SimpleNamespace(host="printer.example", access_code="code"))
        try:
            await stream.__anext__()
        finally:
            await stream.aclose()
    with pytest.raises(itar.ItarOutboundBlocked if itar_on else Reached):
        asyncio.run(first_frame())
