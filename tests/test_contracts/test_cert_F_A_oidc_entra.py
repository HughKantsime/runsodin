"""Cert F / cluster A: OIDC/SSO via an Entra-shaped provider, auto-provisioning, redirect_uri pinning,
and unified JWT signing for OIDC sessions.

Claims: F11-11.1-02, F11-11.1-03 (OIDC half), F11-11.1-01 (OIDC exchange cookie).
The real /api/auth/oidc/login -> IdP -> /api/auth/oidc/callback -> /api/auth/oidc/exchange chain runs
against the real ``main:app`` (LiveOdin). Only the identity provider is a local stub (StubIdP), which
serves an Entra v2.0-shaped tenant discovery document, token endpoint, RS256 JWKS and userinfo.
Uvicorn runs with proxy headers enabled (as the production ``uvicorn`` CLI does by default).
"""
from __future__ import annotations

import secrets
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest

from tests.test_contracts.test_cert_F_00_live import LiveOdin
from tests.test_contracts.test_cert_F_A_00_stubs import StubIdP
from tests.test_contracts.test_cert_F_A_auth_transport import assert_production_cookie, capture

PINNED = "https://odin.example.test/api/auth/oidc/callback"
HOSTILE = "evil.example.test"


def configure(o: LiveOdin, idp: StubIdP) -> int:
    o.admin_auth = {"Authorization": "Bearer " + o.admin_token}
    org = o.post("/api/orgs", headers=o.admin_auth, json={"name": "Entra Org " + secrets.token_hex(3)})
    assert org.status_code == 200, org.text
    org_id = org.json()["id"]
    r = o.put("/api/admin/oidc", headers=o.admin_auth, json={
        "display_name": "Microsoft", "provider_type": "microsoft", "client_id": idp.client_id,
        "client_secret": idp.client_secret, "tenant_id": idp.tenant, "discovery_url": idp.discovery_url,
        "scopes": "openid profile email", "auto_create_users": True, "default_role": "viewer",
        "default_group_id": org_id, "is_enabled": True})
    assert r.status_code == 200, r.text
    return org_id


def start_login(o: LiveOdin, **headers):
    r = o.get("/api/auth/oidc/login", headers=headers or None, allow_redirects=False)
    assert r.status_code == 302, r.text
    loc = urlsplit(r.headers["location"])
    params = {k: v[0] for k, v in parse_qs(loc.query).items()}
    # The browser binding cookie set at login must accompany callback and exchange.
    params["_cookies"] = {"odin_oidc_bind": r.cookies["odin_oidc_bind"]}
    return loc, params


def full_sso(o: LiveOdin, idp: StubIdP, sub: str, email_addr: str, **headers):
    loc, params = start_login(o, **headers)
    code = idp.issue_code(params["nonce"], sub, email_addr)
    cb = o.get("/api/auth/oidc/callback", params={"code": code, "state": params["state"]},
               headers=headers or None, allow_redirects=False, cookies=params["_cookies"])
    assert cb.status_code == 302, cb.text
    target = urlsplit(cb.headers["location"])
    assert not target.query, cb.headers["location"]  # never in the query string
    q = parse_qs(target.fragment)
    assert "oidc_code" in q, cb.headers["location"]
    ex = o.post("/api/auth/oidc/exchange", json={"code": q["oidc_code"][0]}, cookies=params["_cookies"])
    return params, q["oidc_code"][0], ex


@pytest.fixture(scope="module")
def idp():
    s = StubIdP().start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def odin(tmp_path_factory, idp):
    o = LiveOdin(tmp_path_factory.mktemp("odin-oidc"), unset=("COOKIE_SECURE",),
                 env={"ODIN_CERT_PROXY_HEADERS": "1"}).start()
    o.setup_admin()
    o.org_id = configure(o, idp)
    yield o
    o.stop()


@pytest.fixture(scope="module")
def pinned(tmp_path_factory, idp):
    o = LiveOdin(tmp_path_factory.mktemp("odin-oidc-pinned"),
                 env={"ODIN_CERT_PROXY_HEADERS": "1", "OIDC_REDIRECT_URI": PINNED}).start()
    o.setup_admin()
    o.org_id = configure(o, idp)
    yield o
    o.stop()


@pytest.fixture(scope="module")
def trusted(tmp_path_factory, idp):
    o = LiveOdin(tmp_path_factory.mktemp("odin-oidc-trusted"),
                 env={"ODIN_CERT_PROXY_HEADERS": "1", "TRUSTED_HOSTS": "127.0.0.1"}).start()
    o.setup_admin()
    o.org_id = configure(o, idp)
    yield o
    o.stop()


# ---------------------------------------------------------------- F11-11.1-02 Entra SSO + provisioning
def test_entra_authorize_redirect_uses_tenant_discovery(odin, idp):
    loc, params = start_login(odin)
    d = idp.discovery()
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == d["authorization_endpoint"]
    assert params["client_id"] == idp.client_id and params["response_type"] == "code"
    assert params["scope"] == "openid profile email" and params["state"] and params["nonce"]
    assert params["redirect_uri"] == odin.url + "/api/auth/oidc/callback"
    assert any(r["path"] == urlsplit(idp.discovery_url).path for r in idp.requests)


def test_entra_sso_auto_provisions_viewer_in_default_tenant_and_issues_session(odin, idp):
    sub = "entra-" + secrets.token_hex(6)
    before = odin.sql("SELECT COUNT(*) FROM users")[0][0]
    params, oidc_code, ex = full_sso(odin, idp, sub, "Ada.Entra@Contoso.example")
    assert ex.status_code == 200, ex.text
    rows = odin.sql("SELECT id, username, email, role, group_id, oidc_issuer, oidc_subject, password_hash "
                    "FROM users WHERE oidc_subject=:s", s=sub)
    assert odin.sql("SELECT COUNT(*) FROM users")[0][0] == before + 1 and len(rows) == 1
    uid, username, email_addr, role, group_id, issuer, subject, pw_hash = rows[0]
    assert (username, email_addr, role, group_id, issuer) == ("ada.entra", "ada.entra@contoso.example", "viewer",
                                                              odin.org_id, idp.issuer)
    assert pw_hash == ""  # SSO-only account: no usable local password
    token_post = idp.token_requests()[-1]["form"]
    assert token_post["redirect_uri"] == params["redirect_uri"]
    assert token_post["grant_type"] == "authorization_code" and token_post["client_secret"] == idp.client_secret
    # production cookie defaults on OIDC exchange (F11-11.1-01)
    token = assert_production_cookie(ex)
    assert ex.json() == {"access_token": token, "token_type": "bearer"}
    capture("oidc_exchange_response", {"access_token": "<jwt>", "token_type": "bearer"})
    me = odin.get("/api/auth/me", headers={"Cookie": f"session={token}"})
    assert me.status_code == 200 and me.json()["username"] == "ada.entra" and me.json()["group_id"] == odin.org_id
    # the one-time exchange code cannot be replayed
    assert odin.post("/api/auth/oidc/exchange", json={"code": oidc_code}, cookies=params["_cookies"]).status_code == 400
    # the same Entra subject signs in again without creating another account
    _, _, again = full_sso(odin, idp, sub, "ada.entra@contoso.example")
    assert again.status_code == 200
    assert odin.sql("SELECT COUNT(*) FROM users WHERE oidc_subject=:s", s=sub)[0][0] == 1
    assert odin.sql("SELECT COUNT(*) FROM users")[0][0] == before + 1


def test_entra_callback_rejects_forged_state_and_wrong_nonce(odin, idp):
    loc, params = start_login(odin)
    code = idp.issue_code(params["nonce"], "entra-" + secrets.token_hex(4), "x@contoso.example")
    bad = odin.get("/api/auth/oidc/callback", params={"code": code, "state": "forged" + params["state"]},
                   allow_redirects=False, cookies=params["_cookies"])
    assert bad.status_code == 302 and "error=invalid_state" in bad.headers["location"]
    loc, params = start_login(odin)
    code = idp.issue_code("not-the-nonce", "entra-" + secrets.token_hex(4), "y@contoso.example")
    bad = odin.get("/api/auth/oidc/callback", params={"code": code, "state": params["state"]}, allow_redirects=False,
                   cookies=params["_cookies"])
    assert bad.status_code == 302 and "error=auth_failed" in bad.headers["location"]
    assert odin.sql("SELECT COUNT(*) FROM users WHERE email IN ('x@contoso.example','y@contoso.example')")[0][0] == 0


# ---------------------------------------------------------------- F11-11.1-03 (OIDC half)
def test_oidc_and_local_sessions_share_signing_key_algorithm_and_dependency(odin, idp):
    sub = "entra-" + secrets.token_hex(6)
    _, _, ex = full_sso(odin, idp, sub, "grace.entra@contoso.example")
    oidc_token = ex.json()["access_token"]
    local = odin.login(*odin.admin)
    assert local.status_code == 200
    local_token = local.json()["access_token"]
    for token in (oidc_token, local_token):
        assert jwt.get_unverified_header(token)["alg"] == "HS256"
        claims = jwt.decode(token, odin.jwt_secret, algorithms=["HS256"])  # same deployment key
        assert {"sub", "role", "exp", "jti", "iat"} <= set(claims)
        for headers in ({"Authorization": "Bearer " + token}, {"Cookie": f"session={token}"}):
            assert odin.get("/api/auth/me", headers=headers).status_code == 200
            assert odin.get("/api/printers", headers=headers).status_code == 200
    assert jwt.decode(oidc_token, odin.jwt_secret, algorithms=["HS256"])["sub"] == "grace.entra"
    # both are revocable through the same session machinery
    assert odin.post("/api/auth/logout", headers={"Authorization": "Bearer " + oidc_token}).status_code == 200
    assert odin.get("/api/auth/me", headers={"Authorization": "Bearer " + oidc_token}).status_code == 401


# ---------------------------------------------------------------- redirect_uri pinning
def test_configured_redirect_uri_is_pinned_against_hostile_host_and_forwarded_host(pinned, idp):
    for headers in ({"Host": HOSTILE}, {"X-Forwarded-Host": HOSTILE}, {"Host": HOSTILE, "X-Forwarded-Host": HOSTILE,
                                                                        "X-Forwarded-Proto": "https"}):
        _, params = start_login(pinned, **headers)
        assert params["redirect_uri"] == PINNED
    # the code exchange sent to the IdP also uses the pinned URI even with a hostile Host on callback
    _, params = start_login(pinned, Host=HOSTILE)
    code = idp.issue_code(params["nonce"], "entra-" + secrets.token_hex(5), "pin.user@contoso.example")
    cb = pinned.get("/api/auth/oidc/callback", params={"code": code, "state": params["state"]},
                    headers={"Host": HOSTILE}, allow_redirects=False, cookies=params["_cookies"])
    assert cb.status_code == 302 and "#oidc_code=" in cb.headers["location"]
    assert idp.token_requests()[-1]["form"]["redirect_uri"] == PINNED


def test_unconfigured_redirect_uri_ignores_forwarded_host(odin):
    _, params = start_login(odin, **{"X-Forwarded-Host": HOSTILE})
    assert params["redirect_uri"] == odin.url + "/api/auth/oidc/callback"


def test_unconfigured_redirect_uri_follows_hostile_host_header_with_default_trusted_hosts(odin):
    """Evidence for the claim_mismatch: pinning is opt-in (OIDC_REDIRECT_URI); defaults derive from Host."""
    _, params = start_login(odin, Host=HOSTILE)
    assert params["redirect_uri"] == f"http://{HOSTILE}/api/auth/oidc/callback"


def test_trusted_hosts_rejects_hostile_host_before_oidc_login(trusted):
    r = trusted.get("/api/auth/oidc/login", headers={"Host": HOSTILE}, allow_redirects=False)
    assert r.status_code == 400
    _, params = start_login(trusted)
    assert params["redirect_uri"] == trusted.url.replace("127.0.0.1", "127.0.0.1") + "/api/auth/oidc/callback"
