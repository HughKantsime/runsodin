"""Cert F / cluster A: TOTP MFA setup (QR), 6-digit login challenge, pending-token blacklist, admin MFA control.

Claims: F11-11.2-01, F11-11.2-02, F11-11.2-03, F12-12.1-08, F11-11.1-01 (MFA verify cookie).
Real ``main:app`` (LiveOdin) with production cookie defaults; TOTP codes are computed with pyotp from the
secret the server returned, exactly as an authenticator app would. Tests share one server in file order.
"""
from __future__ import annotations

import base64
import io
import re
import threading
import time

import jwt
import pyotp
import pytest
import qrcode
from cryptography.fernet import Fernet

from tests.test_contracts.test_cert_F_00_live import LiveOdin
from tests.test_contracts.test_cert_F_A_auth_transport import assert_production_cookie, capture, make_user


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odin-mfa"), unset=("COOKIE_SECURE",)).start()
    o.setup_admin()
    o.admin_auth = {"Authorization": "Bearer " + o.admin_token}
    o.uid, o.pw = make_user(o, o.admin_auth, "mfa_person", "viewer")
    o.op_id, o.op_pw = make_user(o, o.admin_auth, "mfa_operator", "operator")
    r = o.login("mfa_person", o.pw)
    assert r.status_code == 200 and "mfa_required" not in r.json()
    o.user_auth = {"Authorization": "Bearer " + r.json()["access_token"]}
    yield o
    o.stop()


def sessions(o, uid):
    return o.sql("SELECT COUNT(*) FROM active_sessions WHERE user_id=:u", u=uid)[0][0]


def fresh_code(totp: pyotp.TOTP) -> str:
    # avoid a code minted in the last second of its 30 s step
    if 30 - (time.time() % 30) < 2:
        time.sleep(2.2)
    return totp.now()


# ---------------------------------------------------------------- F11-11.2-01 QR setup
def test_mfa_setup_returns_qr_of_provisioning_uri_and_confirm_enables(odin):
    status = odin.get("/api/auth/mfa/status", headers=odin.user_auth)
    assert status.status_code == 200 and status.json() == {"mfa_enabled": False}
    capture("mfa_status_disabled", status.json())
    setup = odin.post("/api/auth/mfa/setup", headers=odin.user_auth)
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert set(body) == {"secret", "provisioning_uri", "qr_code"}
    secret = body["secret"]
    assert re.fullmatch(r"[A-Z2-7]{32}", secret)
    assert body["provisioning_uri"] == pyotp.TOTP(secret).provisioning_uri(name="mfa_person", issuer_name="O.D.I.N.")
    assert body["provisioning_uri"].startswith("otpauth://totp/O.D.I.N.:mfa_person?secret=" + secret)
    prefix = "data:image/png;base64,"
    assert body["qr_code"].startswith(prefix)
    png = base64.b64decode(body["qr_code"][len(prefix):])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    expected = io.BytesIO()
    qrcode.make(body["provisioning_uri"]).save(expected, format="PNG")
    assert png == expected.getvalue()  # the QR image encodes exactly the provisioning URI
    capture("mfa_setup_response", body)
    stored = odin.sql("SELECT mfa_secret, mfa_enabled FROM users WHERE id=:u", u=odin.uid)[0]
    assert stored[0] != secret and Fernet(odin.fernet_key.encode()).decrypt(stored[0].encode()).decode() == secret
    assert not stored[1]  # not enabled until confirmed
    totp = pyotp.TOTP(secret)
    wrong = str((int(totp.now()) + 1) % 1_000_000).zfill(6)
    bad = odin.post("/api/auth/mfa/confirm", headers=odin.user_auth, json={"code": wrong})
    assert bad.status_code == 400
    ok = odin.post("/api/auth/mfa/confirm", headers=odin.user_auth, json={"code": fresh_code(totp)})
    assert ok.status_code == 200 and ok.json() == {"status": "ok", "message": "MFA enabled successfully"}
    capture("mfa_confirm_response", ok.json())
    capture("mfa_status_enabled", odin.get("/api/auth/mfa/status", headers=odin.user_auth).json())
    assert odin.get("/api/auth/mfa/status", headers=odin.user_auth).json() == {"mfa_enabled": True}
    odin.totp = totp


# ---------------------------------------------------------------- F11-11.2-02 6-digit challenge
def test_login_for_enrolled_user_returns_challenge_without_session(odin):
    before = sessions(odin, odin.uid)
    r = odin.login("mfa_person", odin.pw)
    assert r.status_code == 200
    body = r.json()
    assert body["mfa_required"] is True and body["token_type"] == "bearer" and set(body) == {"access_token", "token_type", "mfa_required"}
    assert not any(k.lower() == "set-cookie" for k in r.raw.headers.keys())
    capture("login_mfa_required_response", {**body, "access_token": "<pending-jwt>"})
    pending = body["access_token"]
    claims = jwt.decode(pending, odin.jwt_secret, algorithms=["HS256"])
    assert claims["mfa_pending"] is True and claims["exp"] - claims["iat"] <= 300
    for headers in ({"Authorization": "Bearer " + pending}, {"Cookie": f"session={pending}"}):
        assert odin.get("/api/auth/me", headers=headers).status_code == 401
        assert odin.get("/api/printers", headers=headers).status_code == 401
    assert sessions(odin, odin.uid) == before
    odin.pending = pending


def test_challenge_requires_the_current_six_digit_code(odin):
    code = fresh_code(odin.totp)
    assert len(code) == 6 and code.isdigit()
    for bad in (code[:5], code + "0", str((int(code) + 1) % 1_000_000).zfill(6)):
        r = odin.post("/api/auth/mfa/verify", json={"mfa_token": odin.pending, "code": bad})
        assert r.status_code == 401 and r.json()["detail"] == "Invalid MFA code"
    assert not odin.sql("SELECT 1 FROM token_blacklist WHERE jti=:j",
                        j=jwt.decode(odin.pending, odin.jwt_secret, algorithms=["HS256"])["jti"])


# ---------------------------------------------------------------- F12-12.1-08 + verify cookie
def test_valid_code_issues_one_session_and_blacklists_pending_token_immediately(odin):
    before = sessions(odin, odin.uid)
    jti = jwt.decode(odin.pending, odin.jwt_secret, algorithms=["HS256"])["jti"]
    code = fresh_code(odin.totp)
    r = odin.post("/api/auth/mfa/verify", json={"mfa_token": odin.pending, "code": code})
    assert r.status_code == 200, r.text
    token = assert_production_cookie(r)  # F11-11.1-01: MFA verify cookie
    assert r.json() == {"access_token": token, "token_type": "bearer"}
    capture("mfa_verify_response", {"access_token": "<jwt>", "token_type": "bearer"})
    # blacklisted by the time the response is returned
    assert odin.sql("SELECT COUNT(*) FROM token_blacklist WHERE jti=:j", j=jti)[0][0] == 1
    assert sessions(odin, odin.uid) == before + 1
    assert odin.get("/api/auth/me", headers={"Cookie": f"session={token}"}).json()["username"] == "mfa_person"
    replay = odin.post("/api/auth/mfa/verify", json={"mfa_token": odin.pending, "code": code})
    assert replay.status_code == 401 and replay.json()["detail"] == "MFA token has already been used"
    assert not any(k.lower() == "set-cookie" for k in replay.raw.headers.keys())
    assert sessions(odin, odin.uid) == before + 1


def test_concurrent_redemption_of_one_pending_token_yields_exactly_one_session(odin):
    r = odin.login("mfa_person", odin.pw)
    pending = r.json()["access_token"]
    before = sessions(odin, odin.uid)
    code = fresh_code(odin.totp)
    barrier = threading.Barrier(4)
    results = []

    def redeem():
        barrier.wait()
        results.append(odin.post("/api/auth/mfa/verify", json={"mfa_token": pending, "code": code}).status_code)

    threads = [threading.Thread(target=redeem) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert len(results) == 4 and results.count(200) == 1, results
    assert all(code in (401, 503) for code in results if code != 200), results  # losers never get a session
    assert sessions(odin, odin.uid) == before + 1


# ---------------------------------------------------------------- F11-11.2-03 admin control
def test_per_user_force_enable_is_not_available(odin):
    target = odin.op_id
    r = odin.patch(f"/api/users/{target}", headers=odin.admin_auth, json={"mfa_enabled": True})
    assert r.status_code in (200, 400, 422)
    assert not odin.sql("SELECT mfa_enabled FROM users WHERE id=:u", u=target)[0][0]
    spec = odin.get("/openapi.json").json()["paths"]
    per_user_mfa = {(p, m) for p, ops in spec.items() for m in ops
                    if "mfa" in p and "{user_id}" in p}
    assert per_user_mfa and {m for _, m in per_user_mfa} == {"delete"}, per_user_mfa


def test_non_admin_cannot_reset_another_users_mfa(odin):
    op = odin.login("mfa_operator", odin.op_pw)
    r = odin.delete(f"/api/admin/users/{odin.uid}/mfa", headers={"Authorization": "Bearer " + op.json()["access_token"]})
    assert r.status_code == 403
    assert odin.sql("SELECT mfa_enabled FROM users WHERE id=:u", u=odin.uid)[0][0]


def test_admin_force_disables_user_mfa_and_next_login_is_unchallenged(odin):
    users = odin.get("/api/users", headers=odin.admin_auth)
    assert users.status_code == 200
    row = [u for u in users.json() if u["id"] == odin.uid][0]
    assert row["mfa_enabled"] is True
    capture("users_list_mfa", users.json())
    r = odin.delete(f"/api/admin/users/{odin.uid}/mfa", headers=odin.admin_auth)
    assert r.status_code == 200 and r.json() == {"status": "ok", "message": "MFA disabled for mfa_person"}
    capture("admin_mfa_reset_response", r.json())
    assert odin.sql("SELECT mfa_enabled, mfa_secret FROM users WHERE id=:u", u=odin.uid)[0] in ((0, None), (False, None))
    again = odin.delete(f"/api/admin/users/{odin.uid}/mfa", headers=odin.admin_auth)
    assert again.status_code == 400
    login = odin.login("mfa_person", odin.pw)
    assert login.status_code == 200 and "mfa_required" not in login.json()
    assert_production_cookie(login)
