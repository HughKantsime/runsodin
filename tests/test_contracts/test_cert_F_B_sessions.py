"""Cert F / cluster B — F11-11.7 session management on the real server (LiveOdin).

"JWT jti tracking for session identification", "Active sessions list with device info",
"Revoke individual sessions or all sessions", "Token blacklist for immediate invalidation".
"""
from __future__ import annotations

import jwt
import pytest
import requests

from tests.test_contracts.test_cert_F_B_00_support import LiveOdinB
from tests.test_contracts.test_cert_F_B_api_tokens import capture

DESKTOP = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdinB(tmp_path_factory.mktemp("sessions")).start()
    o.boot_admin()
    o.alice = o.mkuser("sess_alice", "operator", login=False)
    o.bob = o.mkuser("sess_bob", "viewer")
    yield o
    o.stop()


def bearer(t):
    return {"Authorization": "Bearer " + t}


def claims(t):
    return jwt.decode(t, options={"verify_signature": False})


def test_every_login_token_carries_a_unique_tracked_jti(odin):
    a = odin.alice
    t1 = odin.token(a["username"], a["password"], user_agent=DESKTOP, ip="10.77.0.1")
    t2 = odin.token(a["username"], a["password"], user_agent=PHONE, ip="10.77.0.2")
    j1, j2 = claims(t1)["jti"], claims(t2)["jti"]
    assert j1 and j2 and j1 != j2
    rows = {r.token_jti: r for r in odin.all("SELECT token_jti, ip_address, user_agent FROM active_sessions WHERE user_id=:u", u=a["id"])}
    assert rows[j1].ip_address == "10.77.0.1" and rows[j1].user_agent == DESKTOP
    assert rows[j2].ip_address == "10.77.0.2" and rows[j2].user_agent == PHONE
    odin.t_desktop, odin.t_phone = t1, t2


def test_cookie_session_also_carries_tracked_jti(odin):
    r = odin.login_ip(odin.alice["username"], odin.alice["password"], user_agent=DESKTOP, ip="10.77.0.3")
    cookie = r.cookies.get("session")
    assert cookie, r.headers
    jti = claims(cookie)["jti"]
    assert odin.one("SELECT 1 FROM active_sessions WHERE token_jti=:j", j=jti) is not None
    odin.cookie = cookie


def test_setup_admin_token_is_tracked_and_revocable(odin):
    jti = claims(odin.admin_token)["jti"]
    assert odin.get("/api/users", headers=bearer(odin.admin_token)).status_code == 200
    tracked = odin.one("SELECT 1 FROM active_sessions WHERE token_jti=:j", j=jti) is not None
    assert odin.delete("/api/sessions", headers=odin.admin_bearer).status_code == 200
    still_valid = odin.get("/api/users", headers=bearer(odin.admin_token)).status_code
    assert tracked and still_valid == 401, {"setup_token_status_after_revoke_all": still_valid, "tracked": tracked}


def test_active_sessions_list_shows_device_info_for_own_sessions_only(odin):
    listed = odin.get("/api/sessions", headers=bearer(odin.t_desktop)).json()
    capture("sessions_list", listed)
    assert len(listed) == 3
    assert {s["user_agent"] for s in listed} == {DESKTOP, PHONE}
    assert {s["ip_address"] for s in listed} == {"10.77.0.1", "10.77.0.2", "10.77.0.3"}
    assert [s["is_current"] for s in listed].count(True) == 1
    current = [s for s in listed if s["is_current"]][0]
    assert current["ip_address"] == "10.77.0.1"
    for s in listed:
        assert set(s) == {"id", "ip_address", "user_agent", "created_at", "last_seen_at", "is_current"}
        assert s["created_at"]
    assert "token_jti" not in str(listed) and claims(odin.t_desktop)["jti"] not in str(listed)
    bob = odin.get("/api/sessions", headers=odin.bob["headers"]).json()
    assert {s["ip_address"] for s in bob}.isdisjoint({"10.77.0.1", "10.77.0.2", "10.77.0.3"})


def test_revoke_individual_session_blacklists_it_immediately(odin):
    listed = odin.get("/api/sessions", headers=bearer(odin.t_desktop)).json()
    phone = [s for s in listed if s["ip_address"] == "10.77.0.2"][0]
    assert odin.get("/api/printers", headers=bearer(odin.t_phone)).status_code == 200
    assert odin.delete(f"/api/sessions/{phone['id']}", headers=bearer(odin.t_desktop)).status_code == 200
    assert odin.one("SELECT 1 FROM token_blacklist WHERE jti=:j", j=claims(odin.t_phone)["jti"]) is not None
    assert odin.get("/api/printers", headers=bearer(odin.t_phone)).status_code == 401
    assert odin.get("/api/printers", headers=bearer(odin.t_desktop)).status_code == 200


def test_cannot_revoke_another_users_session(odin):
    alice_session = odin.get("/api/sessions", headers=bearer(odin.t_desktop)).json()[0]["id"]
    assert odin.delete(f"/api/sessions/{alice_session}", headers=odin.bob["headers"]).status_code == 404
    assert odin.get("/api/printers", headers=bearer(odin.t_desktop)).status_code == 200


def test_revoke_all_keeps_current_and_kills_others_including_cookie(odin):
    assert odin.get("/api/printers", cookies={"session": odin.cookie}).status_code == 200
    r = odin.delete("/api/sessions", headers=bearer(odin.t_desktop))
    assert r.status_code == 200 and r.json()["revoked"] == 1
    assert odin.get("/api/printers", cookies={"session": odin.cookie}).status_code == 401
    assert odin.get("/api/printers", headers=bearer(odin.t_desktop)).status_code == 200
    assert [s["is_current"] for s in odin.get("/api/sessions", headers=bearer(odin.t_desktop)).json()] == [True]
    assert odin.get("/api/printers", headers=odin.bob["headers"]).status_code == 200


def test_logout_blacklists_token_for_immediate_invalidation(odin):
    t = odin.token(odin.alice["username"], odin.alice["password"], ip="10.77.0.9")
    assert odin.get("/api/printers", headers=bearer(t)).status_code == 200
    assert odin.post("/api/auth/logout", headers=bearer(t)).status_code == 200
    assert odin.one("SELECT 1 FROM token_blacklist WHERE jti=:j", j=claims(t)["jti"]) is not None
    assert odin.get("/api/printers", headers=bearer(t)).status_code == 401


def test_admin_force_revokes_any_users_session(odin):
    sessions = odin.get("/api/admin/sessions", headers=odin.admin_bearer).json()
    bob = [s for s in sessions if s["username"] == "sess_bob"][0]
    assert bob["user_agent"] and bob["ip_address"]
    assert odin.delete(f"/api/admin/sessions/{bob['id']}", headers=odin.admin_bearer).status_code == 200
    assert odin.get("/api/printers", headers=odin.bob["headers"]).status_code == 401
