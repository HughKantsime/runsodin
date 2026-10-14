"""HTTP acceptance for account theme persistence and diagnostic downloads."""
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.db import get_db
from core.dependencies import get_current_user
from modules.organizations import routes_auth
from modules.system import routes_admin, diagnostics


def test_theme_persists_between_clients_and_is_account_isolated():
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, theme_json TEXT)'))
        conn.execute(text('INSERT INTO users (id) VALUES (1), (2)'))
    app = FastAPI()
    app.include_router(routes_auth.router, prefix='/api')
    identity = {'id': 1, 'role': 'viewer'}
    app.dependency_overrides[get_current_user] = lambda: identity
    def database():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = database
    try:
        preferences = {'accent_color': '#123456', 'sidebar_style': 'light', 'background': 'default'}
        with TestClient(app) as first:
            response = first.put('/api/auth/me/theme', json=preferences)
            assert response.status_code == 200 and response.json() == preferences
        with TestClient(app) as second:
            assert second.get('/api/auth/me/theme').json() == preferences
            identity['id'] = 2
            assert second.get('/api/auth/me/theme').json()['accent_color'] == '#6366f1'
            second.put('/api/auth/me/theme', json={'accent_color': '#abcdef'})
            identity['id'] = 1
            assert second.get('/api/auth/me/theme').json() == preferences
    finally:
        engine.dispose()


@pytest.fixture
def bundle_client(monkeypatch):
    class Result:
        def scalar(self): return 0
        def fetchall(self): return []
    class DB:
        def execute(self, *_args, **_kwargs): return Result()
    monkeypatch.setattr(routes_admin, 'sql', SimpleNamespace(is_sqlite=False))
    monkeypatch.setattr(diagnostics, '_read_tail', lambda *_: ['ERROR address=192.0.2.12 password=synthetic-test-value'])
    app = FastAPI()
    app.include_router(routes_admin.router, prefix='/api')
    app.dependency_overrides[get_current_user] = lambda: {'id': 1, 'role': 'admin', 'group_id': None}
    app.dependency_overrides[get_db] = lambda: DB()
    with TestClient(app) as client:
        yield client


def test_support_bundle_http_download_has_six_parseable_members(bundle_client):
    response = bundle_client.get('/api/admin/support-bundle')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/zip'
    assert 'attachment; filename=odin-support-bundle-' in response.headers['content-disposition']
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {'system_info.json', 'connectivity.json', 'settings_safe.json', 'recent_errors.txt', 'db_health.json', 'bundle_metadata.json'}
        for name in archive.namelist():
            if name.endswith('.json'): json.loads(archive.read(name))
        logs = archive.read('recent_errors.txt').decode()
        assert '192.0.2.12' not in logs and 'synthetic-test-value' not in logs
        assert '[REDACTED]' in logs
