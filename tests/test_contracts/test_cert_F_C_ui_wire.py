"""Cert F / cluster C: producer wire fixtures for the UI cert tests, captured from a real ODIN server.

``test_capture`` records the real responses the frontend consumes (auth/me + permissions per role, admin quota
list/update, IP allowlist get/put/invalid) into artifacts/application-certification/cert-F/C-ui-wire.json.
``test_frontend_fixtures_match_producer`` then checks that the JSON embedded in each frontend
``*.cert.test.tsx`` (between /*WIRE-BEGIN*/ and /*WIRE-END*/) is exactly the captured producer payload, so
the UI tests cannot drift from the real backend contract.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "artifacts/application-certification/cert-F/C-ui-wire.json"
UI = {
    "rolegate": ROOT / "frontend/src/components/auth/RoleGate.settings-audit.cert.test.tsx",
    "quota": ROOT / "frontend/src/components/admin/QuotaManager.quotas.cert.test.tsx",
    "allowlist": ROOT / "frontend/src/components/admin/IPAllowlistSettings.allowlist.cert.test.tsx",
}


def capture(tmp: Path) -> dict:
    wire = {"roles": {}}
    with LiveOdin(tmp, env={"ODIN_CERT_PROXY_HEADERS": "1"}) as o:
        o.setup_admin()
        ah = o.bearer(*o.admin)
        users = {}
        for role in ("operator", "viewer"):
            pw = strong_password()
            r = o.post("/api/users", json={"username": f"ui_{role}", "email": f"ui_{role}@example.test", "password": pw,
                                           "role": role}, headers=ah)
            assert r.status_code == 200
            users[role] = o.bearer(f"ui_{role}", pw)
        users["admin"] = ah
        for role, h in users.items():
            me = o.get("/api/auth/me", headers=h)
            perm = o.get("/api/permissions", headers=h)
            assert me.status_code == 200 and perm.status_code == 200
            wire["roles"][role] = {"me": me.json(), "permissions": perm.json()}
        # quotas: operator with a jobs limit and one consumed job
        op_id = wire["roles"]["operator"]["me"]["id"]
        assert o.put(f"/api/admin/quotas/{op_id}", json={"quota_jobs": 5, "quota_grams": 500, "quota_hours": 10,
                                                         "quota_period": "semester"}, headers=ah).status_code == 200
        assert o.post("/api/jobs", json={"item_name": "UI wire job"}, headers=users["operator"]).status_code == 201
        wire["admin_quotas"] = o.get("/api/admin/quotas", headers=ah).json()
        put = o.put(f"/api/admin/quotas/{op_id}", json={"quota_jobs": 9, "quota_period": "daily"}, headers=ah)
        wire["admin_quota_put"] = {"status": put.status_code, "body": put.json()}
        wire["admin_quotas_after_put"] = o.get("/api/admin/quotas", headers=ah).json()
        # IP allowlist (caller 127.0.0.1 -> lock-out protection appends 127.0.0.1/32)
        wire["allowlist_default"] = o.get("/api/config/ip-allowlist", headers=ah).json()
        put = o.put("/api/config/ip-allowlist", json={"enabled": True, "cidrs": ["10.20.0.0/16"], "mode": "api_and_ui"}, headers=ah)
        wire["allowlist_put"] = {"status": put.status_code, "body": put.json()}
        bad = o.put("/api/config/ip-allowlist", json={"enabled": True, "cidrs": ["10.20.0.0/33"], "mode": "api_and_ui"}, headers=ah)
        wire["allowlist_put_invalid"] = {"status": bad.status_code, "body": bad.json()}
        wire["allowlist_after"] = o.get("/api/config/ip-allowlist", headers=ah).json()
    return wire


def test_capture(tmp_path):
    wire = capture(tmp_path / "odin")
    # facts the UI tests depend on
    assert wire["roles"]["admin"]["permissions"]["page_access"]["settings"] == ["admin"]
    # The real RBAC map has no "audit" page key, which the /audit RoleGate looks up (see UI defect test).
    assert "audit" not in wire["roles"]["admin"]["permissions"]["page_access"]
    assert wire["allowlist_put"]["body"]["cidrs"] == ["10.20.0.0/16", "127.0.0.1/32"]
    assert wire["allowlist_put_invalid"]["status"] == 400
    OUT.write_text(json.dumps(wire, indent=1, sort_keys=True))


def _embedded(path: Path) -> dict:
    text = path.read_text()
    m = re.search(r"/\*WIRE-BEGIN\*/(.*?)/\*WIRE-END\*/", text, re.S)
    assert m, path
    return json.loads(m.group(1))


@pytest.mark.parametrize("name", sorted(UI))
def test_frontend_fixtures_match_producer(name):
    wire = json.loads(OUT.read_text())
    emb = _embedded(UI[name])
    for key, value in emb.items():
        assert key in wire, (name, key)
        if key == "roles":
            for role, doc in value.items():
                assert doc == wire["roles"][role], (name, role)
        else:
            assert value == wire[key], (name, key)
