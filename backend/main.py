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
    __version__ = "1.3.27"


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
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "Accept"],
)

# ──────────────────────────────────────────────
# Security Headers Middleware
# ──────────────────────────────────────────────

_CSP_SKIP_PREFIXES = ("/api/docs", "/api/redoc", "/api/v1/docs", "/api/v1/redoc", "/openapi.json")

_CSP_DIRECTIVES = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self' ws: wss:",
    "media-src 'self' blob:",
    "frame-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
])


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Attach CSP (report-only) and other security headers to every response."""
    response = await call_next(request)

    # CSP — skip Swagger/ReDoc (they load external scripts)
    if not any(request.url.path.startswith(p) for p in _CSP_SKIP_PREFIXES):
        response.headers["Content-Security-Policy-Report-Only"] = _CSP_DIRECTIVES

    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "0"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"

    return response


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
    path = request.url.path
    # Strip /api/v1 or /api prefix for uniform matching
    _api_path = ""
    if path.startswith("/api/v1/"):
        _api_path = path[7:]  # strip "/api/v1" → "/..."
    elif path.startswith("/api/"):
        _api_path = path[4:]  # strip "/api" → "/..."

    if (
        path in ("/health", "/metrics", "/ws")
        or path.endswith("/label")
        or path.endswith("/labels/batch")
        or _api_path.startswith("/auth")
        or _api_path.startswith("/setup")
        or _api_path == "/license"
        or (_api_path == "/branding" and request.method == "GET")
        or path.startswith("/static/branding")
        or request.method == "OPTIONS"
    ):
        return await call_next(request)

    # IP allowlist check
    if path.startswith("/api/"):
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
# Health — root-level (not versioned)
# ──────────────────────────────────────────────

@app.get("/health", tags=["System"], include_in_schema=False)
async def health_root():
    """Root-level health check — delegates to /api/v1/health."""
    return await system.health_check()


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
# Routers — mounted under /api (backwards compat) and /api/v1 (versioned)
# ──────────────────────────────────────────────

_all_routers = [
    auth.router,
    printers.router,
    spools.router,
    cameras.router,
    jobs.router,
    models.router,
    scheduler.router,
    orders.router,
    orgs.router,
    analytics.router,
    alerts.router,
    system.router,
    vision.router,
]

for _router in _all_routers:
    app.include_router(_router, prefix="/api")
    app.include_router(_router, prefix="/api/v1")


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
