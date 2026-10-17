"""Review fix O7: the OIDC binding cookie is Secure per COOKIE_SECURE and reaches every
exchange/callback path.

Secure required request.url.scheme == "https", which behind a TLS-terminating proxy is
http (uvicorn runs without forwarded_allow_ips), so the cookie went out without Secure.
Its path came from OIDC_REDIRECT_URI: a /api/v1 redirect gave /api/v1/auth/oidc, which the
web client's exchange (/api/auth/oidc/exchange) never receives."""
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def login(review_db, monkeypatch):  # noqa: F811
    from core.db import get_db
    from modules.organizations import oidc_handler, routes_oidc
    review_db.execute(text("INSERT INTO oidc_config(id,client_id,tenant_id,is_enabled,provider_type) "
                           "VALUES(1,'client','tenant',TRUE,'microsoft')"))
    review_db.commit()

    class Handler:
        async def get_authorization_url(self):
            return "https://idp.example.test/authorize", "state-123"
    monkeypatch.setattr(oidc_handler, "create_handler_from_config", lambda config, redirect: Handler())
    app = FastAPI()
    app.include_router(routes_oidc.router, prefix="/api")
    app.include_router(routes_oidc.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: review_db

    def run(path="/api/auth/oidc/login", *, redirect=None, secure_env=None, base="http://odin.example.test"):
        monkeypatch.setattr(routes_oidc._settings, "oidc_redirect_uri", redirect, raising=False)
        if secure_env is None:
            monkeypatch.delenv("COOKIE_SECURE", raising=False)
            monkeypatch.setattr(routes_oidc._settings, "cookie_secure", True)
        else:
            monkeypatch.setenv("COOKIE_SECURE", secure_env)
            monkeypatch.setattr(routes_oidc._settings, "cookie_secure", secure_env == "true")
        response = TestClient(app, base_url=base).get(path, follow_redirects=False)
        assert response.status_code == 302, response.text
        return [h for h in response.headers.get_list("set-cookie") if h.startswith(routes_oidc.OIDC_BINDING_COOKIE + "=")]
    return run


def _paths(cookies):
    return {re.search(r"[Pp]ath=([^;]+)", c).group(1) for c in cookies}


def test_secure_follows_cookie_secure_even_behind_a_tls_proxy(login):
    cookies = login(secure_env="true")  # proxy terminated TLS: the app sees http
    assert cookies and all("secure" in c.lower() for c in cookies), cookies
    cookies = login(secure_env="false", base="https://odin.example.test")
    assert cookies and not any("secure" in c.lower() for c in cookies), cookies


def test_secure_falls_back_to_the_request_scheme_when_unset(login):
    assert all("secure" in c.lower() for c in login(base="https://odin.example.test"))
    assert not any("secure" in c.lower() for c in login(base="http://odin.example.test"))


@pytest.mark.parametrize("redirect", [None, "https://odin.example.test/api/v1/auth/oidc/callback",
                                      "https://odin.example.test/api/auth/oidc/callback"])
@pytest.mark.parametrize("start", ["/api/auth/oidc/login", "/api/v1/auth/oidc/login"])
def test_binding_cookie_reaches_api_and_v1_exchange_and_callback(login, redirect, start):
    paths = _paths(login(start, redirect=redirect))
    for target in ("/api/auth/oidc/callback", "/api/auth/oidc/exchange",
                   "/api/v1/auth/oidc/callback", "/api/v1/auth/oidc/exchange"):
        assert any(target == p or target.startswith(p.rstrip("/") + "/") for p in paths), (target, paths)


def test_web_client_exchanges_on_a_covered_path():
    source = (ROOT / "frontend" / "src" / "components" / "auth" / "OIDCBootstrap.tsx").read_text()
    assert "fetch('/api/auth/oidc/exchange'" in source
