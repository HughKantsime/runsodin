"""Cert F / cluster C: authentication security claims F12-12.1-01/02/04/07 on a real ODIN server.

One real server (production bootstrap + main:app under uvicorn) is shared by this module. uvicorn is
started with proxy-header trust for loopback (ODIN_CERT_PROXY_HEADERS=1), so each test drives a distinct
synthetic client IP through ``X-Forwarded-For``: slowapi buckets and the persisted login throttle are
keyed on that resolved client address. A second real server without proxy trust checks that a client
cannot spoof its recorded address. Time travel for rolling windows is done only by ageing the real
``login_attempts`` rows (the server reads ``time.time()``); everything else is real HTTP.

Tests are order-dependent by design (module-scoped server, one restart); pytest keeps file order.
"""
from __future__ import annotations

import secrets
import time

import pytest
import requests

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password

DB_THROTTLE = "Too many login attempts. Try again in 5 minutes."
LOCKED = "Account temporarily locked due to repeated failed attempts. Try again in 15 minutes."


def execute(o, statement: str, **params) -> None:
    from sqlalchemy import text
    with o.engine.begin() as c:
        c.execute(text(statement), params)


def xff(ip: str) -> dict:
    return {"X-Forwarded-For": ip}


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odin_auth"), env={"ODIN_CERT_PROXY_HEADERS": "1"}).start()
    o.setup_admin()
    r = o.login(*o.admin, **xff("198.51.100.250"))
    assert r.status_code == 200, r.text
    o.cached_admin = {"Authorization": "Bearer " + r.json()["access_token"]}
    o.users = {}
    for name, role in (("cert_c_succ", "operator"), ("cert_c_lock", "viewer"), ("cert_c_rate", "viewer"),
                       ("cert_c_sess", "viewer"), ("cert_c_upload", "operator")):
        pw = strong_password()
        body = {"username": name, "email": f"{name}@example.test", "password": pw, "role": role}
        cr = o.post("/api/users", json=body, headers=o.cached_admin)
        assert cr.status_code in (200, 201), cr.text
        uid = o.sql("SELECT id FROM users WHERE username=:u", u=name)[0][0]
        o.users[name] = (pw, uid)
    yield o
    o.stop()


def failed(o, ip, username=None):
    return o.login(username or ("ghost_" + secrets.token_hex(4)), "Wrong" + secrets.token_hex(4) + "x1", **xff(ip))


# ---------------------------------------------------------------------------------------------
# Phase 1 (before restart)
# ---------------------------------------------------------------------------------------------

def test_p1_ten_failures_from_one_ip_are_each_rejected_401(odin):
    for _ in range(10):
        r = failed(odin, "203.0.113.11")
        assert r.status_code == 401, r.text
    assert odin.sql("SELECT COUNT(*) FROM login_attempts WHERE ip='203.0.113.11' AND success=0")[0][0] == 10


def test_p1_successes_then_failures_seed_success_exclusion(odin):
    pw, _ = odin.users["cert_c_succ"]
    for _ in range(5):
        assert odin.login("cert_c_succ", pw, **xff("203.0.113.12")).status_code == 200
    for _ in range(5):
        assert failed(odin, "203.0.113.12").status_code == 401
    rows = dict(odin.sql("SELECT success, COUNT(*) FROM login_attempts WHERE ip='203.0.113.12' GROUP BY success"))
    assert rows == {0: 5, 1: 5}


def test_p1_window_seed_two_ips(odin):
    for ip in ("203.0.113.13", "203.0.113.14"):
        for _ in range(10):
            assert failed(odin, ip).status_code == 401


def test_p1_five_failed_passwords_lock_account_even_with_correct_password(odin):
    pw, _ = odin.users["cert_c_lock"]
    for _ in range(5):
        assert odin.login("cert_c_lock", "Wrong" + secrets.token_hex(3) + "x9", **xff("203.0.113.21")).status_code == 401
    r = odin.login("cert_c_lock", pw, **xff("203.0.113.21"))
    assert r.status_code == 423 and r.json()["detail"] == LOCKED
    # Lockout is per account, not per IP: a different client address is also refused.
    r2 = odin.login("cert_c_lock", pw, **xff("203.0.113.22"))
    assert r2.status_code == 423
    # Attempts refused while locked are not recorded as new failures.
    assert odin.sql("SELECT COUNT(*) FROM login_attempts WHERE username='cert_c_lock' AND success=0")[0][0] == 5


def test_p1_four_failures_do_not_lock(odin):
    name = "cert_c_rate"
    pw, _ = odin.users[name]
    for _ in range(4):
        assert odin.login(name, "Wrong" + secrets.token_hex(3) + "x9", **xff("203.0.113.23")).status_code == 401
    assert odin.login(name, pw, **xff("203.0.113.23")).status_code == 200


def test_p1_slowapi_login_limit_is_10_per_minute_and_counts_successes(odin):
    pw, _ = odin.users["cert_c_succ"]
    codes = [odin.login("cert_c_succ", pw, **xff("203.0.113.31")).status_code for _ in range(10)]
    assert codes == [200] * 10
    r = odin.login("cert_c_succ", pw, **xff("203.0.113.31"))
    assert r.status_code == 429
    assert "10 per 1 minute" in r.text
    # The 11th request never reached the handler: no extra attempt row was written.
    assert odin.sql("SELECT COUNT(*) FROM login_attempts WHERE ip='203.0.113.31'")[0][0] == 10


def test_p1_slowapi_mfa_verify_limit_is_10_per_minute(odin):
    codes = [odin.post("/api/auth/mfa/verify", json={"mfa_token": "x", "code": "1"}, headers=xff("203.0.113.32")).status_code
             for _ in range(11)]
    assert all(c in (400, 401) for c in codes[:10]), codes
    assert codes[10] == 429


def test_p1_slowapi_ws_token_limit_is_10_per_minute(odin):
    h = {**odin.cached_admin, **xff("203.0.113.33")}
    codes = [odin.post("/api/auth/ws-token", headers=h).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


@pytest.mark.parametrize("path,body", [
    ("/api/auth/forgot-password", {"email": "nobody@example.test"}),
    ("/api/auth/reset-password", {"token": "not-a-token", "new_password": "Abcdefg1"}),
])
def test_p1_password_recovery_auth_routes_are_limited_at_5_not_10_per_minute(odin, path, body):
    ip = "203.0.113.%d" % (34 if "forgot" in path else 35)
    codes = [odin.post(path, json=body, headers=xff(ip)).status_code for _ in range(6)]
    assert 429 not in codes[:5], codes
    assert codes[5] == 429, codes  # 6th request inside one minute: the limit is 5/min, not 10/min


def test_p1_print_file_upload_limit_is_30_per_minute(odin):
    pw, _ = odin.users["cert_c_upload"]
    tok = odin.login("cert_c_upload", pw, **xff("203.0.113.36")).json()["access_token"]
    h = {"Authorization": "Bearer " + tok, **xff("203.0.113.36")}
    codes = [odin.post("/api/print-files/upload", files={"file": ("x.txt", b"synthetic", "text/plain")}, headers=h).status_code
             for _ in range(31)]
    assert codes[:30] == [400] * 30, codes
    assert codes[30] == 429


def test_p1_no_global_default_limit_on_undecorated_api_routes(odin):
    h = {**odin.cached_admin, **xff("203.0.113.37")}
    codes = [odin.get("/api/auth/me", headers=h).status_code for _ in range(45)]
    assert codes == [200] * 45  # far above 10 and 30 per minute: limits are per-route, not global


def test_p1_login_audit_trail_records_client_ip_via_real_api(odin):
    r = odin.get("/api/audit-logs", params={"q": "203.0.113.11", "limit": 500}, headers=odin.cached_admin)
    assert r.status_code == 200, r.text
    logs = [e for e in r.json()["logs"] if e["ip_address"] == "203.0.113.11"]
    actions = {e["action"] for e in logs}
    assert {"login_failed", "auth.login_failed"} <= actions
    r2 = odin.get("/api/audit-logs", params={"action": "login_success", "limit": 500}, headers=odin.cached_admin)
    assert any(e["ip_address"] == "203.0.113.12" for e in r2.json()["logs"])
    r3 = odin.get("/api/audit-logs", params={"action": "auth.login", "limit": 500}, headers=odin.cached_admin)
    assert any(e["ip_address"] == "203.0.113.12" for e in r3.json()["logs"])


def test_p1_password_change_revokes_every_existing_session_and_is_audited(odin):
    name = "cert_c_sess"
    pw, uid = odin.users[name]
    t1 = odin.login(name, pw, **xff("203.0.113.41")).json()["access_token"]
    s2 = requests.Session()
    r = s2.post(odin.url + "/api/auth/login", data={"username": name, "password": pw}, headers=xff("203.0.113.42"), timeout=20)
    assert r.status_code == 200 and s2.cookies.get("session")
    assert odin.get("/api/auth/me", headers={"Authorization": "Bearer " + t1}).status_code == 200
    assert s2.get(odin.url + "/api/auth/me", timeout=20).status_code == 200
    new_pw = strong_password()
    ch = odin.patch(f"/api/users/{uid}", json={"password": new_pw}, headers=odin.cached_admin)
    assert ch.status_code == 200, ch.text
    assert odin.get("/api/auth/me", headers={"Authorization": "Bearer " + t1}).status_code == 401
    assert s2.get(odin.url + "/api/auth/me", timeout=20).status_code == 401  # cookie session revoked too
    assert odin.sql("SELECT COUNT(*) FROM active_sessions WHERE user_id=:u", u=uid)[0][0] == 0
    assert odin.login(name, pw, **xff("203.0.113.43")).status_code == 401
    assert odin.login(name, new_pw, **xff("203.0.113.43")).status_code == 200
    a = odin.get("/api/audit-logs", params={"action": "user.password_changed", "limit": 50}, headers=odin.cached_admin)
    assert any(e["entity_id"] == uid for e in a.json()["logs"])
    odin.users[name] = (new_pw, uid)


def test_p1_self_password_change_also_revokes_the_calling_session(odin):
    ip = "203.0.113.44"
    admin_tok = odin.login(*odin.admin, **xff(ip)).json()["access_token"]
    h = {"Authorization": "Bearer " + admin_tok}
    admin_id = odin.sql("SELECT id FROM users WHERE username=:u", u=odin.admin[0])[0][0]
    other = odin.login(*odin.admin, **xff(ip)).json()["access_token"]
    new_pw = strong_password()
    assert odin.patch(f"/api/users/{admin_id}", json={"password": new_pw}, headers=h).status_code == 200
    assert odin.get("/api/auth/me", headers=h).status_code == 401  # caller's own session is revoked, not re-issued
    assert odin.get("/api/auth/me", headers={"Authorization": "Bearer " + other}).status_code == 401
    assert odin.get("/api/auth/me", headers=odin.cached_admin).status_code == 401
    odin.admin = (odin.admin[0], new_pw)
    odin.cached_admin = {"Authorization": "Bearer " + odin.login(*odin.admin, **xff("198.51.100.251")).json()["access_token"]}


def test_p1_password_reset_revokes_sessions_and_is_audited(odin):
    from modules.organizations.password_reset import reset_token_digest
    name = "cert_c_sess"
    pw, uid = odin.users[name]
    tok = odin.login(name, pw, **xff("203.0.113.45")).json()["access_token"]
    raw = secrets.token_urlsafe(32)
    execute(odin, "INSERT INTO password_reset_tokens (user_id, token, expires_at, used) VALUES (:u, :t, datetime('now','+1 hour'), 0)",
             u=uid, t=reset_token_digest(raw))
    new_pw = strong_password()
    r = odin.post("/api/auth/reset-password", json={"token": raw, "new_password": new_pw}, headers=xff("203.0.113.46"))
    assert r.status_code == 200, r.text
    assert odin.get("/api/auth/me", headers={"Authorization": "Bearer " + tok}).status_code == 401
    a = odin.get("/api/audit-logs", params={"action": "user.password_reset", "limit": 50}, headers=odin.cached_admin)
    assert any(e["entity_id"] == uid for e in a.json()["logs"])
    odin.users[name] = (new_pw, uid)


# ---------------------------------------------------------------------------------------------
# Restart: in-memory state (slowapi buckets) is discarded; the DB persists.
# ---------------------------------------------------------------------------------------------

def test_restart(odin):
    # Age IP .13 rows beyond the 5-minute window (301 s), IP .14 rows inside it (270 s + restart time).
    execute(odin, "UPDATE login_attempts SET attempted_at = :t WHERE ip='203.0.113.13'", t=time.time() - 301)
    execute(odin, "UPDATE login_attempts SET attempted_at = :t WHERE ip='203.0.113.14'", t=time.time() - 270)
    odin.restart()
    odin.cached_admin = {"Authorization": "Bearer " + odin.login(*odin.admin, **xff("198.51.100.252")).json()["access_token"]}


def test_p2_eleventh_failed_attempt_from_ip_is_blocked_by_persisted_5min_throttle(odin):
    r = failed(odin, "203.0.113.11")
    assert r.status_code == 429 and r.json()["detail"] == DB_THROTTLE
    # Correct credentials from the throttled IP are refused too.
    pw, _ = odin.users["cert_c_succ"]
    r2 = odin.login("cert_c_succ", pw, **xff("203.0.113.11"))
    assert r2.status_code == 429 and r2.json()["detail"] == DB_THROTTLE
    # Another IP is unaffected (per-IP).
    assert odin.login("cert_c_succ", pw, **xff("203.0.113.15")).status_code == 200


def test_p2_successful_logins_do_not_count_toward_ip_throttle(odin):
    ip = "203.0.113.12"  # 5 successes + 5 failures recorded before restart
    for _ in range(5):
        assert failed(odin, ip).status_code == 401  # failures 6..10 are still accepted for checking
    r = failed(odin, ip)
    assert r.status_code == 429 and r.json()["detail"] == DB_THROTTLE


def test_p2_throttle_window_is_five_minutes(odin):
    assert failed(odin, "203.0.113.13").status_code == 401           # 10 failures older than 300 s: expired
    r = failed(odin, "203.0.113.14")                                   # 10 failures ~275 s old: still inside window
    assert r.status_code == 429 and r.json()["detail"] == DB_THROTTLE


def test_p2_account_lockout_survives_server_restart(odin):
    """Claim text says the lock is in-memory and resets on restart; real behavior: it persists."""
    pw, _ = odin.users["cert_c_lock"]
    r = odin.login("cert_c_lock", pw, **xff("203.0.113.24"))
    assert r.status_code == 423 and r.json()["detail"] == LOCKED


def test_p2_lockout_is_a_rolling_15_minute_window_over_failures(odin):
    pw, _ = odin.users["cert_c_lock"]
    # Age only the oldest failure past 900 s: 4 failures remain in the trailing window -> unlocked,
    # even though the 5th failure is seconds old (not a fixed 15-minute lock from the 5th failure).
    oldest = odin.sql("SELECT MIN(id) FROM login_attempts WHERE username='cert_c_lock' AND success=0")[0][0]
    execute(odin, "UPDATE login_attempts SET attempted_at = attempted_at - 901 WHERE id=:i", i=oldest)
    r = odin.login("cert_c_lock", pw, **xff("203.0.113.25"))
    assert r.status_code == 200, r.text


def test_p2_lock_expires_after_15_minutes(odin):
    name = "cert_c_rate"
    pw, _ = odin.users[name]
    for _ in range(5):
        odin.login(name, "Wrong" + secrets.token_hex(3) + "x9", **xff("203.0.113.26"))
    assert odin.login(name, pw, **xff("203.0.113.26")).status_code == 423
    execute(odin, "UPDATE login_attempts SET attempted_at = :t WHERE username=:u AND success=0", u=name, t=time.time() - 880)
    assert odin.login(name, pw, **xff("203.0.113.27")).status_code == 423   # 880 s old: still inside 15 min
    execute(odin, "UPDATE login_attempts SET attempted_at = :t WHERE username=:u AND success=0", u=name, t=time.time() - 901)
    assert odin.login(name, pw, **xff("203.0.113.27")).status_code == 200   # 901 s old: unlocked


# ---------------------------------------------------------------------------------------------
# Spoofing: without proxy trust a client-supplied X-Forwarded-For is not recorded as its IP.
# ---------------------------------------------------------------------------------------------

def test_untrusted_forwarded_header_is_not_recorded_as_login_ip(tmp_path_factory):
    with LiveOdin(tmp_path_factory.mktemp("odin_noproxy")) as o:
        o.setup_admin()
        assert o.login("nobody_x", "Wrong1abc", **xff("192.0.2.99")).status_code == 401
        rows = o.sql("SELECT ip FROM login_attempts WHERE username='nobody_x'")
        assert rows == [("127.0.0.1",)]
        r = o.get("/api/audit-logs", params={"action": "login_failed"}, headers={"Authorization": "Bearer " + o.admin_token})
        assert r.status_code == 200
        assert {e["ip_address"] for e in r.json()["logs"]} == {"127.0.0.1"}
