"""Behavior tests for the public SPA shell / protected API boundary."""

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.testclient import TestClient

from core.app import _register_http_middleware
from core.config import settings


class _NoAllowlistSession:
    def execute(self, *_args, **_kwargs):
        return self

    def fetchone(self):
        return None

    def close(self):
        return None


def _build_boundary_app(tmp_path: Path, monkeypatch) -> TestClient:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>ODIN SPA</body></html>")
    (dist / "robots.txt").write_text("User-agent: *\nDisallow: /")
    (assets / "app.js").write_text("window.odin = true")

    monkeypatch.setattr(settings, "api_key", "edge-key")
    monkeypatch.setattr("core.db.SessionLocal", _NoAllowlistSession)

    app = FastAPI()

    @app.get("/api/private")
    async def private_api():
        return {"secret": True}

    @app.get("/api/spools/{spool_id}/label")
    async def spool_label(spool_id: int, request: Request):
        if request.cookies.get("route_session") != "valid":
            raise HTTPException(status_code=401, detail="Not authenticated")
        return {"spool_id": spool_id}

    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        exact = dist / path
        if path and exact.is_file():
            return FileResponse(exact)
        return FileResponse(dist / "index.html")

    _register_http_middleware(app)
    return TestClient(app)


def test_public_spa_shell_and_assets_bypass_perimeter(tmp_path, monkeypatch):
    client = _build_boundary_app(tmp_path, monkeypatch)

    for path in ("/", "/login", "/jobs", "/unknown-client-route"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "ODIN SPA" in response.text

    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/robots.txt").status_code == 200


def test_api_boundary_remains_protected(tmp_path, monkeypatch):
    client = _build_boundary_app(tmp_path, monkeypatch)

    denied = client.get("/api/private")
    assert denied.status_code == 401

    allowed = client.get("/api/private", headers={"X-API-Key": "edge-key"})
    assert allowed.status_code == 200
    assert allowed.json() == {"secret": True}

    invalid = client.get("/api/private", headers={"X-API-Key": "wrong"})
    assert invalid.status_code == 401
    assert client.get("/openapi.json").status_code == 401


def test_browser_session_and_label_route_auth_are_preserved(tmp_path, monkeypatch):
    client = _build_boundary_app(tmp_path, monkeypatch)

    monkeypatch.setattr("core.auth.decode_token", lambda _token: {"sub": "edu-admin"})
    monkeypatch.setattr("jwt.decode", lambda *_args, **_kwargs: {"sub": "edu-admin"})
    client.cookies.set("session", "valid-session")
    assert client.get("/api/private").status_code == 200

    client.cookies.clear()
    assert client.get("/api/spools/1/label").status_code == 401
    client.cookies.set("route_session", "valid")
    assert client.get("/api/spools/1/label").status_code == 200


def test_api_shaped_traversal_does_not_reach_spa(tmp_path, monkeypatch):
    client = _build_boundary_app(tmp_path, monkeypatch)
    response = client.get("/api/%2e%2e/private")
    assert response.status_code == 401


def test_malformed_host_cannot_disguise_api_path_as_public_spa(tmp_path, monkeypatch):
    client = _build_boundary_app(tmp_path, monkeypatch)
    response = client.get(
        "/api/private",
        headers={"Host": "example.test/login?disguised="},
    )
    assert response.status_code == 401


def test_non_get_spa_path_is_not_public(tmp_path, monkeypatch):
    client = _build_boundary_app(tmp_path, monkeypatch)
    assert client.post("/login").status_code == 401
