"""Security review HIGH-B / close-out: backups and the diagnostics bundle are
interactive-session only, the bundle redacts transport config, and internal
marker rows never appear in it."""
from __future__ import annotations

import io
import json
import zipfile
import pytest
from tests.test_contracts.test_cert_F_00_live import LiveOdin


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("diag")).start()
    o.setup_admin()
    o.H = o.admin_headers()
    yield o
    o.stop()


def key(t):
    return {"X-API-Key": t}


def _admin_token(o):
    r = o.post("/api/tokens", json={"name": "diag", "scopes": ["admin"]}, headers=o.H)
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.mark.parametrize("method,path", [
    ("get", "/api/backups/does-not-exist.db"),
    ("post", "/api/backups"),
    ("post", "/api/backups/restore"),
    ("delete", "/api/backups/does-not-exist.db"),
    ("get", "/api/admin/support-bundle"),
])
def test_backup_and_bundle_refuse_an_admin_token(odin, method, path):
    tok = _admin_token(odin)
    r = getattr(odin, method)(path, headers=key(tok), **({"json": {}} if method in ("post",) else {}))
    assert r.status_code == 403, (path, r.status_code, r.text[:160])
    assert r.json()["detail"] == "API tokens cannot create or change credentials; sign in with a password session"


def test_support_bundle_redacts_transport_config_and_hides_markers(odin):
    # Seed an SMTP config and an internal quota marker, then pull the bundle interactively.
    from sqlalchemy import text
    odin.put("/api/smtp-config", json={"enabled": True, "host": "smtp.secret.test", "port": 587,
             "username": "relay", "password": "topsecret", "from_address": "a@b.test", "use_tls": True,
             "base_url": "https://odin.example.test"}, headers=odin.H)
    with odin.engine.begin() as c:
        c.execute(text("INSERT INTO system_config(key, value) VALUES('quota_attempt:job:1:x', 'true')"))
    with odin.engine.begin() as c:
        for prefix in ("outside_print_binding:", "outside_print_previous_job:"):
            c.execute(text("INSERT INTO system_config(key,value) VALUES(:key,:value)"), {"key":prefix+"120", "value":json.dumps({"statement":"synthetic private attestation","actor_user_id":12,"filename":"private.3mf","configuration_key":"synthetic"})})
    r = odin.get("/api/admin/support-bundle", headers=odin.H)
    assert r.status_code == 200, r.text
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    settings = json.loads(zf.read("settings_safe.json"))
    assert settings.get("smtp_config") == "[REDACTED]"
    assert settings.get("base_url") == "[REDACTED]"
    assert "topsecret" not in r.content.decode("utf-8", "replace")
    assert not any(k.startswith(("quota_attempt", "outside_print_binding:", "outside_print_previous_job:")) for k in settings)
    assert "synthetic private attestation" not in json.dumps(settings)
