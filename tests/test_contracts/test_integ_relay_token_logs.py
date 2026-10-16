"""Camera-stack review MEDIUM 3: the camera relay's HMAC token never appears in logs, and
install-wide log lines in GET /system/diagnostics are superadmin only."""
import asyncio
import io
import logging
import secrets

TOKEN = secrets.token_hex(32)  # synthetic, generated per run
LINE = f'127.0.0.1:51234 - "GET /internal/camera-source/12/{TOKEN} HTTP/1.1" 200'


def test_relay_token_is_redacted_when_logged():
    from core.log_redaction import install_log_redaction, redact_text
    install_log_redaction()
    assert TOKEN not in redact_text(LINE) and "/internal/camera-source/12/[REDACTED]" in redact_text(LINE)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.access")
    logger.addHandler(handler)
    try:
        logger.warning('%s - "GET %s HTTP/1.1" 200', "127.0.0.1:51234", f"/internal/camera-source/12/{TOKEN}")
    finally:
        logger.removeHandler(handler)
    assert TOKEN not in stream.getvalue() and "/internal/camera-source/12/[REDACTED]" in stream.getvalue()


def test_relay_token_is_redacted_on_read():
    from modules.system.sanitize import sanitize_log_line
    out = sanitize_log_line(LINE)
    assert TOKEN not in out and "/internal/camera-source/12/[REDACTED]" in out


def _diagnostics(monkeypatch, user):
    from modules.system import diagnostics
    monkeypatch.setattr(diagnostics, "_read_tail", lambda path, n: [LINE, "tenant B printed widget.3mf"])
    return asyncio.run(diagnostics.get_diagnostics(current_user=user))


def test_org_admin_does_not_get_install_wide_log_lines(monkeypatch):
    from modules.system.diagnostics import LOGS_SUPERADMIN_ONLY
    result = _diagnostics(monkeypatch, {"id": 5, "role": "admin", "group_id": 2})
    assert result["recent_logs"] == LOGS_SUPERADMIN_ONLY and result["error_buffer"] == LOGS_SUPERADMIN_ONLY
    assert "tenant B" not in str(result)


def test_superadmin_gets_sanitized_log_lines(monkeypatch):
    result = _diagnostics(monkeypatch, {"id": 1, "role": "admin", "group_id": None})
    assert any("tenant B" in line for line in result["recent_logs"])
    assert TOKEN not in str(result["recent_logs"])
