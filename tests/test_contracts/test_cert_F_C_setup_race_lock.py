"""Security review: a SQLite lock-timeout during the first-admin claim maps to a
client error (409/403), never a 500."""
from __future__ import annotations

import secrets
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

BACKEND = Path(__file__).resolve().parents[2] / "backend"


def test_lock_timeout_on_claim_maps_to_409(tmp_path, monkeypatch):
    monkeypatch.setenv("JWT_SECRET_KEY", secrets.token_urlsafe(32))
    from core.db import get_db
    from core.schema import bootstrap_database
    from modules.system import routes_setup
    engine = create_engine("sqlite:///" + str(tmp_path / "s.db"), connect_args={"check_same_thread": False})
    bootstrap_database(engine, BACKEND)
    app = FastAPI()
    app.include_router(routes_setup.router)

    real_execute = Session.execute

    def flaky(self, statement, *a, **k):
        if "setup_admin_claimed" in str(statement):
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return real_execute(self, statement, *a, **k)
    monkeypatch.setattr(Session, "execute", flaky)

    def database():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = database
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/setup/admin", json={"username": "o", "email": "o@example.test",
                                              "password": secrets.token_urlsafe(20) + "aA1"})
    assert r.status_code == 409, r.status_code
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 0
