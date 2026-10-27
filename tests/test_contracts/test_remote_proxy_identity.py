"""Proxy identity must reach every consumer before loopback exemptions apply."""
import asyncio
import ipaddress
import logging
import socket
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.remote_access import TrustedProxySchemeMiddleware
from tests.test_contracts.test_cert_G_harness import run_probe


@pytest.mark.parametrize("kind", ["http", "websocket"])
def test_hostname_proxy_resolution_cache_recreation_and_failure(monkeypatch, kind):
    from core import remote_access
    now = [100.0]
    addresses = ["172.20.0.3", "fd00::3"]
    calls = []
    monkeypatch.setattr(remote_access, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def resolve(host, port, family=0, type=0, proto=0, flags=0):
        assert host == "tailscale" and port is None
        assert family == socket.AF_UNSPEC and type == socket.SOCK_STREAM
        calls.append(host)
        if addresses is None:
            raise socket.gaierror("fixture name unavailable")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                 socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    seen = []
    async def downstream(scope, receive, send):
        seen.append(scope)
    middleware = TrustedProxySchemeMiddleware(downstream, "127.0.0.1,::1,tailscale")

    async def check(peer, trusted=True):
        original = {"type": kind, "scheme": "ws" if kind == "websocket" else "http",
                    "client": (peer, 1234), "headers": [
                        (b"x-forwarded-for", b"127.0.0.1, 100.101.102.103"),
                        (b"x-forwarded-proto", b"https")]}
        await middleware(original, None, None)
        assert seen[-1]["client"] == (("100.101.102.103", 0) if trusted else (peer, 1234))
        assert seen[-1]["scheme"] == (("wss" if kind == "websocket" else "https")
                                       if trusted else original["scheme"])
        assert original["client"] == (peer, 1234)

    async def scenario():
        nonlocal addresses
        await check("127.0.0.1")
        assert calls == []  # Literal peers never need DNS.
        await asyncio.gather(check("172.20.0.3"), check("fd00::3"))
        await check("172.20.0.99", False)
        assert len(calls) == 1  # Concurrent requests share one refresh.
        addresses = ["172.20.0.4"]  # Sidecar recreated; ODIN stays running.
        now[0] += remote_access.PROXY_DNS_CACHE_SECONDS - 0.1
        await check("172.20.0.4", False)
        assert len(calls) == 1
        now[0] += 0.1
        await check("172.20.0.4")
        await check("172.20.0.3", False)
        await check("fd00::3", False)
        assert len(calls) == 2
        addresses = None
        now[0] += remote_access.PROXY_DNS_CACHE_SECONDS
        await check("172.20.0.4", False)  # No stale trust on DNS failure.
        await check("172.20.0.3", False)
        await check("::1")
        assert len(calls) == 3  # Negative results are cached too.
        addresses = ["172.20.0.5"]
        now[0] += remote_access.PROXY_DNS_CACHE_SECONDS
        await check("172.20.0.5")
        assert len(calls) == 4

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unresolvable", "empty", "timeout"])
def test_hostname_unavailable_at_start_trusts_nothing(monkeypatch, failure):
    from core import remote_access
    async def scenario():
        async def resolve(*args, **kwargs):
            if failure == "unresolvable":
                raise socket.gaierror("fixture unavailable")
            if failure == "timeout":
                await asyncio.Event().wait()
            return []
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        monkeypatch.setattr(remote_access, "PROXY_DNS_TIMEOUT_SECONDS", 0.01)
        peers = remote_access.TrustedProxyAddresses("missing-sidecar")
        for ip in ["172.20.0.3", "127.0.0.1", "::1"]:
            assert not await peers.contains(ipaddress.ip_address(ip))
    asyncio.run(scenario())


@pytest.mark.parametrize("value", ["*", "172.20.0.0/16", "http://tailscale", "tailscale:8000"])
def test_proxy_configuration_rejects_wildcards_networks_and_urls(value):
    with pytest.raises(ValueError, match="IP literals or hostnames"):
        TrustedProxySchemeMiddleware(None, value)


def forwarded_scope(headers, peer="127.0.0.1", kind="http", trusted="127.0.0.1,::1"):
    seen = []

    async def downstream(scope, receive, send):
        seen.append(scope)

    original = {"type": kind, "scheme": "ws" if kind == "websocket" else "http",
                "client": (peer, 1234), "headers": headers}
    asyncio.run(TrustedProxySchemeMiddleware(downstream, trusted)(original, None, None))
    assert original["client"] == (peer, 1234)
    return seen[0]


@pytest.mark.parametrize("kind", ["http", "websocket"])
@pytest.mark.parametrize("address", ["100.101.102.103", "fd7a:115c:a1e0::1234", "8.8.8.8"])
def test_rightmost_forwarded_client_replaces_loopback(kind, address):
    scope = forwarded_scope([(b"x-forwarded-for", b"127.0.0.1, 192.168.1.2"),
                             (b"X-Forwarded-For", f"spoofed, {address} ".encode()),
                             (b"x-forwarded-proto", b"https")], kind=kind)
    assert scope["client"] == (address, 0)
    assert scope["scheme"] == ("wss" if kind == "websocket" else "https")


def test_nontrusted_peer_cannot_spoof_forwarded_client():
    scope = forwarded_scope([(b"x-forwarded-for", b"127.0.0.1"),
                             (b"x-forwarded-proto", b"https")], peer="192.0.2.1")
    assert scope["client"] == ("192.0.2.1", 1234)
    assert scope["scheme"] == "http"


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1"])
def test_internal_loopback_identity_unchanged(peer):
    assert forwarded_scope([], peer=peer)["client"] == (peer, 1234)


@pytest.mark.parametrize("xff", [None, b"", b"garbage", b"100.64.1.1,", b"100.64.1.1:123",
                                 b"[fd7a:115c:a1e0::1]", b"fe80::1%eth0", b"\xff"])
def test_missing_or_invalid_client_never_retains_loopback(xff):
    headers = [(b"x-forwarded-proto", b"https")]
    if xff is not None:
        headers.append((b"x-forwarded-for", xff))
    address = ipaddress.ip_address(forwarded_scope(headers)["client"][0])
    assert not address.is_loopback and not address.is_private


def test_xff_without_proto_still_sets_identity():
    assert forwarded_scope([(b"x-forwarded-for", b"100.64.1.2")])["client"] == ("100.64.1.2", 0)
    assert not ipaddress.ip_address(forwarded_scope([(b"x-forwarded-for", b"")])["client"][0]).is_loopback


def test_invalid_forwarding_logs_once_without_header_values(caplog):
    async def downstream(scope, receive, send):
        pass

    middleware = TrustedProxySchemeMiddleware(downstream)
    with caplog.at_level(logging.WARNING, logger="odin.api"):
        for _ in range(3):
            asyncio.run(middleware({"type": "http", "scheme": "http", "client": ("127.0.0.1", 1),
                                    "headers": [(b"x-forwarded-for", b"private-header-value")]}, None, None))
    assert len(caplog.records) == 1
    assert "private-header-value" not in caplog.text


@pytest.mark.parametrize("cidrs", [[], ["0.0.0.0/0"], ["192.0.0.9/32"]])
def test_unknown_proxy_identity_is_denied_by_any_enabled_allowlist(monkeypatch, cidrs):
    from core import ip_allowlist_cache
    from core.app import _ip_allowlist_denial
    from core.remote_access import UNKNOWN_PROXY_CLIENT
    monkeypatch.setattr(ip_allowlist_cache, "policy", lambda _: {"enabled": True, "cidrs": cidrs})
    assert _ip_allowlist_denial(None, UNKNOWN_PROXY_CLIENT) == 403


@pytest.mark.parametrize("headers", [
    {"X-Forwarded-For": "100.101.102.103", "X-Forwarded-Proto": "https"},
    {"X-Forwarded-Proto": "https"},
    {"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"},
])
@pytest.mark.parametrize("peer", ["127.0.0.1", "172.20.0.3"])
def test_camera_relay_refuses_forwarded_requests(monkeypatch, headers, peer):
    from modules.printers import camera_relay
    app = FastAPI()
    app.include_router(camera_relay.router)
    app.add_middleware(TrustedProxySchemeMiddleware, trusted="127.0.0.1,tailscale")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.20.0.3", 0))])
    monkeypatch.setattr(camera_relay, "relay_token", lambda _: "fixture-token")
    def forbidden_lookup(_):
        pytest.fail("forwarded request reached camera lookup")
    monkeypatch.setattr(camera_relay, "_stored_http_camera_url", forbidden_lookup)
    client = TestClient(app, client=(peer, 1234))
    assert client.get("/internal/camera-source/1/fixture-token", headers=headers).status_code == 404


POLICY_PROBE = r'''
from core.ip_allowlist_cache import invalidate
from core.rate_limit import limiter
seed_user('proxy-admin', 'admin')
remote = TestClient(app, client=('127.0.0.1', 1234), headers={
    'X-Forwarded-Proto': 'https', 'X-Forwarded-For': '127.0.0.1, 100.101.102.103'})
remote = login('proxy-admin', remote)
assert sql('SELECT ip_address FROM active_sessions') == [['100.101.102.103']]
assert sql('SELECT ip FROM login_attempts') == [['100.101.102.103']]
assert sql("SELECT ip_address FROM audit_logs WHERE action='auth.login'") == [['100.101.102.103']]
rejected = remote.put('/api/config/ip-allowlist', json={'enabled': True, 'cidrs': ['192.168.0.0/16']})
assert rejected.status_code == 400, rejected.text
assert '100.101.102.103' in rejected.text
sql("INSERT INTO system_config(key,value) VALUES('ip_allowlist',:value)",
    value=json.dumps({'enabled': True, 'cidrs': ['192.168.0.0/16']}))
invalidate()
for prefix in ['/api', '/api/v1']:
    r = remote.get(prefix+'/auth/me')
    assert r.status_code == 403 and r.json()['detail'] == 'IP address not allowed', r.text
    missing = TestClient(app, client=('127.0.0.1', 1234), headers={'X-Forwarded-Proto':'https'})
    assert missing.get(prefix+'/auth/me').status_code == 403
internal = TestClient(app, client=('127.0.0.1', 1234))
assert internal.get('/api/auth/me').status_code == 401
sql("UPDATE system_config SET value=:value WHERE key='ip_allowlist'",
    value=json.dumps({'enabled': True, 'cidrs': ['100.64.0.0/10']}))
invalidate()
assert remote.get('/api/auth/me').status_code == 200
from core.auth_helpers import _LOGIN_RATE_LIMIT
for _ in range(_LOGIN_RATE_LIMIT):
    sql('INSERT INTO login_attempts(ip,username,attempted_at,success) VALUES(:ip,:u,:t,0)',
        ip='100.101.102.103', u='other-account', t=time.time())
limiter.reset()
credentials = {'username':'proxy-admin', 'password':PASSWORD}
assert remote.post('/api/auth/login', data=credentials).status_code == 429
other = TestClient(app, client=('127.0.0.1', 1234), headers={
    'X-Forwarded-Proto':'https', 'X-Forwarded-For':'100.101.102.104'})
assert other.post('/api/auth/login', data=credentials).status_code == 200
assert sql('SELECT DISTINCT ip_address FROM active_sessions ORDER BY ip_address') == [
    ['100.101.102.103'], ['100.101.102.104']]
OUT['identity_policy'] = True
'''


def test_forwarded_identity_drives_allowlist_login_audit_and_sessions(tmp_path):
    result = run_probe('proxy-identity-policy', POLICY_PROBE, tmp_path)
    assert result['identity_policy'] and result['_net_attempts'] == []
