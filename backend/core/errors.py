"""Structured error envelope for agent-friendly error surfacing.

v1.8.9 introduces a stable `{error: {code, detail, retriable}}` shape on
all agent-surface endpoints. The `code` field is a machine-readable
enum so that the MCP tool layer and OpenClaw skill can branch cleanly
on expected error conditions (e.g. "retry with backoff on transient",
"surface to user on validation", "prompt re-auth on scope denial").

Historically ODIN routes raised `HTTPException(status_code=404,
detail="Printer not found")`. That worked fine for human-facing JSON
but forced MCP clients to string-match on `detail` to distinguish
"printer missing" from "quota exceeded" — fragile. OdinError + the
global handler below give agents a stable contract.

Usage:
    from core.errors import OdinError, ErrorCode

    raise OdinError(
        ErrorCode.printer_not_found,
        "Printer 42 not found",
        retriable=False,
        status=404,
    )

The global exception handler in `core/app.py` translates this into:
    HTTP 404
    {"error": {"code": "printer_not_found",
               "detail": "Printer 42 not found",
               "retriable": false}}

Legacy `HTTPException` raises are handled too (for not-yet-migrated
routes): the handler wraps them with `code="http_error"` and
`retriable=False`.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict


class ErrorCode(str, Enum):
    """Stable, machine-readable error codes.

    Agents can branch on these; changing a value is a breaking change
    for the MCP tool surface, so add new codes rather than rename.
    """

    # Resource lookup / identity
    printer_not_found = "printer_not_found"
    job_not_found = "job_not_found"
    spool_not_found = "spool_not_found"
    alert_not_found = "alert_not_found"
    maintenance_task_not_found = "maintenance_task_not_found"
    model_not_found = "model_not_found"
    user_not_found = "user_not_found"
    not_found = "not_found"  # generic fallback

    # State / business rules
    invalid_state_transition = "invalid_state_transition"
    quota_exceeded = "quota_exceeded"
    validation_failed = "validation_failed"
    feature_disabled = "feature_disabled"

    # Auth / authorization
    not_authenticated = "not_authenticated"
    permission_denied = "permission_denied"
    scope_denied = "scope_denied"

    # Agent-surface primitives
    idempotency_conflict = "idempotency_conflict"
    dry_run_unsupported = "dry_run_unsupported"

    # Infra / platform
    itar_outbound_blocked = "itar_outbound_blocked"
    upstream_unavailable = "upstream_unavailable"
    rate_limited = "rate_limited"
    internal_error = "internal_error"
    http_error = "http_error"  # legacy HTTPException wrapper


# Codes where a client should retry after a brief backoff.
_RETRIABLE_CODES: set[ErrorCode] = {
    ErrorCode.upstream_unavailable,
    ErrorCode.rate_limited,
    ErrorCode.internal_error,
}


def is_retriable(code: ErrorCode) -> bool:
    """Whether the error is worth retrying without operator input."""
    return code in _RETRIABLE_CODES


class OdinError(Exception):
    """Raise from route handlers to produce the standard error envelope.

    Parameters:
        code: stable identifier. Use `ErrorCode` enum.
        detail: human-readable message; safe to show in UI.
        status: HTTP status to emit (400–599).
        retriable: if None, derived from `code` via `is_retriable()`.
                   Override only when the context changes retriability
                   (e.g. a generally-retriable upstream failure that
                   we know won't recover this time).
        extra: optional dict merged into the `error` object. Keep
               keys snake_case.
    """

    def __init__(
        self,
        code: ErrorCode | str,
        detail: str,
        *,
        status: int = 400,
        retriable: bool | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> None:
        self.code = ErrorCode(code) if not isinstance(code, ErrorCode) else code
        self.detail = detail
        self.status = int(status)
        self.retriable = is_retriable(self.code) if retriable is None else bool(retriable)
        self.extra = dict(extra) if extra else {}
        super().__init__(f"[{self.code.value}] {detail}")

    def to_envelope(self) -> Dict[str, Any]:
        """Serialize into the standard JSON body."""
        error_obj: Dict[str, Any] = {
            "code": self.code.value,
            "detail": self.detail,
            "retriable": self.retriable,
        }
        error_obj.update(self.extra)
        return {"error": error_obj}
