"""Cert F / cluster B — F11-11.6-01/02/03 scoped API tokens on the real server (LiveOdin).

Claims: "`odin_` prefixed tokens with granular per-route scopes", "Scopes: read:printers, write:jobs, etc.",
"APITokenManager component for CRUD" (backend half: create/list/revoke wire contract).
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest
import requests

from tests.test_contracts.test_cert_F_B_00_support import LiveOdinB
from tests.test_contracts.test_route_auth_census import PUBLIC

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / "artifacts" / "application-certification" / "cert-F"
CAPTURE = RECEIPTS / "B-wire-captures.json"


def capture(key, value):
    data = json.loads(CAPTURE.read_text()) if CAPTURE.exists() else {}
    data[key] = value
    CAPTURE.write_text(json.dumps(data, indent=1, default=str))


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdinB(tmp_path_factory.mktemp("tokens")).start()
    o.boot_admin()
    o.operator = o.mkuser("tok_operator", "operator")
    o.viewer = o.mkuser("tok_viewer", "viewer")
    o.victim = o.mkuser("tok_victim", "viewer", login=False)
    r = o.post("/api/printers", json={"name": "Token printer", "api_type": "moonraker", "api_host": "192.0.2.21"}, headers=o.admin_bearer)
    assert r.status_code == 201, r.text
    o.printer_id = r.json()["id"]
    r = o.post("/api/jobs", json={"item_name": "Token job"}, headers=o.operator["headers"])
    assert r.status_code == 201, r.text
    o.job_id = r.json()["id"]
    yield o
    o.stop()


def key(token):
    return {"X-API-Key": token}


# ---------------------------------------------------------------- F11-11.6-01 prefix + lifecycle
def test_minted_token_is_odin_prefixed_hashed_listed_and_revocable(odin):
    created = odin.mint(odin.operator["headers"], ["read:printers"], name="Cert prefix token")
    raw = created["token"]
    assert raw.startswith("odin_") and len(raw) > 30
    assert created["prefix"] == raw[:10] and created["scopes"] == ["read:printers"]
    row = odin.one("SELECT token_hash, token_prefix, scopes FROM api_tokens WHERE id=:i", i=created["id"])
    assert row.token_prefix == raw[:10] and raw not in row.token_hash and json.loads(row.scopes) == ["read:printers"]
    listed = odin.get("/api/tokens", headers=odin.operator["headers"]).json()
    entry = [t for t in listed if t["id"] == created["id"]]
    assert entry and entry[0]["prefix"] == raw[:10] and "token" not in entry[0] and "token_hash" not in entry[0]
    capture("tokens_create", {k: created[k] for k in created if k != "token"} | {"token": "odin_" + "X" * 43})
    capture("tokens_list", listed)
    assert odin.get("/api/printers", headers=key(raw)).status_code == 200
    assert odin.delete(f"/api/tokens/{created['id']}", headers=odin.operator["headers"]).status_code == 200
    assert odin.get("/api/printers", headers=key(raw)).status_code == 401
    assert all(t["id"] != created["id"] for t in odin.get("/api/tokens", headers=odin.operator["headers"]).json())


def test_fabricated_and_non_prefixed_tokens_are_refused(odin):
    real = odin.mint(odin.operator["headers"], ["read:printers"])["token"]
    assert odin.get("/api/printers", headers=key(real[:-2] + "zz")).status_code == 401
    assert odin.get("/api/printers", headers=key(real.replace("odin_", "nido_", 1))).status_code == 401


# ---------------------------------------------------------------- F11-11.6-02 scope vocabulary + enforcement
@pytest.mark.parametrize("scope", ["read:printers", "write:printers", "read:jobs", "write:jobs", "read:spools", "write:spools",
                                   "read:models", "write:models", "read:analytics", "read:education", "write:education",
                                   "read", "write", "agent:read", "agent:write"])
def test_advertised_scope_vocabulary_is_accepted(odin, scope):
    assert odin.mint(odin.operator["headers"], [scope])["scopes"] == [scope]


@pytest.mark.parametrize("bad", [["printers:read"], ["read:everything"], ["superuser"], []])
def test_unknown_or_empty_scopes_are_rejected(odin, bad):
    r = odin.post("/api/tokens", json={"name": "bad", "scopes": bad}, headers=odin.operator["headers"])
    assert r.status_code == 400


def test_admin_scope_requires_admin_role(odin):
    r = odin.post("/api/tokens", json={"name": "esc", "scopes": ["admin"]}, headers=odin.operator["headers"])
    assert r.status_code == 403


def test_resource_scopes_are_enforced_per_route(odin):
    op = odin.operator["headers"]
    printers = odin.mint(op, ["read:printers"])["token"]
    jobs_read = odin.mint(op, ["read:jobs"])["token"]
    jobs_write = odin.mint(op, ["write:jobs"])["token"]
    # read:printers grants printer reads only
    assert odin.get("/api/printers", headers=key(printers)).status_code == 200
    assert odin.get("/api/jobs", headers=key(printers)).status_code == 403
    assert odin.get("/api/spools", headers=key(printers)).status_code == 403
    assert odin.patch(f"/api/printers/{odin.printer_id}", json={"nickname": "nope"}, headers=key(printers)).status_code == 403
    # read:jobs reads but cannot create
    assert odin.get("/api/jobs", headers=key(jobs_read)).status_code == 200
    before = odin.one("SELECT COUNT(*) AS n FROM jobs").n
    assert odin.post("/api/jobs", json={"item_name": "denied"}, headers=key(jobs_read)).status_code == 403
    assert odin.one("SELECT COUNT(*) AS n FROM jobs").n == before
    # write:jobs creates jobs, but does not grant printer writes
    r = odin.post("/api/jobs", json={"item_name": "Scoped write job"}, headers=key(jobs_write))
    assert r.status_code == 201, r.text
    assert odin.one("SELECT item_name FROM jobs WHERE id=:i", i=r.json()["id"]).item_name == "Scoped write job"
    assert odin.patch(f"/api/printers/{odin.printer_id}", json={"nickname": "nope"}, headers=key(jobs_write)).status_code == 403
    assert odin.one("SELECT nickname FROM printers WHERE id=:i", i=odin.printer_id).nickname is None


def test_token_cannot_exceed_owner_role(odin):
    viewer_write = odin.mint(odin.viewer["headers"], ["write:printers"])["token"]
    assert odin.patch(f"/api/printers/{odin.printer_id}", json={"nickname": "viewer"}, headers=key(viewer_write)).status_code == 403


# ---------------------------------------------------------------- scope bypass defects (security)
# Scoped tokens: routes without a declared scope need the generic read/write scope (core/rbac.py).


def test_read_only_admin_token_cannot_reconfigure_smtp(odin):
    ro = odin.mint(odin.admin_bearer, ["read"])["token"]
    r = odin.put("/api/smtp-config", json={"enabled": True, "host": "smtp.attacker.example.test", "port": 25, "username": "x",
                                           "password": "", "from_address": "odin@example.test", "use_tls": False}, headers=key(ro))
    stored = odin.one("SELECT value FROM system_config WHERE key='smtp_config'")
    assert r.status_code == 403 and (stored is None or "attacker" not in stored.value)


def test_read_only_admin_token_cannot_rewrite_sso(odin):
    ro = odin.mint(odin.admin_bearer, ["read"])["token"]
    r = odin.put("/api/admin/oidc", json={"provider_type": "generic", "client_id": "attacker", "is_enabled": True,
                                          "discovery_url": "https://idp.attacker.example.test/.well-known/openid-configuration"}, headers=key(ro))
    row = odin.one("SELECT client_id FROM oidc_config ORDER BY id LIMIT 1")
    assert r.status_code == 403 and (row is None or row.client_id != "attacker")


def test_read_only_admin_token_cannot_gdpr_erase_users(odin):
    ro = odin.mint(odin.admin_bearer, ["read"])["token"]
    r = odin.delete(f"/api/users/{odin.victim['id']}/erase", headers=key(ro))
    assert r.status_code == 403
    assert odin.one("SELECT username FROM users WHERE id=:i", i=odin.victim["id"]).username == "tok_victim"


def test_read_printers_token_cannot_start_jobs(odin):
    ro = odin.mint(odin.operator["headers"], ["read:printers"])["token"]
    r = odin.post(f"/api/jobs/{odin.job_id}/start", headers=key(ro))
    assert r.status_code == 403
    assert odin.one("SELECT status FROM jobs WHERE id=:i", i=odin.job_id).status == "pending"


def test_unrelated_scope_cannot_read_admin_data(odin):
    edu = odin.mint(odin.admin_bearer, ["read:education"])["token"]
    statuses = {p: odin.get(p, headers=key(edu)).status_code for p in ("/api/users", "/api/admin/oidc", "/api/smtp-config", "/api/admin/logs")}
    assert set(statuses.values()) == {403}, statuses


# Non-GET routes whose declared scope is a read scope (a read token may reach them).
READ_SCOPED_POSTS = {
    "POST /api/cameras/{printer_id}/webrtc",  # WebRTC signaling to view a camera (read:printers)
    "POST /api/auth/ws-token",                # short-lived telemetry socket token (read)
}
# Destructive or process-level routes not probed live (their scope checks are census-covered elsewhere).
SKIP_SIDE_EFFECTS = ("restore", "restart", "shutdown", "license", "logout")
CREDENTIAL_DETAIL = "API tokens cannot create or change credentials; sign in with a password session"
# Routes that create or change authentication material: interactive password sessions only.
CREDENTIAL_ROUTES = [
    ("POST", "/api/auth/biometric-token?device_id=cert-device"), ("DELETE", "/api/auth/biometric-token?device_id=cert-device"),
    ("POST", "/api/auth/mfa/setup"), ("POST", "/api/auth/mfa/confirm"), ("DELETE", "/api/auth/mfa"),
    ("DELETE", "/api/admin/users/{victim}/mfa"), ("PUT", "/api/config/require-mfa"),
    ("POST", "/api/tokens"), ("DELETE", "/api/tokens/{token}"),
    ("DELETE", "/api/sessions"), ("DELETE", "/api/sessions/99999"), ("DELETE", "/api/admin/sessions/99999"),
    ("POST", "/api/users"), ("POST", "/api/users/import"), ("POST", "/api/users/{victim}/reset-password-email"),
    ("PUT", "/api/admin/oidc"), ("PUT", "/api/smtp-config"),
]


def test_census_every_mutating_route_refuses_read_only_token(odin, tmp_path_factory):
    import subprocess, sys, os
    from tests.test_contracts.test_cert_F_B_route_census import SNAPSHOT
    d = tmp_path_factory.mktemp("scope-snapshot")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ODIN_", "API_KEY", "DATABASE_"))}
    env.update(DATABASE_URL=f"sqlite:///{d}/s.db", LICENSE_DIR=str(d), PYTHONPATH=str(ROOT / "backend"))
    subprocess.run([sys.executable, "-c", SNAPSHOT, str(d / "s.json")], cwd=ROOT / "backend", env=env, check=True,
                   capture_output=True, timeout=300)
    pairs = json.loads((d / "s.json").read_text())["pairs"]
    ro = odin.mint(odin.admin_bearer, ["read"])["token"]
    rows = []
    for m, p in pairs:
        if m == "GET" or not p.startswith("/api/"):
            continue
        canonical = re.sub(r"^/api/v1/", "/api/", p)
        if f"{m} {canonical}" in PUBLIC or f"{m} {canonical}" in READ_SCOPED_POSTS or any(x in p for x in SKIP_SIDE_EFFECTS):
            continue
        url = re.sub(r"\{[^}]+\}", "99999", p).replace(":path", "")
        rows.append([m, p, requests.request(m, odin.url + url, json={}, headers=key(ro), timeout=30).status_code])
    unscoped = [r for r in rows if r[2] != 403]
    (RECEIPTS / "B-token-scope-census.json").write_text(json.dumps({
        "probed": len(rows), "histogram": dict(Counter(str(r[2]) for r in rows)), "not_refused_by_scope": unscoped}, indent=1))
    assert unscoped == []


def _credential_calls(odin, headers):
    other = odin.mint(odin.admin_bearer, ["read"])["id"]
    for method, path in CREDENTIAL_ROUTES:
        url = path.replace("{victim}", str(odin.victim["id"])).replace("{token}", str(other))
        body = {"name": "escalate", "scopes": ["admin"]} if path == "/api/tokens" else {}
        yield method, path, requests.request(method, odin.url + url, json=body, headers=headers, timeout=30)


@pytest.mark.parametrize("scopes", [["write"], ["admin"], ["read", "write", "admin"]])
def test_no_api_token_can_create_or_change_credentials(odin, scopes):
    """Security review HIGH 1: a ['write'] token minted a 30-day biometric refresh token (-> admin JWT)."""
    token = odin.mint(odin.admin_bearer, scopes)["token"]
    before = {t: odin.one(f"SELECT COUNT(*) AS n FROM {t}").n for t in ("api_tokens", "biometric_tokens", "users")}
    results = {(m, p): r for m, p, r in _credential_calls(odin, key(token))}
    wrong = {k: (r.status_code, r.text[:120]) for k, r in results.items()
             if r.status_code != 403 or r.json().get("detail") != CREDENTIAL_DETAIL}
    assert wrong == {}, wrong
    after = {t: odin.one(f"SELECT COUNT(*) AS n FROM {t}").n for t in ("api_tokens", "biometric_tokens", "users")}
    assert after == {**before, "api_tokens": before["api_tokens"] + 1}  # only the probe's own revocation target
    hash_before = odin.one("SELECT password_hash FROM users WHERE id=:i", i=odin.victim["id"]).password_hash
    patch = odin.patch(f"/api/users/{odin.victim['id']}", json={"password": "Escalated12345"}, headers=key(token))
    assert patch.status_code == 403  # admin-scope floor first, then the credential rule for admin tokens
    if "admin" in scopes:
        assert patch.json()["detail"] == CREDENTIAL_DETAIL
    assert odin.one("SELECT password_hash FROM users WHERE id=:i", i=odin.victim["id"]).password_hash == hash_before


def test_biometric_escalation_chain_is_closed_but_sessions_still_work(odin):
    token = odin.mint(odin.admin_bearer, ["write"])["token"]
    r = odin.post("/api/auth/biometric-token?device_id=cert-escalation", headers=key(token))
    assert r.status_code == 403
    assert odin.one("SELECT COUNT(*) AS n FROM biometric_tokens").n == 0
    ok = odin.post("/api/auth/biometric-token?device_id=cert-session", headers=odin.admin_bearer)
    assert ok.status_code == 200, ok.text
    odin.delete("/api/auth/biometric-token?device_id=cert-session", headers=odin.admin_bearer)
