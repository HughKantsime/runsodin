"""Review L1: destination URLs are redacted in SSRF logs and stored Spoolman reasons."""
import logging
import socket

import pytest


@pytest.mark.parametrize("raw,expected", [
    ("Webhook hostname 'https://discord.com/api/webhooks/123/SECRETTOKEN' blocked",
     "Webhook hostname 'https://discord.com/[redacted]' blocked"),
    ("POST http://user:pass@hooks.example.test:8443/x?token=abc failed", "POST http://hooks.example.test:8443/[redacted] failed"),
    ("https://api.telegram.org/bot123456:ABC-def_GHI/sendMessage", "https://api.telegram.org/[redacted]"),
    ("token bot123456:ABC-def in text", "token bot[redacted] in text"),
    ("https://ntfy.example.test", "https://ntfy.example.test"),
])
def test_redact_urls(raw, expected):
    from core.redaction import redact_urls
    assert redact_urls(raw) == expected


def test_ssrf_refused_webhook_log_carries_no_secret(monkeypatch, caplog):
    from modules.notifications.webhook_payloads import post_typed_webhook
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))])
    caplog.set_level(logging.ERROR, logger="odin.notifications")
    url = "https://hooks.example.test/services/T000/B000/SUPERSECRETTOKEN?key=alsosecret"
    assert post_typed_webhook(url, "slack", event="e", title="t", message="m") is False
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "SSRF-blocked" in text
    assert "SUPERSECRETTOKEN" not in text and "alsosecret" not in text


def test_spoolman_refusal_reason_carries_no_credentials(monkeypatch):
    from core.config import settings
    from core import webhook_utils
    from modules.inventory import services
    monkeypatch.setattr(settings, "spoolman_url", "http://spoolman.example.test:7912")

    def refuse(*a, **k):
        raise webhook_utils.WebhookSSRFError("blocked http://admin:hunter2@spoolman.example.test:7912/api?api_key=k3y")
    monkeypatch.setattr(services, "_post_spoolman_usage", refuse)
    state, reason = services.deliver_spoolman_usage(42, 1.0)
    assert state == "failed"
    assert "hunter2" not in reason and "k3y" not in reason and "spoolman.example.test:7912" in reason
    errors = services.push_consumption_to_spoolman([{"spoolman_spool_id": 42, "grams": 1, "job_id": 1}])
    assert errors and "hunter2" not in errors[0] and "k3y" not in errors[0]
