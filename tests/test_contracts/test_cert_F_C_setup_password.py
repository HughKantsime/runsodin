"""Cert F / cluster C: password complexity (F12-12.1-03), setup lock (F12-12.1-05, F12-12.6-03) and the
setup-issued session versus password-change revocation (F12-12.1-07) on a real ODIN server.

A fresh real server is bootstrapped un-claimed so the setup wizard endpoints can be driven from the first
request. Every login uses its own synthetic client IP (loopback proxy trust) so slowapi's 10/min login
bucket never interferes. Order-dependent module (one server, one restart).
"""
from __future__ import annotations

import io
import itertools
import random
import secrets
import string

import pytest

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password

_ips = (f"198.18.{a}.{b}" for a, b in itertools.product(range(1, 200), range(1, 250)))


def ip() -> dict:
    return {"X-Forwarded-For": next(_ips)}


def execute(o, statement: str, **params) -> None:
    from sqlalchemy import text
    with o.engine.begin() as c:
        c.execute(text(statement), params)


def bearer(o, user, pw) -> dict:
    r = o.login(user, pw, **ip())
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def exactly_8() -> str:
    """Minimum-length password meeting every class: 1 upper, 6 lower, 1 digit (8 chars)."""
    return "Q" + "".join(random.choice(string.ascii_lowercase) for _ in range(6)) + str(random.randint(0, 9))


def weak_cases():
    low = "".join(random.choice(string.ascii_lowercase) for _ in range(8))
    return [
        ("Qabcde1", "Password must be at least 8 characters"),                       # 7 chars, all classes
        (low + "12", "Password must contain at least one uppercase letter"),
        (low.upper() + "12", "Password must contain at least one lowercase letter"),
        ("Q" + low + "x", "Password must contain at least one number"),
    ]


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odin_setup"), env={"ODIN_CERT_PROXY_HEADERS": "1"}).start()
    yield o
    o.stop()


SETUP_MUTATIONS = [
    ("/api/setup/admin", {"username": "late_owner", "email": "late@example.test", "password": "Lateowner12"}),
    ("/api/setup/test-printer", {"api_type": "moonraker", "api_host": "192.0.2.10"}),
    ("/api/setup/printer", {"name": "Synthetic setup printer", "model": "X1C"}),
    ("/api/setup/complete", {}),
    ("/api/setup/network", {"host_ip": "192.0.2.30"}),
]


# --------------------------------------------------------------------------------- complexity

@pytest.mark.parametrize("case", range(4))
def test_setup_admin_rejects_weak_passwords(odin, case):
    pw, msg = weak_cases()[case]
    r = odin.post("/api/setup/admin", json={"username": "owner", "email": "owner@example.test", "password": pw})
    assert r.status_code == 400 and r.json()["detail"] == msg
    assert odin.sql("SELECT COUNT(*) FROM users")[0][0] == 0


def test_setup_admin_accepts_exactly_8_char_compliant_password(odin):
    pw = exactly_8()
    r = odin.post("/api/setup/admin", json={"username": "owner", "email": "owner@example.test", "password": pw})
    assert r.status_code == 200, r.text
    odin.admin = ("owner", pw)
    odin.admin_token = r.json()["access_token"]
    odin.h = bearer(odin, *odin.admin)


@pytest.mark.parametrize("case", range(4))
def test_user_create_rejects_weak_passwords(odin, case):
    pw, msg = weak_cases()[case]
    r = odin.post("/api/users", json={"username": f"weak{case}", "email": f"weak{case}@example.test", "password": pw,
                                      "role": "viewer"}, headers=odin.h)
    assert r.status_code == 400 and r.json()["detail"] == msg
    assert odin.sql("SELECT COUNT(*) FROM users WHERE username=:u", u=f"weak{case}")[0][0] == 0


def test_user_create_accepts_exactly_8_and_creates_viewer(odin):
    pw = exactly_8()
    r = odin.post("/api/users", json={"username": "viewer1", "email": "viewer1@example.test", "password": pw,
                                      "role": "viewer"}, headers=odin.h)
    assert r.status_code == 200, r.text
    odin.viewer = ("viewer1", pw)
    odin.viewer_id = odin.sql("SELECT id FROM users WHERE username='viewer1'")[0][0]
    assert odin.login(*odin.viewer, **ip()).status_code == 200


@pytest.mark.parametrize("case", range(4))
def test_user_update_rejects_weak_passwords_and_keeps_old_hash(odin, case):
    before = odin.sql("SELECT password_hash FROM users WHERE id=:i", i=odin.viewer_id)[0][0]
    pw, msg = weak_cases()[case]
    r = odin.patch(f"/api/users/{odin.viewer_id}", json={"password": pw}, headers=odin.h)
    assert r.status_code == 400 and r.json()["detail"] == msg
    assert odin.sql("SELECT password_hash FROM users WHERE id=:i", i=odin.viewer_id)[0][0] == before


def test_user_update_accepts_exactly_8(odin):
    pw = exactly_8()
    assert odin.patch(f"/api/users/{odin.viewer_id}", json={"password": pw}, headers=odin.h).status_code == 200
    odin.viewer = ("viewer1", pw)
    assert odin.login(*odin.viewer, **ip()).status_code == 200


def _reset_token(o, uid) -> str:
    from modules.organizations.password_reset import reset_token_digest
    raw = secrets.token_urlsafe(32)
    execute(o, "INSERT INTO password_reset_tokens (user_id, token, expires_at, used) VALUES (:u,:t,datetime('now','+1 hour'),0)",
            u=uid, t=reset_token_digest(raw))
    return raw


def test_password_reset_rejects_weak_then_accepts_compliant(odin):
    raw = _reset_token(odin, odin.viewer_id)
    for pw, msg in weak_cases():
        r = odin.post("/api/auth/reset-password", json={"token": raw, "new_password": pw}, headers=ip())
        assert r.status_code == 400 and r.json()["detail"] == msg
    assert odin.sql("SELECT used FROM password_reset_tokens WHERE user_id=:u", u=odin.viewer_id)[0][0] == 0
    good = exactly_8()
    r = odin.post("/api/auth/reset-password", json={"token": raw, "new_password": good}, headers=ip())
    assert r.status_code == 200, r.text
    odin.viewer = ("viewer1", good)
    assert odin.login(*odin.viewer, **ip()).status_code == 200


def test_csv_import_applies_same_policy(odin):
    rows = ["username,email,password,role"]
    for i, (pw, _) in enumerate(weak_cases()):
        rows.append(f"csvweak{i},csvweak{i}@example.test,{pw},viewer")
    rows.append(f"csvgood,csvgood@example.test,{exactly_8()},viewer")
    data = ("\n".join(rows) + "\n").encode()
    r = odin.post("/api/users/import", files={"file": ("u.csv", io.BytesIO(data), "text/csv")}, headers=odin.h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 1
    assert [e["reason"] for e in body["errors"]] == [m for _, m in weak_cases()]
    assert odin.sql("SELECT COUNT(*) FROM users WHERE username LIKE 'csvweak%'")[0][0] == 0


# --------------------------------------------------------------------------------- setup lock

def test_claimed_install_refuses_unauthenticated_setup_admin_and_probe(odin):
    for path, body in SETUP_MUTATIONS[:2]:
        r = odin.post(path, json=body)
        assert r.status_code == 403, (path, r.status_code, r.text)
    assert odin.sql("SELECT COUNT(*) FROM users WHERE username='late_owner'")[0][0] == 0


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/system/routes_setup.py:357-386 POST /api/setup/printer only requires any "
    "authenticated user (get_current_user) and setup_complete!='true'; it ignores the claimed-install lock "
    "(_validate_setup_phase). Scenario: an admin finishes /setup/admin but never reaches /setup/complete "
    "(wizard abandoned; /setup/status then reports needs_setup=false so the wizard never returns). Any "
    "viewer/student account then POSTs /api/setup/printer {name, api_type, api_host} and creates a printer "
    "with an arbitrary api_host the monitors will contact, bypassing the operator/admin role required by "
    "/api/printers."))
def test_viewer_cannot_create_printer_through_setup_on_claimed_install(odin):
    vh = bearer(odin, *odin.viewer)
    assert odin.post("/api/printers", json={"name": "Viewer printer", "model": "X1C"}, headers=vh).status_code == 403
    r = odin.post("/api/setup/printer", json={"name": "Viewer setup printer", "model": "X1C",
                                              "api_type": "moonraker", "api_host": "192.0.2.77"}, headers=vh)
    assert r.status_code == 403, r.text


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/system/routes_setup.py:389-408 POST /api/setup/complete accepts any authenticated "
    "user. Scenario: on a claimed install whose wizard was not finished, a viewer POSTs /api/setup/complete "
    "and flips the install-wide setup_complete flag (a system configuration change reserved to admins)."))
def test_viewer_cannot_mark_setup_complete(tmp_path_factory):
    with LiveOdin(tmp_path_factory.mktemp("odin_setup_viewer")) as o:
        o.setup_admin()
        uname, pw, _ = o.create_user("viewer_c", "viewer")
        r = o.post("/api/setup/complete", headers=o.bearer(uname, pw))
        assert r.status_code == 403, r.text


COMPLETE_DEFECT = (
    "DEFECT: backend/modules/system/routes_setup.py:405 records completion with SystemConfig(value=\"true\") on a "
    "JSON column, so SQLite stores the text '\"true\"'; _setup_is_complete (routes_setup.py:109-112) compares the raw "
    "value to 'true' and the install is never read as complete (only a second POST /setup/complete takes the raw "
    "UPDATE path at :403 and stores 'true'). Scenario: the Setup wizard (frontend/src/pages/auth/Setup.tsx:256) calls "
    "/api/setup/complete once; afterwards /api/setup/status reports is_complete=false, any authenticated viewer can "
    "still POST /api/setup/printer to create printers with arbitrary api_host (and a repeated name returns 500), "
    "and admins can still rewrite host_ip via /api/setup/network. SQLite (default) verified; PostgreSQL not probed.")


def test_admin_completes_setup_via_wizard_endpoint(odin):
    r = odin.post("/api/setup/complete", headers=odin.h)
    assert r.status_code == 200 and r.json() == {"status": "complete"}


@pytest.mark.xfail(strict=True, reason=COMPLETE_DEFECT)
def test_single_wizard_completion_is_reported_complete(odin):
    st = odin.get("/api/setup/status").json()
    assert st == {"needs_setup": False, "has_users": True, "is_complete": True}


@pytest.mark.xfail(strict=True, reason=COMPLETE_DEFECT)
def test_after_single_wizard_completion_setup_printer_is_locked_and_never_500(odin):
    vh = bearer(odin, *odin.viewer)
    codes = [odin.post("/api/setup/printer", json={"name": "Post-completion printer", "model": "X1C"}, headers=vh).status_code
             for _ in range(2)]
    assert codes == [403, 403], codes


def test_second_completion_call_records_flag_that_locks(odin):
    # The only real API path that stores the raw 'true' the lock reads (see COMPLETE_DEFECT).
    assert odin.post("/api/setup/complete", headers=odin.h).status_code == 200
    assert odin.sql("SELECT value FROM system_config WHERE key='setup_complete'") == [("true",)]
    assert odin.get("/api/setup/status").json() == {"needs_setup": False, "has_users": True, "is_complete": True}


def _assert_locked_matrix(o):
    principals = {"none": {}, "viewer": bearer(o, *o.viewer), "admin": bearer(o, *o.admin)}
    users_before = o.sql("SELECT COUNT(*) FROM users")[0][0]
    printers_before = o.sql("SELECT COUNT(*) FROM printers")[0][0]
    seen = {}
    for who, h in principals.items():
        for path, body in SETUP_MUTATIONS:
            r = o.post(path, json=body, headers=h)
            seen[(who, path)] = r.status_code
            assert r.status_code < 500, (who, path, r.status_code, r.text)
            if who == "admin":
                assert r.status_code == 403, (who, path, r.text)
            elif who == "none" and path in ("/api/setup/admin", "/api/setup/test-printer"):
                assert r.status_code == 403
            else:
                assert r.status_code in (401, 403), (who, path, r.status_code)
            # malformed bodies after completion are client errors, never 5xx
            bad = o.post(path, data=b"{not json", headers={**h, "Content-Type": "application/json"})
            assert 400 <= bad.status_code < 500, (who, path, bad.status_code, bad.text)
    assert o.sql("SELECT COUNT(*) FROM users")[0][0] == users_before
    assert o.sql("SELECT COUNT(*) FROM printers")[0][0] == printers_before
    assert o.sql("SELECT value FROM system_config WHERE key='host_ip'") == []
    assert o.get("/api/setup/network").status_code == 401
    assert o.get("/api/setup/network", headers=principals["admin"]).status_code == 200
    return seen


def test_recorded_completion_makes_every_setup_mutation_403_not_500(odin):
    _assert_locked_matrix(odin)


def test_recorded_completion_stays_locked_after_restart(odin):
    odin.restart()
    _assert_locked_matrix(odin)
    assert odin.get("/api/setup/status").json()["is_complete"] is True


def test_recorded_completion_stays_locked_even_if_all_users_are_removed(tmp_path_factory):
    with LiveOdin(tmp_path_factory.mktemp("odin_setup_empty")) as o:
        o.setup_admin()
        for _ in range(2):  # second call stores the raw flag the lock reads (COMPLETE_DEFECT)
            assert o.post("/api/setup/complete", headers={"Authorization": "Bearer " + o.admin_token}).status_code == 200
        execute(o, "DELETE FROM users")
        r = o.post("/api/setup/admin", json={"username": "hijack", "email": "h@example.test", "password": strong_password()})
        assert r.status_code == 403
        assert o.sql("SELECT COUNT(*) FROM users")[0][0] == 0


# --------------------------------------------------------------------------------- revocation gap

@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/system/routes_setup.py:168 issues the first admin's JWT without recording an "
    "active_sessions row, and backend/modules/organizations/routes_users.py:276-281 revokes only jtis found in "
    "active_sessions. Scenario: whoever captured the token returned by POST /api/setup/admin (wizard browser, "
    "proxy log, shared lab machine) keeps full admin API access for its 24 h lifetime after the admin changes "
    "the password specifically to cut them off."))
def test_setup_issued_admin_token_is_revoked_by_password_change(odin):
    setup_token = {"Authorization": "Bearer " + odin.admin_token}
    assert odin.get("/api/auth/me", headers=setup_token).status_code == 200
    admin_id = odin.sql("SELECT id FROM users WHERE username=:u", u=odin.admin[0])[0][0]
    new_pw = strong_password()
    assert odin.patch(f"/api/users/{admin_id}", json={"password": new_pw}, headers=bearer(odin, *odin.admin)).status_code == 200
    odin.admin = (odin.admin[0], new_pw)
    assert odin.get("/api/auth/me", headers=setup_token).status_code == 401
