# core/app.py — App factory with dynamic module discovery
#
# Creates and configures the FastAPI application. Discovers all modules under
# backend/modules/, resolves load order from REQUIRES/IMPLEMENTS declarations,
# and calls each module's register(app, registry) function.
#
# main.py becomes: from core.app import create_app; app = create_app()

import asyncio
import importlib
import json
import logging
import os
import pathlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from core.error_buffer import error_buffer

log = logging.getLogger("odin.api")

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

_version_file = pathlib.Path(__file__).parent.parent.parent / "VERSION"
if _version_file.exists():
    __version__ = _version_file.read_text().strip()
else:
    __version__ = "1.3.76"


# ---------------------------------------------------------------------------
# Module discovery helpers
# ---------------------------------------------------------------------------

def _discover_modules() -> list[str]:
    """Return a list of module package names found under backend/modules/.

    A valid module directory contains an __init__.py with a MODULE_ID attribute.
    """
    modules_dir = pathlib.Path(__file__).parent.parent / "modules"
    found = []
    for entry in sorted(modules_dir.iterdir()):
        if not entry.is_dir():
            continue
        init_file = entry / "__init__.py"
        if not init_file.exists():
            continue
        pkg_name = f"modules.{entry.name}"
        try:
            mod = importlib.import_module(pkg_name)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- verified safe — plugin discovery via os.scandir, not user input
            if hasattr(mod, "MODULE_ID"):
                found.append(pkg_name)
        except Exception as exc:
            log.warning(f"Module discovery: skipping {pkg_name!r} — {exc}")
    return found


def _resolve_load_order(pkg_names: list[str]) -> list[str]:
    """Topologically sort modules so that providers load before their consumers.

    Algorithm:
    1. Build a map of interface_name -> module_pkg for IMPLEMENTS declarations.
    2. For each module's REQUIRES list, find which module pkg provides that interface.
    3. Perform Kahn's topological sort (no-dependency modules first).
    4. Any modules with circular or unresolvable deps load in discovery order at
       the end (with a warning) rather than crashing startup.
    """
    # Load manifest metadata
    mods: dict[str, dict] = {}
    for pkg in pkg_names:
        m = importlib.import_module(pkg)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- verified safe — plugin discovery via os.scandir, not user input
        mods[pkg] = {
            "implements": getattr(m, "IMPLEMENTS", []),
            "requires": getattr(m, "REQUIRES", []),
        }

    # interface -> providing pkg
    providers: dict[str, str] = {}
    for pkg, info in mods.items():
        for iface in info["implements"]:
            providers[iface] = pkg

    # Build dependency edges: pkg -> set of pkgs it depends on
    edges: dict[str, set[str]] = {pkg: set() for pkg in pkg_names}
    for pkg, info in mods.items():
        for iface in info["requires"]:
            provider_pkg = providers.get(iface)
            if provider_pkg and provider_pkg != pkg:
                edges[pkg].add(provider_pkg)

    # Kahn's algorithm — in_degree = number of dependencies each pkg has
    in_degree: dict[str, int] = {pkg: len(deps) for pkg, deps in edges.items()}

    queue = sorted(pkg for pkg in pkg_names if in_degree[pkg] == 0)
    ordered: list[str] = []

    while queue:
        pkg = queue.pop(0)
        ordered.append(pkg)
        # Find all packages that depend on pkg and decrement their in_degree
        for other_pkg, deps in edges.items():
            if pkg in deps:
                in_degree[other_pkg] -= 1
                if in_degree[other_pkg] == 0:
                    queue.append(other_pkg)
                    queue.sort()

    # Any remaining packages have circular deps — append in discovery order
    remaining = [pkg for pkg in pkg_names if pkg not in ordered]
    if remaining:
        log.warning(
            f"Module load order: circular or unresolvable dependencies for "
            f"{remaining} — appending in discovery order."
        )
        ordered.extend(remaining)

    return ordered


# ---------------------------------------------------------------------------
# WebSocket manager (unchanged from original main.py)
# ---------------------------------------------------------------------------

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


async def _ws_broadcaster():
    """Background task: read events from ws_events table, broadcast to WebSocket clients."""
    from core.ws_hub import read_events_since

    last_id = 0
    while True:
        await asyncio.sleep(1)
        if not ws_manager.active:
            continue
        events, last_id = read_events_since(last_id)
        for evt in events:
            await ws_manager.broadcast(evt)


async def _periodic_cleanup():
    """Background task: hourly cleanup of stale login attempts, sessions, and expired tokens."""
    from core.db import SessionLocal

    while True:
        await asyncio.sleep(3600)
        try:
            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                db.execute(
                    text("DELETE FROM login_attempts WHERE attempted_at < :cutoff"),
                    {"cutoff": (now - timedelta(minutes=30)).timestamp()},
                )
                db.execute(
                    text("DELETE FROM active_sessions WHERE created_at < :cutoff"),
                    {"cutoff": (now - timedelta(hours=48)).isoformat()},
                )
                db.execute(
                    text("DELETE FROM token_blacklist WHERE expires_at < :now"),
                    {"now": now.isoformat()},
                )
                # v1.8.6 (codex pass 4): the digest-sends idempotency tables
                # grow monotonically without this — one row per user per
                # quiet-hours window forever. 30-day retention is well past
                # any reasonable troubleshooting window for "did I get the
                # 2026-Q1-04-13 digest?" while keeping the row count bounded.
                # Tables are guarded with `IF EXISTS` since older
                # deployments may not have run migrations 002/003 yet.
                # Inlined to avoid a semgrep false-positive on f-string
                # interpolation into text() — the alternative was a
                # hardcoded-allowlist loop, which semgrep flags anyway.
                # Wrapped each in its own try so a missing table on an
                # older deployment doesn't kill the other delete.
                _digest_cutoff = (now - timedelta(days=30)).isoformat()
                try:
                    db.execute(
                        text("DELETE FROM quiet_hours_digest_sends WHERE window_ended_at < :cutoff"),
                        {"cutoff": _digest_cutoff},
                    )
                except Exception:
                    db.rollback()
                try:
                    db.execute(
                        text("DELETE FROM quiet_hours_org_digest_sends WHERE window_ended_at < :cutoff"),
                        {"cutoff": _digest_cutoff},
                    )
                except Exception:
                    db.rollback()
                db.commit()

                # v1.8.9: prune expired idempotency-cache rows. 24h TTL
                # is enforced at read time too, but the hourly prune
                # keeps the table from growing unbounded. Wrapped in its
                # own try so a missing table on an older deployment
                # (migration 005 not applied) doesn't block the rest.
                try:
                    from core.middleware.idempotency import prune_expired_idempotency_keys
                    pruned = prune_expired_idempotency_keys(db)
                    if pruned:
                        log.info("Pruned %d expired idempotency-key rows", pruned)
                except Exception:
                    db.rollback()
                    log.debug("idempotency-key prune skipped (table may not exist yet)")

                log.info("Periodic cleanup completed: stale sessions, login attempts, expired tokens, old digest-send rows, idempotency cache")
            finally:
                db.close()
        except Exception:
            log.warning("Periodic cleanup failed", exc_info=True)


# ---------------------------------------------------------------------------
# Schema drift check (unchanged from original main.py)
# ---------------------------------------------------------------------------

_DUAL_SCHEMA_TABLES = [
    "vision_detections", "vision_settings", "vision_models",
    "timelapses", "nozzle_lifecycle",
    "consumables", "product_consumables", "consumable_usage",
]


def _check_schema_drift(engine, Base):
    """Compare SQLAlchemy column definitions against live PRAGMA table_info."""
    try:
        with engine.connect() as conn:
            for table_name in _DUAL_SCHEMA_TABLES:
                sa_table = Base.metadata.tables.get(table_name)
                if sa_table is None:
                    continue
                sa_cols = {c.name for c in sa_table.columns}
                rows = conn.execute(
                    text(f"PRAGMA table_info({table_name})")  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
                ).fetchall()
                if not rows:
                    continue
                db_cols = {r[1] for r in rows}
                only_sa = sa_cols - db_cols
                only_db = db_cols - sa_cols
                if only_sa:
                    log.warning(
                        f"Schema drift [{table_name}]: columns in models.py "
                        f"but not in DB: {sorted(only_sa)}"
                    )
                if only_db:
                    log.warning(
                        f"Schema drift [{table_name}]: columns in DB "
                        f"but not in models.py: {sorted(only_db)}"
                    )
    except Exception:
        log.debug("Schema drift check skipped", exc_info=True)


# ---------------------------------------------------------------------------
# Middleware setup
# ---------------------------------------------------------------------------

def _setup_middleware(app: FastAPI) -> None:
    """Attach CORS, rate limiting, and security-headers middleware to the app."""
    from core.config import settings
    from core.rate_limit import limiter
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # CORS
    _cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if "*" in _cors_origins:
        log.warning(
            "CORS origin '*' is incompatible with allow_credentials=True — "
            "falling back to empty origins list. Set explicit origins in CORS_ORIGINS."
        )
        _cors_origins = []

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "Accept"],
    )


def _register_http_middleware(app: FastAPI) -> None:
    """Register @app.middleware("http") handlers for security headers and auth.

    ASGI middleware ordering note: `@app.middleware("http")` wraps
    earlier-registered middleware with later-registered ones. The net
    effect: the LAST registration in this function becomes the OUTERMOST
    wrapper on the request path.

    Codex pass 1 (2026-04-14) flagged a critical auth-bypass risk in
    the prior ordering: the idempotency middleware was registered
    outermost so cached replays could short-circuit BEFORE auth ran.
    An expired/revoked token could keep replaying 2xx responses
    forever. The ordering below puts `authenticate_request` outermost
    so every request — cache hit, cache miss, pending, or conflict —
    first passes the live auth chain (JWT expiry, token expiry,
    is_active, blacklist, session cookie). The idempotency and
    dry-run layers sit inside, only reached by authorized callers.
    """
    from core.config import settings
    from core.middleware.dry_run import dry_run_middleware
    from core.middleware.idempotency import idempotency_middleware

    _CSP_SKIP_PREFIXES = (
        "/api/docs", "/api/redoc", "/api/v1/docs", "/api/v1/redoc", "/openapi.json"
    )
    _CSP_DIRECTIVES = "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "frame-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
    ])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        """Attach CSP and other security headers to every response."""
        response = await call_next(request)
        if not any(request.url.path.startswith(p) for p in _CSP_SKIP_PREFIXES):
            response.headers["Content-Security-Policy"] = _CSP_DIRECTIVES
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )
        return response

    # v1.8.9 (post-codex-fix-1): dry-run + idempotency registered BEFORE
    # authenticate_request so auth wraps them on the inbound path.
    # A cache-replay request must pass the live auth chain (token
    # expiry, revocation, is_active) first; only then does idempotency
    # decide whether to replay. See idempotency_middleware for the
    # claim-before-execute protocol.
    app.middleware("http")(dry_run_middleware)
    app.middleware("http")(idempotency_middleware)

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        """Check IP allowlist and API key for all routes."""
        from core.db import SessionLocal
        from fastapi.responses import JSONResponse

        path = request.url.path
        _api_path = ""
        if path.startswith("/api/v1/"):
            _api_path = path[7:]
        elif path.startswith("/api/"):
            _api_path = path[4:]

        if (
            path in ("/health", "/ws", "/api/v1/ws")
            or path.endswith("/label")
            or path.endswith("/labels/batch")
            or _api_path.startswith("/auth")
            or _api_path.startswith("/overlay/")
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
                        ip_config = (
                            json.loads(ip_row[0]) if isinstance(ip_row[0], str) else ip_row[0]
                        )
                        if ip_config and ip_config.get("enabled") and ip_config.get("cidrs"):
                            client_ip = request.client.host if request.client else "127.0.0.1"
                            if client_ip not in ("127.0.0.1", "::1"):
                                allowed = any(
                                    ipaddress.ip_address(client_ip)
                                    in ipaddress.ip_network(c, strict=False)
                                    for c in ip_config["cidrs"]
                                )
                                if not allowed:
                                    return JSONResponse(
                                        status_code=403,
                                        content={"detail": "IP address not allowed"},
                                    )
                finally:
                    _db.close()
            except Exception as e:
                log.error("IP allowlist check failed — denying request (fail-closed): %s", e)
                # IP allowlist: fail-closed
                return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})

        if not settings.api_key:
            return await call_next(request)

        import hmac

        # Check X-API-Key header (API clients, CLI tools)
        api_key = request.headers.get("X-API-Key")
        if api_key:
            # Global API key (constant-time comparison)
            if hmac.compare_digest(api_key, settings.api_key):
                return await call_next(request)
            # Per-user scoped tokens (odin_ prefix) are validated by get_current_user
            if api_key.startswith("odin_"):
                return await call_next(request)
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid or missing API key"},
            )

        # Fallback: allow requests with a valid session cookie (browser SPA auth).
        # The login endpoint (bypassed above) sets an httpOnly session cookie;
        # subsequent browser requests include it automatically via credentials:'include'.
        session_cookie = request.cookies.get("session")
        if session_cookie:
            from core.auth import decode_token
            import jwt as _jwt
            from core.auth import SECRET_KEY, ALGORITHM
            try:
                token_data = decode_token(session_cookie)
                if token_data:
                    # Reject mfa_pending and ws-only tokens at the perimeter
                    payload = _jwt.decode(session_cookie, SECRET_KEY, algorithms=[ALGORITHM])
                    if not payload.get("mfa_pending") and not payload.get("ws"):
                        return await call_next(request)
            except Exception:
                pass  # malformed / expired cookie → fall through to 401

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    """Create and fully configure the ODIN FastAPI application.

    1. Discover all modules under backend/modules/.
    2. Resolve load order by REQUIRES/IMPLEMENTS declarations.
    3. Build ModuleRegistry; call each module's register(app, registry) at
       module-level (not in lifespan) so routes are registered before the
       SPA catch-all route and before the first request arrives.
    4. Create FastAPI instance with lifespan (for DB init, event bus, bg tasks).
    5. Attach middleware and CORS.
    6. Register WebSocket endpoint, health endpoint, and frontend static files.

    Returns the fully configured app object. Uvicorn finds it via main:app.
    """
    from core.config import settings
    from core.db import engine, Base
    from core.auth import decode_token
    from core.registry import registry
    from core.itar import is_itar_mode, enforce_boot_config

    # -----------------------------------------------------------------------
    # v1.8.9: ITAR boot-audit. If ODIN_ITAR_MODE=1, every configured
    # outbound destination must resolve to a private/loopback range or
    # the container refuses to start. This fails loud rather than
    # leaking the first outbound request.
    #
    # Webhook URLs live in `system_config` (DB-backed) so we can only
    # audit the statically-known ones here (license_server_url). The
    # webhook runtime guard (`enforce_request_destination` inside
    # safe_post/trusted_post) catches DB-configured URLs at send time.
    # -----------------------------------------------------------------------
    if is_itar_mode():
        log.warning(
            "ODIN_ITAR_MODE=1 — refusing outbound to public addresses. "
            "Private/loopback targets only. This is a hard mode."
        )
        # Codex pass 8 (2026-04-15): spoolman_url was missing from the
        # boot audit. Include every statically-known outbound URL.
        enforce_boot_config([
            settings.license_server_url,
            getattr(settings, "spoolman_url", "") or "",
        ])

    # -----------------------------------------------------------------------
    # Module discovery and registry (at import time, not in lifespan)
    # -----------------------------------------------------------------------
    pkg_names = _discover_modules()
    ordered_pkgs = _resolve_load_order(pkg_names)

    log.info(f"Module load order: {[p.split('.')[-1] for p in ordered_pkgs]}")

    # -----------------------------------------------------------------------
    # Lifespan (DB init, event bus, background tasks)
    # -----------------------------------------------------------------------
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from core.event_bus import get_event_bus
        from core.ws_hub import subscribe_to_bus as ws_subscribe
        from modules.printers import register_subscribers as printers_register
        from modules.notifications import register_subscribers as notifications_register
        from modules.archives import register_subscribers as archives_register

        Base.metadata.create_all(bind=engine)

        from core.ws_hub import ensure_table as _ws_ensure
        _ws_ensure()

        _check_schema_drift(engine, Base)

        # v1.8.9 codex pass 4: second ITAR audit, now that DB is
        # populated. The early `create_app`-level check only sees
        # static settings (license_server_url). DB-configured
        # webhooks / update URLs are only visible after migrations
        # run. Fail closed BEFORE accepting traffic if any of those
        # resolve to a public address under ITAR mode.
        if is_itar_mode():
            from core.itar import collect_db_configured_urls
            db_urls = collect_db_configured_urls()
            if db_urls:
                log.info(
                    "ITAR mode: auditing %d DB-configured outbound URL(s).",
                    len(db_urls),
                )
                enforce_boot_config(db_urls)

        # Validate all declared dependencies are satisfied
        registry.validate_dependencies()

        # Wire event bus subscribers
        _bus = get_event_bus()
        ws_subscribe(_bus)
        printers_register(_bus)
        notifications_register(_bus)
        archives_register(_bus)

        log.info("Event bus initialized with module subscribers")

        if not settings.api_key:
            log.warning(
                "API_KEY is not set — perimeter authentication is DISABLED. "
                "All requests are accepted without an API key. "
                "Set API_KEY in your environment or docker-compose.yml for production use."
            )

        # Sync go2rtc camera config on startup
        try:
            from modules.printers.route_utils import sync_go2rtc_config_standalone
            sync_go2rtc_config_standalone()
            log.info("go2rtc config synced on startup")
        except Exception as e:
            log.warning(f"go2rtc config sync failed on startup: {e}")

        broadcast_task = asyncio.create_task(_ws_broadcaster())
        cleanup_task = asyncio.create_task(_periodic_cleanup())
        yield
        broadcast_task.cancel()
        cleanup_task.cancel()

    # -----------------------------------------------------------------------
    # FastAPI instance
    # -----------------------------------------------------------------------
    app = FastAPI(
        title="O.D.I.N.",
        description=(
            "Orchestrated Dispatch & Inventory Network — "
            "Self-hosted 3D print farm management"
        ),
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/v1/docs",
        redoc_url="/api/v1/redoc",
    )

    # v1.8.9: structured error envelope for agent-surface endpoints.
    # OdinError gets the machine-readable `{error: {code, detail, retriable}}`
    # shape; HTTPException falls through to a legacy-compatible envelope
    # so not-yet-migrated routes still produce something agents can parse.
    from core.errors import OdinError, ErrorCode
    from fastapi import HTTPException

    @app.exception_handler(OdinError)
    async def _odin_error_handler(request: Request, exc: OdinError):
        return JSONResponse(status_code=exc.status, content=exc.to_envelope())

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request: Request, exc: HTTPException):
        # Codex pass 3 (2026-04-14): preserve the legacy top-level
        # `detail` field. Several shipped clients — including
        # `frontend/src/api/client.ts` and `frontend/src/api/auth.ts`
        # — read `err.detail` directly. Rewriting every HTTPException
        # into only `{error: {...}}` would break those callers.
        # Dual-shape envelope: old clients keep reading `detail`, new
        # clients (agents / MCP tool layer) read `error.code`.
        #
        # Also: pass structured `detail` (dict, list) through
        # unchanged. Some routes intentionally raise with a dict
        # payload (e.g. quota-usage breakdown); stringifying those
        # would turn a JSON body into a Python repr which no caller
        # can parse.
        code = ErrorCode.http_error
        if exc.status_code == 401:
            code = ErrorCode.not_authenticated
        elif exc.status_code == 403:
            code = ErrorCode.permission_denied
        elif exc.status_code == 404:
            code = ErrorCode.not_found
        elif exc.status_code == 429:
            code = ErrorCode.rate_limited

        # Detail: pass structured payloads through; stringify only scalars.
        if isinstance(exc.detail, (dict, list)):
            legacy_detail = exc.detail
            agent_detail = exc.detail
        elif exc.detail is None:
            legacy_detail = code.value
            agent_detail = code.value
        else:
            legacy_detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
            agent_detail = legacy_detail

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": legacy_detail,
                "error": {
                    "code": code.value,
                    "detail": agent_detail,
                    "retriable": code == ErrorCode.rate_limited,
                },
            },
        )

    # Global exception handler — capture unhandled exceptions into ring buffer
    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        error_buffer.capture(exc, request)
        log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
        # Dual-shape envelope (see HTTPException handler above). Keep
        # the legacy `detail` so existing frontend fallback messages
        # still produce a readable error.
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "error": {
                    "code": ErrorCode.internal_error.value,
                    "detail": "Internal server error",
                    "retriable": True,
                },
            },
        )

    # Middleware (order matters: added in reverse call order for ASGI stack)
    _setup_middleware(app)
    _register_http_middleware(app)

    # Static files for branding assets
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    os.makedirs(static_dir, exist_ok=True)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # -----------------------------------------------------------------------
    # Health endpoint (registered before module routes to ensure priority)
    # -----------------------------------------------------------------------
    @app.get("/health", tags=["System"], include_in_schema=False)
    async def health_root():
        """Root-level health check — delegates to system module health_check."""
        from modules.system import routes as system
        return await system.health_check()

    # -----------------------------------------------------------------------
    # WebSocket endpoint
    # -----------------------------------------------------------------------
    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket, token: str = Query(default=None)):
        """
        WebSocket endpoint for real-time printer telemetry and job updates.

        Requires a valid JWT token passed as a query parameter (?token=...) or
        a valid API key (?token=<api_key>). Rejects unauthenticated connections.
        """
        import hmac

        authenticated = False
        if token:
            if decode_token(token):
                authenticated = True
            elif settings.api_key and hmac.compare_digest(token, settings.api_key):
                authenticated = True

        if not settings.api_key and not authenticated:
            authenticated = True

        if not authenticated:
            await ws.close(code=4001, reason="Authentication required")
            return

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

    @app.websocket("/api/v1/ws")
    async def websocket_endpoint_v1(ws: WebSocket, token: str = Query(default=None)):
        """Alias for /ws — native clients connect via /api/v1/ws."""
        await websocket_endpoint(ws, token=token)

    # -----------------------------------------------------------------------
    # Module registration — MUST happen before SPA catch-all
    # -----------------------------------------------------------------------
    for pkg in ordered_pkgs:
        mod = importlib.import_module(pkg)  # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import -- verified safe — plugin discovery via os.scandir, not user input
        # Record REQUIRES for dependency validation (done at startup)
        registry.record_requires(
            getattr(mod, "MODULE_ID", pkg),
            getattr(mod, "REQUIRES", []),
        )
        if hasattr(mod, "register"):
            try:
                mod.register(app, registry)
                log.debug(f"Registered module: {pkg}")
            except Exception as exc:
                log.error(f"Failed to register module {pkg!r}: {exc}", exc_info=True)

    # -----------------------------------------------------------------------
    # Production frontend serving (SPA fallback) — MUST be last route
    # -----------------------------------------------------------------------
    _frontend_dist = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")
    if os.path.isdir(_frontend_dist):
        from fastapi.responses import FileResponse

        app.mount(
            "/assets",
            StaticFiles(directory=os.path.join(_frontend_dist, "assets")),
            name="frontend-assets",
        )

        @app.get("/{path:path}", include_in_schema=False)
        async def _serve_frontend(path: str):
            if (
                path.startswith("api/")
                or path.startswith("static/")
                or path.startswith("health")
            ):
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
            exact_path = os.path.realpath(os.path.join(_frontend_dist, path))
            if not exact_path.startswith(os.path.realpath(_frontend_dist)):
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
            if path and os.path.isfile(exact_path):
                return FileResponse(exact_path)
            return FileResponse(os.path.join(_frontend_dist, "index.html"))

    return app
