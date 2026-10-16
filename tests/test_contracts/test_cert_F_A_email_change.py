"""Security review HIGH-A: changing a recovery identifier (email/username) is
credential material — interactive session only — and changing the email voids
outstanding reset tokens and biometric refresh tokens."""
from __future__ import annotations

import secrets
import pytest
from tests.test_contracts.test_cert_F_B_00_support import LiveOdinB


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdinB(tmp_path_factory.mktemp("emailchg")).start()
    o.boot_admin()
    yield o
    o.stop()


def key(t):
    return {"X-API-Key": t}


def test_admin_token_cannot_change_email_or_username(odin):
    u = odin.mkuser("victim_e", "viewer")
    admin_tok = odin.mint(odin.admin_bearer, ["admin"])["token"]
    before = odin.one("SELECT email, username FROM users WHERE id=:i", i=u["id"])
    for body in ({"email": "attacker@evil.test"}, {"username": "attacker"}):
        r = odin.patch(f"/api/users/{u['id']}", json=body, headers=key(admin_tok))
        assert r.status_code == 403, (body, r.text)
        assert r.json()["detail"] == "API tokens cannot create or change credentials; sign in with a password session"
    after = odin.one("SELECT email, username FROM users WHERE id=:i", i=u["id"])
    assert (after.email, after.username) == (before.email, before.username)
    # Role still changeable with an admin token (not recovery material).
    assert odin.patch(f"/api/users/{u['id']}", json={"role": "operator"}, headers=key(admin_tok)).status_code == 200


def test_interactive_email_change_voids_reset_and_biometric_tokens(odin):
    u = odin.mkuser("victim_f", "viewer")
    # Seed a reset token and a biometric token for the account.
    from sqlalchemy import text
    with odin.engine.begin() as c:
        c.execute(text("INSERT INTO password_reset_tokens(user_id, token, expires_at, used) "
                       "VALUES(:u, :t, datetime('now','+1 hour'), 0)"),
                  {"u": u["id"], "t": "sha256:" + secrets.token_hex(16)})
        c.execute(text("INSERT INTO biometric_tokens(user_id, device_id, token_hash, expires_at, is_revoked) "
                       "VALUES(:u, 'dev', :h, datetime('now','+30 day'), 0)"),
                  {"u": u["id"], "h": secrets.token_hex(16)})
    assert odin.one("SELECT COUNT(*) AS n FROM password_reset_tokens WHERE user_id=:u AND used=0", u=u["id"]).n == 1
    assert odin.one("SELECT COUNT(*) AS n FROM biometric_tokens WHERE user_id=:u", u=u["id"]).n == 1
    # Admin interactive session (bearer JWT) changes the email.
    r = odin.patch(f"/api/users/{u['id']}", json={"email": "newaddr@example.test"}, headers=odin.admin_bearer)
    assert r.status_code == 200, r.text
    assert odin.one("SELECT email FROM users WHERE id=:i", i=u["id"]).email == "newaddr@example.test"
    assert odin.one("SELECT COUNT(*) AS n FROM password_reset_tokens WHERE user_id=:u AND used=0", u=u["id"]).n == 0
    assert odin.one("SELECT COUNT(*) AS n FROM biometric_tokens WHERE user_id=:u", u=u["id"]).n == 0


def test_null_email_or_username_is_a_credential_change(odin):
    u = odin.mkuser("victim_null", "viewer")
    admin_tok = odin.mint(odin.admin_bearer, ["admin"])["token"]
    for body in ({"email": None}, {"username": None}):
        r = odin.patch(f"/api/users/{u['id']}", json=body, headers=key(admin_tok))
        assert r.status_code == 403, (body, r.text)
    row = odin.one("SELECT email, username FROM users WHERE id=:i", i=u["id"])
    assert row.email and row.username  # unchanged


def test_resending_unchanged_email_or_username_is_a_no_op_not_403(odin):
    u = odin.mkuser("victim_same", "viewer")
    cur = odin.one("SELECT email, username FROM users WHERE id=:i", i=u["id"])
    admin_tok = odin.mint(odin.admin_bearer, ["admin"])["token"]
    # Same value via an API token is a no-op and must be allowed (also lets an
    # unrelated field like role ride along).
    r = odin.patch(f"/api/users/{u['id']}", json={"email": cur.email, "username": cur.username, "role": "operator"},
                   headers=key(admin_tok))
    assert r.status_code == 200, r.text
    assert odin.one("SELECT role FROM users WHERE id=:i", i=u["id"]).role == "operator"


def test_password_change_revokes_biometric_and_other_sessions(odin):
    u = odin.mkuser("victim_pw", "viewer")
    from sqlalchemy import text
    with odin.engine.begin() as c:
        c.execute(text("INSERT INTO biometric_tokens(user_id, device_id, token_hash, expires_at, is_revoked) "
                       "VALUES(:u, 'd', :h, datetime('now','+30 day'), 0)"), {"u": u["id"], "h": "h" * 16})
        c.execute(text("INSERT INTO active_sessions(user_id, token_jti) VALUES(:u, 'jti-a'), (:u, 'jti-b')"),
                  {"u": u["id"]})
    r = odin.patch(f"/api/users/{u['id']}", json={"password": "NewPassw0rd"}, headers=odin.admin_bearer)
    assert r.status_code == 200, r.text
    assert odin.one("SELECT COUNT(*) AS n FROM biometric_tokens WHERE user_id=:u", u=u["id"]).n == 0
    assert odin.one("SELECT COUNT(*) AS n FROM active_sessions WHERE user_id=:u", u=u["id"]).n == 0
    assert odin.one("SELECT COUNT(*) AS n FROM token_blacklist WHERE jti IN ('jti-a','jti-b')").n == 2


def test_self_password_change_keeps_the_callers_current_session(odin):
    # An admin changing their OWN password keeps the session making the request
    # (the admin_bearer login's recorded session); every other session is revoked.
    from sqlalchemy import text
    admin_id = odin.one("SELECT id FROM users WHERE username=:u", u=odin.admin[0]).id
    assert odin.one("SELECT COUNT(*) AS n FROM active_sessions WHERE user_id=:u", u=admin_id).n >= 1
    with odin.engine.begin() as c:
        c.execute(text("INSERT INTO active_sessions(user_id, token_jti) VALUES(:u, 'other-device')"),
                  {"u": admin_id})
    r = odin.patch(f"/api/users/{admin_id}", json={"password": "FreshAdminPw9"}, headers=odin.admin_bearer)
    assert r.status_code == 200, r.text
    live = {row.token_jti for row in odin.all("SELECT token_jti FROM active_sessions WHERE user_id=:u", u=admin_id)}
    # Other sessions (the seeded device and the earlier setup session) are gone;
    # the caller's own session survives and still authenticates.
    assert "other-device" not in live
    assert odin.get("/api/auth/me", headers=odin.admin_bearer).status_code == 200
    assert odin.one("SELECT COUNT(*) AS n FROM active_sessions WHERE user_id=:u", u=admin_id).n == 1
