"""Authentication and capability rules for Education workflows."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unicodedata
from collections.abc import Callable

from fastapi import Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

import core.auth as auth_module
from core.db import get_db
from core.dependencies import get_current_user
from core.errors import ErrorCode, OdinError
from license_manager import get_license


EDUCATION_FEATURE = "education_workflows"
EDUCATION_TIERS = frozenset({"education", "enterprise"})
CURSOR_ORDER_VERSION = 1


def education_license_enabled() -> bool:
    license_info = get_license()
    return bool(
        license_info.valid
        and license_info.tier in EDUCATION_TIERS
        and license_info.has_feature(EDUCATION_FEATURE)
    )


def require_education_license() -> None:
    if not education_license_enabled():
        raise OdinError(
            ErrorCode.feature_disabled,
            "Education workflows require a valid Education or Enterprise entitlement",
            status=403,
        )


def normalize_center_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "")
    return " ".join(normalized.strip().split()).casefold()


def _education_token_allowed(principal: dict, *, write: bool) -> bool:
    auth_kind = principal.get("_auth_kind")
    if auth_kind in {"session_jwt", "bearer_jwt"}:
        return True
    if auth_kind != "user_api_token":
        return False
    scopes = set(principal.get("_token_scopes") or ())
    required = "write:education" if write else "read:education"
    return required in scopes or (not write and "write:education" in scopes)


def require_education_principal(*, write: bool = False) -> Callable:
    async def dependency(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        require_education_license()
        if not current_user or not bool(current_user.get("is_active")):
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not _education_token_allowed(current_user, write=write):
            raise HTTPException(
                status_code=403,
                detail="This authentication method is not authorized for Education workflows",
            )
        return current_user

    return dependency


def require_education_capability_principal() -> Callable:
    """Authenticate capabilities requests without requiring an entitlement."""
    async def dependency(
        current_user: dict = Depends(get_current_user),
    ) -> dict:
        if not current_user or not bool(current_user.get("is_active")):
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not _education_token_allowed(current_user, write=False):
            raise HTTPException(
                status_code=403,
                detail="This authentication method is not authorized for Education workflows",
            )
        return current_user

    return dependency


def is_superadmin(principal: dict) -> bool:
    return principal.get("role") == "admin" and principal.get("group_id") is None


def require_tenant_admin(principal: dict, requested_org_id: int | None = None) -> int:
    if is_superadmin(principal):
        if requested_org_id is None:
            raise HTTPException(
                status_code=400,
                detail="Superadmin Education administration requires org_id",
            )
        return int(requested_org_id)
    if principal.get("role") != "admin" or principal.get("group_id") is None:
        raise HTTPException(status_code=403, detail="Tenant admin access required")
    org_id = int(principal["group_id"])
    if requested_org_id is not None and int(requested_org_id) != org_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org_id


def capabilities_for(db: Session, principal: dict) -> dict:
    enabled = education_license_enabled()
    if not enabled or not bool(principal.get("is_active")):
        return {
            "education_enabled": False,
            "student": False,
            "manager": False,
            "tenant_admin": False,
            "student_cost_center_ids": [],
            "managed_cost_center_ids": [],
        }

    org_id = principal.get("group_id")
    tenant_admin = principal.get("role") == "admin" and org_id is not None
    if org_id is None:
        return {
            "education_enabled": False,
            "student": False,
            "manager": False,
            "tenant_admin": False,
            "student_cost_center_ids": [],
            "managed_cost_center_ids": [],
        }

    rows = db.execute(
        text(
            "SELECT g.cost_center_id, g.role FROM education_cost_center_grants g "
            "JOIN education_cost_centers c ON c.id=g.cost_center_id AND c.org_id=g.org_id "
            "WHERE g.org_id=:org_id AND g.user_id=:user_id "
            "AND g.state='active' AND c.state='active' "
            "ORDER BY g.cost_center_id, g.role"
        ),
        {"org_id": org_id, "user_id": principal["id"]},
    ).fetchall()
    student_ids = sorted({int(row.cost_center_id) for row in rows if row.role == "student"})
    manager_ids = sorted({int(row.cost_center_id) for row in rows if row.role == "manager"})
    return {
        "education_enabled": True,
        "student": bool(student_ids),
        "manager": bool(manager_ids),
        "tenant_admin": tenant_admin,
        "student_cost_center_ids": student_ids,
        "managed_cost_center_ids": manager_ids,
    }


def capability_snapshot_id(db: Session, principal: dict) -> str:
    """Hash the live identity and Education capabilities embedded in a WS token."""
    payload = {
        "user_id": int(principal["id"]),
        "role": principal.get("role"),
        "group_id": principal.get("group_id"),
        "capabilities": capabilities_for(db, principal),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def encode_cursor(payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        auth_module.SECRET_KEY.encode("utf-8"), body, hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(body + signature).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) <= hashlib.sha256().digest_size:
            raise ValueError("short cursor")
        body = raw[: -hashlib.sha256().digest_size]
        signature = raw[-hashlib.sha256().digest_size :]
        expected = hmac.new(
            auth_module.SECRET_KEY.encode("utf-8"), body, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("bad signature")
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("bad payload")
        return payload
    except Exception as exc:
        raise OdinError(
            ErrorCode.invalid_cursor, "Invalid Education cursor", status=400
        ) from exc


def validate_cursor(
    cursor: str | None,
    *,
    kind: str,
    center_id: int,
    state: str,
    limit: int,
    revision: int,
) -> list | None:
    if not cursor:
        return None
    payload = decode_cursor(cursor)
    expected = {
        "kind": kind,
        "center_id": center_id,
        "state": state,
        "limit": limit,
        "order_version": CURSOR_ORDER_VERSION,
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise OdinError(
            ErrorCode.invalid_cursor,
            "Education cursor does not match request",
            status=400,
        )
    if payload.get("center_revision") != revision:
        raise OdinError(
            ErrorCode.revision_conflict,
            "reload_required",
            status=409,
            extra={"reason": "stale_revision", "fields": ["revision"]},
        )
    last = payload.get("last")
    if not isinstance(last, list):
        raise OdinError(ErrorCode.invalid_cursor, "Invalid Education cursor", status=400)
    return last


def page_rows(rows: list, *, last: list | None, limit: int, key) -> tuple[list, list | None]:
    filtered = [row for row in rows if last is None or list(key(row)) > last]
    page = filtered[:limit]
    next_last = list(key(page[-1])) if len(filtered) > limit and page else None
    return page, next_last
