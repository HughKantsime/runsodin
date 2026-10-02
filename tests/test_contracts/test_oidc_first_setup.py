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
def setup_client(tmp_path, monkeypatch):
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
