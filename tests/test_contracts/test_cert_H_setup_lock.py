"""Cert group H — F22-22.1-07 "Setup endpoints permanently locked after completion".

Real setup router on a real bootstrapped SQLite schema. Unlike the existing
acceptance test (which seeds system_config with the raw text 'true'), completion
here goes through the real POST /setup/complete handler, i.e. the path the
onboarding wizard uses.
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
from core.dependencies import get_current_user
from core.schema import bootstrap_database
from modules.system import routes_setup

BACKEND = Path(__file__).resolve().parents[2] / "backend"
DEFECT = (
    "DEFECT: backend/modules/system/routes_setup.py:401-406 persists completion via SystemConfig(value='true') "
    "whose JSON column (core/models.py:19) stores the text '\"true\"', while _setup_is_complete "
    "(routes_setup.py:110-112) compares the raw value to 'true'. Scenario: wizard calls POST /setup/complete "
    "(200) -> GET /setup/status is_complete=false; /setup/complete accepted again; any authenticated user "
    "(even viewer) can still POST /setup/printer; with zero users /setup/admin reopens."
)


@pytest.fixture
def client(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "setup.db"), connect_args={"check_same_thread": False})
    bootstrap_database(engine, BACKEND)
    app = FastAPI()
    app.include_router(routes_setup.router)
    actor = {"value": None}
    app.dependency_overrides[get_current_user] = lambda: actor["value"]

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as test_client:
        # first-user-wins: real admin creation, then real completion
        response = test_client.post("/setup/admin", json={
            "username": "certh-owner", "email": "owner@example.invalid",
            "password": secrets.token_urlsafe(24) + "aA1"})
        assert response.status_code == 200, response.text
        actor["value"] = {"id": 1, "username": "certh-owner", "role": "admin", "group_id": None}
        assert test_client.post("/setup/complete").status_code == 200
        yield test_client, engine, actor
    engine.dispose()


def test_claim_gate_closed_once_admin_exists(client):
    test_client, engine, actor = client
    for actor_value in (None, actor["value"]):
        actor["value"] = actor_value
        assert test_client.post("/setup/admin", json={
            "username": "intruder", "email": "i@example.invalid", "password": "Intruder123"}).status_code == 403
        assert test_client.post("/setup/test-printer", json={"api_type": "moonraker", "api_host": "127.0.0.1"}).status_code == 403
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1


@pytest.mark.xfail(strict=True, reason=DEFECT)
def test_status_reports_complete_after_real_completion(client):
    test_client, _, _ = client
    assert test_client.get("/setup/status").json()["is_complete"] is True


@pytest.mark.xfail(strict=True, reason=DEFECT)
def test_completion_cannot_be_repeated(client):
    test_client, _, _ = client
    assert test_client.post("/setup/complete").status_code == 403


@pytest.mark.xfail(strict=True, reason=DEFECT)
def test_viewer_cannot_create_printers_through_setup_after_completion(client):
    test_client, engine, actor = client
    actor["value"] = {"id": 2, "username": "certh-viewer", "role": "viewer", "group_id": None}
    response = test_client.post("/setup/printer", json={"name": "viewer-printer", "api_type": "moonraker", "api_host": "127.0.0.1"})
    with engine.connect() as conn:
        created = conn.execute(text("SELECT COUNT(*) FROM printers WHERE name='viewer-printer'")).scalar_one()
    assert response.status_code == 403 and created == 0, (response.status_code, created)


@pytest.mark.xfail(strict=True, reason=DEFECT)
def test_completed_setup_stays_locked_when_all_users_are_removed(client):
    test_client, engine, actor = client
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users"))
    actor["value"] = None
    response = test_client.post("/setup/admin", json={
        "username": "late-claimer", "email": "l@example.invalid", "password": secrets.token_urlsafe(24) + "aA1"})
    assert response.status_code == 403, response.status_code
