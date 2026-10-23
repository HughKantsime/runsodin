"""Bounded websocket backlog, audience DB work, and untrusted Moonraker input."""
import asyncio
from contextlib import asynccontextmanager, suppress
import io
import json
import threading
import time

import pytest
from sqlalchemy import create_engine, text


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from core import ws_hub
    engine = create_engine('sqlite:///' + str(tmp_path / 'events.db'))
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE ws_events(id INTEGER PRIMARY KEY, data TEXT, created_at REAL)'))
    monkeypatch.setattr(ws_hub, 'engine', engine)
    monkeypatch.setattr(ws_hub, '_last_cleanup', 0)
    yield ws_hub, engine
    engine.dispose()


def seed(engine, count, stamp=None):
    with engine.begin() as conn:
        conn.execute(text('INSERT INTO ws_events(data,created_at) VALUES(:data,:stamp)'),
                     [{'data':json.dumps({'type':'test','data':{}}), 'stamp':time.time() if stamp is None else stamp}] * count)


def test_50k_backlog_client_and_live_batches_keep_health_under_one_second(hub, monkeypatch):
    from fastapi import FastAPI, WebSocket
    from fastapi.testclient import TestClient
    from core import app_websocket as runtime
    from modules.system import routes_health
    hub_module, engine = hub
    seed(engine, 50_000)
    manager = runtime.ConnectionManager()
    monkeypatch.setattr(runtime, 'ws_manager', manager)
    monkeypatch.setattr(routes_health, 'get_spoolman_url', lambda: '')
    batches = []
    original = hub_module.read_events_since
    def read(last_id):
        result = original(last_id)
        batches.append((last_id, len(result[0])))
        return result
    monkeypatch.setattr(hub_module, 'read_events_since', read)
    @asynccontextmanager
    async def lifespan(app):
        watermark = await asyncio.to_thread(hub_module.current_event_id)
        assert watermark == 50_000
        task = asyncio.create_task(runtime._ws_broadcaster(watermark))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    app = FastAPI(lifespan=lifespan)
    app.add_api_route('/health', routes_health.health_check)
    @app.websocket('/ws')
    async def socket(ws: WebSocket):
        await manager.connect(ws, {'id':1})
        try:
            await ws.receive_text()
        finally:
            manager.disconnect(ws)
    with TestClient(app) as client, client.websocket_connect('/ws') as ws:
        # Also exercise live catch-up rather than only the boot watermark.
        seed(engine, 1024)
        for _ in range(35):
            started = time.monotonic()
            assert client.get('/health').status_code == 200
            assert time.monotonic() - started < 1
            time.sleep(0.04)
        assert batches and all(last >= 50_000 and size <= 128 for last, size in batches)
        assert sum(size for _, size in batches) == 1024
        ws.send_text('close')


def test_idle_cleanup_does_not_require_clients(hub, monkeypatch):
    from core import app_websocket as runtime
    module, engine = hub
    seed(engine, 500, stamp=0)
    monkeypatch.setattr(runtime, 'ws_manager', runtime.ConnectionManager())
    async def run():
        task = asyncio.create_task(runtime._ws_broadcaster(module.current_event_id()))
        try:
            for _ in range(100):
                with engine.connect() as conn:
                    if conn.execute(text('SELECT COUNT(*) FROM ws_events')).scalar_one() == 0:
                        return
                await asyncio.sleep(0.01)
            pytest.fail('No-client TTL cleanup did not run')
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    asyncio.run(run())


def test_audience_database_lookups_run_off_event_loop(monkeypatch):
    from core.app_websocket import ConnectionManager
    manager = ConnectionManager()
    loop_thread = threading.get_ident()
    checked = []
    def scope(message):
        checked.append(threading.get_ident())
        return 1, False
    def current(principal, message):
        checked.append(threading.get_ident())
        return True
    class Socket:
        async def send_json(self, message):
            pass
    manager.active = [(Socket(), {'id':1,'group_id':1})]
    monkeypatch.setattr(manager, '_printer_scope', scope)
    monkeypatch.setattr(manager, '_education_principal_current', current)
    assert asyncio.run(manager.broadcast({'data':{'printer_id':1,'education_internal':True}})) == 1
    assert len(checked) == 2 and all(identifier != loop_thread for identifier in checked)


@pytest.mark.parametrize('count', [-1, True, '4', None, 65, 10**100, 1.5])
def test_mmu_rejects_unbounded_or_malformed_gate_counts(count):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    assert MoonrakerPrinter('192.0.2.1')._parse_mmu_slots({'num_gates':count}) == []


def test_mmu_supported_gate_boundary():
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    assert len(MoonrakerPrinter('192.0.2.1')._parse_mmu_slots({'num_gates':64})) == 64


@pytest.mark.parametrize('method', ['_get', '_post'])
@pytest.mark.parametrize('oversized', [False, True])
def test_moonraker_response_reads_are_bounded(monkeypatch, method, oversized):
    from modules.printers.adapters import moonraker
    from core import itar
    monkeypatch.setattr(itar, 'enforce_request_destination', lambda url: None)
    reads = []
    class Response(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert size == 2 * 1024 * 1024 + 1
            return super().read(size)
    data = b' ' * (2 * 1024 * 1024 + 1) if oversized else b'{"result":{}}'
    monkeypatch.setattr(moonraker, 'urlopen', lambda *a, **kw: Response(data))
    result = getattr(moonraker.MoonrakerPrinter('192.0.2.1'), method)('/test')
    assert result == (None if oversized else {'result':{}})
    assert len(reads) == 1
