"""Caller error contracts for bounded outbound bodies and transport failures."""

import asyncio
from collections import OrderedDict

import httpx
import pytest
from fastapi import HTTPException

from core import outbound_http
from modules.organizations import classroom_service, oidc_handler
from modules.system import routes_health


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "0")
    client = httpx.Client

    def install(respond):
        monkeypatch.setattr(outbound_http.httpx, "Client", lambda **kw: client(
            transport=httpx.MockTransport(respond), **kw))

    return install


@pytest.mark.parametrize("size", [2 * 1024 * 1024 + 1, 4 * 1024 * 1024 + 1])
def test_classroom_oversized_body_is_bounded_and_maps_to_502(transport, size):
    consumed, closed = [], []

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(3):
                consumed.append(True)
                yield b"x" * size

        def close(self):
            closed.append(True)

    transport(lambda request: httpx.Response(200, stream=Body()))
    with pytest.raises(classroom_service.ClassroomError) as caught:
        asyncio.run(classroom_service._json_request("GET", "https://google.test/roster"))
    assert caught.value.status == 502
    assert caught.value.code == "google_response_too_large"
    assert consumed == [True], "must stop at Classroom's 2 MiB limit"
    assert closed == [True]


def test_classroom_body_at_limit_is_accepted(transport):
    body = b'{"ok":true}' + b" " * (classroom_service.MAX_RESPONSE_BYTES - 11)
    transport(lambda request: httpx.Response(200, content=body))
    assert asyncio.run(classroom_service._json_request(
        "GET", "https://google.test/roster")) == {"ok": True}


@pytest.mark.parametrize("error_type", [
    httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout,
    httpx.ConnectError, httpx.ReadError,
])
def test_classroom_transport_failure_maps_to_generic_502(transport, error_type):
    def fail(request):
        raise error_type("sensitive upstream detail", request=request)

    transport(fail)
    with pytest.raises(classroom_service.ClassroomError) as caught:
        asyncio.run(classroom_service._json_request("POST", "https://google.test/token"))
    assert caught.value.status == 502
    assert caught.value.code == "google_request_failed"
    assert caught.value.message == "Google authorization or Classroom request failed"
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize("caller", ["discovery", "exchange", "userinfo", "jwks", "license"])
def test_other_async_callers_preserve_oversize_error_behavior(monkeypatch, transport, caller):
    monkeypatch.setattr(oidc_handler, "_discovery_cache", OrderedDict())
    transport(lambda request: httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)))
    url = "https://upstream.test/oversize"
    handler = oidc_handler.OIDCHandler("client", "secret", "tenant", "https://callback",
                                       discovery_url=url)

    async def config():
        return {"token_endpoint": url, "userinfo_endpoint": url, "jwks_uri": url, "issuer": url}

    if caller != "discovery":
        monkeypatch.setattr(handler, "_get_oidc_config", config)

    async def run():
        if caller == "discovery":
            return await handler._get_oidc_config()
        if caller == "exchange":
            return await handler.exchange_code("code")
        if caller == "userinfo":
            return await handler.get_user_info("token")
        if caller == "jwks":
            return await handler.parse_id_token("unused")
        return await routes_health._fetch_license_challenge(url, "installation")

    expected = HTTPException if caller == "license" else ValueError if caller == "jwks" else httpx.HTTPError
    with pytest.raises(expected) as caught:
        asyncio.run(run())
    if caller == "license":
        assert caught.value.status_code == 502
        assert caught.value.detail == "Could not reach license server."
    elif caller == "jwks":
        assert "ID token validation failed" in str(caught.value)
    else:
        assert isinstance(caught.value, outbound_http.OutboundResponseTooLarge)
    assert not oidc_handler._discovery_cache


@pytest.mark.parametrize("kind", ["generic", "telegram"])
def test_webhook_error_handling_accepts_new_http_error_subclass(monkeypatch, kind):
    from modules.notifications import webhook_payloads

    def fail(*args, **kwargs):
        raise outbound_http.OutboundResponseTooLarge("Outbound response body exceeds size limit")

    monkeypatch.setattr(webhook_payloads, "safe_post", fail)
    monkeypatch.setattr(webhook_payloads, "trusted_post", fail)
    url = "test-token|test-chat" if kind == "telegram" else "https://webhook.test"
    assert webhook_payloads.post_typed_webhook(
        url, kind, event="test", title="Test", message="Test") is False
