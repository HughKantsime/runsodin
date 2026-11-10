"""ITAR mode + explicit identity-provider allowance (ODIN_ITAR_ALLOWED_IDP_HOSTS).

The OIDC client marks its requests with `itar.oidc_request_scope()` (a ContextVar);
`pin_for_request` reads it. No function signature carries the marker. Covers the
allowance parser, scope semantics, https/443, mixed answers, DNS pinning and
resolution count, the audit row, propagation into the outbound worker, the
discovery-endpoint check, the boot audit, GCC High (including a full fake-provider
sign-in through discovery, token exchange, JWKS and ID-token issuer validation),
and that only oidc_handler opts in.
"""

import asyncio
import socket
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from core import itar, outbound_http, webhook_utils
from modules.organizations import oidc_handler
from modules.organizations.oidc_handler import OIDCHandler, create_handler_from_config
from tests.test_contracts.test_cert_F_A_00_stubs import StubIdP

GCC = "login.microsoftonline.us"
GRAPH = "graph.microsoft.us"
PUBLIC_IP = "20.140.0.1"
TENANT = "11111111-2222-3333-4444-555555555555"
DISCOVERY = f"https://{GCC}/{TENANT}/v2.0/.well-known/openid-configuration"
ISSUER = f"https://{GCC}/{TENANT}/v2.0"
CLIENT_ID = "odin-gcc-client"
NONCE = "nonce-1"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(itar.IDP_HOSTS_ENV, raising=False)
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    monkeypatch.setattr(oidc_handler, "_discovery_cache", OrderedDict(), raising=False)


@pytest.fixture
def itar_on(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")


@pytest.fixture
def audit(monkeypatch):
    rows = []
    monkeypatch.setattr(itar, "_record_idp_outbound", lambda host, kind: rows.append((host, kind)))
    return rows


def resolver(monkeypatch, ips):
    calls = []

    def fake(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", fake)
    return calls


# ---- allowance parser ------------------------------------------------------

def test_parse_accepts_exact_hostnames_and_lowercases():
    hosts, errors = itar.parse_allowed_idp_hosts(" Login.MicrosoftOnline.us , accounts.google.com,login.microsoftonline.us")
    assert errors == []
    assert hosts == (GCC, "accounts.google.com")


@pytest.mark.parametrize("entry", [
    "login.microsoftonline.us", "graph.microsoft.us", "accounts.google.com",
    "login.microsoftonline.com", "idp-1.example.co.uk", "a.b", "xn--bcher-kva.example",
    "example.xn--p1ai", "a" * 63 + ".example.com", "idp.example.c0m",
])
def test_parse_accepts_valid_dns_hostnames(entry):
    assert itar.parse_allowed_idp_hosts(entry) == ((entry,), [])


@pytest.mark.parametrize("entry", [
    "*.microsoftonline.us", "10.0.0.5", "8.8.8.8", "[::1]", "https://login.microsoftonline.us",
    "login.microsoftonline.us:443", "login.microsoftonline.us/path", "login.microsoftonline.us.",
    "localhost", "bad_host.example.com", "-x.example.com",
    # numeric forms the resolver reads as IPv4 (8.8 is 8.0.0.8, 127.1 is 127.0.0.1)
    "8.8", "0x8.8.8.8", "010.1.1.1", "1.2.3.4", "127.1", "0x7f.1", "2130706433", "0x7f000001",
    # not proper DNS names
    "example.123", "example.com-", "x-.example.com", "a..example.com", ".example.com",
    "a" * 64 + ".example.com", "exa mple.com", "ex%61mple.com", "例え.jp",
])
def test_parse_rejects_non_exact_hostnames_and_invalid_value_fails_closed(monkeypatch, entry):
    assert itar.parse_allowed_idp_hosts(entry)[1]
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},{entry}")
    assert itar.allowed_idp_hosts() == frozenset()
    assert itar.idp_allowance_errors()


def test_parse_rejects_over_long_name():
    entry = ".".join(["a" * 60] * 5)  # 304 characters, every label valid
    assert len(entry) > 253
    assert itar.parse_allowed_idp_hosts(entry)[1]


def test_parse_enforces_maximum_of_eight():
    ok = ",".join(f"idp{i}.example.com" for i in range(8))
    assert itar.parse_allowed_idp_hosts(ok)[1] == []
    assert itar.parse_allowed_idp_hosts(ok + ",idp8.example.com")[1]


# ---- scope semantics and unchanged behaviour -------------------------------

def test_unset_env_changes_nothing_inside_scope(monkeypatch, itar_on, audit):
    calls = resolver(monkeypatch, [PUBLIC_IP])
    with itar.oidc_request_scope("token"):
        with pytest.raises(itar.ItarOutboundBlocked, match="public"):
            with itar.pin_for_request(f"https://{GCC}/x"):
                pass
    assert calls == [GCC] and audit == []


def test_itar_off_is_a_noop_even_when_listed(monkeypatch, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    calls = resolver(monkeypatch, [PUBLIC_IP])
    with itar.oidc_request_scope("token"):
        with itar.pin_for_request(f"https://{GCC}/x"):
            assert webhook_utils._dns_pin_state.get() is None
    assert calls == [] and audit == []


def test_listed_host_in_scope_is_pinned_audited_and_resolved_once(monkeypatch, itar_on, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    calls = resolver(monkeypatch, [PUBLIC_IP, PUBLIC_IP])
    with itar.oidc_request_scope("token"):
        with itar.pin_for_request(f"https://{GCC}/{TENANT}/oauth2/v2.0/token"):
            pinned = socket.getaddrinfo(GCC, 443)
            assert [info[4][0] for info in pinned] == [PUBLIC_IP]
    assert calls == [GCC], "validation is the only resolution"
    assert audit == [(GCC, "token")]
    assert webhook_utils._dns_pin_state.get() is None


def test_listed_host_without_scope_stays_refused(monkeypatch, itar_on, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    resolver(monkeypatch, [PUBLIC_IP])
    with pytest.raises(itar.ItarOutboundBlocked):
        with itar.pin_for_request(f"https://{GCC}/x"):
            pass
    assert audit == []


def test_unlisted_host_in_scope_is_refused(monkeypatch, itar_on, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    resolver(monkeypatch, [PUBLIC_IP])
    with itar.oidc_request_scope("jwks"):
        for host in ("evil.example.com", f"sub.{GCC}", f"{GCC}.evil.example.com"):
            with pytest.raises(itar.ItarOutboundBlocked):
                with itar.pin_for_request(f"https://{host}/keys"):
                    pass
    assert audit == []


@pytest.mark.parametrize("url", [f"http://{GCC}/x", f"https://{GCC}:8443/x", f"http://{GCC}:443/x"])
def test_listed_host_requires_https_443(monkeypatch, itar_on, audit, url):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    resolver(monkeypatch, [PUBLIC_IP])
    with itar.oidc_request_scope("discovery"):
        with pytest.raises(itar.ItarOutboundBlocked, match="https"):
            with itar.pin_for_request(url):
                pass
    assert audit == []


def test_mixed_private_and_public_answer_is_refused_for_listed_host(monkeypatch, itar_on, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    resolver(monkeypatch, ["10.0.0.5", PUBLIC_IP])
    with itar.oidc_request_scope("token"):
        with pytest.raises(itar.ItarOutboundBlocked, match="public"):
            with itar.pin_for_request(f"https://{GCC}/x"):
                pass
    assert audit == []


def test_literal_public_ip_is_refused_even_with_listed_host(monkeypatch, itar_on, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    with itar.oidc_request_scope("token"):
        with pytest.raises(itar.ItarOutboundBlocked, match="literal IP"):
            with itar.pin_for_request(f"https://{PUBLIC_IP}/x"):
                pass


def test_listed_host_resolving_private_needs_no_allowance_and_no_audit(monkeypatch, itar_on, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    resolver(monkeypatch, ["10.0.0.5"])
    with itar.oidc_request_scope("token"):
        with itar.pin_for_request(f"http://{GCC}:8080/x"):
            pass
    assert audit == []


def test_audit_failure_refuses_the_request(monkeypatch, itar_on):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    resolver(monkeypatch, [PUBLIC_IP])

    def boom(host, kind):
        raise itar.ItarOutboundBlocked("audit row could not be written")

    monkeypatch.setattr(itar, "_record_idp_outbound", boom)
    with itar.oidc_request_scope("token"):
        with pytest.raises(itar.ItarOutboundBlocked, match="audit"):
            with itar.pin_for_request(f"https://{GCC}/x"):
                pass
    assert webhook_utils._dns_pin_state.get() is None


def test_scope_resets_on_exit_exception_and_nesting():
    assert itar._oidc_scope.get() is None
    with itar.oidc_request_scope("token"):
        with itar.oidc_request_scope("jwks"):
            assert itar._oidc_scope.get() == "jwks"
        assert itar._oidc_scope.get() == "token"
    assert itar._oidc_scope.get() is None
    with pytest.raises(RuntimeError):
        with itar.oidc_request_scope("token"):
            raise RuntimeError("x")
    assert itar._oidc_scope.get() is None
    with pytest.raises(ValueError):
        with itar.oidc_request_scope("webhook"):
            pass


# ---- worker thread propagation --------------------------------------------

def test_scope_reaches_the_outbound_worker_and_connects_to_the_pinned_address(monkeypatch, itar_on, audit):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    calls = resolver(monkeypatch, [PUBLIC_IP])
    connections = []

    def connect(sock, address):
        connections.append(address)
        raise ConnectionRefusedError("test stops before sending traffic")

    monkeypatch.setattr(socket.socket, "connect", connect)

    async def scoped():
        with itar.oidc_request_scope("userinfo"):
            return await outbound_http.pinned_async_request("GET", f"https://{GCC}/userinfo", timeout=5)

    with pytest.raises(httpx.ConnectError):
        asyncio.run(scoped())
    assert connections == [(PUBLIC_IP, 443)]
    assert calls == [GCC]
    assert audit == [(GCC, "userinfo")]

    connections.clear()
    with pytest.raises(itar.ItarOutboundBlocked):
        asyncio.run(outbound_http.pinned_async_request("GET", f"https://{GCC}/userinfo", timeout=5))
    assert connections == []


# ---- oidc_handler marks each request kind ----------------------------------

def test_oidc_handler_wraps_every_request_kind(monkeypatch, pro_license):
    seen = []

    async def request(method, url, **kwargs):
        seen.append(itar._oidc_scope.get())
        return httpx.Response(200, json={"issuer": "i", "keys": [], "access_token": "t"},
                              request=httpx.Request(method, url))

    monkeypatch.setattr(oidc_handler, "pinned_async_request", request)
    handler = OIDCHandler("c", "s", "t", "https://cb", discovery_url="https://idp.example.com/d")

    config = {"token_endpoint": "https://idp.example.com/t", "jwks_uri": "https://idp.example.com/k", "issuer": "i"}

    async def cfg():
        return config

    async def go():
        await handler._get_oidc_config()
        monkeypatch.setattr(handler, "_get_oidc_config", cfg)
        await handler.exchange_code("code")
        with pytest.raises(ValueError):
            await handler.parse_id_token("not-a-jwt")
        monkeypatch.setattr(handler, "_get_oidc_config", lambda: _userinfo_cfg())
        await handler.get_user_info("tok")

    async def _userinfo_cfg():
        return {"userinfo_endpoint": "https://idp.example.com/u"}

    asyncio.run(go())
    assert seen == ["discovery", "token", "jwks", "userinfo"]
    assert itar._oidc_scope.get() is None


# ---- discovery endpoint hosts ----------------------------------------------

def _discovery_fake(monkeypatch, doc):
    async def request(method, url, **kwargs):
        return httpx.Response(200, json=doc, request=httpx.Request(method, url))

    monkeypatch.setattr(oidc_handler, "pinned_async_request", request)


def gcc_doc(**overrides):
    base = f"https://{GCC}/{TENANT}"
    doc = {
        "issuer": f"{base}/v2.0",
        "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
        "token_endpoint": f"{base}/oauth2/v2.0/token",
        "userinfo_endpoint": f"https://{GRAPH}/oidc/userinfo",
        "jwks_uri": f"{base}/discovery/v2.0/keys",
    }
    doc.update(overrides)
    return doc


def test_discovery_endpoints_must_be_allowed_hosts(monkeypatch, itar_on):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},{GRAPH}")
    _discovery_fake(monkeypatch, gcc_doc())
    handler = OIDCHandler("c", "s", TENANT, "https://cb", discovery_url=DISCOVERY)
    assert asyncio.run(handler._get_oidc_config())["issuer"].endswith("/v2.0")


@pytest.mark.parametrize("key", ["token_endpoint", "userinfo_endpoint", "jwks_uri"])
def test_discovery_doc_pointing_at_unlisted_host_is_refused_and_not_cached(monkeypatch, itar_on, key):
    # Both real GCC High hosts are allowed, so only the overridden endpoint is unlisted.
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},{GRAPH}")
    _discovery_fake(monkeypatch, gcc_doc(**{key: "https://attacker.example.net/x"}))
    handler = OIDCHandler("c", "s", TENANT, "https://cb", discovery_url=DISCOVERY)
    with pytest.raises(itar.ItarOutboundBlocked, match=key):
        asyncio.run(handler._get_oidc_config())
    assert DISCOVERY not in oidc_handler._discovery_cache


def test_discovery_doc_pointing_at_private_unlisted_host_is_refused(monkeypatch, itar_on):
    # A private address does not exempt an endpoint when the discovery host is listed.
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},{GRAPH}")
    _discovery_fake(monkeypatch, gcc_doc(token_endpoint="https://10.0.0.5/token"))
    handler = OIDCHandler("c", "s", TENANT, "https://cb", discovery_url=DISCOVERY)
    with pytest.raises(itar.ItarOutboundBlocked, match="token_endpoint"):
        asyncio.run(handler._get_oidc_config())


def test_cached_doc_is_rechecked(monkeypatch, itar_on):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},{GRAPH}")
    _discovery_fake(monkeypatch, gcc_doc())
    handler = OIDCHandler("c", "s", TENANT, "https://cb", discovery_url=DISCOVERY)
    asyncio.run(handler._get_oidc_config())
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)  # graph host no longer listed
    with pytest.raises(itar.ItarOutboundBlocked, match="userinfo_endpoint"):
        asyncio.run(handler._get_oidc_config())


def test_endpoint_check_is_inert_when_unset_off_or_idp_not_listed(monkeypatch):
    doc = gcc_doc(token_endpoint="https://attacker.example.net/t")
    _discovery_fake(monkeypatch, doc)
    handler = OIDCHandler("c", "s", TENANT, "https://cb", discovery_url=DISCOVERY)
    # ITAR off, env set
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    assert asyncio.run(handler._get_oidc_config()) == doc
    # ITAR on, env unset
    monkeypatch.delenv(itar.IDP_HOSTS_ENV)
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    oidc_handler._discovery_cache.clear()
    assert asyncio.run(handler._get_oidc_config()) == doc
    # ITAR on, allowance set for a different host: private-IdP path untouched here
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, "accounts.google.com")
    oidc_handler._discovery_cache.clear()
    assert asyncio.run(handler._get_oidc_config()) == doc


# ---- boot audit ------------------------------------------------------------

def test_boot_refuses_invalid_allowance_only_under_itar(monkeypatch):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, "*.example.com")
    itar.enforce_boot_config([])  # ITAR off: no-op
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    with pytest.raises(RuntimeError, match=itar.IDP_HOSTS_ENV):
        itar.enforce_boot_config([])


def test_boot_refuses_numeric_ip_form_allowance(monkeypatch, itar_on):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},8.8")
    with pytest.raises(RuntimeError, match="8.8"):
        itar.enforce_boot_config([])


def test_boot_log_lists_the_allowance(monkeypatch, itar_on, caplog):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},accounts.google.com")
    with caplog.at_level("INFO", logger="odin.itar"):
        itar.enforce_boot_config([])
    assert f"{itar.IDP_HOSTS_ENV}=accounts.google.com,{GCC}" in caplog.text


def _fake_db(monkeypatch, discovery_urls):
    import core.db as db_mod

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class DB:
        def execute(self, clause, params=None):
            if "oidc_config" in str(clause):
                return Result([(u,) for u in discovery_urls])
            return Result([])

        def close(self):
            pass

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: DB())


def test_boot_collects_enabled_idp_url_unless_https_443_on_allowed_host(monkeypatch, itar_on):
    other = "https://idp.example.com/.well-known/openid-configuration"
    insecure = f"http://{GCC}/x"
    wrong_port = f"https://{GCC}:8443/x"
    _fake_db(monkeypatch, [DISCOVERY, other, insecure, wrong_port])
    # no allowance: everything is collected (identical to before)
    assert itar.collect_db_configured_urls() == [DISCOVERY, other, insecure, wrong_port]
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    assert itar.collect_db_configured_urls() == [other, insecure, wrong_port]


def test_boot_refuses_enabled_public_idp_not_in_allowance(monkeypatch, itar_on):
    other = "https://idp.example.com/.well-known/openid-configuration"
    _fake_db(monkeypatch, [DISCOVERY, other])
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, GCC)
    resolver(monkeypatch, [PUBLIC_IP])
    with pytest.raises(RuntimeError, match="idp.example.com"):
        itar.enforce_boot_config(itar.collect_db_configured_urls())
    _fake_db(monkeypatch, [DISCOVERY])
    itar.enforce_boot_config(itar.collect_db_configured_urls())


# ---- GCC High --------------------------------------------------------------

def test_gcc_high_environment_detection_and_default_discovery():
    handler = create_handler_from_config({"discovery_url": DISCOVERY, "tenant_id": TENANT}, "https://cb")
    assert handler.discovery_url == DISCOVERY
    default = OIDCHandler("c", "s", TENANT, "https://cb", environment="gcc_high")
    assert default.discovery_url == DISCOVERY


class FakeGccHighProvider:
    """In-process GCC High provider served through httpx.MockTransport.

    Discovery, token, JWKS and userinfo are all answered by routing on the real
    request URL, so OIDCHandler's own discovery, code exchange, JWKS fetch and
    ID-token validation run unmodified. Reuses StubIdP's RSA key, JWKS and ID-token
    signing, re-pointed at the GCC High issuer. `token_issuer` overrides `iss` in
    the signed ID token while discovery keeps advertising ISSUER.
    """

    def __init__(self, token_issuer=None):
        self.signer = StubIdP(client_id=CLIENT_ID)
        self.signer.tenant = TENANT
        self.signer.issuer = ISSUER
        self.token_issuer = token_issuer
        self.requests = []

    def __call__(self, request):
        url = request.url
        self.requests.append((request.method, url.host, url.path))
        base = f"/{TENANT}"
        if request.method == "GET" and url.host == GCC and url.path == f"{base}/v2.0/.well-known/openid-configuration":
            return httpx.Response(200, json=gcc_doc())
        if request.method == "POST" and url.host == GCC and url.path == f"{base}/oauth2/v2.0/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form.get("code") != "good-code" or form.get("client_id") != CLIENT_ID:
                return httpx.Response(400, json={"error": "invalid_grant"})
            entry = {
                "sub": "u1", "nonce": NONCE, "name": "Gcc User", "email": "a@example.test",
                "extra_claims": {"iss": self.token_issuer} if self.token_issuer else {},
            }
            return httpx.Response(200, json={
                "token_type": "Bearer", "access_token": "at-1",
                "id_token": self.signer._id_token(entry),
            })
        if request.method == "GET" and url.host == GCC and url.path == f"{base}/discovery/v2.0/keys":
            return httpx.Response(200, json=self.signer.jwks())
        if request.method == "GET" and url.host == GRAPH and url.path == "/oidc/userinfo":
            if request.headers.get("Authorization") != "Bearer at-1":
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"sub": "u1", "email": "a@example.test", "not_allowed": "dropped"})
        return httpx.Response(404, json={"error": "not_found"})


def _gcc_high_sign_in_setup(monkeypatch, token_issuer=None):
    monkeypatch.setenv(itar.IDP_HOSTS_ENV, f"{GCC},{GRAPH}")
    calls = resolver(monkeypatch, [PUBLIC_IP])
    provider = FakeGccHighProvider(token_issuer)
    real_client = httpx.Client
    monkeypatch.setattr(outbound_http.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(provider), **kw))
    handler = create_handler_from_config(
        {"discovery_url": DISCOVERY, "tenant_id": TENANT, "client_id": CLIENT_ID}, "https://cb")
    handler._expected_nonce = NONCE  # as validate_state() would set it after the callback
    return provider, handler, calls


def test_gcc_high_signs_in_through_a_fake_idp_with_itar_on(monkeypatch, itar_on, audit, pro_license):
    provider, handler, calls = _gcc_high_sign_in_setup(monkeypatch)

    async def sign_in():
        tokens = await handler.exchange_code("good-code")      # discovery + token
        claims = await handler.parse_id_token(tokens["id_token"])  # JWKS + signature + issuer
        info = await handler.get_user_info(tokens["access_token"])  # userinfo on the graph host
        return claims, info

    claims, info = asyncio.run(sign_in())
    assert claims["iss"] == ISSUER
    assert claims["aud"] == CLIENT_ID and claims["sub"] == "u1" and claims["nonce"] == NONCE
    assert handler._expected_nonce is None, "nonce is consumed on success"
    assert info == {"sub": "u1", "email": "a@example.test"}
    assert provider.requests == [
        ("GET", GCC, f"/{TENANT}/v2.0/.well-known/openid-configuration"),
        ("POST", GCC, f"/{TENANT}/oauth2/v2.0/token"),
        ("GET", GCC, f"/{TENANT}/discovery/v2.0/keys"),
        ("GET", GRAPH, "/oidc/userinfo"),
    ]
    assert audit == [(GCC, "discovery"), (GCC, "token"), (GCC, "jwks"), (GRAPH, "userinfo")]
    assert calls == [GCC, GCC, GCC, GRAPH], "each request validates with exactly one resolution"


@pytest.mark.parametrize("wrong_issuer", [
    f"https://sts.windows.net/{TENANT}/",
    f"https://login.microsoftonline.com/{TENANT}/v2.0",
    f"https://{GCC}/99999999-2222-3333-4444-555555555555/v2.0",
])
def test_gcc_high_id_token_with_wrong_issuer_is_refused(monkeypatch, itar_on, audit, pro_license, wrong_issuer):
    provider, handler, _ = _gcc_high_sign_in_setup(monkeypatch, token_issuer=wrong_issuer)

    async def sign_in():
        tokens = await handler.exchange_code("good-code")
        return await handler.parse_id_token(tokens["id_token"])

    with pytest.raises(ValueError, match="ID token validation failed"):
        asyncio.run(sign_in())
    # The signature was good and the JWKS was fetched; only the issuer check failed.
    assert ("GET", GCC, f"/{TENANT}/discovery/v2.0/keys") in provider.requests
    assert handler._expected_nonce == NONCE, "a refused token must not consume the nonce"
    assert audit == [(GCC, "discovery"), (GCC, "token"), (GCC, "jwks")]
    assert all(host != GRAPH for _, host, _ in provider.requests)


def test_gcc_high_without_allowance_is_refused(monkeypatch, itar_on, audit, pro_license):
    resolver(monkeypatch, [PUBLIC_IP])
    handler = OIDCHandler("c", "s", TENANT, "https://cb", discovery_url=DISCOVERY)
    with pytest.raises(itar.ItarOutboundBlocked):
        asyncio.run(handler._get_oidc_config())
    assert audit == []


# ---- only oidc_handler opts in ---------------------------------------------

def test_only_oidc_handler_uses_oidc_request_scope():
    backend = Path(__file__).resolve().parents[2] / "backend"
    users = set()
    for path in backend.rglob("*.py"):
        rel = path.relative_to(backend).as_posix()
        if rel.startswith("core/schema/") or rel == "core/itar.py":
            continue  # preserved migration snapshots; itar.py defines it
        if "oidc_request_scope" in path.read_text(encoding="utf-8"):
            users.add(rel)
    assert users == {"modules/organizations/oidc_handler.py"}
