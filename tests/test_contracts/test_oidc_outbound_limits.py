"""Discovery caching across handlers and HTTP login throttling per remote IP."""

import asyncio
from collections import OrderedDict
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.db import get_db
from core.rate_limit import limiter
from core.config import Settings
from core.remote_access import TrustedProxySchemeMiddleware
from modules.organizations import oidc_handler, routes_oidc


def handler(url):
    return oidc_handler.OIDCHandler("client", "secret", "tenant", "https://callback", discovery_url=url)


def test_discovery_cache_is_shared_per_url_expires_in_one_hour(monkeypatch):
    monkeypatch.setattr(oidc_handler, "_discovery_cache", OrderedDict(), raising=False)
    now = [1000.0]
    monkeypatch.setattr(oidc_handler, "_cache_clock", lambda: now[0], raising=False)
    fetched = []

    async def request(method, url, **kwargs):
        fetched.append(url)
        return httpx.Response(200, json={"issuer": url, "version": len(fetched)},
                              request=httpx.Request(method, url))

    monkeypatch.setattr(oidc_handler, "pinned_async_request", request)

    async def run():
        url = "https://first.test/discovery"
        original = handler(url)
        first = await original._get_oidc_config()
        now[0] += 3599
        assert await handler(url)._get_oidc_config() == first
        assert len(fetched) == 1
        assert (await handler("https://second.test/discovery")._get_oidc_config())["issuer"] != url
        now[0] += 1
        assert await original._get_oidc_config() != first
        assert fetched == [url, "https://second.test/discovery", url]

    asyncio.run(run())


def test_failed_discovery_is_not_cached(monkeypatch):
    monkeypatch.setattr(oidc_handler, "_discovery_cache", OrderedDict(), raising=False)
    fetched = []

    async def request(method, url, **kwargs):
        fetched.append(url)
        return httpx.Response(503 if len(fetched) == 1 else 200, json={"issuer": url},
                              request=httpx.Request(method, url))

    monkeypatch.setattr(oidc_handler, "pinned_async_request", request)

    async def run():
        url = "https://retry.test/discovery"
        with pytest.raises(httpx.HTTPStatusError):
            await handler(url)._get_oidc_config()
        assert (await handler(url)._get_oidc_config())["issuer"] == url
        assert len(fetched) == 2

    asyncio.run(run())


@pytest.fixture
def login_client(monkeypatch):
    calls = []

    class Handler:
        async def get_authorization_url(self):
            calls.append(True)
            return "https://idp.test/authorize", "state"

    monkeypatch.setattr(oidc_handler, "create_handler_from_config", lambda *args: Handler())
    db = SimpleNamespace(execute=lambda *args: SimpleNamespace(
        fetchone=lambda: SimpleNamespace(_mapping={}),
    ))
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(routes_oidc.router, prefix="/api")
    app.include_router(routes_oidc.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    limiter._storage.reset()
    monkeypatch.setenv("ODIN_TRUSTED_PROXIES", "10.0.0.10")
    app.add_middleware(TrustedProxySchemeMiddleware,
                       trusted=Settings(_env_file=None).odin_trusted_proxies)

    @app.middleware("http")
    async def owned_peer(request, call_next):
        # Test-only transport peer injection, never a production forwarded-header policy.
        request.scope["client"] = (request.headers["x-test-peer"], 12345)
        return await call_next(request)

    try:
        with TestClient(app) as client:
            yield client, calls
    finally:
        limiter._storage.reset()


def login(client, i, peer, forwarded=None):
    path = "/api/auth/oidc/login" if i % 2 else "/api/v1/auth/oidc/login"
    headers = {"x-test-peer": peer}
    if forwarded is not None:
        headers["x-forwarded-for"] = forwarded
    return client.get(path, headers=headers, follow_redirects=False).status_code


def test_oidc_login_rate_limit_is_per_ip_and_covers_both_aliases(login_client):
    client, calls = login_client
    for i in range(30):
        assert login(client, i, "192.0.2.1") == 302
    assert login(client, 30, "192.0.2.1") == 429
    assert len(calls) == 30
    assert login(client, 31, "192.0.2.2") == 302


def test_trusted_proxy_clients_have_separate_thirty_per_minute_buckets(login_client):
    client, calls = login_client
    for address in ("198.51.100.1", "198.51.100.2"):
        for i in range(30):
            assert login(client, i, "10.0.0.10", f"203.0.113.1, {address}") == 302
        # Spoofing the first hop cannot change the trusted proxy's last hop.
        assert login(client, 30, "10.0.0.10", f"203.0.113.2, {address}") == 429
    assert len(calls) == 60


def test_untrusted_peer_cannot_change_bucket_with_forwarded_header(login_client):
    client, calls = login_client
    for i in range(31):
        assert login(client, i, "192.0.2.1", f"198.51.100.{i + 1}") == (302 if i < 30 else 429)
    assert len(calls) == 30
