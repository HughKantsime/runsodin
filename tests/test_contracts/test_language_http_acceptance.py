"""Language HTTP persistence and real superadmin authority on owned SQLite."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from modules.system.routes_settings import router


@pytest.fixture
def language_app(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'language.db'}",
        connect_args={"check_same_thread": False},
    )
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT)"))
    app = FastAPI()
    app.include_router(router)

    def database():
        with Session(engine) as session:
            yield session

    # Leave require_superadmin itself intact: only its identity source is replaced.
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "role": "admin", "group_id": None,
    }
    try:
        yield app, engine
    finally:
        engine.dispose()


def stored_language(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT value FROM system_config WHERE key = 'language'")).scalar_one_or_none()


def test_global_admin_italian_persists_across_new_client(language_app):
    app, engine = language_app
    with TestClient(app) as client:
        assert client.get('/settings/language').json() == {"language": "en"}
        response = client.put('/settings/language', json={"language": "it"})
        assert response.status_code == 200
        assert response.json() == {"language": "it"}
        assert stored_language(engine) == "it"
        assert client.get('/settings/language').json() == {"language": "it"}
        # Exercise the UPDATE/upsert path as well as the original INSERT.
        assert client.put('/settings/language', json={"language": "de"}).status_code == 200
        assert client.put('/settings/language', json={"language": "it"}).status_code == 200
    with TestClient(app) as refreshed:
        response = refreshed.get('/settings/language')
        assert response.status_code == 200
        assert response.json() == {"language": "it"}
    assert stored_language(engine) == "it"


@pytest.mark.parametrize("identity", [
    {"id": 2, "role": "admin", "group_id": 7},
    {"id": 3, "role": "operator", "group_id": None},
    {"id": 4, "role": "viewer", "group_id": None},
])
def test_non_platform_admin_cannot_change_shared_language(language_app, identity):
    app, engine = language_app
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO system_config VALUES ('language', 'es')"))
    app.dependency_overrides[get_current_user] = lambda: identity
    with TestClient(app) as client:
        response = client.put('/settings/language', json={"language": "it"})
        assert response.status_code == 403
        assert client.get('/settings/language').json() == {"language": "es"}
    assert stored_language(engine) == "es"


def test_unsupported_language_400_preserves_stored_value(language_app):
    app, engine = language_app
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO system_config VALUES ('language', 'it')"))
    with TestClient(app) as client:
        response = client.put('/settings/language', json={"language": "fr"})
        assert response.status_code == 400
        assert client.get('/settings/language').json() == {"language": "it"}
    assert stored_language(engine) == "it"
