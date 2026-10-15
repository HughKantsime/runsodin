"""Cert F / cluster D — F12-12.4-01 (full JSON export) and F12-12.4-02 (anonymization) on the real server.

User-generated rows are created through real HTTP routes where a route exists (login sessions,
alert preferences, API tokens, push subscription/device registration); rows that only server
internals produce (jobs, alerts, archives, uploads, audit actor rows) are seeded with SQL into the
real bootstrapped schema. The export / erase endpoints themselves are exercised over HTTP.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time

import pytest

from tests.test_contracts.test_cert_F_00_live import LiveOdin


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odinD_gdpr")).start()
    o.setup_admin()
    o.H = o.admin_headers()
    yield o
    o.stop()


def _create(o, username, role):
    """Create a user with the cached admin bearer (LiveOdin.create_user logs in each call)."""
    from tests.test_contracts.test_cert_F_00_live import strong_password
    pw = strong_password()
    r = o.post("/api/users", headers=o.H, json={"username": username, "email": f"{username}@example.test",
               "password": pw, "role": role})
    assert r.status_code in (200, 201), r.text
    return username, pw, r.json()


def _exec(o, statement, **params):
    from sqlalchemy import text
    with o.engine.begin() as c:
        c.execute(text(statement), params)


def _uid(o, name):
    return o.sql("SELECT id FROM users WHERE username=:n", n=name)[0][0]


def _seed_user_world(o, username, role="operator"):
    """Create a user and personal data in every user-linked table. Returns (uid, headers, marks)."""
    _, pw, _ = _create(o, username, role)
    uid = _uid(o, username)
    h = o.bearer(username, pw)                        # active_sessions + login_attempts rows
    o.login(username, "wrong-" + pw)                  # failed attempt row
    m = {k: f"{k}-{secrets.token_hex(5)}" for k in
         ("job", "alert", "archive", "upload", "project", "push_ep", "token_name", "audit")}
    m["device"] = secrets.token_hex(32)
    assert o.put("/api/alert-preferences", headers=h, json={"preferences": [
        {"alert_type": "print_complete", "in_app": True, "browser_push": False, "email": True}]}).status_code == 200
    r = o.post("/api/tokens", headers=h, json={"name": m["token_name"], "scopes": ["read"]})
    assert r.status_code in (200, 201), r.text
    r = o.post("/api/push/subscribe", headers=h, json={"endpoint": f"https://push.example.test/{m['push_ep']}",
               "p256dh_key": "BOr" + secrets.token_hex(20), "auth_key": secrets.token_hex(8)})
    assert r.status_code in (200, 201), r.text
    device_id = "a1b2c3d4-0000-4000-8000-" + secrets.token_hex(6)
    r = o.post("/api/push/register", headers=h, json={"device_id": device_id, "platform": "apns", "token": m["device"]})
    assert r.status_code in (200, 201), r.text
    for i in range(3):
        _exec(o, "INSERT INTO jobs (item_name, status, submitted_by, charged_to_user_id, quantity, duration_hours, "
              "created_at, updated_at) VALUES (:n, 'completed', :u, :u, 1, 2.0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
              n=f"{m['job']}-{i}", u=uid)
    _exec(o, "INSERT INTO alerts (user_id, alert_type, severity, title, message, is_read, is_dismissed) "
          "VALUES (:u, 'print_complete', 'info', :t, 'done', 0, 0)", u=uid, t=m["alert"])
    _exec(o, "INSERT INTO print_archives (user_id, print_name, status, filament_used_grams, created_at) "
          "VALUES (:u, :n, 'completed', 42.5, CURRENT_TIMESTAMP)", u=uid, n=m["archive"])
    _exec(o, "INSERT INTO print_files (filename, original_filename, created_by, storage_bytes, blob_state, uploaded_at) "
          "VALUES (:f, :f, :u, 10, 'present', CURRENT_TIMESTAMP)", f=m["upload"] + ".3mf", u=uid)
    _exec(o, "INSERT INTO projects (name, created_by) VALUES (:n, :u)", n=m["project"], u=uid)
    # A real audited action performed BY the user (actor_user_id = uid, entity_type='printer').
    r = o.post("/api/printers", headers=h, json={"name": m["audit"], "api_type": "moonraker", "api_host": "192.168.60.5"})
    assert r.status_code == 201, r.text
    assert o.sql("SELECT COUNT(*) FROM audit_logs WHERE actor_user_id=:u AND entity_type='printer'", u=uid)[0][0] == 1
    return uid, h, m


# ---------------------------------------------------------------- F12-12.4-01 ---------------

@pytest.fixture(scope="module")
def exported(odin):
    o = odin
    uid, h, m = _seed_user_world(o, "certd_export")
    r = o.get(f"/api/users/{uid}/export", headers=h)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/json")
    return uid, m, r.json(), r.text


def test_export_contains_profile_jobs_sessions_prefs_tokens_quota(exported):
    uid, m, data, text = exported
    assert data["user"]["id"] == uid and data["user"]["username"] == "certd_export"
    assert "password_hash" not in data["user"] and "mfa_secret" not in data["user"]
    assert sorted(j["item_name"] for j in data["jobs_submitted"]) == [f"{m['job']}-{i}" for i in range(3)]
    assert len(data["active_sessions"]) >= 1
    assert any(p["alert_type"] == "print_complete" for p in data["alert_preferences"])
    assert [t["name"] for t in data["api_tokens"]] == [m["token_name"]]
    assert all("token_hash" not in t for t in data["api_tokens"])
    for key in ("quota_usage", "audit_log_entries", "classroom_identity", "classroom_memberships"):
        assert isinstance(data[key], list)


def test_export_forbidden_for_other_non_admin_user(odin, exported):
    o = odin
    uid = exported[0]
    _, pw, _ = _create(o, "certd_other", "viewer")
    r = o.get(f"/api/users/{uid}/export", headers=o.bearer("certd_other", pw))
    assert r.status_code == 403


def test_export_includes_every_user_linked_record(exported):
    _, m, _, text = exported
    missing = [k for k in ("alert", "archive", "upload", "project", "push_ep", "device", "audit") if m[k] not in text]
    assert missing == [], missing


# ---------------------------------------------------------------- F12-12.4-02 ---------------

def _analytics_snapshot(o):
    a = o.get("/api/analytics", headers=o.H)
    assert a.status_code == 200, a.text
    s = o.get("/api/stats", headers=o.H)
    assert s.status_code == 200, s.text
    return a.json(), s.json()


@pytest.fixture(scope="module")
def erased(odin):
    o = odin
    name = "certd_erase_" + secrets.token_hex(3)
    uid, h, m = _seed_user_world(o, name)
    email = f"{name}@example.test"
    jobs_before = o.sql("SELECT COUNT(*), SUM(duration_hours) FROM jobs WHERE submitted_by=:u", u=uid)[0]
    grams_before = o.sql("SELECT SUM(filament_used_grams) FROM print_archives WHERE user_id=:u", u=uid)[0][0]
    analytics_before = _analytics_snapshot(o)
    r = o.delete(f"/api/users/{uid}/erase", headers=o.H)
    assert r.status_code == 200, r.text
    return dict(o=o, uid=uid, name=name, email=email, m=m, h=h, jobs_before=tuple(jobs_before),
                grams_before=grams_before, analytics_before=analytics_before)


def test_erase_anonymizes_account_and_revokes_access(erased):
    o, uid = erased["o"], erased["uid"]
    row = o.sql("SELECT username, email, password_hash, is_active, mfa_secret, oidc_subject FROM users WHERE id=:u", u=uid)[0]
    assert row[0] == f"[deleted-{uid}]" and row[1] == "[deleted]" and row[2] == "[deleted]"
    assert not row[3] and row[4] is None and row[5] is None
    assert o.get("/api/auth/me", headers=erased["h"]).status_code == 401
    for t in ("active_sessions", "api_tokens", "alert_preferences", "push_subscriptions"):
        assert o.sql(f"SELECT COUNT(*) FROM {t} WHERE user_id=:u", u=uid)[0][0] == 0, t


def test_erase_preserves_analytics_records_and_totals(erased):
    o, uid = erased["o"], erased["uid"]
    assert tuple(o.sql("SELECT COUNT(*), SUM(duration_hours) FROM jobs WHERE submitted_by=:u", u=uid)[0]) == erased["jobs_before"]
    assert o.sql("SELECT SUM(filament_used_grams) FROM print_archives WHERE user_id=:u", u=uid)[0][0] == erased["grams_before"]
    assert _analytics_snapshot(o) == erased["analytics_before"]


def test_erase_rejected_for_non_admin_and_last_admin(odin):
    o = odin
    _, pw, _ = _create(o, "certd_op2", "operator")
    target = _uid(o, "certd_other")
    assert o.delete(f"/api/users/{target}/erase", headers=o.bearer("certd_op2", pw)).status_code == 403
    assert o.delete("/api/users/1/erase", headers=o.H).status_code == 400   # sole admin


def test_erase_leaves_no_former_identifier_anywhere_in_database(erased):
    o = erased["o"]
    needles = [erased["name"], erased["email"], erased["m"]["device"]]
    con = sqlite3.connect(str(o.db_path))
    hits = set()
    try:
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in con.execute(f"SELECT * FROM '{t}'"):
                blob = " ".join(str(v) for v in row if v is not None)
                for n in needles:
                    if n in blob:
                        hits.add((t, needles.index(n)))
    finally:
        con.close()
    erased["o"].erase_residue = sorted(hits)
    assert sorted(hits) == [], [(t, ("username", "email", "device_token")[i]) for t, i in sorted(hits)]


# ------------------------------------------------- security review MED 5: structural scrub
def _audit(o, action, details):
    _exec(o, "INSERT INTO audit_logs (action, entity_type, entity_id, details, ip_address) "
          "VALUES (:a, 'system', NULL, :d, '192.0.2.9')", a=action, d=json.dumps(details))
    return o.sql("SELECT MAX(id) FROM audit_logs")[0][0]


def _details(o, row_id):
    return o.sql("SELECT details FROM audit_logs WHERE id=:i", i=row_id)[0][0]


def test_erase_scrubs_non_ascii_identifiers_stored_json_escaped(odin):
    o = odin
    name = "zoë_ström_" + secrets.token_hex(2)
    _create(o, name, "viewer")
    uid = _uid(o, name)
    row = _audit(o, "export.requested", {"by": name, "note": f"Requested by {name} today"})
    assert name not in _details(o, row) and "\\u00eb" in _details(o, row)   # stored escaped
    assert o.delete(f"/api/users/{uid}/erase", headers=o.H).status_code == 200
    stored = json.loads(_details(o, row))
    assert stored == {"by": "[deleted-user]", "note": "Requested by [deleted-user] today"}


def test_identifier_colliding_with_a_json_key_and_bystanders_are_left_intact(odin):
    o = odin
    _create(o, "username", "viewer")          # a real account literally named "username"
    _create(o, "username2", "viewer")         # a bystander whose name contains it
    uid = _uid(o, "username")
    target = _audit(o, "auth.login_failed", {"username": "username"})
    bystander = _audit(o, "auth.login_failed", {"username": "username2", "note": "username2 from lab"})
    unrelated = _audit(o, "printer.created", {"name": "Lab", "username": "someone-else"})
    before = {r: _details(o, r) for r in (bystander, unrelated)}
    assert o.delete(f"/api/users/{uid}/erase", headers=o.H).status_code == 200
    assert json.loads(_details(o, target)) == {"username": "[deleted-user]"}   # key kept, value scrubbed
    assert {r: _details(o, r) for r in (bystander, unrelated)} == before         # byte-identical
