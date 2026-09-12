"""Deterministic operational HTTP security evidence for EDU readiness."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from modules.system import backup_service


class _NoAllowlistSession:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return None

    def close(self):
        return None


def _app(monkeypatch, *, api_key=None, cors_origins="", trusted_hosts="*"):
    from core.app import _register_http_middleware, _setup_middleware
    from core.config import settings

    monkeypatch.setattr(settings, "api_key", api_key)
    monkeypatch.setattr(settings, "cors_origins", cors_origins)
    monkeypatch.setattr(settings, "trusted_hosts", trusted_hosts)
    monkeypatch.setattr("core.db.SessionLocal", _NoAllowlistSession)
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/health")
    async def api_health():
        return {"status": "ready"}

    @app.get("/api/student-record")
    async def student_record():
        return {"record": "synthetic"}

    @app.get("/public")
    async def public():
        return {"public": True}

    _setup_middleware(app)
    _register_http_middleware(app)
    return app


def test_configured_trusted_hosts_reject_unlisted_host(monkeypatch):
    client = TestClient(_app(monkeypatch, trusted_hosts="school.test"))
    assert client.get("/health", headers={"Host": "school.test"}).status_code == 200
    assert client.get("/health", headers={"Host": "untrusted.test"}).status_code == 400


def test_api_and_openapi_responses_are_never_cacheable(monkeypatch):
    client = TestClient(_app(monkeypatch))
    for path in ("/api/student-record", "/openapi.json"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Pragma"] == "no-cache"
    assert "Cache-Control" not in client.get("/public").headers


def test_openapi_is_behind_the_configured_perimeter(monkeypatch):
    client = TestClient(_app(monkeypatch, api_key="synthetic-edge-key"))
    rejected = client.get("/openapi.json")
    allowed = client.get("/openapi.json", headers={"X-API-Key": "synthetic-edge-key"})
    assert rejected.status_code == 401
    assert rejected.headers["Cache-Control"] == "no-store"
    assert allowed.status_code == 200


def test_public_health_and_authenticated_readiness_boundaries(monkeypatch):
    client = TestClient(_app(monkeypatch, api_key="synthetic-edge-key"))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/api/health").status_code == 401
    ready = client.get("/api/health", headers={"X-API-Key": "synthetic-edge-key"})
    assert ready.status_code == 200
    assert ready.json() == {"status": "ready"}


def test_cors_allows_only_explicit_origin(monkeypatch):
    client = TestClient(_app(monkeypatch, cors_origins="https://portal.school.test"))
    headers = {
        "Origin": "https://portal.school.test",
        "Access-Control-Request-Method": "GET",
    }
    allowed = client.options("/api/student-record", headers=headers)
    denied = client.options(
        "/api/student-record",
        headers={**headers, "Origin": "https://untrusted.test"},
    )
    assert allowed.status_code == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://portal.school.test"
    assert "Access-Control-Allow-Origin" not in denied.headers


def test_successful_login_sets_httponly_secure_samesite_cookie(monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", "synthetic-operational-cookie-secret")
    from modules.organizations import routes_auth

    user = SimpleNamespace(
        id=1,
        username="student@school.test",
        password_hash="synthetic-hash",
        role="viewer",
        is_active=True,
        mfa_enabled=False,
    )

    class _Result:
        def __init__(self, row=None):
            self.row = row

        def fetchone(self):
            return self.row

    class _LoginSession:
        def execute(self, statement, *_args, **_kwargs):
            sql = str(statement)
            if "SELECT * FROM users" in sql:
                return _Result(user)
            return _Result()

        def commit(self):
            return None

    session = _LoginSession()
    monkeypatch.setattr(routes_auth, "_check_rate_limit", lambda *_args: False)
    monkeypatch.setattr(routes_auth, "_is_locked_out", lambda *_args: False)
    monkeypatch.setattr(routes_auth, "verify_password", lambda *_args: True)
    monkeypatch.setattr(routes_auth, "_record_login_attempt", lambda *_args: None)
    monkeypatch.setattr(routes_auth, "_record_session", lambda *_args: None)
    monkeypatch.setattr(routes_auth, "log_audit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(routes_auth, "create_access_token", lambda *_args, **_kwargs: "synthetic-session")
    monkeypatch.setattr(routes_auth._settings, "cookie_secure", True)
    monkeypatch.setattr(routes_auth._settings, "cookie_samesite", "strict")

    app = FastAPI()
    app.include_router(routes_auth.router, prefix="/api")
    app.dependency_overrides[routes_auth.get_db] = lambda: session
    response = TestClient(app).post(
        "/api/auth/login",
        data={"username": "student@school.test", "password": "synthetic-password"},
    )

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "session=synthetic-session" in cookie
    assert "httponly" in cookie
    assert "secure" in cookie
    assert "samesite=strict" in cookie


def test_backup_capacity_failure_is_fail_closed(tmp_path, monkeypatch):
    database = tmp_path / "odin.db"
    database.write_bytes(b"synthetic-database-placeholder")
    paths = backup_service.paths_from_database_url(f"sqlite:///{database}")
    usage = shutil._ntuple_diskusage(total=1024, used=1024, free=0)
    monkeypatch.setattr(backup_service.shutil, "disk_usage", lambda _path: usage)

    with pytest.raises(backup_service.BackupValidationError, match="Insufficient free disk space"):
        backup_service._create_online_backup_unlocked(paths, "test_")
    assert not list(paths.backups.glob("test_*.db"))


def test_allowlist_backend_failure_returns_generic_observable_503(monkeypatch):
    app = _app(monkeypatch)

    def fail_session():
        raise RuntimeError("synthetic internal database detail")

    monkeypatch.setattr("core.db.SessionLocal", fail_session)
    response = TestClient(app, raise_server_exceptions=False).get("/api/student-record")
    assert response.status_code == 503
    assert response.json() == {"detail": "Service temporarily unavailable"}
    assert "synthetic internal database detail" not in response.text
