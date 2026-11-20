import asyncio
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
