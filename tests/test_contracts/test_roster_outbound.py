"""D4 uses the shared streaming transport without weakening provider policy."""
import asyncio
import socket
from types import SimpleNamespace

import httpx
import pytest

from core import outbound_http, webhook_utils
from core.errors import OdinError
from modules.organizations import education_roster_providers as providers


@pytest.mark.parametrize('status,body,reason', [
    (302, b'{}', 'roster_provider_refused'),
    (503, b'{}', 'roster_provider_refused'),
    (200, b'[]', 'roster_response_invalid'),
    (200, b'not json', 'roster_provider_unavailable'),
    (200, b'x' * (2 * 1024 * 1024 + 1), 'roster_response_too_large'),
])
def test_shared_stream_preserves_roster_refusal_reasons(monkeypatch, status, body, reason):
    _transport(monkeypatch, lambda request: httpx.Response(status, content=body))
    with pytest.raises(OdinError) as error:
        asyncio.run(providers.request_json('GET', providers.GOOGLE_INFO))
    assert error.value.status == 502
    assert error.value.extra['reason'] == reason


def _transport(monkeypatch, handler):
    monkeypatch.setenv('ODIN_ITAR_MODE', '0')
    monkeypatch.setattr(webhook_utils, '_real_getaddrinfo', lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port))])
    def client(**kwargs):
        assert kwargs['trust_env'] is False
        assert 0 < kwargs['timeout'] <= 15
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(outbound_http, 'httpx', SimpleNamespace(Client=client))


def test_shared_stream_keeps_exact_limit_body_headers_and_pagination(monkeypatch):
    body = b'{"ok":true}' + b' ' * (2 * 1024 * 1024 - 11)
    def upstream(request):
        assert request.method == 'POST'
        assert request.content == b'code=fixture'
        assert request.headers['authorization'] == 'Bearer fixture'
        assert webhook_utils._dns_pin_state.get()['ips'] == ['93.184.216.34']
        return httpx.Response(200, content=body, headers={'Link': '<https://canvas.example.test/next>; rel="next"'})
    _transport(monkeypatch, upstream)
    assert asyncio.run(providers.request_json('POST', providers.GOOGLE_TOKEN,
        data={'code': 'fixture'}, headers={'Authorization': 'Bearer fixture'})) == (
            {'ok': True}, 'https://canvas.example.test/next')
    assert webhook_utils._dns_pin_state.get() is None


@pytest.mark.parametrize('error', [httpx.ConnectError, httpx.ReadTimeout, httpx.TimeoutException])
def test_shared_transport_errors_remain_provider_unavailable(monkeypatch, error):
    def upstream(request):
        raise error('fixture')
    _transport(monkeypatch, upstream)
    with pytest.raises(OdinError) as caught:
        asyncio.run(providers.request_json('GET', providers.GOOGLE_INFO))
    assert caught.value.status == 502
    assert caught.value.extra['reason'] == 'roster_provider_unavailable'


@pytest.mark.parametrize('itar,addresses', [
    ('0', ['127.0.0.1']), ('0', ['93.184.216.34', '10.0.0.1']),
    ('1', ['93.184.216.34']), ('1', ['10.0.0.1']), ('0', []),
])
def test_shared_transport_refuses_ssrf_and_itar_destinations(monkeypatch, itar, addresses):
    monkeypatch.setenv('ODIN_ITAR_MODE', itar)
    monkeypatch.setattr(webhook_utils, '_real_getaddrinfo', lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, port)) for ip in addresses])
    monkeypatch.setattr(socket.socket, 'connect', lambda *a: pytest.fail('must refuse before connecting'))
    with pytest.raises(OdinError) as caught:
        asyncio.run(providers.request_json('GET', 'https://canvas.example.test/jwks'))
    assert caught.value.status == 502
    assert caught.value.extra['reason'] == 'roster_provider_unavailable'
