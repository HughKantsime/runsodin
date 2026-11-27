"""Printer PATCH and read contracts on the bootstrapped SQLite schema."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from modules.printers.models import Printer
from modules.printers.routes_crud import router
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def printer_client(review_db, monkeypatch):
    review_db.add(Printer(
        id=42, org_id=2, name="Workshop", model="Test model", nickname="North",
        tags=["blue"], shared=True, timelapse_enabled=True, api_type="manual",
        api_host="printer.invalid", api_key="test-only", camera_url="http://camera.invalid/",
        bed_x_mm=200, bed_y_mm=180,
    ))
    review_db.commit()
    monkeypatch.setattr("modules.printers.route_utils.sync_go2rtc_config", lambda db: None)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: {"id": 5, "role": "admin", "group_id": 2}

    def session():
        with Session(review_db.get_bind()) as db:
            yield db

    app.dependency_overrides[get_db] = session
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("field", ["tags", "timelapse_enabled", "shared", "name", "slot_count", "is_active"])
def test_required_field_rejects_null_without_saving(printer_client, review_db, field):
    row = text("SELECT * FROM printers WHERE id=42")
    before = review_db.execute(row).one()
    response = printer_client.patch("/api/printers/42", json={field: None, "nickname": "Changed"})
    assert response.status_code == 422
    error, = response.json()["detail"]
    assert error["loc"] == ["body", field]
    assert f"{field.replace('_', ' ')} cannot be null" in error["msg"]
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(row).one() == before
    listing = printer_client.get("/api/printers")
    assert listing.status_code == 200
    assert [p["id"] for p in listing.json()] == [42]


@pytest.mark.parametrize("field", ["model", "nickname", "api_type", "api_host", "api_key", "camera_url", "bed_x_mm", "bed_y_mm"])
def test_nullable_field_can_be_cleared(printer_client, review_db, field):
    assert getattr(review_db.get(Printer, 42), field) is not None
    response = printer_client.patch("/api/printers/42", json={field: None})
    assert response.status_code == 200
    body = response.json()
    if field == "api_key":
        assert "api_key" not in body and body["has_api_key"] is False
    else:
        assert body[field] is None
    assert body["tags"] == ["blue"] and body["timelapse_enabled"] is True and body["shared"] is True
    with Session(review_db.get_bind()) as fresh:
        assert getattr(fresh.get(Printer, 42), field) is None


@pytest.mark.parametrize("stored_tags", [None, "null"], ids=["sql-null", "json-null"])
def test_existing_nulls_read_as_defaults_and_allow_other_edits(printer_client, review_db, stored_tags):
    review_db.execute(text("UPDATE printers SET tags=:tags, timelapse_enabled=NULL, shared=NULL WHERE id=42"),
                      {"tags": stored_tags})
    review_db.commit()
    listing = printer_client.get("/api/printers")
    detail = printer_client.get("/api/printers/42")
    tags = printer_client.get("/api/printers/tags")
    assert listing.status_code == detail.status_code == tags.status_code == 200
    assert tags.json() == []
    edited = printer_client.patch("/api/printers/42", json={"nickname": "South"})
    assert edited.status_code == 200 and edited.json()["nickname"] == "South"
    for printer in [*listing.json(), detail.json(), edited.json()]:
        assert printer["id"] == 42 and printer["tags"] == []
        assert printer["timelapse_enabled"] is False and printer["shared"] is False
    assert len(listing.json()) == 1
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text("SELECT tags,timelapse_enabled,shared FROM printers WHERE id=42")).one() == (
            stored_tags, None, None)
