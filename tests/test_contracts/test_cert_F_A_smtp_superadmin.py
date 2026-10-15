"""Security review HIGH 2: one SMTP transport and one reset-link URL serve every organization,
so only the install's superadmin may read or change them. Real server (LiveOdinB)."""
from __future__ import annotations

import pytest

from tests.test_contracts.test_cert_F_B_00_support import LiveOdinB

SMTP = {"enabled": True, "host": "smtp.example.test", "port": 587, "username": "relay", "password": "",
        "from_address": "odin@example.test", "use_tls": True}


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdinB(tmp_path_factory.mktemp("smtp-superadmin")).start()
    o.boot_admin()
    org = o.post("/api/orgs", json={"name": "Tenant A"}, headers=o.admin_bearer).json()["id"]
    o.org_admin = o.mkuser("tenant_admin", "admin", group_id=org)
    assert o.one("SELECT group_id FROM users WHERE id=:i", i=o.org_admin["id"]).group_id == org
    yield o
    o.stop()


def test_org_scoped_admin_cannot_read_or_redirect_smtp(odin):
    put = odin.put("/api/smtp-config", json={**SMTP, "host": "smtp.attacker.example.test",
                                            "base_url": "https://attacker.example.test"}, headers=odin.org_admin["headers"])
    assert put.status_code == 403, put.text
    assert odin.one("SELECT 1 FROM system_config WHERE key IN ('smtp_config','base_url')") is None
    assert odin.get("/api/smtp-config", headers=odin.org_admin["headers"]).status_code == 403


def test_superadmin_sets_smtp_and_https_application_url(odin):
    r = odin.put("/api/smtp-config", json={**SMTP, "base_url": "https://odin.example.test"}, headers=odin.admin_bearer)
    assert r.status_code == 200, r.text
    assert odin.get("/api/smtp-config", headers=odin.admin_bearer).json()["base_url"] == "https://odin.example.test"


@pytest.mark.parametrize("url", ["http://odin.example.test", "http://93.184.216.34:8000"])
def test_plain_http_application_url_is_refused_for_public_hosts(odin, url):
    r = odin.put("/api/smtp-config", json={**SMTP, "base_url": url}, headers=odin.admin_bearer)
    assert r.status_code == 400 and "https" in r.json()["detail"]
    assert odin.get("/api/smtp-config", headers=odin.admin_bearer).json()["base_url"] == "https://odin.example.test"


@pytest.mark.parametrize("url", ["http://192.168.1.50:8000", "http://localhost:8000", "http://printfarm.local", "http://odin:8000"])
def test_plain_http_application_url_is_allowed_on_the_lan(odin, url):
    r = odin.put("/api/smtp-config", json={**SMTP, "base_url": url}, headers=odin.admin_bearer)
    assert r.status_code == 200, r.text
    assert odin.get("/api/smtp-config", headers=odin.admin_bearer).json()["base_url"] == url
