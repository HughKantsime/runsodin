"""Printer telemetry persistence and its REST contract on native PostgreSQL."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from core.models import SystemConfig
from core.printer_live_values import cache_key, save_snapshot
from tests.test_contracts.test_cert_E_notif_postgres import pg_engine  # noqa: F401

pytestmark = pytest.mark.postgresql

FIRST = "2026-11-20T12:00:00.000001Z"
NEWER = "2026-11-20T12:00:00.000002Z"
LATEST = "2026-11-20T12:00:00.000003Z"


@pytest.fixture
def printer_id(pg_engine, request):
    from modules.printers.models import Printer

    with pg_engine.begin() as conn:
        return conn.scalar(insert(Printer).values(name=request.node.name).returning(Printer.id))


def stored_snapshot(conn, printer_id):
    return conn.scalar(select(SystemConfig.value).where(SystemConfig.key == cache_key(printer_id)))


def test_only_strictly_newer_snapshots_replace_the_stored_frame(pg_engine, printer_id):
    with pg_engine.begin() as conn:
        for stamp, progress, expected in [
            (FIRST, 10, (FIRST, 10)),
            (NEWER, 20, (NEWER, 20)),
            (FIRST, 30, (NEWER, 20)),
            (NEWER, 40, (NEWER, 20)),
        ]:
            save_snapshot(conn, {"printer_id": printer_id, "telemetry_at": stamp, "progress": progress})
            row = stored_snapshot(conn, printer_id)
            assert (row["telemetry_at"], row["progress"]) == expected


def test_omitted_measurement_survives_and_explicit_null_clears_it(pg_engine, printer_id):
    with pg_engine.begin() as conn:
        for stamp, patch, expected_progress in [
            (FIRST, {"progress": 42, "speed_percent": 80}, 42),
            (NEWER, {"speed_percent": 90}, 42),
            (LATEST, {"progress": None}, None),
        ]:
            save_snapshot(conn, {"printer_id": printer_id, "telemetry_at": stamp, **patch})
            row = stored_snapshot(conn, printer_id)
            assert (row["telemetry_at"], row["progress"]) == (stamp, expected_progress)
        assert row["speed_percent"] == 90


def test_printer_list_and_detail_serve_the_stored_snapshot(pg_engine, printer_id):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.printers.routes_crud import router

    with pg_engine.begin() as conn:
        save_snapshot(conn, {"printer_id": printer_id, "telemetry_at": FIRST, "progress": 42.5,
                             "remaining_min": 18, "current_layer": 7, "total_layers": 31,
                             "speed_level": 3, "speed_percent": 125})

    def session():
        with Session(pg_engine) as db:
            yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "role": "viewer", "group_id": 1}
    expected = {"telemetry_at": FIRST, "mc_percent": 42.5, "mc_remaining_time": 18,
                "layer_num": 7, "total_layer_num": 31, "speed_level": 3, "speed_percent": 125}
    with TestClient(app) as client:
        listed = client.get("/printers")
        detail = client.get(f"/printers/{printer_id}")
        assert listed.status_code == detail.status_code == 200
        printer = next(row for row in listed.json() if row["id"] == printer_id)
        for row in (printer, detail.json()):
            assert {key: row[key] for key in expected} == expected


def test_event_is_committed_before_a_failed_snapshot_write(pg_engine, printer_id, monkeypatch):
    from core import printer_live_values, ws_hub

    monkeypatch.setattr(ws_hub, "engine", pg_engine)
    monkeypatch.setattr(ws_hub, "_snapshot_warnings", {})
    watermark = ws_hub.current_event_id()
    seen_before_failure = []

    def fail_snapshot(conn, data):
        # Use another connection: the event must already be committed at this point.
        seen_before_failure.extend(ws_hub.read_events_since(watermark)[0])
        save_snapshot(conn, data)
        # A duplicate primary key aborts the real snapshot transaction.
        conn.execute(insert(SystemConfig).values(key=cache_key(printer_id), value={}))

    monkeypatch.setattr(printer_live_values, "save_snapshot", fail_snapshot)
    ws_hub.push_event("printer_telemetry", {"printer_id": printer_id, "progress": 42})
    events, _ = ws_hub.read_events_since(watermark)
    assert events == seen_before_failure
    event, = events
    assert event["type"] == "printer_telemetry"
    assert event["data"]["printer_id"] == printer_id and event["data"]["progress"] == 42
    with pg_engine.connect() as conn:
        assert stored_snapshot(conn, printer_id) is None
