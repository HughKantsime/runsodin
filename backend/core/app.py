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

from fastapi import Depends, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from sqlalchemy import text


log = logging.getLogger("odin.api")


_version_file = pathlib.Path(__file__).parent.parent.parent / "VERSION"
if _version_file.exists():
    __version__ = _version_file.read_text().strip()
else:
    __version__ = "2.0.15"

# Module discovery helpers

# WebSocket manager (unchanged from original main.py)

from core.app_websocket import ConnectionManager, ws_manager, _ws_broadcaster
from core.app_maintenance import (
    _discover_modules, _resolve_load_order, _periodic_cleanup,
    _check_schema_drift, _revoke_legacy_biometric_tokens,
)

# Middleware setup

def _setup_middleware(app: FastAPI) -> None:
    """Attach CORS, rate limiting, and security-headers middleware to the app."""
    from core.config import settings
    from core.rate_limit import limiter
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    trusted_hosts = [host.strip() for host in settings.trusted_hosts.split(",") if host.strip()]
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=trusted_hosts or ["localhost", "127.0.0.1", "testserver"],
    )

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
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-API-Key",
            "Accept",
            # v1.8.9 (codex pass 10): agent-surface headers. Without
            # these in the allow-list, browser cross-origin clients
            # would fail preflight when sending the new primitives,
            # making safe retries + dry-run unavailable exactly in
            # the deployments where CORS is in play.
            "Idempotency-Key",
            "X-Dry-Run",
        ],
        expose_headers=[
            # Let JS read the replay indicator so SDKs can surface
            # "this was replayed" to their caller.
            "X-Idempotent-Replay",
        ],
    )

def _ip_allowlist_denial(session_factory, client_ip):
    """None when the IP allowlist admits client_ip; 403 when it refuses; 503 when the check failed.

    Loopback is always admitted (local administration and the bundled
    reverse proxy). The check fails closed.
    """
    try:
        # Cached per worker for a short TTL (core.ip_allowlist_cache); a failed read denies.
        from core.ip_allowlist_cache import policy
        config = policy(session_factory)
        if not config:
            return None
        import ipaddress
        if not (config and config.get("enabled") and config.get("cidrs")):
            return None
        from core.net_identity import is_loopback
        client_ip = client_ip or "127.0.0.1"
        if is_loopback(client_ip):
            return None
        address = ipaddress.ip_address(client_ip)
        if any(address in ipaddress.ip_network(c, strict=False) for c in config["cidrs"]):
            return None
        return 403
    except Exception as e:
        log.error("IP allowlist check failed — denying request (fail-closed): %s", e)
        return 503

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
    forever. The ordering below puts `authenticate_request` outside
    idempotency/dry-run so every request — cache hit, cache miss,
    pending, or conflict — first passes the live auth chain (JWT
    expiry, token expiry, is_active, blacklist, session cookie). The
    idempotency and dry-run layers sit inside, only reached by
    authorized callers.

    `security_headers` is registered LAST and is therefore the
    OUTERMOST wrapper. It must wrap auth: when auth returns 401/403
    via JSONResponse without calling call_next, those early responses
    still need CSP/HSTS/etc. (ODIN-136 root cause: HSTS missing on
    `/api/health` 401 because security_headers was previously
    innermost and never ran on auth-rejected responses.) The
    middleware is purely additive on the response side — it does not
    short-circuit requests, so wrapping auth introduces no bypass.
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

    # v1.8.9 (post-codex-fix-1): dry-run + idempotency registered BEFORE
    # authenticate_request so auth wraps them on the inbound path.
    # A cache-replay request must pass the live auth chain (token
    # expiry, revocation, is_active) first; only then does idempotency
    # decide whether to replay. See idempotency_middleware for the
    # claim-before-execute protocol.
    app.middleware("http")(dry_run_middleware)
    app.middleware("http")(idempotency_middleware)

    def _is_public_browser_request(request: Request) -> bool:
        """Return whether a request may reach the public browser shell.

        The SPA document and its static bundle must load before a user has a
        session cookie.  API and server-owned prefixes remain behind the
        perimeter, except for the explicit route exceptions below.
        """
        if request.method not in ("GET", "HEAD"):
            return False
        # Security decisions must use the router's ASGI path, not the URL
        # reconstructed from the attacker-controlled Host header. Affected
        # Starlette versions can otherwise produce a request.url.path that
        # disagrees with the path actually dispatched by the router.
        path = request.scope.get("path") or request.url.path
        reserved = ("/api", "/static", "/health", "/ws", "/openapi.json")
        return not any(path == prefix or path.startswith(prefix + "/") for prefix in reserved)

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        """Check IP allowlist and API key for all routes."""
        from core.db import SessionLocal
        from fastapi.responses import JSONResponse

        path = request.scope.get("path") or request.url.path
        _api_path = ""
        if path.startswith("/api/v1/"):
            _api_path = path[7:]
        elif path.startswith("/api/"):
            _api_path = path[4:]

        # IP allowlist: every /api request from a non-allowlisted address is
        # refused, authenticated or not (login, labels, setup and overlay
        # included). Only CORS preflights pass; they carry no credentials.
        if path.startswith("/api/") and request.method != "OPTIONS":
            denial = _ip_allowlist_denial(SessionLocal, request.client.host if request.client else None)
            if denial == 403:
                return JSONResponse(status_code=403, content={"detail": "IP address not allowed"})
            if denial == 503:
                return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})

        # Routes exempt from the global API key (they authenticate otherwise
        # or are public by design).
        if (
            path in ("/health", "/health/ready", "/ws", "/api/v1/ws")
            or path.endswith("/label")
            or path.endswith("/labels/batch")
            or _api_path.startswith("/auth")
            or _api_path.startswith("/overlay/")
            or _api_path.startswith("/setup")
            or _api_path == "/license"
            or (_api_path == "/branding" and request.method == "GET")
            or path.startswith("/static/branding")
            or _is_public_browser_request(request)
            or request.method == "OPTIONS"
        ):
            return await call_next(request)

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

        # Native clients deliberately do not retain browser cookies. Admit a
        # full access JWT, then let the route enforce its own auth and RBAC.
        authorization = request.headers.get("Authorization", "")
        scheme, _, bearer = authorization.partition(" ")
        if scheme.lower() == "bearer" and bearer.strip():
            from core.dependencies import validate_perimeter_access_token
            from starlette.concurrency import run_in_threadpool

            def validate_native_bearer():
                # SQLAlchemy sessions must be created, used and closed together
                # off the event loop, including when validation raises.
                with SessionLocal() as auth_db:
                    return validate_perimeter_access_token(bearer.strip(), auth_db)

            try:
                principal = await run_in_threadpool(validate_native_bearer)
            except Exception:
                return JSONResponse(status_code=503, content={"detail": "Service temporarily unavailable"})
            if principal is not None:
                return await call_next(request)

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

    # security_headers must be the OUTERMOST middleware so it runs on every
    # response — including 401/403/503 short-circuits from authenticate_request
    # that never reach inner middleware. ODIN-136: prior ordering (innermost)
    # left HSTS/CSP/etc. missing on auth-rejected responses like
    # `curl -sI https://odin.subsystem.app/api/health`.
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
        # HSTS emitted unconditionally: RFC 6797 §7.2 requires UAs to ignore the
        # header over plain HTTP, so this is safe behind a TLS-terminating proxy
        # (openresty/NPM on the LAN edge) where request.url.scheme is "http".
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        path = request.scope.get("path") or request.url.path
        if path == "/openapi.json" or path.startswith("/api/"):
            # API responses can contain student, user, job, report, or auth
            # state. Shared lab browsers and intermediary caches must not keep it.
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

# App factory

def _mount_static_assets(app,static_dir):
    from pathlib import Path
    from core.branding_storage import branding_directory,BrandingAssets
    directory=branding_directory(Path(static_dir).resolve()/'branding')
    assets=BrandingAssets(directory)
    app.state.branding_assets=assets
    app.mount('/static/branding',assets,name='branding-assets')
    app.mount('/static',StaticFiles(directory=static_dir),name='static')

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
    # Credential-bearing URLs (?token=...) must never reach access or error logs.
    from core.log_redaction import install_log_redaction
    install_log_redaction()
    from core.config import settings
    from core.db import engine, Base, get_db
    from core.auth import decode_token
    from core.registry import registry
    from core.itar import is_itar_mode, enforce_boot_config

    # v1.8.9: ITAR boot-audit. If ODIN_ITAR_MODE=1, every configured
    # outbound destination must resolve to a private/loopback range or
    # the container refuses to start. This fails loud rather than
    # leaking the first outbound request.
    #
    # Webhook URLs live in `system_config` (DB-backed) so we can only
    # audit the statically-known ones here (license_server_url). The
    # webhook runtime guard (`enforce_request_destination` inside
    # safe_post/trusted_post) catches DB-configured URLs at send time.
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

    # ODIN-142 Wake 2b: Bambu MQTT broker policy boot audit.
    # Refuses to start if `ODIN_ALLOW_INSECURE_BAMBU_BROKER` is set
    # together with `ODIN_ITAR_MODE=1`, or if the insecure-broker port
    # is unparsable, or if the allowlist contains a public IP literal.
    # No-op on production deploys (no env vars set).
    from modules.printers.telemetry.bambu.broker_policy import (
        enforce_boot_audit as _bambu_broker_boot_audit,
    )
    _bambu_broker_boot_audit()

    # Opt-in stress-test instrumentation. Env-gated so production code path
    # is byte-identical when unset. See tests/stress/mqtt/instrument.py.
    _stress_dir = os.environ.get("ODIN_STRESS_INSTRUMENTATION")
    if _stress_dir:
        try:
            from tests.stress.mqtt.instrument import install as _install_stress
            _install_stress(_stress_dir)
            log.warning("ODIN_STRESS_INSTRUMENTATION active — wrote to %s", _stress_dir)
        except Exception as _e:
            log.error("stress instrumentation install failed: %s", _e)

    # Module discovery and registry (at import time, not in lifespan)
    pkg_names = _discover_modules()
    ordered_pkgs = _resolve_load_order(pkg_names)

    log.info(f"Module load order: {[p.split('.')[-1] for p in ordered_pkgs]}")

    # Lifespan (DB init, event bus, background tasks)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from core.event_bus import get_event_bus
        from core.ws_hub import subscribe_to_bus as ws_subscribe
        from modules.printers import register_subscribers as printers_register
        from modules.notifications import register_subscribers as notifications_register
        from modules.archives import register_subscribers as archives_register

        import faulthandler
        import signal
        faulthandler.register(signal.SIGUSR1, all_threads=True)

        _check_schema_drift(engine, Base)

        # 2.0 one-time data cleanup: biometric refresh tokens minted before the
        # credential-route hardening (a scoped token could create one) are
        # revoked once, so every device re-enrols under the interactive-only
        # rule. Guarded by a system_config flag — no schema change.
        try:
            _revoke_legacy_biometric_tokens(engine)
        except Exception as e:
            log.warning("One-time biometric token revocation did not complete: %s", e)  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- static message; exception comes from SQL whose only bound values are the marker key, never token data

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
            from modules.printers.camera_relay import rotate_relay_secret
            from modules.printers.route_utils import sync_go2rtc_config_standalone
            # Per-boot secret for the internal camera relay (and go2rtc's RTSP credentials).
            # The container entrypoint rotates it and writes the go2rtc config before
            # go2rtc starts; then the API keeps it, and this sync restarts nothing.
            if os.environ.get("ODIN_CAMERA_RELAY_SECRET_ROTATED") != "1":
                rotate_relay_secret()
            sync_go2rtc_config_standalone()
            log.info("go2rtc config synced on startup")
        except Exception as e:
            log.warning(f"go2rtc config sync failed on startup: {e}")

        # Revoke combined read:cameras tokens (never valid in a release; overlay
        # URLs must carry camera-only tokens). Idempotent; never blocks startup.
        try:
            from core.db import SessionLocal
            from core.api_token_policy import revoke_legacy_camera_tokens
            with SessionLocal() as session:
                revoked = await asyncio.to_thread(revoke_legacy_camera_tokens, session)
            if revoked:
                log.warning("Revoked %d legacy combined read:cameras API token(s)", revoked)
        except Exception as e:
            log.warning("Legacy camera token revocation failed on startup: %s", type(e).__name__)  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs only the exception class name

        from core.ws_hub import current_event_id
        last_event_id = await asyncio.to_thread(current_event_id)
        broadcast_task = asyncio.create_task(_ws_broadcaster(last_event_id))
        cleanup_task = asyncio.create_task(_periodic_cleanup())
        # Replays job alerts a crash or a failed dispatch left in the outbox (migration 034).
        from modules.notifications.alert_outbox import run_sweeper
        outbox_task = asyncio.create_task(run_sweeper())
        yield
        broadcast_task.cancel()
        cleanup_task.cancel()
        outbox_task.cancel()
        app.state.branding_assets.close()

    # FastAPI instance
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

    # Keep the advertised documentation entry points alongside versioned URLs.
    from starlette.responses import RedirectResponse

    @app.get("/api/docs", include_in_schema=False)
    async def swagger_docs_alias(request: Request):
        return RedirectResponse(request.scope.get("root_path", "") + "/api/v1/docs")

    @app.get("/api/redoc", include_in_schema=False)
    async def redoc_docs_alias(request: Request):
        return RedirectResponse(request.scope.get("root_path", "") + "/api/v1/redoc")

    from core.app_errors import register_error_handlers
    register_error_handlers(app)

    # Middleware (order matters: added in reverse call order for ASGI stack)
    _setup_middleware(app)
    _register_http_middleware(app)
    from core.middleware.audit_actor import AuditActorMiddleware
    app.add_middleware(AuditActorMiddleware)

    # Static files for branding assets
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    os.makedirs(static_dir, exist_ok=True)
    _mount_static_assets(app,static_dir)

    # Health endpoint (registered before module routes to ensure priority)
    @app.get("/health", tags=["System"], include_in_schema=False)
    async def health_root():
        """Root-level health check — delegates to system module health_check."""
        from modules.system import routes as system
        return await system.health_check()

    @app.get("/health/ready", tags=["System"], include_in_schema=False)
    def health_ready_root(db=Depends(get_db)):
        """Public orchestrator probe backed by database and migration checks."""
        from modules.system.routes_health import readiness_check
        return readiness_check(db)

    # WebSocket endpoint
    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket, token: str = Query(default=None)):
        """
        WebSocket endpoint for real-time printer telemetry and job updates.

        Requires a short-lived, purpose-limited JWT passed as a query parameter
        (?token=...). Rejects all other credentials and unauthenticated connections.
        """
        from core.db import SessionLocal as _ws_session
        denial = _ip_allowlist_denial(_ws_session, ws.client.host if ws.client else None)
        if denial is not None:
            await ws.close(code=4003, reason="IP address not allowed" if denial == 403 else "Service temporarily unavailable")
            return

        principal = None
        if token:
            token_data = decode_token(token)
            if token_data:
                try:
                    import jwt as jwt_library
                    from core import auth as auth_module
                    from core.db import SessionLocal

                    payload = jwt_library.decode(
                        token,
                        auth_module.SECRET_KEY,
                        algorithms=[auth_module.ALGORITHM],
                    )
                    capability_snapshot_id = payload.get("capability_snapshot_id")
                    if (
                        payload.get("ws") is True
                        and isinstance(capability_snapshot_id, str)
                        and capability_snapshot_id
                    ):
                        with SessionLocal() as db:
                            row = db.execute(
                                text("SELECT id, username, role, group_id, created_at FROM users WHERE username = :username AND is_active IS TRUE"),
                                {"username": token_data.username},
                            ).fetchone()
                            jti = payload.get("jti")
                            revoked = bool(jti) and db.execute(
                                text("SELECT 1 FROM token_blacklist WHERE jti = :jti"), {"jti": jti}
                            ).first() is not None
                        from core.dependencies import token_predates_account
                        # A ws token names its account by id too; a recreated username
                        # (new id, later created_at) must never inherit it.
                        bound_id = payload.get("user_id")
                        if row and (revoked or token_predates_account(payload, row)
                                    or (bound_id is not None and bound_id != row.id)):
                            row = None
                        if row:
                            principal = dict(row._mapping)
                            principal.pop("created_at", None)
                            principal["_auth_kind"] = "websocket_token"
                            principal["_token_id"] = str(payload.get("jti") or "")
                            principal["_capability_snapshot_id"] = capability_snapshot_id
                except Exception:
                    log.exception("WebSocket principal lookup failed")

        if not principal:
            await ws.close(code=4001, reason="Authentication required")
            return

        await ws_manager.connect(ws, principal)
        try:
            while True:
                try:
                    data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                    if data == "ping":
                        await ws.send_text("pong")
                    else:
                        try:
                            request = json.loads(data)
                        except (json.JSONDecodeError, TypeError):
                            request = None
                        if request == {"type": "subscribe", "channels": ["events"]}:
                            await ws.send_json({"type": "subscribed", "channels": ["events"]})
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

    # Module registration — MUST happen before SPA catch-all
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

    # Production frontend serving (SPA fallback) — MUST be last route
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
