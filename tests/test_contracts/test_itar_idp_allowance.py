"""ITAR mode: explicit OIDC identity-provider allowance (ODIN_ITAR_ALLOWED_IDP_HOSTS).

DNS is monkeypatched at the resolver behind the pin, and the HTTP transport is an
httpx.MockTransport, so the real itar.pin_for_request / pinned_async_request code
runs against a fake identity provider (including a GCC High shaped tenant).
"""

import asyncio
import json
import logging
import re
import socket
import time
from collections import OrderedDict
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core import itar, outbound_http, webhook_utils
from modules.organizations import oidc_handler

pytestmark = pytest.mark.usefixtures("pro_license")

PUBLIC_IP = "203.0.113.10"
PRIVATE_IP = "10.20.30.40"
GCC_HOST = "login.microsoftonline.us"
COMMERCIAL_HOST = "login.microsoftonline.com"
EVIL_HOST = "evil.example.test"
TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "odin-test-client"
DISCOVERY = f"https://{GCC_HOST}/{TENANT}/v2.0/.well-known/openid-configuration"


# ------------------------------------------------------------------ helpers
@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    monkeypatch.delenv(itar.IDP_ALLOWANCE_ENV, raising=False)
    monkeypatch.setattr(oidc_handler, "_discovery_cache", OrderedDict(), raising=False)
    monkeypatch.setattr(oidc_handler, "_state_db_store", lambda *a, **k: None)
    monkeypatch.setattr(oidc_handler, "_state_db_cleanup", lambda: None)


@pytest.fixture
def itar_on(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")


def allow(monkeypatch, *hosts):
    monkeypatch.setenv(itar.IDP_ALLOWANCE_ENV, ",".join(hosts))


def resolve(monkeypatch, mapping, default=None):
    """Make every name resolve from `mapping` (else `default`, else NXDOMAIN)."""
    def fake(host, port, *args, **kwargs):
        ips = mapping.get(host, default)
        if ips is None:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in ips]
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", fake)


def install_transport(monkeypatch, handler):
    real_client = httpx.Client

    class MockedClient(real_client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(outbound_http.httpx, "Client", MockedClient)


def run(coro):
    return asyncio.run(coro)


class FakeSession:
    commits = 0

    def commit(self):
        FakeSession.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def audit_rows(monkeypatch):
    """Capture staged audit rows without a database."""
    import core.audit as audit_mod
    import core.db as db_mod

    rows = []
    FakeSession.commits = 0
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: FakeSession())
    monkeypatch.setattr(
        audit_mod, "log_audit",
        lambda db, action, entity_type=None, entity_id=None, details=None, ip=None:
            rows.append({"action": action, "entity_type": entity_type, "details": details}),
    )
    return rows


class FakeIdP:
    """An Entra v2.0 shaped tenant served over a MockTransport."""

    def __init__(self, host, *, offsite=(), userinfo_host=None):
        self.host = host
        self.offsite = set(offsite)  # discovery keys that point at EVIL_HOST
        self.userinfo_host = userinfo_host
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "kid-1"
        self.nonce = "test-nonce"
        self.sub = "idp-subject-1"
        self.token_issuer = None
        self.requests = []

    @property
    def issuer(self):
        return f"https://{self.host}/{TENANT}/v2.0"

    @property
    def discovery_url(self):
        return f"{self.issuer}/.well-known/openid-configuration"

    def _host_for(self, key):
        if key in self.offsite:
            return EVIL_HOST
        if key == "userinfo_endpoint" and self.userinfo_host:
            return self.userinfo_host
        return self.host

    def discovery(self):
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"https://{self.host}/{TENANT}/oauth2/v2.0/authorize",
            "token_endpoint": f"https://{self._host_for('token_endpoint')}/{TENANT}/oauth2/v2.0/token",
            "userinfo_endpoint": f"https://{self._host_for('userinfo_endpoint')}/oidc/userinfo",
            "jwks_uri": f"https://{self._host_for('jwks_uri')}/{TENANT}/discovery/v2.0/keys",
        }

    def id_token(self, **overrides):
        now = int(time.time())
        claims = {
            "iss": self.token_issuer or self.issuer, "aud": CLIENT_ID, "sub": self.sub,
            "nonce": self.nonce, "iat": now, "exp": now + 300,
            "email": "ada@contoso.example",
        }
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def jwks(self):
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

    @staticmethod
    def kind(path):
        for suffix, kind in (("/.well-known/openid-configuration", "discovery"),
                             ("/oauth2/v2.0/token", "token"),
                             ("/discovery/v2.0/keys", "jwks"),
                             ("/oidc/userinfo", "userinfo")):
            if path.endswith(suffix):
                return kind
        return "unknown"

    def handle(self, request):
        pin = webhook_utils._dns_pin_state.get()
        kind = self.kind(request.url.path)
        self.requests.append({
            "kind": kind, "method": request.method, "host": request.url.host,
            "scheme": request.url.scheme, "port": request.url.port,
            "pin": dict(pin) if pin else None,
        })
        if kind == "discovery":
            return httpx.Response(200, json=self.discovery())
        if kind == "token":
            return httpx.Response(200, json={"access_token": "at-secret", "token_type": "Bearer",
                                             "id_token": self.id_token()})
        if kind == "jwks":
            return httpx.Response(200, json=self.jwks())
        if kind == "userinfo":
            return httpx.Response(200, json={"sub": self.sub, "email": "ada@contoso.example"})
        return httpx.Response(404)

    def install(self, monkeypatch):
        install_transport(monkeypatch, self.handle)


def make_handler(idp):
    return oidc_handler.OIDCHandler(
        CLIENT_ID, "s3cret", TENANT, "https://odin.example.test/api/auth/oidc/callback",
        discovery_url=idp.discovery_url,
    )


def setup_itar_idp(monkeypatch, host=GCC_HOST, allowed=(GCC_HOST,), **idp_kwargs):
    idp = FakeIdP(host, **idp_kwargs)
    if allowed:
        allow(monkeypatch, *allowed)
    resolve(monkeypatch, {}, default=[PUBLIC_IP])
    idp.install(monkeypatch)
    return idp, make_handler(idp)


# ------------------------------------------------------------ env parsing
@pytest.mark.parametrize("raw, hosts", [
    (None, []), ("", []), ("  ", []),
    (GCC_HOST, [GCC_HOST]),
    (f"{GCC_HOST}, graph.microsoft.us ,{GCC_HOST}", [GCC_HOST, "graph.microsoft.us"]),
    (",".join(f"h{i}.example.test" for i in range(8)), [f"h{i}.example.test" for i in range(8)]),
])
def test_parse_allowance_accepts_exact_lowercase_hostnames(raw, hosts):
    assert itar.parse_idp_allowance(raw) == (hosts, [])


@pytest.mark.parametrize("raw", [
    "*.microsoftonline.us", f"{GCC_HOST},*", "10.0.0.5", "127.1", "[::1]", "2001:db8::1",
    "Login.MicrosoftOnline.us", f"https://{GCC_HOST}", f"{GCC_HOST}:443", f"{GCC_HOST}/",
    f"{GCC_HOST}.", f"{GCC_HOST},,graph.microsoft.us", f"{GCC_HOST},", "bad_host.example.test",
    "-bad.example.test", "user@host.example.test", "two words.example.test",
    ",".join(f"h{i}.example.test" for i in range(9)),
])
def test_parse_allowance_rejects_invalid_entries(raw):
    hosts, errors = itar.parse_idp_allowance(raw)
    assert errors, f"{raw!r} should be rejected"


def test_invalid_allowance_refuses_boot_when_itar_on(itar_on, monkeypatch):
    monkeypatch.setenv(itar.IDP_ALLOWANCE_ENV, f"{GCC_HOST},*.example.test,10.0.0.5")
    with pytest.raises(RuntimeError) as exc:
        itar.enforce_boot_config([])
    text = str(exc.value)
    assert itar.IDP_ALLOWANCE_ENV in text and "wildcards" in text and "IP addresses" in text


def test_invalid_allowance_is_ignored_when_itar_off(monkeypatch):
    monkeypatch.setenv(itar.IDP_ALLOWANCE_ENV, "*.example.test")
    itar.enforce_boot_config([])
    assert itar.idp_allowed_hosts() == frozenset()


def test_invalid_allowance_fails_closed_at_runtime(itar_on, monkeypatch):
    allow(monkeypatch, GCC_HOST, "*")
    resolve(monkeypatch, {}, default=[PUBLIC_IP])
    assert itar.idp_allowed_hosts() == frozenset()
    with pytest.raises(itar.ItarOutboundBlocked):
        with itar.pin_for_request(DISCOVERY, purpose="oidc"):
            pytest.fail("a rejected allowance must not pin a public host")


# ------------------------------------------------------------ pin_for_request
def test_pin_allows_public_for_listed_oidc_host(itar_on, monkeypatch):
    allow(monkeypatch, GCC_HOST)
    resolve(monkeypatch, {GCC_HOST: [PUBLIC_IP]})
    with itar.pin_for_request(DISCOVERY, purpose="oidc"):
        assert webhook_utils._dns_pin_state.get() == {
            "hostname": GCC_HOST, "port": 443, "ips": [PUBLIC_IP]}


def test_pin_allows_listed_host_that_resolves_private(itar_on, monkeypatch):
    allow(monkeypatch, GCC_HOST)
    resolve(monkeypatch, {GCC_HOST: [PRIVATE_IP]})
    with itar.pin_for_request(DISCOVERY, purpose="oidc"):
        assert webhook_utils._dns_pin_state.get()["ips"] == [PRIVATE_IP]


@pytest.mark.parametrize("url, purpose, allowance", [
    (DISCOVERY, None, GCC_HOST),                                   # every other caller
    (DISCOVERY, "webhook", GCC_HOST),                              # unknown purpose is not oidc
    (DISCOVERY, "oidc", None),                                     # no allowance
    (DISCOVERY, "oidc", COMMERCIAL_HOST),                          # a different host is listed
    (DISCOVERY.replace("https://", "http://"), "oidc", GCC_HOST),  # https only
    (f"https://{GCC_HOST}:8443/x", "oidc", GCC_HOST),              # port 443 only
    (f"https://user:pw@{GCC_HOST}/x", "oidc", GCC_HOST),           # no URL credentials
    (f"https://sub.{GCC_HOST}/x", "oidc", GCC_HOST),               # exact match, no subdomains
    (f"https://{GCC_HOST}./x", "oidc", GCC_HOST),                  # exact match, no trailing dot
    ("https://8.8.8.8/", "oidc", GCC_HOST),                        # IP literals never match
])
def test_pin_refuses_public_unless_oidc_and_exactly_listed(itar_on, monkeypatch, url, purpose, allowance):
    if allowance:
        allow(monkeypatch, allowance)
    resolve(monkeypatch, {}, default=[PUBLIC_IP])
    with pytest.raises(itar.ItarOutboundBlocked):
        with itar.pin_for_request(url, purpose=purpose):
            pytest.fail("public destination was pinned")


def test_pin_refuses_mixed_private_and_public_answer_for_listed_host(itar_on, monkeypatch):
    allow(monkeypatch, GCC_HOST)
    resolve(monkeypatch, {GCC_HOST: [PUBLIC_IP, PRIVATE_IP]})
    with pytest.raises(itar.ItarOutboundBlocked, match="mixed"):
        with itar.pin_for_request(DISCOVERY, purpose="oidc"):
            pytest.fail("mixed answer was pinned")


def test_pinned_request_does_not_follow_redirects(itar_on, monkeypatch):
    allow(monkeypatch, GCC_HOST)
    resolve(monkeypatch, {GCC_HOST: [PUBLIC_IP]}, default=[PUBLIC_IP])
    seen = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": f"https://{EVIL_HOST}/steal"})

    install_transport(monkeypatch, handler)
    resp = run(outbound_http.pinned_async_request("GET", DISCOVERY, purpose="oidc"))
    assert resp.status_code == 302 and seen == [GCC_HOST]


def test_unknown_purpose_is_rejected_by_the_request_helper():
    with pytest.raises(ValueError):
        run(outbound_http.pinned_async_request("GET", DISCOVERY, purpose="webhook"))


# ------------------------------------- other callers stay refused (same host)
OTHER_HOSTS = ("hooks.example.test", "license.example.test", "spoolman.example.test",
               "classroom.example.test", "smtp.example.test", "mqtt.example.test",
               "api.telegram.org")


def other_callers_setup(monkeypatch):
    allow(monkeypatch, *OTHER_HOSTS)
    resolve(monkeypatch, {}, default=[PUBLIC_IP])


def test_webhook_callers_refuse_an_allowed_public_host(itar_on, monkeypatch):
    other_callers_setup(monkeypatch)
    with pytest.raises(webhook_utils.WebhookSSRFError):
        webhook_utils.safe_post("https://hooks.example.test/hook", json={})
    with pytest.raises(webhook_utils.WebhookSSRFError):
        webhook_utils.trusted_post("https://api.telegram.org/botX/sendMessage", json={})


@pytest.mark.parametrize("host", ["license.example.test", "spoolman.example.test", "classroom.example.test"])
def test_license_spoolman_classroom_requests_refuse_an_allowed_public_host(itar_on, monkeypatch, host):
    """These callers use pinned_async_request without a purpose."""
    other_callers_setup(monkeypatch)
    install_transport(monkeypatch, lambda request: pytest.fail("request reached the network"))
    with pytest.raises(itar.ItarOutboundBlocked):
        run(outbound_http.pinned_async_request("GET", f"https://{host}/api"))


@pytest.mark.parametrize("host, label", [("smtp.example.test", "smtp"), ("mqtt.example.test", "mqtt")])
def test_smtp_and_mqtt_refuse_an_allowed_public_host(itar_on, monkeypatch, host, label):
    other_callers_setup(monkeypatch)
    with pytest.raises(itar.ItarOutboundBlocked):
        itar.enforce_host_destination(host, label)


def test_boot_audit_of_non_oidc_urls_ignores_the_allowance(itar_on, monkeypatch):
    other_callers_setup(monkeypatch)
    for url in ("https://hooks.example.test/hook", "smtp://smtp.example.test:25",
                "mqtt://mqtt.example.test:1883", DISCOVERY.replace(GCC_HOST, "hooks.example.test")):
        with pytest.raises(RuntimeError):
            itar.enforce_boot_config([url])
    # an untagged URL is not an identity-provider discovery URL, even for a listed host
    allow(monkeypatch, GCC_HOST)
    with pytest.raises(RuntimeError):
        itar.enforce_boot_config([DISCOVERY])


def test_only_the_oidc_handler_passes_the_oidc_purpose():
    backend = Path(itar.__file__).resolve().parents[1]
    pattern = re.compile(r"purpose\s*=\s*(?:itar\.)?(?:PURPOSE_OIDC|[\"']oidc[\"'])")
    offenders = sorted(
        str(path.relative_to(backend)) for path in backend.rglob("*.py")
        if pattern.search(path.read_text(encoding="utf-8", errors="ignore"))
        and path.name != "oidc_handler.py"
        # itar.py defines and documents the purpose; it never passes it.
        and str(path.relative_to(backend)) != "core/itar.py"
    )
    assert offenders == []


# ------------------------------------------------------------ boot audit
def test_boot_refuses_enabled_public_idp_that_is_not_allowed(itar_on, monkeypatch):
    resolve(monkeypatch, {}, default=[PUBLIC_IP])
    with pytest.raises(RuntimeError, match=GCC_HOST):
        itar.enforce_boot_config([itar.IdpDiscoveryUrl(DISCOVERY)])
    allow(monkeypatch, COMMERCIAL_HOST)  # a different host listed
    with pytest.raises(RuntimeError, match=GCC_HOST):
        itar.enforce_boot_config([itar.IdpDiscoveryUrl(DISCOVERY)])


def test_boot_passes_for_allowed_idp_and_logs_the_allowance(itar_on, monkeypatch, caplog):
    allow(monkeypatch, GCC_HOST)
    resolve(monkeypatch, {}, default=[PUBLIC_IP])
    with caplog.at_level(logging.INFO, logger="odin.itar"):
        itar.enforce_boot_config([itar.IdpDiscoveryUrl(DISCOVERY)])
    assert GCC_HOST in caplog.text and itar.IDP_ALLOWANCE_ENV in caplog.text


def test_boot_passes_for_private_idp_without_allowance(itar_on, monkeypatch):
    itar.enforce_boot_config([itar.IdpDiscoveryUrl("https://10.1.2.3/realms/x/.well-known/openid-configuration")])


def test_boot_refuses_listed_idp_over_http_or_odd_port(itar_on, monkeypatch):
    allow(monkeypatch, GCC_HOST)
    resolve(monkeypatch, {}, default=[PUBLIC_IP])
    for url in (DISCOVERY.replace("https://", "http://"), DISCOVERY.replace(GCC_HOST, f"{GCC_HOST}:8443")):
        with pytest.raises(RuntimeError):
            itar.enforce_boot_config([itar.IdpDiscoveryUrl(url)])


def test_collect_tags_the_oidc_discovery_url(monkeypatch):
    import core.db as db_mod

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class DB:
        def execute(self, clause, params=None):
            return Result([(DISCOVERY,)] if "oidc_config" in str(clause) else [])

        def close(self):
            pass

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: DB())
    urls = itar.collect_db_configured_urls()
    assert urls == [DISCOVERY] and isinstance(urls[0], itar.IdpDiscoveryUrl)


# -------------------------------------------- sign-in through a fake IdP
def test_allowed_idp_signs_in_through_fake_idp_under_itar(itar_on, monkeypatch, audit_rows):
    idp, handler = setup_itar_idp(monkeypatch)

    async def flow():
        url, _state = await handler.get_authorization_url()
        tokens = await handler.exchange_code("auth-code")
        handler._expected_nonce = idp.nonce
        claims = await handler.parse_id_token(tokens["id_token"])
        info = await handler.get_user_info(tokens["access_token"])
        return url, claims, info

    url, claims, info = run(flow())
    assert url.startswith(f"https://{GCC_HOST}/{TENANT}/oauth2/v2.0/authorize?")
    assert claims["sub"] == idp.sub and info["email"] == "ada@contoso.example"
    kinds = ["discovery", "token", "jwks", "userinfo"]  # discovery is cached afterwards
    assert [r["kind"] for r in idp.requests] == kinds
    for request in idp.requests:
        assert request["host"] == GCC_HOST and request["scheme"] == "https"
        assert request["port"] in (None, 443)
        assert request["pin"] == {"hostname": GCC_HOST, "port": 443, "ips": [PUBLIC_IP]}
    assert audit_rows == [
        {"action": "itar.idp_outbound", "entity_type": "system",
         "details": {"host": GCC_HOST, "endpoint_kind": kind}}
        for kind in kinds
    ]
    assert FakeSession.commits == len(kinds)


@pytest.mark.parametrize("allowed", [(), (COMMERCIAL_HOST,)])
def test_without_the_allowance_the_idp_is_refused(itar_on, monkeypatch, audit_rows, allowed):
    idp, handler = setup_itar_idp(monkeypatch, allowed=allowed)
    with pytest.raises(itar.ItarOutboundBlocked, match=itar.IDP_ALLOWANCE_ENV):
        run(handler._get_oidc_config())
    assert idp.requests == [] and audit_rows == []


@pytest.mark.parametrize("key", ["token_endpoint", "userinfo_endpoint", "jwks_uri"])
def test_discovery_document_endpoint_on_an_unlisted_host_is_refused(itar_on, monkeypatch, audit_rows, key):
    idp, handler = setup_itar_idp(monkeypatch, offsite={key})
    with pytest.raises(itar.ItarOutboundBlocked, match=key):
        run(handler._get_oidc_config())
    assert [r["kind"] for r in idp.requests] == ["discovery"]
    assert all(r["host"] == GCC_HOST for r in idp.requests)
    assert len(oidc_handler._discovery_cache) == 0
    assert [r["details"]["endpoint_kind"] for r in audit_rows] == ["discovery"]


def test_token_request_is_never_sent_to_an_unlisted_token_endpoint(itar_on, monkeypatch, audit_rows):
    idp, handler = setup_itar_idp(monkeypatch, offsite={"token_endpoint"})
    with pytest.raises(itar.ItarOutboundBlocked):
        run(handler.exchange_code("auth-code"))
    assert [r["kind"] for r in idp.requests] == ["discovery"]


def test_endpoints_on_a_second_listed_host_are_allowed(itar_on, monkeypatch, audit_rows):
    idp, handler = setup_itar_idp(
        monkeypatch, allowed=(GCC_HOST, "graph.microsoft.us"), userinfo_host="graph.microsoft.us")
    info = run(handler.get_user_info("at-secret"))
    assert info["sub"] == idp.sub
    assert [r["host"] for r in idp.requests] == [GCC_HOST, "graph.microsoft.us"]
    assert [r["details"]["host"] for r in audit_rows] == [GCC_HOST, "graph.microsoft.us"]


def test_cached_discovery_document_is_rechecked_under_itar(monkeypatch, audit_rows):
    idp, handler = setup_itar_idp(monkeypatch, offsite={"jwks_uri"})
    assert run(handler._get_oidc_config())["jwks_uri"].startswith(f"https://{EVIL_HOST}/")  # ITAR off
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    with pytest.raises(itar.ItarOutboundBlocked, match="jwks_uri"):
        run(handler._get_oidc_config())


def test_audit_failure_refuses_the_request(itar_on, monkeypatch):
    import core.audit as audit_mod
    import core.db as db_mod

    idp, handler = setup_itar_idp(monkeypatch)
    monkeypatch.setattr(db_mod, "SessionLocal", lambda: FakeSession())

    def boom(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr(audit_mod, "log_audit", boom)
    with pytest.raises(itar.ItarOutboundBlocked, match="audit"):
        run(handler._get_oidc_config())
    assert idp.requests == []


def test_itar_off_behaviour_is_unchanged(monkeypatch, audit_rows):
    monkeypatch.setenv(itar.IDP_ALLOWANCE_ENV, "*.invalid")  # ignored while ITAR mode is off
    idp, handler = setup_itar_idp(monkeypatch, allowed=())
    monkeypatch.setenv(itar.IDP_ALLOWANCE_ENV, "*.invalid")
    config = run(handler._get_oidc_config())
    assert config["issuer"] == idp.issuer
    assert audit_rows == [] and idp.requests[0]["pin"] is None


# ------------------------------------------------------------ GCC High
def test_gcc_high_environment_is_detected_from_the_discovery_url():
    base = {"client_id": CLIENT_ID, "client_secret_encrypted": None, "tenant_id": TENANT}
    gcc = oidc_handler.create_handler_from_config({**base, "discovery_url": DISCOVERY}, "https://odin.example.test/cb")
    commercial = oidc_handler.create_handler_from_config(
        {**base, "discovery_url": DISCOVERY.replace(GCC_HOST, COMMERCIAL_HOST)}, "https://odin.example.test/cb")
    assert gcc.environment == "gcc_high" and gcc.discovery_url == DISCOVERY
    assert commercial.environment == "commercial"


def test_gcc_high_template_is_used_without_a_discovery_url():
    handler = oidc_handler.OIDCHandler(CLIENT_ID, "s3cret", TENANT, "https://odin.example.test/cb",
                                       environment="gcc_high")
    assert handler.discovery_url == DISCOVERY


def test_gcc_high_id_token_issuer_is_validated(itar_on, monkeypatch, audit_rows):
    idp, handler = setup_itar_idp(monkeypatch)
    handler._expected_nonce = idp.nonce
    claims = run(handler.parse_id_token(idp.id_token()))
    assert claims["iss"] == f"https://{GCC_HOST}/{TENANT}/v2.0"

    idp.token_issuer = f"https://{COMMERCIAL_HOST}/{TENANT}/v2.0"  # commercial issuer on a GCC High tenant
    handler._expected_nonce = idp.nonce
    with pytest.raises(ValueError, match="ID token validation failed"):
        run(handler.parse_id_token(idp.id_token()))


def test_gcc_high_passes_with_the_allowance_and_is_refused_without_it(itar_on, monkeypatch, audit_rows):
    idp, handler = setup_itar_idp(monkeypatch)
    assert run(handler._get_oidc_config())["issuer"] == f"https://{GCC_HOST}/{TENANT}/v2.0"
    monkeypatch.delenv(itar.IDP_ALLOWANCE_ENV)
    oidc_handler._discovery_cache.clear()
    with pytest.raises(itar.ItarOutboundBlocked):
        run(handler._get_oidc_config())
