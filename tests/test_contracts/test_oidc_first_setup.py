"""Exercise first-time setup through real HTTP routes and a fresh database."""
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND)) if str(BACKEND) not in sys.path else None


@pytest.fixture
def setup_client(tmp_path, monkeypatch, education_license):
    # This setup creates organizations (Education) and configures SSO/permissions.
    from core.schema import bootstrap_database
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations import routes, routes_oidc, routes_permissions

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    engine = create_engine(f"sqlite:///{tmp_path / 'setup.db'}", connect_args={"check_same_thread": False})
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (id, username, password_hash, role) VALUES (1, 'fixture-admin', 'unused', 'admin')"))
    actor = {"id": 1, "username": "fixture-admin", "role": "admin", "group_id": None}
    app = FastAPI()
    for router in (routes.router, routes_oidc.router, routes_permissions.router):
        app.include_router(router)

    def session():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: actor
    with TestClient(app) as client:
        yield client, engine, actor
    engine.dispose()


def test_fresh_setup_persists_and_redacts_secret(setup_client):
    from core.crypto import decrypt

    client, engine, _ = setup_client
    assert client.get('/admin/oidc').json() == {"configured": False}
    assert client.get('/orgs').json() == []
    assert client.get('/permissions').json()['action_access']['settings.edit'] == ['admin']
    created = client.post('/orgs', json={"name": "Fixture School"})
    assert created.status_code == 200
    tenant = created.json()['id']
    response = client.put('/admin/oidc', json={
        "provider_type": "google", "client_id": "fixture-client",
        "client_secret": "fixture-secret", "auto_create_users": True,
        "default_role": "viewer", "default_group_id": tenant,
        "allowed_domains": " SCHOOL.TEST ", "is_enabled": True,
    })
    assert response.status_code == 200, response.text
    loaded = client.get('/admin/oidc').json()
    assert loaded['client_id'] == 'fixture-client'
    assert loaded['default_group_id'] == tenant
    assert loaded['allowed_domains'] == 'school.test'
    assert loaded['has_client_secret'] is True
    assert 'client_secret_encrypted' not in loaded
    assert 'fixture-secret' not in str(loaded)
    with engine.connect() as db:
        encrypted = db.execute(text('SELECT client_secret_encrypted FROM oidc_config')).scalar_one()
        assert encrypted != 'fixture-secret'
        assert decrypt(encrypted) == 'fixture-secret'
    for update in ({"display_name": "School SSO"}, {"client_secret": "", "display_name": "School"}):
        assert client.put('/admin/oidc', json=update).status_code == 200
    with engine.connect() as db:
        assert db.execute(text('SELECT COUNT(*) FROM oidc_config')).scalar_one() == 1
        assert db.execute(text('SELECT client_secret_encrypted FROM oidc_config')).scalar_one() == encrypted
        assert 'fixture-secret' not in str(db.execute(text('SELECT details FROM audit_logs')).fetchall())
    assert client.get('/admin/oidc').json()['display_name'] == 'School'


@pytest.mark.parametrize('payload', [
    {"auto_create_users": True}, {"default_role": "admin"},
    {"default_group_id": 999}, {"provider_type": "invalid"},
])
def test_invalid_first_save_creates_no_row(setup_client, payload):
    client, _, _ = setup_client
    assert client.put('/admin/oidc', json=payload).status_code == 422
    assert client.get('/admin/oidc').json() == {"configured": False}


@pytest.mark.parametrize('role,group', [('viewer', None), ('operator', None), ('admin', 1)])
def test_first_setup_remains_superadmin_only(setup_client, role, group):
    client, _, actor = setup_client
    actor.update(role=role, group_id=group)
    assert client.put('/admin/oidc', json={"display_name": "Denied"}).status_code == 403
    assert client.get('/admin/oidc').status_code == 403
    assert client.post('/orgs', json={"name": "Denied"}).status_code == 403


def test_explicit_settings_permission_override_is_preserved(setup_client):
    from core.models import SystemConfig

    client, engine, _ = setup_client
    with Session(engine) as db:
        db.add(SystemConfig(key='rbac_permissions', value={"action_access": {"settings.edit": []}}))
        db.commit()
    assert client.get('/permissions').json()['action_access']['settings.edit'] == []


@pytest.mark.parametrize("existing, update", [
    (None, {"provider_type": "generic", "is_enabled": True}),
    ({"provider_type": "google", "is_enabled": True}, {"provider_type": "generic"}),
    ({"provider_type": "generic", "is_enabled": True, "discovery_url": "https://idp.example.test/discovery"}, {"discovery_url": "  "}),
])
def test_generic_provider_requires_a_discovery_address(setup_client, existing, update):
    client, _, _ = setup_client
    if existing:
        assert client.put("/admin/oidc", json=existing).status_code == 200
    before = client.get("/admin/oidc").json()
    response = client.put("/admin/oidc", json=update)
    assert response.status_code == 422
    assert response.json()["detail"] == "A discovery address is required for a generic OpenID Connect provider"
    assert client.get("/admin/oidc").json() == before


@pytest.mark.parametrize("entry", ["login", "callback"])
def test_legacy_generic_provider_refuses_login_before_discovery(setup_client, monkeypatch, entry):
    from unittest.mock import AsyncMock
    from modules.organizations import oidc_handler

    client, engine, _ = setup_client
    with engine.begin() as db:
        db.execute(text("INSERT INTO oidc_config (id, provider_type, is_enabled) VALUES (1, 'generic', TRUE)"))
    request = AsyncMock()
    monkeypatch.setattr(oidc_handler, "pinned_async_request", request)
    from modules.organizations.routes_oidc import OIDC_BINDING_COOKIE, oidc_state_binding
    client.cookies.set(OIDC_BINDING_COOKIE, oidc_state_binding("test-state"))
    response = client.get(f"/auth/oidc/{entry}", params={"code": "test-code", "state": "test-state"},
                          follow_redirects=False)
    assert response.status_code == 400
    assert response.json() == {"detail": "OIDC not configured"}
    request.assert_not_called()


def test_blank_generic_provider_can_be_saved_disabled(setup_client):
    client, engine, _ = setup_client
    with engine.begin() as db:
        db.execute(text("INSERT INTO oidc_config (id, provider_type, is_enabled) VALUES (1, 'generic', TRUE)"))
    assert client.put("/admin/oidc", json={"is_enabled": False}).status_code == 200
    assert client.put("/admin/oidc", json={"display_name": "School login"}).status_code == 200
    assert client.put("/admin/oidc", json={"is_enabled": True}).status_code == 422
    assert not client.get("/admin/oidc").json()["is_enabled"]
    assert client.get("/auth/oidc/login").status_code == 400


def test_first_save_may_leave_generic_provider_disabled(setup_client):
    client, _, _ = setup_client
    assert client.put("/admin/oidc", json={"provider_type": "generic", "is_enabled": False}).status_code == 200
    assert client.put("/admin/oidc", json={"is_enabled": True}).status_code == 422
    assert client.put("/admin/oidc", json={
        "is_enabled": True, "discovery_url": "https://idp.example.test/discovery",
    }).status_code == 200


@pytest.mark.parametrize("page", ["admin", "settings", "audit", "branding"])
def test_locked_page_refuses_non_admin_roles_and_ignores_stored_grants(setup_client, page):
    from core.models import SystemConfig
    from core.rbac_policy import page_roles

    client, engine, _ = setup_client
    stale = {"page_access": {page: ["admin", "operator", "viewer"]}, "action_access": {}}
    with Session(engine) as db:
        db.add(SystemConfig(key="rbac_permissions", value=stale))
        db.commit()
    matrix = client.get("/permissions?include_locks=true").json()
    assert matrix["page_access"][page] == ["admin"]
    response = client.put("/permissions", json=stale)
    assert response.status_code == 422
    assert response.json()["detail"] == matrix["locked_pages"][page]
    assert page in response.json()["detail"].lower()
    with Session(engine) as db:
        assert page_roles(db, page) == ["admin"]
        assert db.query(SystemConfig).filter_by(key="rbac_permissions").one().value == stale


def test_pro_auto_create_accepts_viewers_without_an_organization(setup_client, tier_license):
    client, _, _ = setup_client
    tier_license("pro")
    assert client.put("/admin/oidc", json={"auto_create_users": True}).status_code == 200
    assert client.put("/admin/oidc", json={"default_role": "operator"}).status_code == 422
    stored = client.get("/admin/oidc").json()
    assert stored["auto_create_users"] and stored["default_role"] == "viewer"
    assert stored["default_group_id"] is None
