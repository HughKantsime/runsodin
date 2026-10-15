"""Cert-E integ: F14 14.5 Spoolman link / pull / push / SSRF / loud failure.

The external Spoolman is a protocol-accurate local HTTP peer on 127.0.0.1 (ephemeral port)
modelled on Spoolman's v1 REST API: GET /api/v1/spool, GET /api/v1/filament and
PUT /api/v1/spool/{id}/use {"use_weight": g} (Spoolman declares /use as PUT; any other
method gets FastAPI's 405 {"detail": "Method Not Allowed"}). Everything inside ODIN is real:
routers, bootstrapped SQLite schema, background push thread, event bus -> archive chain.
"""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case  # noqa: F401
from modules.archives import archive as _archive_module

REAL_ARCHIVE_SYNC = _archive_module._sync_spoolman_consumption

SPOOLMAN_SPOOLS = [
    {"id": 42, "registered": "2026-01-01T00:00:00Z", "remaining_weight": 800.0, "used_weight": 200.0,
     "initial_weight": 1000.0, "archived": False,
     "filament": {"id": 7, "name": "Synthetic Galaxy Black", "material": "PLA", "color_hex": "1a1a1a",
                  "density": 1.24, "diameter": 1.75, "vendor": {"id": 1, "name": "Synthetic Vendor"}}},
    {"id": 43, "registered": "2026-01-02T00:00:00Z", "remaining_weight": 0.0, "used_weight": 1000.0,
     "initial_weight": 1000.0, "archived": False,
     "filament": {"id": 8, "name": "Synthetic Signal Red", "material": "PETG", "color_hex": "cc0000",
                  "density": 1.27, "diameter": 1.75}},
]


class SpoolmanPeer:
    def __init__(self):
        self.requests = []
        self.spools = {s["id"]: json.loads(json.dumps(s)) for s in SPOOLMAN_SPOOLS}
        self.event = threading.Event()
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else None

            def _route(self, method):
                body = self._body() if method in ("POST", "PUT") else None
                peer.requests.append((method, self.path, body))
                try:
                    if self.path == "/api/v1/spool" and method == "GET":
                        return self._send(200, list(peer.spools.values()))
                    if self.path == "/api/v1/filament" and method == "GET":
                        return self._send(200, [s["filament"] for s in peer.spools.values()])
                    parts = self.path.strip("/").split("/")
                    if len(parts) == 5 and parts[:3] == ["api", "v1", "spool"] and parts[4] == "use":
                        if method != "PUT":
                            return self._send(405, {"detail": "Method Not Allowed"})
                        spool = peer.spools.get(int(parts[3]))
                        if spool is None:
                            return self._send(404, {"message": "Spool not found"})
                        spool["used_weight"] += body["use_weight"]
                        spool["remaining_weight"] = max(0.0, spool["remaining_weight"] - body["use_weight"])
                        return self._send(200, spool)
                    return self._send(404, {"detail": "Not Found"})
                finally:
                    peer.event.set()

            def do_GET(self): self._route("GET")
            def do_PUT(self): self._route("PUT")
            def do_POST(self): self._route("POST")
            def log_message(self, *a): pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


@pytest.fixture
def spoolman(monkeypatch):
    from core.config import settings
    peer = SpoolmanPeer()
    monkeypatch.setattr(settings, "spoolman_url", peer.url)
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    yield peer
    peer.close()


@pytest.fixture
def odin(review_db, monkeypatch):  # noqa: F811
    """Real inventory/spoolman/job-lifecycle routers on the bootstrapped schema."""
    from core import db as core_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.inventory.routes import spools, spoolman as spoolman_routes
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    binding = review_db.get_bind()
    monkeypatch.setattr(core_db, "SessionLocal", sessionmaker(bind=binding))
    app = FastAPI()
    for module in (spools, spoolman_routes, jobs_lifecycle):
        app.include_router(module.router)

    def request_db():
        with Session(binding) as session:
            yield session
    app.dependency_overrides[core_db.get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {"id": 3, "username": "admin", "role": "operator",
                                                          "group_id": 1, "is_active": True}
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
    review_db.commit()
    with TestClient(app) as client:
        yield client, binding


def _background_threads(monkeypatch):
    original = threading.Thread
    started = []

    def factory(*args, **kwargs):
        thread = original(*args, **kwargs)
        target = kwargs.get("target")
        if target is not None and target.__name__ == "_push_async":
            started.append(thread)
        return thread
    monkeypatch.setattr(threading, "Thread", factory)
    return started


def _completion_case(review_db, spoolman_id=42):  # noqa: F811
    review_db.execute(text("INSERT INTO models(id,name,org_id,color_requirements) VALUES(101,'Fixture model',1,:r)"),
                      {"r": json.dumps({"slot1": {"grams": 12.75}})})
    review_db.execute(text("INSERT INTO jobs(id,item_name,model_id,printer_id,status,charged_to_org_id,quantity,hold) "
                           "VALUES(101,'Fixture print',101,9,'printing',1,1,0)"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,status,location_printer_id,location_slot,remaining_weight_g,"
                           "initial_weight_g,spoolman_spool_id,org_id) VALUES(101,101,'active',9,1,100,100,:sm,1)"),
                      {"sm": spoolman_id})
    review_db.commit()


# ---------------------------------------------------------------- link (14.5-01)

@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/inventory/routes/spools.py:145-161 create_spool builds the INSERT values without "
    "SpoolCreate.spoolman_spool_id (schemas.py:34) and core/economic_source_creation.py:_SPOOL_FIELDS omits it; "
    "SpoolUpdate (schemas.py:40) has no such field, so POST /spools {spoolman_spool_id: 42} returns 200 and "
    "persists NULL - no API can link an ODIN spool to a Spoolman spool"))
def test_spool_created_with_spoolman_spool_id_is_linked(odin, review_db):  # noqa: F811
    client, binding = odin
    response = client.post("/spools", json={"filament_id": 101, "initial_weight_g": 1000, "spoolman_spool_id": 42})
    assert response.status_code == 200, response.text
    spool_id = response.json()["id"]
    with Session(binding) as fresh:
        assert fresh.execute(text("SELECT spoolman_spool_id FROM spools WHERE id=:i"), {"i": spool_id}).scalar_one() == 42
    assert client.get(f"/spools/{spool_id}").json()["spoolman_spool_id"] == 42


def test_spool_create_accepts_but_drops_spoolman_spool_id(odin, review_db):  # noqa: F811
    """Evidence for the DEFECT above: the request succeeds, the link is silently lost."""
    client, binding = odin
    response = client.post("/spools", json={"filament_id": 101, "initial_weight_g": 1000, "spoolman_spool_id": 42})
    assert response.status_code == 200, response.text
    with Session(binding) as fresh:
        assert fresh.execute(text("SELECT spoolman_spool_id FROM spools WHERE id=:i"),
                             {"i": response.json()["id"]}).scalar_one() is None


# ---------------------------------------------------------------- pull on demand (14.5-02)

def test_pull_on_demand_reads_spoolman_inventory_but_imports_nothing(odin, spoolman, review_db):  # noqa: F811
    client, binding = odin
    with Session(binding) as fresh:
        before = fresh.execute(text("SELECT COUNT(*) FROM spools")).scalar_one()
    listed = client.get("/spoolman/spools")
    assert listed.status_code == 200, listed.text
    assert listed.json() == [
        {"id": 42, "filament_name": "Synthetic Galaxy Black", "filament_type": "PLA", "color_name": None,
         "color_hex": "1a1a1a", "remaining_weight": 800.0},
        {"id": 43, "filament_name": "Synthetic Signal Red", "filament_type": "PETG", "color_name": None,
         "color_hex": "cc0000", "remaining_weight": 0.0},
    ]
    synced = client.post("/spoolman/sync")
    assert synced.status_code == 200, synced.text
    assert synced.json()["success"] is True and synced.json()["spools_found"] == 2
    assert synced.json()["slots_updated"] == 0
    assert ("GET", "/api/v1/spool", None) in spoolman.requests
    with Session(binding) as fresh:
        assert fresh.execute(text("SELECT COUNT(*) FROM spools")).scalar_one() == before
        assert fresh.execute(text("SELECT COUNT(*) FROM spools WHERE spoolman_spool_id IS NOT NULL")).scalar_one() == 0


# ---------------------------------------------------------------- push on completion (14.5-02)

def _complete(client, started):
    response = client.post("/jobs/101/complete")
    assert response.status_code == 200, response.text
    assert len(started) == 1
    started[0].join(timeout=10)
    assert not started[0].is_alive()


def test_manual_completion_sends_post_use_with_consumed_grams(odin, spoolman, review_db, monkeypatch):  # noqa: F811
    client, binding = odin
    _completion_case(review_db)
    started = _background_threads(monkeypatch)
    _complete(client, started)
    assert [r for r in spoolman.requests if r[1].endswith("/use")] == [
        ("POST", "/api/v1/spool/42/use", {"use_weight": 12.75})]
    with Session(binding) as fresh:
        assert fresh.execute(text("SELECT remaining_weight_g FROM spools WHERE id=101")).scalar_one() == 87.25


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/inventory/services.py:53 _post_spoolman_usage sends POST /api/v1/spool/{id}/use, "
    "but Spoolman's API declares /api/v1/spool/{id}/use as PUT (archive.py:175 already uses PUT); a "
    "protocol-accurate Spoolman answers 405 and the consumption never lands in Spoolman"))
def test_manual_completion_consumption_lands_in_spoolman(odin, spoolman, review_db, monkeypatch):  # noqa: F811
    client, binding = odin
    _completion_case(review_db)
    started = _background_threads(monkeypatch)
    _complete(client, started)
    assert spoolman.spools[42]["remaining_weight"] == pytest.approx(800.0 - 12.75)
    with Session(binding) as fresh:
        assert "Spoolman push failed" not in (fresh.execute(text("SELECT notes FROM jobs WHERE id=101")).scalar_one() or "")


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/archives/attempt_accounting.py:336 queues printer-observed consumption in "
    "filament_delivery_outbox(state='pending') but no production code ever reads/delivers that outbox "
    "(only the manual POST /jobs/{id}/complete path pushes); a monitor-observed completion published on the "
    "event bus never reaches Spoolman"))
def test_printer_observed_completion_pushes_consumption_to_spoolman(archive_case, spoolman, monkeypatch):  # noqa: F811
    archive, engine = archive_case
    monkeypatch.setattr(archive, "_sync_spoolman_consumption", REAL_ARCHIVE_SYNC)
    import core.event_bus as event_bus
    from core.committed_terminal_events import publish_committed_completion
    from modules.archives import register_subscribers
    with engine.begin() as conn:
        conn.execute(text("UPDATE spools SET spoolman_spool_id=42 WHERE id=7099"))
        conn.execute(text("UPDATE print_jobs SET status='running' WHERE id=7099"))  # not terminal until published
    bus = event_bus.InMemoryEventBus(); register_subscribers(bus)  # also runs startup recovery
    monkeypatch.setattr(event_bus, "_bus", bus)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM print_archives")).scalar_one() == 0
        conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
    assert publish_committed_completion(9, 7099, 7099)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT state FROM filament_delivery_outbox WHERE print_job_id=7099")).scalar_one() == "pending"
    assert spoolman.event.wait(5), "Spoolman never contacted after observed completion"
    assert any(r[1] == "/api/v1/spool/42/use" for r in spoolman.requests)


def test_printer_observed_completion_leaves_outbox_pending_without_delivery(archive_case, spoolman, monkeypatch):  # noqa: F811
    """Evidence for the DEFECT above through the real event-bus -> archive chain."""
    archive, engine = archive_case
    monkeypatch.setattr(archive, "_sync_spoolman_consumption", REAL_ARCHIVE_SYNC)
    import core.event_bus as event_bus
    from core.committed_terminal_events import publish_committed_completion
    from modules.archives import register_subscribers
    with engine.begin() as conn:
        conn.execute(text("UPDATE spools SET spoolman_spool_id=42 WHERE id=7099"))
        conn.execute(text("UPDATE print_jobs SET status='running' WHERE id=7099"))  # not terminal until published
    bus = event_bus.InMemoryEventBus(); register_subscribers(bus)  # also runs startup recovery
    monkeypatch.setattr(event_bus, "_bus", bus)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM print_archives")).scalar_one() == 0
        conn.execute(text("UPDATE print_jobs SET status='completed' WHERE id=7099"))
    assert publish_committed_completion(9, 7099, 7099)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT spoolman_spool_id,grams_used,state FROM filament_delivery_outbox")).all()
        assert [tuple(r) for r in rows] == [(42, 40, "pending")]
        assert conn.execute(text("SELECT remaining_weight_g FROM spools WHERE id=7099")).scalar_one() == 960
    assert spoolman.requests == []


# ---------------------------------------------------------------- SSRF (14.5-03)

def test_push_dispatch_does_not_use_safe_post(odin, spoolman, review_db, monkeypatch):  # noqa: F811
    """The push succeeds with core.webhook_utils.safe_post/trusted_post made unusable: the
    DNS-pinned dispatcher is inventory/services.py:_post_spoolman_usage, not safe_post()."""
    import core.webhook_utils as webhook_utils
    def forbidden(*a, **k): raise AssertionError("safe_post/trusted_post used")
    monkeypatch.setattr(webhook_utils, "safe_post", forbidden)
    monkeypatch.setattr(webhook_utils, "trusted_post", forbidden)
    from modules.inventory import services
    calls = []
    real = services._post_spoolman_usage
    monkeypatch.setattr(services, "_post_spoolman_usage", lambda *a: calls.append(a) or real(*a))
    assert services.push_consumption_to_spoolman([{"spoolman_spool_id": 42, "grams": 1.5, "job_id": 1}]) == [
        f"Spoolman spool=42 job=1 rejected push: HTTP 405"]
    assert calls == [(spoolman.url, 42, 1.5)]


@pytest.mark.parametrize("address", ["169.254.169.254", "100.100.100.200"])
def test_configured_metadata_destination_is_refused_before_any_connection(odin, review_db, monkeypatch, address):  # noqa: F811
    """User-configured URL resolving to cloud metadata is refused by DNS validation through the completion path."""
    from core.config import settings
    client, binding = odin
    monkeypatch.setattr(settings, "spoolman_url", "http://spoolman.synthetic.test:7912")
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))])
    def no_connect(*a, **k): raise AssertionError("connection attempted")
    monkeypatch.setattr(socket, "create_connection", no_connect)
    _completion_case(review_db)
    started = _background_threads(monkeypatch)
    _complete(client, started)
    with Session(binding) as fresh:
        notes = fresh.execute(text("SELECT notes FROM jobs WHERE id=101")).scalar_one()
    assert "Spoolman push failed for 1 spool(s); see server logs." in notes
    assert address not in notes


# ---------------------------------------------------------------- unreachable -> loud (14.5-04)

def test_unreachable_spoolman_is_logged_and_appended_to_job_notes(odin, review_db, monkeypatch, caplog):  # noqa: F811
    from core.config import settings
    client, binding = odin
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); closed_port = s.getsockname()[1]
    monkeypatch.setattr(settings, "spoolman_url", f"http://127.0.0.1:{closed_port}")
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    _completion_case(review_db)
    started = _background_threads(monkeypatch)
    with caplog.at_level("ERROR", logger="odin.api"):
        _complete(client, started)
    with Session(binding) as fresh:
        state, notes = fresh.execute(text("SELECT status,notes FROM jobs WHERE id=101")).one()
        remaining = fresh.execute(text("SELECT remaining_weight_g FROM spools WHERE id=101")).scalar_one()
    assert state == "completed" and remaining == 87.25
    assert notes.endswith("[v1.8.6] Spoolman push failed for 1 spool(s); see server logs.")
    assert "127.0.0.1" not in notes
    errors = [r.getMessage() for r in caplog.records if "Spoolman push error on job 101" in r.getMessage()]
    assert len(errors) == 1 and "spool=42" in errors[0]
