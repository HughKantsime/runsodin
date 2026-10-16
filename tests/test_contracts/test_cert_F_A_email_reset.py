"""Cert F / cluster A: welcome-email onboarding, self-service and admin password reset, auth capabilities.

Claims: F11-11.1-05, F11-11.1-06, F11-11.1-07, F11-11.1-08.
Real ``main:app`` (LiveOdin) over HTTP; ODIN's real smtplib client talks to a loopback stub SMTP server
(StubSMTP), which is the only substituted boundary. SMTP is configured through the product API
(PUT /api/smtp-config) exactly as the Settings UI does. Tests in this module run in file order and share
one server (budgeted for the 10/min login and 5/min reset limits).
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password
from tests.test_contracts.test_cert_F_A_00_stubs import StubSMTP
from tests.test_contracts.test_cert_F_A_auth_transport import capture, make_user

BASE = "https://odin.example.test"


@pytest.fixture(scope="module")
def smtp():
    s = StubSMTP().start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odin-mail")).start()
    o.setup_admin()
    o.admin_auth = {"Authorization": "Bearer " + o.admin_token}
    o.reader_id, o.reader_pw = make_user(o, o.admin_auth, "reset_reader", "viewer")
    o.op_id, o.op_pw = make_user(o, o.admin_auth, "reset_operator", "operator")
    yield o
    o.stop()


def execute(o: LiveOdin, statement: str, **params):
    from sqlalchemy import text
    with o.engine.begin() as c:
        c.execute(text(statement), params)


def link_token(msg: dict) -> str:
    hrefs = re.findall(r'href="([^"]+)"', msg["html"])
    assert len(hrefs) == 1, msg["html"]
    url = urlsplit(unescape(hrefs[0]))
    assert f"{url.scheme}://{url.netloc}" == BASE and url.path == "/reset-password"
    return parse_qs(url.query)["token"][0]


# ---------------------------------------------------------------- F11-11.1-08 capabilities (SMTP off)
def test_capabilities_public_and_false_without_smtp(odin):
    r = odin.get("/api/auth/capabilities")  # no credentials
    assert r.status_code == 200 and r.json() == {"smtp_enabled": False}
    capture("capabilities_smtp_disabled", r.json())


def test_welcome_without_smtp_creates_account_and_reports_unconfirmed(odin, smtp):
    r = odin.post("/api/users", headers=odin.admin_auth,
                  json={"username": "welcome_nosmtp", "email": "welcome_nosmtp@example.test", "role": "viewer",
                        "send_welcome_email": True})
    assert r.status_code == 200, r.text
    assert r.json()["welcome_email"] == "unconfirmed" and r.json()["status"] == "created"
    assert odin.sql("SELECT COUNT(*) FROM users WHERE username='welcome_nosmtp'")[0][0] == 1
    assert smtp.for_rcpt("welcome_nosmtp@example.test") == []


def test_capabilities_reflect_real_smtp_configuration(odin, smtp):
    cfg = smtp.config()
    assert odin.put("/api/smtp-config", headers=odin.admin_auth, json={**cfg, "host": ""}).status_code == 200
    assert odin.get("/api/auth/capabilities").json() == {"smtp_enabled": False}  # enabled but no host
    assert odin.put("/api/smtp-config", headers=odin.admin_auth, json={**cfg, "enabled": False}).status_code == 200
    assert odin.get("/api/auth/capabilities").json() == {"smtp_enabled": False}
    assert odin.put("/api/smtp-config", headers=odin.admin_auth, json=cfg).status_code == 200
    r = odin.get("/api/auth/capabilities")
    assert r.status_code == 200 and r.json() == {"smtp_enabled": True}
    capture("capabilities_smtp_enabled", r.json())


# ---------------------------------------------------------------- F11-11.1-06/07 product-configurable reset
def test_reset_routes_without_application_url_refuse_with_503_and_send_nothing(odin, smtp):
    """SMTP alone is not enough: links need the admin-set application URL, never the request Host."""
    sessions = smtp.sessions
    r = odin.post("/api/auth/forgot-password", json={"email": "reset_reader@example.test"})
    assert r.status_code == 503 and r.json()["detail"] == "Password reset requires a valid application base URL."
    a = odin.post(f"/api/users/{odin.reader_id}/reset-password-email", headers=odin.admin_auth)
    assert a.status_code == 503 and a.json()["detail"] == "Password reset requires a valid application base URL."
    assert smtp.sessions == sessions


def test_smtp_settings_reject_an_invalid_application_url(odin, smtp):
    for bad in ("ftp://odin.example.test", "https://user:pw@odin.example.test", "https://odin.example.test/?q=1", "odin"):
        r = odin.put("/api/smtp-config", headers=odin.admin_auth, json={**smtp.config(), "base_url": bad})
        assert r.status_code == 400, (bad, r.text)
    assert odin.get("/api/smtp-config", headers=odin.admin_auth).json()["base_url"] == ""


def test_application_url_is_set_through_smtp_settings(odin, smtp):
    r = odin.put("/api/smtp-config", headers=odin.admin_auth, json={**smtp.config(), "base_url": BASE + "/"})
    assert r.status_code == 200, r.text
    got = odin.get("/api/smtp-config", headers=odin.admin_auth).json()
    assert got["base_url"] == BASE and got["host"] == "127.0.0.1"
    # Saving SMTP again without the field keeps the stored URL.
    assert odin.put("/api/smtp-config", headers=odin.admin_auth, json=smtp.config()).status_code == 200
    assert odin.get("/api/smtp-config", headers=odin.admin_auth).json()["base_url"] == BASE


def test_forgot_password_works_with_product_configured_smtp(odin, smtp):
    r = odin.post("/api/auth/forgot-password", json={"email": "reset_reader@example.test"})
    assert r.status_code == 200, r.text
    msgs = smtp.for_rcpt("reset_reader@example.test")
    assert len(msgs) == 1
    link_token(msgs[0])


def test_admin_reset_email_works_with_product_configured_smtp(odin, smtp):
    r = odin.post(f"/api/users/{odin.reader_id}/reset-password-email", headers=odin.admin_auth)
    assert r.status_code == 200, r.text
    assert len(smtp.for_rcpt("reset_reader@example.test")) == 2


@pytest.fixture
def base_url(odin):
    # Set through the product (Settings > Notifications) by test_application_url_is_set_through_smtp_settings.
    assert odin.get("/api/smtp-config", headers=odin.admin_auth).json()["base_url"] == BASE
    return BASE


# ---------------------------------------------------------------- F11-11.1-05 welcome email
def test_welcome_email_generates_random_password_and_delivers_invite(odin, smtp):
    sent = {}
    for name in ("welcome_one", "welcome_two"):
        r = odin.post("/api/users", headers=odin.admin_auth,
                      json={"username": name, "email": f"{name}@example.test", "role": "viewer",
                            "send_welcome_email": True})
        assert r.status_code == 200, r.text
        assert r.json() == {"status": "created", "welcome_email": "sent"}
        capture("user_create_welcome_sent", r.json())
        msgs = smtp.for_rcpt(f"{name}@example.test")
        assert len(msgs) == 1
        m = msgs[0]
        assert m["subject"] == "Your O.D.I.N. Account" and m["to"] == f"{name}@example.test"
        assert m["from"].strip("<>") == "odin@stub.smtp.test"
        found = re.search(r"<strong>Username:</strong> ([^<]+)<br>\s*<strong>Temporary Password:</strong> ([^<]+)</p>",
                          m["html"])
        assert found and unescape(found.group(1)) == name
        sent[name] = unescape(found.group(2))
    one, two = sent["welcome_one"], sent["welcome_two"]
    assert one != two and len(one) >= 32 and len(two) >= 32  # 192-bit token_urlsafe + policy suffix
    stored = odin.sql("SELECT password_hash FROM users WHERE username='welcome_one'")[0][0]
    assert one not in stored
    login = odin.login("welcome_one", one)  # emailed temporary password is the real credential
    assert login.status_code == 200 and login.json()["token_type"] == "bearer"


def test_admin_supplied_password_is_replaced_when_welcome_email_requested(odin, smtp):
    chosen = strong_password()
    r = odin.post("/api/users", headers=odin.admin_auth,
                  json={"username": "welcome_three", "email": "welcome_three@example.test", "role": "viewer",
                        "password": chosen, "send_welcome_email": True})
    assert r.status_code == 200 and r.json()["welcome_email"] == "sent"
    html = smtp.for_rcpt("welcome_three@example.test")[0]["html"]
    assert chosen not in html
    users = odin.get("/api/users", headers=odin.admin_auth)
    assert users.status_code == 200
    capture("users_list", users.json())


# ---------------------------------------------------------------- F11-11.1-06 self-service reset
def test_forgot_password_issues_one_hour_hashed_single_use_token(odin, smtp, base_url):
    addr = "reset_reader@example.test"
    before = len(smtp.for_rcpt(addr))
    t0 = datetime.now(timezone.utc)
    r1 = odin.post("/api/auth/forgot-password", json={"email": addr})
    r2 = odin.post("/api/auth/forgot-password", json={"email": addr})
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == {"status": "ok", "message": "If that email is registered, a reset link has been sent."}
    capture("forgot_password_response", r1.json())
    msgs = smtp.for_rcpt(addr)[before:]
    assert len(msgs) == 2 and all(m["subject"] == "O.D.I.N. Password Reset" for m in msgs)
    assert "This link expires in 1 hour." in msgs[0]["html"]
    a, b = link_token(msgs[0]), link_token(msgs[1])
    rows = odin.sql("SELECT token, expires_at, used FROM password_reset_tokens WHERE user_id=:u ORDER BY id",
                    u=odin.reader_id)[-2:]
    for (stored, expires, used), raw in zip(rows, (a, b)):
        assert stored.startswith("sha256:") and raw not in stored and used == 0
        exp = datetime.fromisoformat(expires)
        delta = (exp - t0).total_seconds()
        assert 3590 <= delta <= 3700, delta  # 1 hour
    new_pw = strong_password()
    ok = odin.post("/api/auth/reset-password", json={"token": a, "new_password": new_pw})
    assert ok.status_code == 200 and ok.json()["status"] == "ok"
    # single use: the same link is refused, and the sibling link issued earlier is invalidated too
    assert odin.post("/api/auth/reset-password", json={"token": a, "new_password": strong_password()}).status_code == 400
    assert odin.post("/api/auth/reset-password", json={"token": b, "new_password": strong_password()}).status_code == 400
    assert all(u == 1 for (_, _, u) in odin.sql("SELECT token, expires_at, used FROM password_reset_tokens "
                                                 "WHERE user_id=:u", u=odin.reader_id))
    assert odin.login("reset_reader", new_pw).status_code == 200
    assert odin.login("reset_reader", odin.reader_pw).status_code == 401
    odin.reader_pw = new_pw


def test_expired_reset_token_is_refused(odin, smtp, base_url):
    odin.restart()  # same database; fresh process so the 5/minute forgot-password limit has budget
    addr = "reset_reader@example.test"
    before = len(smtp.for_rcpt(addr))
    assert odin.post("/api/auth/forgot-password", json={"email": addr}).status_code == 200
    raw = link_token(smtp.for_rcpt(addr)[before])
    execute(odin, "UPDATE password_reset_tokens SET expires_at=:e WHERE used=0 AND user_id=:u",
            e="2000-01-01T00:00:00+00:00", u=odin.reader_id)
    assert odin.post("/api/auth/reset-password", json={"token": raw, "new_password": strong_password()}).status_code == 400


def test_forgot_password_unknown_email_same_response_no_mail(odin, smtp, base_url):
    sessions = smtp.sessions
    r = odin.post("/api/auth/forgot-password", json={"email": "nobody-" + secrets.token_hex(4) + "@example.test"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "message": "If that email is registered, a reset link has been sent."}
    assert smtp.sessions == sessions


# ---------------------------------------------------------------- F11-11.1-07 admin-triggered reset
def test_admin_triggers_reset_email_for_user_without_changing_password(odin, smtp, base_url):
    addr = "reset_reader@example.test"
    before = len(smtp.for_rcpt(addr))
    r = odin.post(f"/api/users/{odin.reader_id}/reset-password-email", headers=odin.admin_auth)
    assert r.status_code == 200 and r.json() == {"status": "password_reset_email_sent"}
    capture("admin_reset_email_response", r.json())
    msgs = smtp.for_rcpt(addr)[before:]
    assert len(msgs) == 1 and "An administrator requested a password reset" in msgs[0]["html"]
    raw = link_token(msgs[0])
    from hashlib import sha256
    row = odin.sql("SELECT used FROM password_reset_tokens WHERE token=:t", t="sha256:" + sha256(raw.encode()).hexdigest())
    assert row == [(0,)]
    assert odin.login("reset_reader", odin.reader_pw).status_code == 200  # credentials unchanged


def test_non_admin_cannot_trigger_reset_email(odin, smtp, base_url):
    op = odin.login("reset_operator", odin.op_pw)
    assert op.status_code == 200
    sessions = smtp.sessions
    r = odin.post(f"/api/users/{odin.reader_id}/reset-password-email",
                  headers={"Authorization": "Bearer " + op.json()["access_token"]})
    assert r.status_code == 403
    assert smtp.sessions == sessions
