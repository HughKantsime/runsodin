"""F22-22.1-06: the farm name entered in the setup wizard becomes the branding app name.

Real setup and branding routers on a real bootstrapped SQLite schema; the request body
is the one Setup.tsx sends (username, email, password, site_name).
"""
from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from core.db import get_db
from modules.system import routes_settings, routes_setup

BACKEND = Path(__file__).resolve().parents[2] / "backend"


@pytest.fixture
def client(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "setup.db"), connect_args={"check_same_thread": False})
    from core.schema import bootstrap_database
    bootstrap_database(engine, BACKEND)
    app = FastAPI()
    app.include_router(routes_setup.router)
    app.include_router(routes_settings.router)

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as test_client:
        yield test_client, engine
    engine.dispose()


def admin_body(**extra):
    return {"username": "farm-owner", "email": "owner@example.invalid",
            "password": secrets.token_urlsafe(24) + "aA1", **extra}


def test_wizard_farm_name_is_returned_by_branding(client, tier_license):
    """Decision (2026-10-16): the farm name is Pro branding. A provided name is stored on
    every tier; Community serves the stock branding, and the name appears under Pro."""
    test_client, engine = client
    tier_license(None)  # Community
    assert test_client.get("/branding").json()["app_name"] == "O.D.I.N."
    r = test_client.post("/setup/admin", json=admin_body(site_name="  Riverside Print Lab  "))
    assert r.status_code == 200, r.text
    assert test_client.get("/branding").json()["app_name"] == "O.D.I.N."  # stock branding below Pro
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1
        assert conn.execute(text("SELECT app_name FROM branding")).scalar_one() == "Riverside Print Lab"  # kept for upgrade
    tier_license("pro")
    assert test_client.get("/branding").json()["app_name"] == "Riverside Print Lab"


def test_missing_or_blank_farm_name_keeps_default(client):
    test_client, _ = client
    assert test_client.post("/setup/admin", json=admin_body(site_name="   ")).status_code == 200
    assert test_client.get("/branding").json()["app_name"] == "O.D.I.N."


def test_invalid_farm_name_creates_nothing(client):
    test_client, engine = client
    r = test_client.post("/setup/admin", json=admin_body(site_name="x" * 101))
    assert r.status_code == 422
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 0
    assert test_client.get("/branding").json()["app_name"] == "O.D.I.N."
