from __future__ import annotations

import json
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _ready_database(tmp_path: Path):
    from core.schema import bootstrap_database

    path = tmp_path / "preflight.db"
    engine = create_engine(f"sqlite:///{path}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO groups (id, name, is_org) VALUES (1, 'CTEC', 1)"))
        connection.execute(text("INSERT INTO users (id, username, email, password_hash, role, is_active, group_id) VALUES (1, 'admin', 'admin@ctechigh.org', 'x', 'admin', 1, 1), (2, 'teacher', 'teacher@ctechigh.org', '', 'viewer', 1, 1), (3, 'student', 'student@ctechigh.org', '', 'viewer', 1, 1)"))
        connection.execute(text("INSERT INTO system_config (key, value) VALUES ('education_mode', 'true')"))
        connection.execute(text("INSERT INTO oidc_config (id, display_name, client_id, client_secret_encrypted, discovery_url, scopes, auto_create_users, default_role, default_group_id, provider_type, allowed_domains, is_enabled) VALUES (1, 'Google Workspace', 'client-id', 'encrypted-secret-canary', 'https://accounts.google.com/.well-known/openid-configuration', 'openid profile email', 1, 'viewer', 1, 'google', 'ctechigh.org', 1)"))
        connection.execute(text("INSERT INTO classroom_connections (org_id, client_id, client_secret_encrypted, allowed_domains, refresh_token_encrypted, state, created_by) VALUES (1, 'classroom-client', 'encrypted-client-canary', 'ctechigh.org', 'encrypted-refresh-canary', 'connected', 1)"))
        connection.execute(text("INSERT INTO education_cost_centers (id, org_id, name_key, code_key, display_name, code, created_by) VALUES (1, 1, 'pilot', 'pilot', 'Pilot', 'PILOT', 1)"))
        connection.execute(text("INSERT INTO education_cost_center_grants (org_id, cost_center_id, user_id, role, state, granted_by) VALUES (1, 1, 2, 'manager', 'active', 1), (1, 1, 3, 'student', 'active', 1)"))
        connection.execute(text("INSERT INTO printers (id, name, is_active, org_id, shared) VALUES (1, 'Pilot X1C', 1, 1, 0)"))
        connection.execute(text("INSERT INTO education_cost_center_printers (org_id, cost_center_id, printer_id, state, granted_by) VALUES (1, 1, 1, 'active', 1)"))
    return engine, path


def test_preflight_passes_ready_shape_and_never_emits_secrets(tmp_path: Path) -> None:
    from ops.ctec_poc.preflight import collect_checks, write_report

    engine, path = _ready_database(tmp_path)
    secret = Fernet.generate_key().decode()
    env = {
        "TRUSTED_HOSTS": "odin.ctechigh.org",
        "COOKIE_SECURE": "true",
        "COOKIE_SAMESITE": "lax",
        "OIDC_REDIRECT_URI": "https://odin.ctechigh.org/api/auth/oidc/callback",
        "ENCRYPTION_KEY": secret,
        "DATABASE_URL": f"sqlite:///{path}",
        "GOOGLE_CLIENT_SECRET": "external-secret-canary-value",
    }
    try:
        checks = collect_checks(engine, env, license_ready=True, require_classroom=True)
        report = write_report(checks, tmp_path / "report", env)
    finally:
        engine.dispose()
    assert report["passed"] is True
    raw = (tmp_path / "report" / "ctec-poc-preflight.json").read_text()
    rendered = (tmp_path / "report" / "ctec-poc-preflight.html").read_text()
    assert secret not in raw + rendered
    assert "external-secret-canary-value" not in raw + rendered
    assert "encrypted-refresh-canary" not in raw + rendered
    assert json.loads(raw)["checks"]


def test_preflight_fails_wildcard_host_and_missing_cookie_callback(tmp_path: Path) -> None:
    from ops.ctec_poc.preflight import collect_checks

    engine, path = _ready_database(tmp_path)
    try:
        checks = collect_checks(
            engine,
            {"TRUSTED_HOSTS": "*", "COOKIE_SECURE": "false", "COOKIE_SAMESITE": "strict", "DATABASE_URL": f"sqlite:///{path}"},
            license_ready=True,
            require_classroom=False,
        )
    finally:
        engine.dispose()
    failed = {item.key for item in checks if item.required and not item.passed}
    assert {"trusted_hosts", "secure_cookie", "oidc_cookie", "oidc_redirect", "encryption"} <= failed


def test_preflight_does_not_borrow_readiness_from_another_tenant(
    tmp_path: Path,
) -> None:
    from ops.ctec_poc.preflight import collect_checks

    engine, path = _ready_database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("UPDATE classroom_connections SET state='not_connected', refresh_token_encrypted=NULL WHERE org_id=1"))
        connection.execute(text("UPDATE education_cost_centers SET state='archived' WHERE org_id=1"))
        connection.execute(text("INSERT INTO groups (id, name, is_org) VALUES (2, 'Other School', 1)"))
        connection.execute(text("INSERT INTO users (id, username, email, password_hash, role, is_active, group_id) VALUES (20, 'other-admin', 'admin@other.edu', 'x', 'admin', 1, 2), (21, 'other-teacher', 'teacher@other.edu', '', 'viewer', 1, 2), (22, 'other-student', 'student@other.edu', '', 'viewer', 1, 2)"))
        connection.execute(text("INSERT INTO classroom_connections (org_id, client_id, client_secret_encrypted, allowed_domains, refresh_token_encrypted, state, created_by) VALUES (2, 'other-client', 'other-encrypted-client', 'other.edu', 'other-encrypted-refresh', 'connected', 20)"))
        connection.execute(text("INSERT INTO education_cost_centers (id, org_id, name_key, code_key, display_name, code, created_by) VALUES (2, 2, 'other-pilot', 'other-pilot', 'Other Pilot', 'OTHER', 20)"))
        connection.execute(text("INSERT INTO education_cost_center_grants (org_id, cost_center_id, user_id, role, state, granted_by) VALUES (2, 2, 21, 'manager', 'active', 20), (2, 2, 22, 'student', 'active', 20)"))
        connection.execute(text("INSERT INTO printers (id, name, is_active, org_id, shared) VALUES (2, 'Other Printer', 1, 2, 0)"))
        connection.execute(text("INSERT INTO education_cost_center_printers (org_id, cost_center_id, printer_id, state, granted_by) VALUES (2, 2, 2, 'active', 20)"))
    env = {
        "TRUSTED_HOSTS": "odin.ctechigh.org",
        "COOKIE_SECURE": "true",
        "COOKIE_SAMESITE": "lax",
        "OIDC_REDIRECT_URI": "https://odin.ctechigh.org/api/auth/oidc/callback",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "DATABASE_URL": f"sqlite:///{path}",
    }
    try:
        checks = collect_checks(engine, env, license_ready=True, require_classroom=True)
    finally:
        engine.dispose()
    states = {item.key: item.passed for item in checks}
    assert states["oidc"] is True
    assert states["classroom"] is False
    assert states["centers"] is False
    assert states["students"] is False
    assert states["managers"] is False
    assert states["printers"] is False


def test_handoff_html_is_clickable_and_excludes_internal_production_route() -> None:
    html = (ROOT / "docs" / "CTEC_POC_HANDOFF.html").read_text(encoding="utf-8")
    markdown = (ROOT / "docs" / "CTEC_POC_HANDOFF.md").read_text(encoding="utf-8")
    assert "<title>ODIN Education POC — CTEC Handoff</title>" in html
    assert "ctec-poc-preflight" in html
    assert "Google Classroom" in html and "Bambu" in html
    assert "odi.subsystem" not in (html + markdown)
    assert "refresh-token" not in (html + markdown)
