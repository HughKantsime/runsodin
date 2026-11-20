import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from tests.test_contracts.test_controlled_guards import marked  # noqa: F401
from tests.test_contracts.test_controlled_parts import actor, database  # noqa: F401


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_broadcast_ends_failed_reader_and_keeps_serving_peers(monkeypatch, failure):
    from core.app_websocket import ConnectionManager
    manager = ConnectionManager()
    monkeypatch.setattr("core.controlled_events.audience_messages", lambda audience, msg: [msg] * len(audience))
    async def run():
        broken, healthy = AsyncMock(), AsyncMock()
        inbox = asyncio.Queue()
        healthy.receive_text.side_effect = inbox.get
        async def block(*args):
            await asyncio.Event().wait()
        broken.receive_text.side_effect = block
        broken.send_json.side_effect = block if failure == "timeout" else OSError()
        manager.active = [(sock, {"_license_allowed": True}) for sock in (broken, healthy)]
        dead = asyncio.create_task(manager.receive_messages(broken))
        live = asyncio.create_task(manager.receive_messages(healthy))
        event = {"type": "test", "data": {}}
        try:
            assert await manager.broadcast(event) == 1
            await asyncio.wait_for(asyncio.shield(dead), .1)
            assert manager.active == [(healthy, {"_license_allowed": True})]
            broken.close.assert_awaited_once_with(code=1011, reason="delivery_failed")
            manager.disconnect(broken)
            assert await manager.broadcast(event) == 1
            await inbox.put("ping")
            await asyncio.sleep(.01)
            healthy.send_text.assert_awaited_once_with("pong")
            assert healthy.send_json.await_count == 2 and not live.done()
        finally:
            manager.disconnect(healthy)
            await live
    asyncio.run(run())


@pytest.mark.parametrize("identifier", [1, 2])
def test_latest_snapshot_in_both_printer_responses_after_queue_expiry(marked, monkeypatch, identifier):
    from core import db, ws_hub
    from core.controlled_events import audience_messages
    from core.printer_live_values import LIVE_FIELDS
    from modules.printers.routes_crud import get_printer, list_printers
    from modules.printers.schemas import PrinterResponse

    monkeypatch.setattr(db, "engine", marked.get_bind())
    marked.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"))
    marked.execute(text("UPDATE printers SET is_active=TRUE,slot_count=1,timelapse_enabled=FALSE,shared=FALSE"))
    marked.commit()
    viewer = {**actor(identifier), "role": "viewer"}
    first = {"printer_id": 1, "progress": 12, "remaining_min": 30, "speed_level": 2}
    ws_hub.push_event("printer_telemetry", first)
    last = {**first, "progress": 42.5, "remaining_min": 0, "current_layer": 5, "total_layers": 14,
            "speed_level": None, "speed_percent": 80, "gcode_file": "controlled.3mf"}
    ws_hub.push_event("printer_telemetry", last)
    messages, _ = ws_hub.read_events_since(0)
    message = audience_messages([viewer], messages[-1])[0]["data"]
    assert message["gcode_file"] == ("[controlled]" if identifier == 2 else last["gcode_file"])
    assert message["telemetry_at"].endswith("Z")
    assert datetime.fromisoformat(message["telemetry_at"]).tzinfo == timezone.utc
    ws_hub._cleanup(float("inf"))
    assert not ws_hub.read_events_since(0)[0]
    def response(printer):
        return PrinterResponse.model_validate(printer).model_dump(mode="json")
    rows = {p.id: response(p) for p in list_printers(current_user=viewer, db=marked)}
    for pid in (1, 2):
        detail = response(get_printer(pid, current_user=viewer, db=marked))
        for wire, field in {**LIVE_FIELDS, "telemetry_at": "telemetry_at"}.items():
            assert rows[pid][field] == detail[field] == (message[wire] if pid == 1 else None)


def test_older_producer_cannot_replace_newer_snapshot_or_leak_it_to_a_reused_printer_id(database, monkeypatch):
    from core import ws_hub
    from modules.printers.routes_crud import get_printer
    monkeypatch.setattr(ws_hub, "engine", database.get_bind())
    clock = iter([2.0, 1.0])
    monkeypatch.setattr(ws_hub, "time", SimpleNamespace(time=lambda: next(clock)))
    for progress in (52, 12):
        ws_hub.push_event("printer_telemetry", {"printer_id": 1, "progress": progress})
    printer = get_printer(1, current_user=actor(), db=database)
    assert printer.mc_percent == 52 and printer.telemetry_at == "1970-01-01T00:00:02.000000Z"
    database.execute(text("UPDATE printers SET created_at='2001-02-03' WHERE id=1"))
    database.commit()
    replacement = get_printer(1, current_user=actor(), db=database)
    assert replacement.mc_percent is None and replacement.telemetry_at is None


@pytest.mark.parametrize(("decision", "code"), [(False, 4403), (None, 1013)])
def test_licence_eviction_stops_reader_even_if_close_fails(monkeypatch, decision, code):
    from core.app_websocket import ConnectionManager
    monkeypatch.setattr("core.controlled_events.audience_messages", lambda principals, msg: [msg])
    monkeypatch.setattr("core.license_lock.enabled", lambda: True)
    async def run():
        manager, ws = ConnectionManager(), AsyncMock()
        ws.receive_text.side_effect = asyncio.Event().wait
        ws.close.side_effect = OSError()
        manager.active = [(ws, {"_license_allowed": decision})]
        loop = asyncio.create_task(manager.receive_messages(ws))
        await asyncio.sleep(0)
        assert await manager.broadcast({"type": "test", "data": {}}) == 0
        await asyncio.wait_for(asyncio.shield(loop), .1)
        assert not manager.active
        assert ws.close.await_args.kwargs["code"] == code
        ws.send_json.assert_not_awaited()
    asyncio.run(run())


def test_failed_snapshot_preserves_events_and_warns_once_per_printer(database, monkeypatch, caplog):
    from core import ws_hub
    from core import printer_live_values

    monkeypatch.setattr(ws_hub, "engine", database.get_bind())

    monkeypatch.setattr(ws_hub, "_snapshot_warnings", {}, raising=False)

    def fail_snapshot(conn, data):
        conn.execute(text("INSERT INTO missing_snapshot_table DEFAULT VALUES"))

    monkeypatch.setattr(printer_live_values, "save_snapshot", fail_snapshot)
    for printer_id in (1, 1, 2):
        ws_hub.push_event("printer_telemetry", {"printer_id": printer_id})
    events, _ = ws_hub.read_events_since(0)
    assert [event["data"]["printer_id"] for event in events] == [1, 1, 2]
    warnings = [r.getMessage() for r in caplog.records if r.name == "ws_hub" and r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "1" in warnings[0] and "2" in warnings[1]


def test_sparse_frame_preserves_measurements_and_explicit_null_clears(database, monkeypatch):
    from core import ws_hub
    from modules.printers.routes_crud import get_printer

    monkeypatch.setattr(ws_hub, "engine", database.get_bind())
    ws_hub.push_event("printer_telemetry", {"printer_id": 1, "progress": 42, "speed_percent": 125})
    for patch, expected in [({"bed_temp": 62}, 42), ({"progress": None}, None)]:
        ws_hub.push_event("printer_telemetry", {"printer_id": 1, **patch})
        frame = ws_hub.read_events_since(0)[0][-1]["data"]
        assert frame == {"printer_id": 1, "telemetry_at": frame["telemetry_at"], **patch}
        printer = get_printer(1, current_user=actor(), db=database)
        assert (printer.mc_percent, printer.speed_percent) == (expected, 125)
        assert printer.telemetry_at == frame["telemetry_at"]


def test_malformed_snapshot_is_absent_in_list_and_detail(database, monkeypatch):
    from core import ws_hub
    from core.printer_live_values import LIVE_FIELDS
    from modules.printers.routes_crud import get_printer, list_printers
    from modules.printers.schemas import PrinterResponse

    database.execute(text("UPDATE printers SET is_active=TRUE,slot_count=1,timelapse_enabled=FALSE,shared=FALSE"))
    database.commit()
    monkeypatch.setattr(ws_hub, "engine", database.get_bind())
    ws_hub.push_event("printer_telemetry", {"printer_id": 1, "progress": 40})
    row = "printer.live_values.1"
    valid = json.loads(database.scalar(text("SELECT value FROM system_config WHERE key=:key"), {"key": row}))
    bad_records = [json.dumps({**valid, field: value}) for field, value in [
        ("telemetry_at", "yesterday"), ("progress", 101), ("remaining_min", -1),
        ("current_layer", 1.5), ("total_layers", True), ("speed_level", 5), ("speed_percent", "80"),
    ]]
    for raw in [*bad_records, "{"]:
        database.execute(text("UPDATE system_config SET value=:raw WHERE key=:key"), {"key": row, "raw": raw})
        database.commit()
        listed = next(p for p in list_printers(current_user=actor(), db=database) if p.id == 1)
        list_response = PrinterResponse.model_validate(listed)
        detail_response = PrinterResponse.model_validate(get_printer(1, current_user=actor(), db=database))
        for response in (list_response, detail_response):
            assert response.telemetry_at is None
            assert all(getattr(response, field) is None for field in LIVE_FIELDS.values())


def test_null_creation_date_uses_printer_id_for_snapshot(database, monkeypatch):
    from core import ws_hub
    from modules.printers.routes_crud import get_printer
    from modules.printers.schemas import PrinterResponse

    database.execute(text("UPDATE printers SET created_at=NULL,is_active=TRUE,slot_count=1,timelapse_enabled=FALSE,shared=FALSE WHERE id=1"))
    database.commit()
    monkeypatch.setattr(ws_hub, "engine", database.get_bind())
    ws_hub.push_event("printer_telemetry", {"printer_id": 1, "progress": 42})
    printer = get_printer(1, current_user=actor(), db=database)
    assert printer.mc_percent == 42
    assert PrinterResponse.model_validate(printer).created_at is None


def test_endpoint_shutdown_propagates_during_disconnect():
    from core.app_websocket import ConnectionManager

    async def run():
        manager, ws = ConnectionManager(), AsyncMock()
        started = asyncio.Event()

        async def receive():
            started.set()
            await asyncio.Event().wait()

        ws.receive_text.side_effect = receive
        manager.active = [(ws, {})]
        endpoint = asyncio.create_task(manager.receive_messages(ws))
        await started.wait()
        manager.disconnect(ws)
        endpoint.cancel()
        with pytest.raises(asyncio.CancelledError):
            await endpoint

    asyncio.run(run())
