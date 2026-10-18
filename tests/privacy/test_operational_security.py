"""Deterministic operational HTTP security evidence for EDU readiness."""

from __future__ import annotations

import shutil

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


def test_successful_login_sets_httponly_secure_samesite_cookie(tmp_path, monkeypatch):
    # A real bootstrapped database: login records the attempt, the session and the
    # verified actor, and fails closed (503) when it cannot, so a stub session that
    # records nothing no longer stands in for it.
    monkeypatch.setenv("JWT_SECRET_KEY", "synthetic-operational-cookie-secret")
    from pathlib import Path
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from core.auth import hash_password
    from core.schema import bootstrap_database
    from modules.organizations import routes_auth

    engine = create_engine(f"sqlite:///{tmp_path / 'login.db'}")
    bootstrap_database(engine, Path(__file__).resolve().parents[2] / "backend")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, is_active) "
            "VALUES (1, 'student@school.test', 'student@school.test', :hash, 'viewer', 1)"),
            {"hash": hash_password("synthetic-password")})
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(routes_auth._settings, "cookie_secure", True)
    monkeypatch.setattr(routes_auth._settings, "cookie_samesite", "strict")

    def database():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(routes_auth.router, prefix="/api")
    app.dependency_overrides[routes_auth.get_db] = database
    response = TestClient(app).post(
        "/api/auth/login",
        data={"username": "student@school.test", "password": "synthetic-password"},
    )

    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "session=" + response.json()["access_token"].lower() in cookie
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
