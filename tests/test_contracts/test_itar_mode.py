"""
Contract test — ODIN_ITAR_MODE=1 hard-lock (v1.8.9).

Verifies:
  1. `is_itar_mode()` reads the env var truthily.
  2. `is_private_destination()` correctly classifies IPs and hostnames.
  3. `check_url_allowed()` allows private, rejects public.
  4. `audit_boot_config()` returns violation list for public URLs.
  5. `enforce_boot_config()` no-ops when ITAR is off; raises when on+violation.
  6. `enforce_request_destination()` no-ops when off; raises when on+public.
  7. `safe_post`/`trusted_post` refuse public destinations in ITAR mode.
"""

import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


@pytest.fixture
def itar_on(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    yield


@pytest.fixture
def itar_off(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    yield


def test_is_itar_mode_truthy_env(monkeypatch):
    from core.itar import is_itar_mode

    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("ODIN_ITAR_MODE", v)
        assert is_itar_mode() is True, f"{v!r} should enable"


def test_is_itar_mode_falsy_env(monkeypatch):
    from core.itar import is_itar_mode

    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    assert is_itar_mode() is False

    for v in ("0", "false", "no", "off", ""):
        monkeypatch.setenv("ODIN_ITAR_MODE", v)
        assert is_itar_mode() is False, f"{v!r} should disable"


def test_private_literals_classified_as_private():
    from core.itar import is_private_destination

    for host in ("127.0.0.1", "10.0.0.5", "172.16.1.1", "192.168.0.100", "::1"):
        assert is_private_destination(host) is True, f"{host} should be private"


def test_public_literals_classified_as_public():
    from core.itar import is_private_destination

    for host in ("8.8.8.8", "1.1.1.1", "93.184.216.34"):
        assert is_private_destination(host) is False, f"{host} should be public"


def test_localhost_hostname_is_private():
    from core.itar import is_private_destination

    assert is_private_destination("localhost") is True


def test_check_url_allowed_accepts_private():
    from core.itar import check_url_allowed

    ok, _ = check_url_allowed("http://127.0.0.1:8000/hook")
    assert ok is True
    ok, _ = check_url_allowed("http://10.5.5.5/x")
    assert ok is True


def test_check_url_allowed_rejects_public():
    from core.itar import check_url_allowed

    ok, reason = check_url_allowed("http://8.8.8.8/")
    assert ok is False
    assert "public" in reason.lower()


def test_check_url_allowed_passes_empty():
    from core.itar import check_url_allowed

    ok, _ = check_url_allowed(None)
    assert ok is True
    ok, _ = check_url_allowed("")
    assert ok is True


def test_audit_boot_config_lists_violations():
    from core.itar import audit_boot_config

    violations = audit_boot_config(
        ["http://127.0.0.1/", "https://runsodin.com", "http://192.168.1.5/"]
    )
    # Only the runsodin.com entry violates.
    assert len(violations) == 1
    assert "runsodin.com" in violations[0]


def test_audit_boot_config_clean():
    from core.itar import audit_boot_config

    violations = audit_boot_config(["http://127.0.0.1/", "http://10.5.5.5/", None, ""])
    assert violations == []


def test_enforce_boot_config_noop_when_off(itar_off):
    from core.itar import enforce_boot_config

    # Public URLs allowed when ITAR is off — no raise.
    enforce_boot_config(["https://runsodin.com"])


def test_enforce_boot_config_raises_when_on_and_violation(itar_on):
    from core.itar import enforce_boot_config

    with pytest.raises(RuntimeError) as exc_info:
        enforce_boot_config(["https://8.8.8.8/"])
    assert "ODIN_ITAR_MODE=1" in str(exc_info.value)


def test_enforce_boot_config_passes_when_on_and_clean(itar_on):
    from core.itar import enforce_boot_config

    # All private — no raise.
    enforce_boot_config(["http://127.0.0.1/", "http://10.5.5.5/"])


def test_enforce_request_destination_noop_when_off(itar_off):
    from core.itar import enforce_request_destination

    enforce_request_destination("https://8.8.8.8/anything")


def test_enforce_request_destination_raises_when_on_public(itar_on):
    from core.itar import enforce_request_destination, ItarOutboundBlocked

    with pytest.raises(ItarOutboundBlocked):
        enforce_request_destination("https://8.8.8.8/")


def test_enforce_request_destination_allows_private_when_on(itar_on):
    from core.itar import enforce_request_destination

    # Internal webhook URL — allowed under ITAR.
    enforce_request_destination("http://10.0.0.5/hook")
    enforce_request_destination("http://127.0.0.1:8000/")


def test_safe_post_refuses_public_in_itar_mode(itar_on, monkeypatch):
    """safe_post integration: ITAR blocks public before SSRF pin runs."""
    from core.webhook_utils import safe_post, WebhookSSRFError

    with pytest.raises(WebhookSSRFError) as exc_info:
        safe_post("https://8.8.8.8/hook", json={"a": 1})
    assert "ITAR" in str(exc_info.value) or "public" in str(exc_info.value).lower()


def test_trusted_post_refuses_in_itar_mode(itar_on):
    """trusted_post's allowlisted hosts (Telegram etc.) are all public
    → ITAR refuses every call."""
    from core.webhook_utils import trusted_post, WebhookSSRFError

    with pytest.raises(WebhookSSRFError):
        trusted_post("https://api.telegram.org/botXYZ/sendMessage", json={})


def test_collect_db_configured_urls_handles_missing_tables(monkeypatch):
    """If neither `webhooks` nor `system_config` is queryable (fresh
    install, pre-migration), collect returns an empty list without
    raising — boot must not fail just because a table is missing."""
    from core.itar import collect_db_configured_urls

    class _RaiseDB:
        def execute(self, *a, **k):
            raise RuntimeError("table missing")
        def close(self):
            pass

    import core.db as db_mod
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _RaiseDB())

    # Should not raise; returns [].
    urls = collect_db_configured_urls()
    assert urls == []


def test_apns_disabled_under_itar(itar_on, monkeypatch):
    """Codex pass 18: APNs targets api.push.apple.com, inherently
    public. ITAR mode must refuse to report APNs as configured even
    when credentials are present."""
    monkeypatch.setenv("APNS_KEY_ID", "K1")
    monkeypatch.setenv("APNS_TEAM_ID", "T1")
    monkeypatch.setenv("APNS_KEY_CONTENT", "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----")

    from modules.push.apns import APNsProvider
    provider = APNsProvider()
    assert provider._is_configured() is False, (
        "APNs must report not-configured under ITAR even with credentials "
        "present — Apple push service is inherently public."
    )


def test_apns_configured_outside_itar(itar_off, monkeypatch):
    """Sanity: outside ITAR, APNs works if credentials are set."""
    monkeypatch.setenv("APNS_KEY_ID", "K1")
    monkeypatch.setenv("APNS_TEAM_ID", "T1")
    monkeypatch.setenv("APNS_KEY_CONTENT", "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----")

    from modules.push.apns import APNsProvider
    provider = APNsProvider()
    assert provider._is_configured() is True


def test_pin_for_request_noop_when_itar_off(itar_off):
    """When ITAR is disabled, pin_for_request is a pure passthrough."""
    from core.itar import pin_for_request

    with pin_for_request("https://8.8.8.8/"):
        pass  # Should not raise.


def test_pin_for_request_blocks_public_when_itar_on(itar_on):
    """Literal public IP → refused with DNS pinning context."""
    from core.itar import pin_for_request, ItarOutboundBlocked

    with pytest.raises(ItarOutboundBlocked):
        with pin_for_request("https://8.8.8.8/"):
            pass


def test_pin_for_request_allows_private_literal_when_itar_on(itar_on):
    """Literal private IP → context enters fine."""
    from core.itar import pin_for_request

    with pin_for_request("http://10.0.0.5:8080/"):
        pass  # Should not raise.


def test_pin_for_request_refuses_empty_hostname(itar_on):
    """Malformed URL (no host) → refused rather than silently accepted."""
    from core.itar import pin_for_request, ItarOutboundBlocked

    with pytest.raises(ItarOutboundBlocked):
        with pin_for_request("not-a-url"):
            pass


def test_pin_propagates_to_asyncio_to_thread():
    """Codex pass 17 (2026-04-15): verify the ContextVar-based DNS
    pin survives the async thread-pool hop that httpx actually uses.

    httpx (async) uses anyio.to_thread.run_sync for blocking DNS.
    anyio's thread-pool wrapper calls `copy_context().run(...)` so
    ContextVars propagate. The stdlib equivalent is
    `asyncio.to_thread` (Python 3.9+), which also copies context.

    Note: the raw `loop.run_in_executor(None, func)` does NOT copy
    context by default, so care matters at every integration site.
    This test proves the canonical async path works; call-site code
    must use `to_thread` / anyio / httpx (all of which propagate),
    not raw run_in_executor.
    """
    import asyncio
    import socket as _sock

    from core.webhook_utils import _dns_pin_state, _pinned_getaddrinfo

    # Sanity: our monkeypatch is installed.
    assert _sock.getaddrinfo is _pinned_getaddrinfo

    def _resolve_blocking():
        return _sock.getaddrinfo(
            "pin-test.local", 80, 0, _sock.SOCK_STREAM,
        )

    async def _run_in_pin():
        token = _dns_pin_state.set({
            "hostname": "pin-test.local",
            "port": 80,
            "ips": ["10.99.99.99"],
        })
        try:
            # asyncio.to_thread is the context-copying wrapper used by
            # anyio/httpx under the hood.
            infos = await asyncio.to_thread(_resolve_blocking)
            return [info[4][0] for info in infos]
        finally:
            _dns_pin_state.reset(token)

    result = asyncio.run(_run_in_pin())
    assert "10.99.99.99" in result, (
        f"ContextVar pin did not reach asyncio.to_thread — the canonical "
        f"async DNS path would also miss it. Got {result}."
    )


def test_pin_does_not_propagate_to_raw_run_in_executor():
    """Documentation test (codex pass 17): the raw
    loop.run_in_executor(None, func) does NOT copy ContextVar. This
    matters because call sites MUST route async DNS through anyio /
    httpx / asyncio.to_thread, never raw run_in_executor. The failure
    mode is silent (no pin) — so this test pins the boundary."""
    import asyncio
    import socket as _sock

    from core.webhook_utils import _dns_pin_state, _pinned_getaddrinfo

    async def _run():
        token = _dns_pin_state.set({
            "hostname": "pin-test-raw.local",
            "port": 80,
            "ips": ["10.77.77.77"],
        })
        try:
            loop = asyncio.get_event_loop()
            try:
                infos = await loop.run_in_executor(
                    None, _sock.getaddrinfo,
                    "pin-test-raw.local", 80, 0, _sock.SOCK_STREAM,
                )
                return [info[4][0] for info in infos]
            except _sock.gaierror:
                # Expected: pin not visible in raw executor, real DNS
                # fails for a made-up hostname.
                return []
        finally:
            _dns_pin_state.reset(token)

    result = asyncio.run(_run())
    assert "10.77.77.77" not in result, (
        "UNEXPECTED: raw run_in_executor propagated ContextVar. "
        "If this starts passing on a future Python, update "
        "webhook_utils docstring accordingly."
    )


def test_collect_db_configured_urls_returns_webhook_urls(monkeypatch):
    """Happy path: populated webhooks table yields plaintext URLs."""
    from core.itar import collect_db_configured_urls
    import core.db as db_mod

    class _Row:
        def __init__(self, v):
            self._v = v
        def __getitem__(self, i):
            return self._v

    class _Result:
        def __init__(self, rows):
            self._rows = rows
        def fetchall(self):
            return self._rows

    class _FakeDB:
        def __init__(self):
            self._calls = 0
        def execute(self, clause, params=None):
            self._calls += 1
            sql = str(clause)
            if "webhooks" in sql:
                return _Result([_Row("https://internal.example/hook")])
            if "system_config" in sql:
                return _Result([("license_server_url", "http://10.0.0.5")])
            return _Result([])
        def close(self):
            pass

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _FakeDB())
    # Bypass decryption for this test.
    import core.crypto as _crypto
    monkeypatch.setattr(_crypto, "is_encrypted", lambda v: False)

    urls = collect_db_configured_urls()
    assert "https://internal.example/hook" in urls
    assert "http://10.0.0.5" in urls
