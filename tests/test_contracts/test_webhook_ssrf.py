"""
Functional test — Webhook URL resolution must reject private addresses.

Guards R8 from the 2026-04-12 Codex adversarial review:
    webhook_utils.py blocked LITERAL private IPs but trusted arbitrary
    hostnames without DNS resolution. A malicious admin could configure
    https://hooks.example.com with DNS that resolves to 10.0.0.1 — O.D.I.N.
    would happily POST to internal infrastructure. DNS rebinding and
    split-horizon DNS both exploit this.

This test actually exercises resolve_and_check_webhook_url() with real
IPs and a hostname we know resolves to a disallowed range.

Run without container: pytest tests/test_contracts/test_webhook_ssrf.py -v
"""

import socket
from unittest.mock import patch

import pytest

from core.webhook_utils import (
    WebhookSSRFError,
    resolve_and_check_webhook_url,
    _validate_webhook_url,
)
from fastapi import HTTPException


class TestLiteralIPChecks:
    """Literal IPs should be rejected synchronously without DNS."""

    def test_rejects_literal_private_ipv4(self):
        with pytest.raises(WebhookSSRFError, match="private|reserved"):
            resolve_and_check_webhook_url("http://10.0.0.1/hook")

    def test_rejects_literal_loopback_ipv4(self):
        with pytest.raises(WebhookSSRFError, match="private|reserved"):
            resolve_and_check_webhook_url("http://127.0.0.1/hook")

    def test_rejects_literal_link_local_ipv4(self):
        with pytest.raises(WebhookSSRFError, match="private|reserved"):
            resolve_and_check_webhook_url("http://169.254.169.254/latest")

    def test_rejects_ipv6_loopback(self):
        with pytest.raises(WebhookSSRFError, match="private|reserved"):
            resolve_and_check_webhook_url("http://[::1]/hook")


class TestHostnameResolution:
    """Hostnames must be resolved and every returned address checked."""

    def test_rejects_hostname_resolving_to_private(self):
        """Simulate DNS rebinding: hostname resolves to 10.0.0.1."""
        fake_infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),
        ]
        with patch("core.webhook_utils.socket.getaddrinfo", return_value=fake_infos):
            with pytest.raises(WebhookSSRFError, match="private|reserved"):
                resolve_and_check_webhook_url("https://evil-rebind.example.com/hook")

    def test_rejects_hostname_with_mixed_results(self):
        """If ANY returned address is private, reject — don't race-choose."""
        fake_infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),  # public
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", 0)),       # private
        ]
        with patch("core.webhook_utils.socket.getaddrinfo", return_value=fake_infos):
            with pytest.raises(WebhookSSRFError, match="private|reserved"):
                resolve_and_check_webhook_url("https://mixed.example.com/hook")

    def test_accepts_hostname_resolving_to_public(self):
        """Public IPs should pass through unchanged."""
        fake_infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 0)),
        ]
        with patch("core.webhook_utils.socket.getaddrinfo", return_value=fake_infos):
            result = resolve_and_check_webhook_url("https://example.com/hook")
            assert result == "https://example.com/hook"

    def test_unresolvable_hostname_is_rejected(self):
        """gaierror → WebhookSSRFError, not a silent pass."""
        with patch("core.webhook_utils.socket.getaddrinfo",
                   side_effect=socket.gaierror("Name or service not known")):
            with pytest.raises(WebhookSSRFError, match="could not be resolved"):
                resolve_and_check_webhook_url("https://does-not-exist.invalid/hook")


class TestNoCachingContract:
    """Verify the source does NOT cache resolution results."""

    def test_no_lru_cache_on_resolver(self):
        import core.webhook_utils as mod
        fn = mod.resolve_and_check_webhook_url
        # functools.lru_cache wraps would set __wrapped__ and cache_info
        assert not hasattr(fn, "cache_info"), (
            "resolve_and_check_webhook_url appears to be lru_cache-wrapped. "
            "Caching defeats the R8 dispatch-time SSRF check: a webhook can "
            "be safe at configuration and malicious minutes later via DNS "
            "rebinding. Remove the cache."
        )


class TestSafePostHardening:
    """Codex pass 3 (2026-04-13) closures — must not regress."""

    def test_safe_post_disables_environment_proxies(self):
        """trust_env=False prevents HTTP_PROXY/HTTPS_PROXY hijack.

        With trust_env left default, an outbound proxy in the environment
        would route the connection through itself and re-resolve the
        hostname — bypassing the DNS-pin defense entirely.
        """
        import core.webhook_utils as mod
        import inspect
        src = inspect.getsource(mod.safe_post)
        assert "trust_env=False" in src, (
            "safe_post() must construct the httpx Client with "
            "trust_env=False. Without it, HTTP_PROXY/HTTPS_PROXY env "
            "vars route the request through a proxy that does its own "
            "DNS resolution, defeating the SSRF pin."
        )

    def test_safe_post_uses_dns_pin(self):
        """The implementation must call _pin_dns around the httpx call."""
        import core.webhook_utils as mod
        import inspect
        src = inspect.getsource(mod.safe_post)
        assert "_pin_dns(" in src, (
            "safe_post() must wrap the httpx call in _pin_dns(...) so "
            "the actual TCP connect can only land on the pre-validated "
            "IP. Without it, httpx re-resolves the hostname and the "
            "DNS-rebinding window remains open."
        )

    def test_dns_pin_is_thread_local_not_global_lock(self):
        """_pin_dns must use thread-local state, not a process-wide lock.

        Codex pass 3: the previous lock serialised every webhook
        dispatch behind one slow endpoint. Thread-local lets concurrent
        dispatches each carry their own pin without contention.
        """
        import core.webhook_utils as mod
        import inspect
        src = inspect.getsource(mod)
        assert "threading.local" in src, (
            "DNS pin state must be threading.local() so concurrent "
            "webhook dispatches don't serialise behind a global lock."
        )
        # Also: there must NOT be a module-level Lock held around the
        # entire request — that's the regression we're guarding against.
        # We allow Lock for other uses, but flag a suspicious pattern.
        assert "_DNS_PIN_LOCK" not in src, (
            "Old global _DNS_PIN_LOCK is back. Use thread-local pinning "
            "instead so concurrent webhooks don't block each other."
        )


class TestBackwardCompatibility:
    """The original _validate_webhook_url config-time gate still works."""

    def test_validate_allows_public_literal(self):
        _validate_webhook_url("https://93.184.216.34/hook")  # example.com IP

    def test_validate_rejects_private_literal(self):
        with pytest.raises(HTTPException) as exc:
            _validate_webhook_url("http://10.0.0.1/hook")
        assert exc.value.status_code == 400

    def test_validate_rejects_loopback_literal(self):
        with pytest.raises(HTTPException) as exc:
            _validate_webhook_url("http://127.0.0.1/hook")
        assert exc.value.status_code == 400

    def test_validate_allows_hostname_at_config_time(self):
        """Hostnames pass config-time check; dispatch-time is the real gate."""
        _validate_webhook_url("https://hooks.example.com/webhook")
