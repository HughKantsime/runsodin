"""
O.D.I.N. — Orchestrated Dispatch & Inventory Network API

FastAPI application shell. Route handlers live in backend/routers/,
shared dependencies in backend/deps.py.
"""

import asyncio
import json
import logging
import os
import pathlib

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from config import settings
from deps import engine, SessionLocal, Base
from routers import (
    alerts,
    analytics,
    auth,
    cameras,
    jobs,
    models,
    orders,
    orgs,
    printers,
    scheduler,
    spools,
    system,
    vision,
)

log = logging.getLogger("odin.api")

# ──────────────────────────────────────────────
# Version
# ──────────────────────────────────────────────

_version_file = pathlib.Path(__file__).parent.parent / "VERSION"
if _version_file.exists():
    __version__ = _version_file.read_text().strip()
else:
    __version__ = "1.3.25"


# ──────────────────────────────────────────────
# WebSocket Manager
# ──────────────────────────────────────────────

class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()


async def ws_broadcaster():
    """Background task: read events from hub file, broadcast to WebSocket clients."""
    from ws_hub import read_events_since
    import time

    last_ts = time.time()

    while True:
        await asyncio.sleep(1)

        if not ws_manager.active:
            last_ts = time.time()
            continue

        events, last_ts = read_events_since(last_ts)

        for evt in events:
            await ws_manager.broadcast(evt)


# ──────────────────────────────────────────────
# Lifespan
# ──────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup, start WebSocket broadcaster."""
    Base.metadata.create_all(bind=engine)
    broadcast_task = asyncio.create_task(ws_broadcaster())
    yield
    broadcast_task.cancel()


# ──────────────────────────────────────────────
# Application
# ──────────────────────────────────────────────

app = FastAPI(
    title="O.D.I.N.",
    description="Orchestrated Dispatch & Inventory Network — Self-hosted 3D print farm management",
    version=__version__,
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "Accept"],
)

# Static files for branding assets
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ──────────────────────────────────────────────
# Auth Middleware
# ──────────────────────────────────────────────

@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    """Check IP allowlist and API key for all routes."""
    # Skip auth for health endpoint and OPTIONS (CORS preflight)
    if (
        request.url.path in ("/health", "/metrics", "/ws")
        or request.url.path.endswith("/label")
        or request.url.path.endswith("/labels/batch")
        or request.url.path.startswith("/api/auth")
        or request.url.path.startswith("/api/setup")
        or request.url.path == "/api/license"
        or (request.url.path == "/api/branding" and request.method == "GET")
        or request.url.path.startswith("/static/branding")
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    # IP allowlist check
    if request.url.path.startswith("/api/"):
        try:
            _db = SessionLocal()
            try:
                ip_row = _db.execute(
                    text("SELECT value FROM system_config WHERE key = 'ip_allowlist'")
                ).fetchone()
                if ip_row:
                    import ipaddress

                    ip_config = json.loads(ip_row[0]) if isinstance(ip_row[0], str) else ip_row[0]
                    if ip_config and ip_config.get("enabled") and ip_config.get("cidrs"):
                        client_ip = request.client.host if request.client else "127.0.0.1"
                        if client_ip not in ("127.0.0.1", "::1"):
                            allowed = any(
                                ipaddress.ip_address(client_ip) in ipaddress.ip_network(c, strict=False)
                                for c in ip_config["cidrs"]
                            )
                            if not allowed:
                                from fastapi.responses import JSONResponse

                                return JSONResponse(
                                    status_code=403,
                                    content={"detail": "IP address not allowed"},
                                )
            finally:
                _db.close()
        except Exception:
            pass

    # If no API key configured, auth is disabled
    if not settings.api_key:
        return await call_next(request)

    # Check the API key
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != settings.api_key:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )

    return await call_next(request)


# ──────────────────────────────────────────────
# WebSocket Endpoint
# ──────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket endpoint for real-time printer telemetry and job updates.

    Pushes events:
    - printer_telemetry: {printer_id, bed_temp, nozzle_temp, state, progress, ...}
    - job_update: {printer_id, job_name, status, progress, layer, ...}
    - alert_new: {count} (new unread alert count)
    """
    await ws_manager.connect(ws)
    try:
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                if data == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_manager.disconnect(ws)


# ──────────────────────────────────────────────
# Routers
# ──────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(printers.router)
app.include_router(spools.router)
app.include_router(cameras.router)
app.include_router(jobs.router)
app.include_router(models.router)
app.include_router(scheduler.router)
app.include_router(orders.router)
app.include_router(orgs.router)
app.include_router(analytics.router)
app.include_router(alerts.router)
app.include_router(system.router)
app.include_router(vision.router)


# ──────────────────────────────────────────────
# Production Frontend Serving (SPA fallback)
# ──────────────────────────────────────────────

_frontend_dist = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
if os.path.isdir(_frontend_dist):
    from fastapi.responses import FileResponse

    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_frontend_dist, "assets")),
        name="frontend-assets",
    )

    @app.get("/{path:path}", include_in_schema=False)
    async def _serve_frontend(path: str):
        if path.startswith("api/") or path.startswith("static/") or path.startswith("health"):
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        exact_path = os.path.realpath(os.path.join(_frontend_dist, path))
        if not exact_path.startswith(os.path.realpath(_frontend_dist)):
            from fastapi import HTTPException

            raise HTTPException(status_code=404)
        if path and os.path.isfile(exact_path):
            return FileResponse(exact_path)
        return FileResponse(os.path.join(_frontend_dist, "index.html"))
