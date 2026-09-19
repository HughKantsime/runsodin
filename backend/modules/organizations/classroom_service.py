"""Bounded Google Classroom OAuth and read-only API client."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.crypto import decrypt, encrypt
from core.itar import pin_for_request, should_trust_env


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
CLASSROOM_API = "https://classroom.googleapis.com/v1"
CLASSROOM_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.profile.emails",
)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 20
MAX_MEMBERS = 10_000
_refresh_locks: dict[int, asyncio.Lock] = {}


class ClassroomError(RuntimeError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _json_request(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    data: dict | None = None,
) -> dict:
    with pin_for_request(url):
        async with httpx.AsyncClient(trust_env=should_trust_env()) as client:
            response = await client.request(
                method,
                url,
                headers=headers,
                data=data,
                timeout=15,
                follow_redirects=False,
            )
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ClassroomError("google_response_too_large", "Google response exceeded the allowed size", 502)
    if response.status_code < 200 or response.status_code >= 300:
        raise ClassroomError("google_request_failed", "Google authorization or Classroom request failed", 502)
    try:
        value = response.json()
    except Exception as exc:
        raise ClassroomError("google_response_invalid", "Google returned an invalid response", 502) from exc
    if not isinstance(value, dict):
        raise ClassroomError("google_response_invalid", "Google returned an invalid response", 502)
    return value


def connection_status(row) -> dict:
    if not row:
        return {
            "configured": False,
            "state": "not_connected",
            "connected": False,
            "client_id": "",
            "account_email": None,
            "allowed_domains": "",
            "last_success_at": None,
            "last_error_code": None,
        }
    mapping = row._mapping
    return {
        "configured": bool(mapping.get("client_id") and mapping.get("client_secret_encrypted")),
        "state": mapping.get("state") or "not_connected",
        "connected": mapping.get("state") == "connected" and bool(mapping.get("refresh_token_encrypted")),
        "client_id": mapping.get("client_id") or "",
        "account_email": mapping.get("account_email"),
        "allowed_domains": mapping.get("allowed_domains") or "",
        "last_success_at": mapping.get("last_success_at"),
        "last_error_code": mapping.get("last_error_code"),
    }


def configure_connection(
    db: Session,
    *,
    org_id: int,
    admin_id: int,
    client_id: str,
    client_secret: str | None,
    allowed_domains: str,
) -> dict:
    existing = db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"),
        {"org_id": org_id},
    ).fetchone()
    encrypted_secret = encrypt(client_secret) if client_secret else (
        existing.client_secret_encrypted if existing else None
    )
    if not encrypted_secret:
        raise ClassroomError("classroom_secret_required", "Google OAuth client secret is required", 422)
    if existing and existing.client_id != client_id:
        db.execute(
            text(
                "UPDATE classroom_connections SET account_subject=NULL, account_email=NULL, "
                "granted_scopes=NULL, refresh_token_encrypted=NULL, access_token_encrypted=NULL, "
                "access_token_expires_at=NULL, state='not_connected' WHERE org_id=:org_id"
            ),
            {"org_id": org_id},
        )
    db.execute(
        text(
            "INSERT INTO classroom_connections "
            "(org_id, client_id, client_secret_encrypted, allowed_domains, state, created_by) "
            "VALUES (:org_id, :client_id, :secret, :domains, 'not_connected', :admin_id) "
            "ON CONFLICT (org_id) DO UPDATE SET client_id=:client_id, "
            "client_secret_encrypted=:secret, allowed_domains=:domains, "
            "state=CASE WHEN classroom_connections.client_id=:client_id "
            "THEN classroom_connections.state ELSE 'not_connected' END, "
            "updated_at=CURRENT_TIMESTAMP"
        ),
        {
            "org_id": org_id,
            "client_id": client_id,
            "secret": encrypted_secret,
            "domains": allowed_domains,
            "admin_id": admin_id,
        },
    )
    db.commit()
    return connection_status(db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"), {"org_id": org_id}
    ).one())


def create_connect_url(
    db: Session,
    *,
    org_id: int,
    admin_id: int,
    redirect_uri: str,
) -> str:
    connection = db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"), {"org_id": org_id}
    ).fetchone()
    if not connection or not connection.client_id or not connection.client_secret_encrypted:
        raise ClassroomError("classroom_not_configured", "Configure Google Classroom OAuth first", 409)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    expires = (_utcnow() + timedelta(minutes=10)).isoformat()
    db.execute(
        text(
            "INSERT INTO classroom_oauth_states "
            "(state, org_id, admin_id, code_verifier_encrypted, redirect_uri, expires_at) "
            "VALUES (:state, :org_id, :admin_id, :verifier, :redirect_uri, :expires_at)"
        ),
        {
            "state": state,
            "org_id": org_id,
            "admin_id": admin_id,
            "verifier": encrypt(verifier),
            "redirect_uri": redirect_uri,
            "expires_at": expires,
        },
    )
    db.execute(text("DELETE FROM classroom_oauth_states WHERE expires_at<:now"), {"now": _utcnow().isoformat()})
    db.commit()
    params = {
        "client_id": connection.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(CLASSROOM_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "false",
        "prompt": "consent select_account",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    domains = [part.strip() for part in (connection.allowed_domains or "").split(",") if part.strip()]
    if domains:
        params["hd"] = domains[0]
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def complete_connection(
    db: Session,
    *,
    state: str,
    code: str,
    principal: dict,
) -> dict:
    pending = db.execute(
        text("SELECT * FROM classroom_oauth_states WHERE state=:state"), {"state": state}
    ).fetchone()
    db.execute(text("DELETE FROM classroom_oauth_states WHERE state=:state"), {"state": state})
    if not pending or _parse_time(pending.expires_at) <= _utcnow():
        db.commit()
        raise ClassroomError("classroom_state_invalid", "Classroom authorization state is invalid or expired", 400)
    if int(pending.admin_id) != int(principal["id"]) or int(pending.org_id) != int(principal.get("group_id") or -1):
        db.commit()
        raise ClassroomError("classroom_state_owner_mismatch", "Classroom authorization belongs to another administrator", 403)
    connection = db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"), {"org_id": pending.org_id}
    ).one()
    # State is one-time even if Google rejects the code or the response fails validation.
    db.commit()
    token = await _json_request(
        "POST",
        GOOGLE_TOKEN_URL,
        data={
            "client_id": connection.client_id,
            "client_secret": decrypt(connection.client_secret_encrypted),
            "code": code,
            "code_verifier": decrypt(pending.code_verifier_encrypted),
            "redirect_uri": pending.redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    access_token = token.get("access_token")
    refresh_token = token.get("refresh_token")
    granted = set(str(token.get("scope") or "").split())
    required = set(CLASSROOM_SCOPES)
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        raise ClassroomError("classroom_offline_access_missing", "Google did not grant offline Classroom access", 409)
    if not required.issubset(granted):
        raise ClassroomError("classroom_scopes_missing", "Google did not grant every required read-only scope", 409)
    profile = await _json_request(
        "GET", GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}
    )
    subject, email = profile.get("sub"), profile.get("email")
    if not isinstance(subject, str) or not isinstance(email, str) or profile.get("email_verified") is not True:
        raise ClassroomError("classroom_account_unverified", "Google account identity could not be verified", 409)
    email = email.strip().lower()
    domains = {part.strip().lower() for part in (connection.allowed_domains or "").split(",") if part.strip()}
    if domains and email.rsplit("@", 1)[-1] not in domains:
        raise ClassroomError("classroom_domain_invalid", "Google account is outside the configured Workspace domain", 403)
    expires_at = (_utcnow() + timedelta(seconds=max(60, int(token.get("expires_in") or 3600)))).isoformat()
    db.execute(
        text(
            "UPDATE classroom_connections SET account_subject=:subject, account_email=:email, "
            "granted_scopes=:scopes, refresh_token_encrypted=:refresh, "
            "access_token_encrypted=:access, access_token_expires_at=:expires, "
            "state='connected', last_success_at=:now, last_error_code=NULL, "
            "updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id"
        ),
        {
            "subject": subject,
            "email": email,
            "scopes": " ".join(sorted(granted)),
            "refresh": encrypt(refresh_token),
            "access": encrypt(access_token),
            "expires": expires_at,
            "now": _utcnow().isoformat(),
            "org_id": pending.org_id,
        },
    )
    db.commit()
    return connection_status(db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"), {"org_id": pending.org_id}
    ).one())


async def _access_token(db: Session, org_id: int) -> str:
    lock = _refresh_locks.setdefault(org_id, asyncio.Lock())
    async with lock:
        connection = db.execute(
            text("SELECT * FROM classroom_connections WHERE org_id=:org_id"), {"org_id": org_id}
        ).fetchone()
        if not connection or connection.state != "connected" or not connection.refresh_token_encrypted:
            raise ClassroomError("classroom_reconnect_required", "Google Classroom must be connected", 409)
        expires = _parse_time(connection.access_token_expires_at)
        if connection.access_token_encrypted and expires and expires > _utcnow() + timedelta(seconds=60):
            return decrypt(connection.access_token_encrypted) or ""
        try:
            token = await _json_request(
                "POST",
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": connection.client_id,
                    "client_secret": decrypt(connection.client_secret_encrypted),
                    "refresh_token": decrypt(connection.refresh_token_encrypted),
                    "grant_type": "refresh_token",
                },
            )
            access = token.get("access_token")
            if not isinstance(access, str):
                raise ClassroomError("classroom_refresh_failed", "Google Classroom reconnect is required", 409)
            expires_at = (_utcnow() + timedelta(seconds=max(60, int(token.get("expires_in") or 3600)))).isoformat()
            db.execute(
                text(
                    "UPDATE classroom_connections SET access_token_encrypted=:access, "
                    "access_token_expires_at=:expires, last_success_at=:now, "
                    "last_error_code=NULL, updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id"
                ),
                {"access": encrypt(access), "expires": expires_at, "now": _utcnow().isoformat(), "org_id": org_id},
            )
            db.commit()
            return access
        except Exception as exc:
            db.rollback()
            db.execute(
                text("UPDATE classroom_connections SET state='reconnect_required', last_error_code='classroom_refresh_failed', updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id"),
                {"org_id": org_id},
            )
            db.commit()
            if isinstance(exc, ClassroomError):
                raise ClassroomError("classroom_refresh_failed", "Google Classroom reconnect is required", 409) from exc
            raise


async def _paged_google(db: Session, org_id: int, path: str, item_key: str, *, page_size: int) -> list[dict]:
    token = await _access_token(db, org_id)
    items: list[dict] = []
    page_token = None
    for _ in range(MAX_PAGES):
        query = {"pageSize": page_size}
        if page_token:
            query["pageToken"] = page_token
        separator = "&" if "?" in path else "?"
        payload = await _json_request(
            "GET",
            f"{CLASSROOM_API}/{path}{separator}{urlencode(query)}",
            headers={"Authorization": f"Bearer {token}"},
        )
        batch = payload.get(item_key) or []
        if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
            raise ClassroomError("classroom_response_invalid", "Google Classroom returned invalid data", 502)
        items.extend(batch)
        if len(items) > MAX_MEMBERS:
            raise ClassroomError("classroom_roster_too_large", "Classroom roster exceeds the POC safety limit", 422)
        page_token = payload.get("nextPageToken")
        if not page_token:
            return items
    raise ClassroomError("classroom_pagination_limit", "Google Classroom pagination exceeded the safety limit", 502)


async def list_courses(db: Session, org_id: int) -> list[dict]:
    courses = await _paged_google(db, org_id, "courses?courseStates=ACTIVE", "courses", page_size=100)
    return [
        {
            "id": str(course.get("id", "")),
            "name": str(course.get("name", "")),
            "section": str(course.get("section", "")),
            "description": str(course.get("descriptionHeading") or course.get("description") or ""),
            "course_state": str(course.get("courseState", "")),
        }
        for course in courses
        if course.get("id") and course.get("name")
    ]


async def get_roster(db: Session, org_id: int, course_id: str) -> dict:
    safe_course = quote(course_id, safe="")
    courses = await list_courses(db, org_id)
    course = next((item for item in courses if item["id"] == course_id), None)
    if not course:
        raise ClassroomError("classroom_course_not_found", "Google Classroom course was not found", 404)
    teachers, students = await asyncio.gather(
        _paged_google(db, org_id, f"courses/{safe_course}/teachers", "teachers", page_size=100),
        _paged_google(db, org_id, f"courses/{safe_course}/students", "students", page_size=100),
    )

    def project(items: list[dict]) -> list[dict]:
        result = []
        for item in items:
            profile = item.get("profile") or {}
            email = profile.get("emailAddress")
            user_id = profile.get("id") or item.get("userId")
            if not isinstance(email, str) or not isinstance(user_id, str):
                raise ClassroomError("classroom_member_email_missing", "A Classroom member has no readable email address", 422)
            name = profile.get("name") or {}
            result.append({
                "provider_user_id": user_id,
                "email": email.strip().lower(),
                "name": str(name.get("fullName") or email),
            })
        return result

    return {"course": course, "teachers": project(teachers), "students": project(students)}


def disconnect(db: Session, org_id: int) -> dict:
    db.execute(
        text(
            "UPDATE classroom_connections SET account_subject=NULL, account_email=NULL, "
            "granted_scopes=NULL, refresh_token_encrypted=NULL, access_token_encrypted=NULL, "
            "access_token_expires_at=NULL, state='not_connected', last_error_code=NULL, "
            "updated_at=CURRENT_TIMESTAMP WHERE org_id=:org_id"
        ),
        {"org_id": org_id},
    )
    db.execute(text("DELETE FROM classroom_oauth_states WHERE org_id=:org_id"), {"org_id": org_id})
    db.commit()
    return connection_status(db.execute(
        text("SELECT * FROM classroom_connections WHERE org_id=:org_id"), {"org_id": org_id}
    ).fetchone())
