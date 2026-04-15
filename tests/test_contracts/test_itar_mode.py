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
