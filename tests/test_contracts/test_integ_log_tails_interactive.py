"""Review fix O3: install-wide log content and VAPID rotation need an interactive session.

A superadmin's API token (or the legacy global key) could read the backend log tail through
GET /system/diagnostics and the troubleshooting report's include_logs, and rotate the VAPID
key pair. Log lines can carry another tenant's activity; rotation invalidates every push
subscription. Token callers now get the rest of the diagnostics without log content, a 403
for include_logs, and a 403 on rotation."""
import asyncio

import pytest
from fastapi import HTTPException

LINE = "2026-10-17 10:00:00 [ERROR] tenant B printed widget.3mf"
INTERACTIVE = {"id": 1, "role": "admin", "group_id": None}
TOKEN = {"id": 1, "role": "admin", "group_id": None, "_auth_kind": "user_api_token", "_token_scopes": ["admin"]}
LEGACY = {"id": 1, "role": "admin", "group_id": None, "_auth_kind": "legacy_global_api_key"}


@pytest.fixture
def tails(monkeypatch):
    from modules.system import diagnostics
    monkeypatch.setattr(diagnostics, "_read_tail", lambda path, n: [LINE])
    return diagnostics


@pytest.mark.parametrize("principal", [TOKEN, LEGACY])
def test_token_superadmin_gets_diagnostics_without_log_content(tails, principal):
    result = asyncio.run(tails.get_diagnostics(current_user=principal))
    assert "tenant B" not in str(result["recent_logs"]) and "tenant B" not in str(result["error_buffer"])
    assert result["recent_logs"] == tails.LOGS_INTERACTIVE_ONLY


def test_interactive_superadmin_still_gets_sanitized_logs(tails):
    result = asyncio.run(tails.get_diagnostics(current_user=INTERACTIVE))
    assert any("tenant B" in line for line in result["recent_logs"])


@pytest.mark.parametrize("principal", [TOKEN, LEGACY])
def test_troubleshooting_report_refuses_logs_to_a_token(tails, monkeypatch, principal):
    from modules.system import troubleshooting
    monkeypatch.setattr(troubleshooting, "server_checks", lambda db: {"checks": []})
    with pytest.raises(HTTPException) as refused:
        troubleshooting.support_report(troubleshooting.ReportRequest(problem="x", include_logs=True), user=principal, db=None)
    assert refused.value.status_code == 403
    report = troubleshooting.support_report(troubleshooting.ReportRequest(problem="x", include_logs=False), user=principal, db=None)
    assert "recent_logs" not in report


def test_vapid_rotation_requires_an_interactive_session():
    from tests.test_contracts.test_cert_F_B_credential_census import _interactive_routes
    assert any(method == "POST" and path.endswith("/push/vapid-key/rotate") for method, path in _interactive_routes())
