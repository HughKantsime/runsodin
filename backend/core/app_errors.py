"""Register the API error envelopes."""
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from core.error_buffer import error_buffer
log = logging.getLogger("odin.api")

def register_error_handlers(app: FastAPI):
    # v1.8.9: structured error envelope for agent-surface endpoints.
    # OdinError gets the machine-readable `{error: {code, detail, retriable}}`
    # shape; HTTPException falls through to a legacy-compatible envelope
    # so not-yet-migrated routes still produce something agents can parse.
    from core.errors import OdinError, ErrorCode
    from fastapi import HTTPException
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError

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

        # Codex pass 14 (2026-04-15): preserve exc.headers. Auth
        # routes raise HTTPException(401, headers={"WWW-Authenticate":
        # ...}); slowapi raises 429 with Retry-After. Losing those is
        # a protocol-level regression — spec-compliant clients can't
        # recover and operators see generic "access denied" instead of
        # the challenge/backoff signal.
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
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(request: Request, exc: RequestValidationError):
        from core.errors import request_validation_response
        return request_validation_response(exc)

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

