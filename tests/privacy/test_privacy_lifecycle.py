"""End-to-end privacy lifecycle checks against the real ASGI application."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ops.edu_readiness.api_load import _prepare_database


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("privacy") / "odin.db"
    app, tokens = _prepare_database(database)
    connection = sqlite3.connect(database)
    connection.execute("UPDATE users SET group_id = NULL WHERE id = 30")
    connection.commit()
    connection.close()
    with TestClient(app) as client:
        yield client, tokens, database


def headers(token: str) -> dict[str, str]:
    # The perimeter intentionally accepts browser JWTs only from the HttpOnly
    # session cookie when a global API key is configured.
    return {"Cookie": f"session={token}"}


def test_export_is_complete_but_excludes_credentials(stack):
    client, tokens, _ = stack
    response = client.get("/api/users/1/export", headers=headers(tokens["viewer"][0]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"user", "jobs_submitted", "audit_log_entries", "active_sessions", "api_tokens", "quota_usage"} <= body.keys()
    assert "password_hash" not in body["user"]
    assert "mfa_secret" not in body["user"]
    assert all("token_hash" not in token for token in body["api_tokens"])


def test_export_csv_and_backup_authorization_is_least_privilege(stack):
    client, tokens, _ = stack
    viewer = headers(tokens["viewer"][0])
    assert client.get("/api/users/2/export", headers=viewer).status_code == 403
    assert client.get("/api/export/jobs", headers=viewer).status_code == 403
    assert client.post("/api/backups", headers=viewer).status_code == 403


def test_education_usage_report_is_tenant_scoped(stack):
    client, tokens, _ = stack
    response = client.get(
        "/api/education/usage-report?days=30",
        headers=headers(tokens["operator"][0]),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["total_jobs"] == 300
    assert sum(body["daily_submissions"].values()) == 300
    assert "foreign-tenant-canary" not in response.text
    assert "load-user-29@school.test" not in response.text


def test_job_submission_alert_does_not_cross_operator_tenants(stack):
    client, tokens, database = stack
    connection = sqlite3.connect(database)
    connection.execute("UPDATE groups SET owner_id = NULL WHERE id = 1")
    connection.commit()
    connection.close()
    response = client.post(
        "/api/jobs",
        headers=headers(tokens["viewer"][0]),
        json={"item_name": "tenant-alert-canary", "quantity": 1},
    )
    assert response.status_code == 201, response.text
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM alerts WHERE user_id = 29 AND title LIKE '%tenant-alert-canary%'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_retention_cutoff_deletes_only_expired_completed_records(stack):
    client, tokens, database = stack
    admin = headers(tokens["admin"][0])
    connection = sqlite3.connect(database)
    connection.execute("UPDATE jobs SET status='completed', updated_at='2000-01-01' WHERE id=10000")
    connection.execute("UPDATE jobs SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE id=10001")
    connection.commit()
    connection.close()
    configured = client.put(
        "/api/config/retention",
        headers=admin,
        json={"completed_jobs_days": 30, "audit_logs_days": 365, "timelapses_days": 30, "alert_history_days": 90},
    )
    assert configured.status_code == 200, configured.text
    cleaned = client.post("/api/admin/retention/cleanup", headers=admin)
    assert cleaned.status_code == 200, cleaned.text
    connection = sqlite3.connect(database)
    try:
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE id=10000").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jobs WHERE id=10001").fetchone()[0] == 1
    finally:
        connection.close()


def test_erasure_revokes_access_and_removes_canary_from_database(stack):
    client, tokens, database = stack
    canary = "load-user-1@school.test"
    assert client.get("/api/users/1/export", headers=headers(tokens["viewer"][0])).status_code == 200
    erased = client.delete("/api/users/1/erase", headers=headers(tokens["admin"][0]))
    assert erased.status_code == 200, erased.text
    assert canary not in "\n".join(sqlite3.connect(database).iterdump())
    assert client.get("/api/auth/me", headers=headers(tokens["viewer"][0])).status_code == 401


def test_cross_tenant_canary_never_appears_in_viewer_reads(stack):
    client, tokens, _ = stack
    viewer = headers(tokens["viewer"][1])
    for endpoint in ("/api/printers", "/api/jobs?limit=500"):
        response = client.get(endpoint, headers=viewer)
        assert response.status_code == 200, response.text
        assert "foreign-tenant-canary" not in response.text
