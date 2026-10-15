"""Cert F / cluster A: session cookie transport, Bearer + X-API-Key fallbacks, unified signing, WS tokens.

Claims: F11-11.1-01, F11-11.1-03 (local half), F11-11.1-04.
Every test drives the real ``main:app`` under uvicorn (``LiveOdin``) over real HTTP/WebSocket.
The "prod" server runs with COOKIE_SECURE / COOKIE_SAMESITE unset so the shipped defaults apply.
"""
from __future__ import annotations

import json
import os
import secrets
from http.cookies import SimpleCookie
from pathlib import Path

import jwt
import pytest
import requests
import websocket

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password

FIXTURES = os.environ.get("CERT_F_A_FIXTURES")


def capture(name: str, value):
    if FIXTURES:
        Path(FIXTURES).mkdir(parents=True, exist_ok=True)
        Path(FIXTURES, name + ".json").write_text(json.dumps(value, indent=1, sort_keys=True))


def session_cookie(resp: requests.Response) -> tuple[str, dict]:
    """Return (raw Set-Cookie header for 'session', parsed attributes)."""
    headers = [v for k, v in resp.raw.headers.items() if k.lower() == "set-cookie"]
    raw = [h for h in headers if h.startswith("session=")]
    assert len(raw) == 1, headers
    jar = SimpleCookie()
    jar.load(raw[0])
    return raw[0], jar["session"]


def assert_production_cookie(resp):
    raw, morsel = session_cookie(resp)
    attrs = [a.strip().lower() for a in raw.split(";")]
    assert "httponly" in attrs, raw
    assert "secure" in attrs, raw
    assert "samesite=strict" in attrs, raw
    assert "path=/" in attrs, raw
    return morsel.value


def make_user(o: LiveOdin, admin_auth: dict, username: str, role: str):
    password = strong_password()
    r = o.post("/api/users", headers=admin_auth,
               json={"username": username, "email": f"{username}@example.test", "password": password, "role": role})
    assert r.status_code == 200, r.text
    uid = o.sql("SELECT id FROM users WHERE username=:u", u=username)[0][0]
    return uid, password


@pytest.fixture(scope="module")
def prod(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odin-prod-cookie"), unset=("COOKIE_SECURE",)).start()
    o.setup_admin()
    o.admin_auth = {"Authorization": "Bearer " + o.admin_token}
    o.viewer_id, o.viewer_pw = make_user(o, o.admin_auth, "transport_viewer", "viewer")
    r = o.login("transport_viewer", o.viewer_pw)
    assert r.status_code == 200, r.text
    o.viewer_login = r
    yield o
    o.stop()


@pytest.fixture(scope="module")
def keyed(tmp_path_factory):
    key = secrets.token_urlsafe(32)
    o = LiveOdin(tmp_path_factory.mktemp("odin-api-key"), env={"API_KEY": key}).start()
    o.global_key = key
    o.setup_admin()
    o.admin_auth = {"Authorization": "Bearer " + o.admin_token, "X-API-Key": key}
    yield o
    o.stop()


# ---------------------------------------------------------------- F11-11.1-01 cookie
def test_password_login_sets_httponly_secure_samestrict_cookie_by_default(prod):
    r = prod.viewer_login
    token = assert_production_cookie(r)
    body = r.json()
    assert set(body) == {"access_token", "token_type"} and body["token_type"] == "bearer"
    assert body["access_token"] == token  # same JWT; browser keeps it only in the cookie (see UI test)
    capture("login_full_response", {"token_type": "bearer", "access_token": "<jwt>"})


def test_cookie_alone_authenticates_real_rest_calls(prod):
    token = session_cookie(prod.viewer_login)[1].value
    s = requests.Session()
    me = s.get(prod.url + "/api/auth/me", headers={"Cookie": f"session={token}"}, timeout=20)
    assert me.status_code == 200 and me.json()["username"] == "transport_viewer"
    capture("auth_me_viewer", me.json())
    perms = s.get(prod.url + "/api/permissions", headers={"Cookie": f"session={token}"}, timeout=20)
    assert perms.status_code == 200
    capture("permissions_viewer", perms.json())
    printers = s.get(prod.url + "/api/printers", headers={"Cookie": f"session={token}"}, timeout=20)
    assert printers.status_code == 200 and isinstance(printers.json(), list)
    # no credential at all is refused on the same route
    assert requests.get(prod.url + "/api/auth/me", timeout=20).status_code == 401


def test_logout_revokes_cookie_session(prod):
    r = prod.login("transport_viewer", prod.viewer_pw)
    token = assert_production_cookie(r)
    cookie = {"Cookie": f"session={token}"}
    assert prod.get("/api/auth/me", headers=cookie).status_code == 200
    out = prod.post("/api/auth/logout", headers=cookie)
    assert out.status_code == 200
    assert prod.get("/api/auth/me", headers=cookie).status_code == 401


def test_bearer_fallback_authenticates_real_rest_call(prod):
    token = prod.viewer_login.json()["access_token"]
    r = prod.get("/api/printers", headers={"Authorization": "Bearer " + token})
    assert r.status_code == 200 and isinstance(r.json(), list)
    me = prod.get("/api/auth/me", headers={"Authorization": "Bearer " + token})
    assert me.json()["username"] == "transport_viewer"
    assert prod.get("/api/printers", headers={"Authorization": "Bearer " + token[:-4] + "AAAA"}).status_code == 401


def test_scoped_odin_api_key_fallback_authenticates_real_rest_call(prod):
    token = prod.viewer_login.json()["access_token"]
    created = prod.post("/api/tokens", headers={"Authorization": "Bearer " + token},
                        json={"name": "cert-f-a", "scopes": ["read:printers"]})
    assert created.status_code == 200, created.text
    raw = created.json()["token"]
    assert raw.startswith("odin_")
    r = prod.get("/api/printers", headers={"X-API-Key": raw})
    assert r.status_code == 200 and isinstance(r.json(), list)
    me = prod.get("/api/auth/me", headers={"X-API-Key": raw})
    assert me.status_code == 200 and me.json()["username"] == "transport_viewer"
    assert prod.get("/api/printers", headers={"X-API-Key": "odin_" + secrets.token_urlsafe(32)}).status_code == 401


def test_global_api_key_perimeter_fallback_authenticates_real_rest_call(keyed):
    r = keyed.get("/api/printers", headers={"X-API-Key": keyed.global_key})
    assert r.status_code == 200 and isinstance(r.json(), list)
    me = keyed.get("/api/auth/me", headers={"X-API-Key": keyed.global_key})
    assert me.status_code == 200 and me.json()["role"] == "admin"
    assert keyed.get("/api/printers", headers={"X-API-Key": "wrong-" + secrets.token_hex(8)}).status_code == 401
    assert keyed.get("/api/printers").status_code == 401


def test_api_key_deployment_keeps_cookie_scoped_token_and_bearer_with_key(keyed):
    uid, pw = make_user(keyed, keyed.admin_auth, "keyed_viewer", "viewer")
    login = keyed.login("keyed_viewer", pw)
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    # browser session cookie passes the perimeter without the key
    assert keyed.get("/api/printers", headers={"Cookie": f"session={token}"}).status_code == 200
    # Bearer combined with the perimeter key resolves to the Bearer identity, not the key's admin
    me = keyed.get("/api/auth/me", headers={"Authorization": "Bearer " + token, "X-API-Key": keyed.global_key})
    assert me.status_code == 200 and me.json()["username"] == "keyed_viewer"
    # per-user scoped token passes the perimeter on its own
    created = keyed.post("/api/tokens", headers={"Cookie": f"session={token}"},
                         json={"name": "k", "scopes": ["read:printers"]})
    assert created.status_code == 200, created.text
    assert keyed.get("/api/printers", headers={"X-API-Key": created.json()["token"]}).status_code == 200
    # documented perimeter semantics: a bare Bearer header without the key is refused when API_KEY is set
    assert keyed.get("/api/printers", headers={"Authorization": "Bearer " + token}).status_code == 401


# ---------------------------------------------------------------- F11-11.1-03 (local half)
def test_local_login_token_is_hs256_signed_with_deployment_key_and_forgery_rejected(prod):
    token = prod.viewer_login.json()["access_token"]
    assert jwt.get_unverified_header(token)["alg"] == "HS256"
    claims = jwt.decode(token, prod.jwt_secret, algorithms=["HS256"])
    assert claims["sub"] == "transport_viewer" and claims["jti"]
    forged = jwt.encode({**claims, "jti": secrets.token_hex(8)}, secrets.token_hex(32), algorithm="HS256")
    assert prod.get("/api/auth/me", headers={"Authorization": "Bearer " + forged}).status_code == 401
    resigned = jwt.encode({**claims, "jti": secrets.token_hex(8)}, prod.jwt_secret, algorithm="HS256")
    assert prod.get("/api/auth/me", headers={"Authorization": "Bearer " + resigned}).status_code == 200


# ---------------------------------------------------------------- F11-11.1-04 WS tokens
def ws_url(o, token, path="/api/v1/ws"):
    return o.url.replace("http://", "ws://") + f"{path}?token={token}"


def test_ws_token_connects_websocket_and_is_refused_on_rest(prod):
    session = prod.viewer_login.json()["access_token"]
    issued = prod.post("/api/auth/ws-token", headers={"Cookie": f"session={session}"})
    assert issued.status_code == 200, issued.text
    ws_token = issued.json()["token"]
    capture("ws_token_response", {"token": "<jwt>"})
    claims = jwt.decode(ws_token, prod.jwt_secret, algorithms=["HS256"])
    assert claims["ws"] is True and claims["exp"] - claims["iat"] <= 300
    for path in ("/api/v1/ws", "/ws"):
        conn = websocket.create_connection(ws_url(prod, ws_token, path), timeout=10)
        try:
            conn.send("ping")
            assert conn.recv() == "pong"
            conn.send(json.dumps({"type": "subscribe", "channels": ["events"]}))
            assert json.loads(conn.recv()) == {"type": "subscribed", "channels": ["events"]}
        finally:
            conn.close()
    # replay on REST: Bearer, cookie and both REST-identity routes refuse it
    for headers in ({"Authorization": "Bearer " + ws_token}, {"Cookie": f"session={ws_token}"}):
        assert prod.get("/api/printers", headers=headers).status_code == 401
        assert prod.get("/api/auth/me", headers=headers).status_code == 401
    assert prod.post("/api/tokens", headers={"Authorization": "Bearer " + ws_token},
                     json={"name": "x", "scopes": ["read"]}).status_code == 401


def test_websocket_refuses_ordinary_session_token_and_no_token(prod):
    session = prod.viewer_login.json()["access_token"]
    for url in (ws_url(prod, session), prod.url.replace("http://", "ws://") + "/api/v1/ws",
                ws_url(prod, session, "/ws")):
        try:
            conn = websocket.create_connection(url, timeout=10)
        except websocket.WebSocketBadStatusException as exc:
            assert exc.status_code == 403  # close-before-accept -> handshake rejected
            continue
        try:  # if the handshake completed, the server must close at once without serving
            conn.send("ping")
            opcode, data = conn.recv_data(control_frame=True)
            assert opcode == websocket.ABNF.OPCODE_CLOSE
            assert int.from_bytes(data[:2], "big") == 4001
        finally:
            conn.close()


def test_ws_token_refused_on_rest_even_under_api_key_perimeter(keyed):
    uid, pw = make_user(keyed, keyed.admin_auth, "keyed_ws", "viewer")
    token = keyed.login("keyed_ws", pw).json()["access_token"]
    ws_token = keyed.post("/api/auth/ws-token", headers={"Cookie": f"session={token}"}).json()["token"]
    assert keyed.get("/api/printers", headers={"Cookie": f"session={ws_token}"}).status_code == 401
    assert keyed.get("/api/auth/me", headers={"Authorization": "Bearer " + ws_token,
                                               "X-API-Key": keyed.global_key}).status_code in (401,)
