"""Authorization, namespace serialization and owned payload paths for generic uploads."""
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import uuid
import json
from typing import Optional
from fastapi import HTTPException
from sqlalchemy import text
from core.api_token_policy import parse_token_scopes, token_scope_granted
from core.model_preview_access import accessible_preview_file
from core.rbac import require_role


def _lock_upload_workflow(db):
    # Serialize only this workflow's automatic name matching, not all model writers.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        db.execute(text("SELECT pg_advisory_xact_lock(1330071886, 1)"))
    else:
        db.execute(text("UPDATE groups SET id=id WHERE id=-1"))


async def _fresh_upload_actor(db, principal):
    _lock_upload_workflow(db)
    row = db.execute(text("SELECT id,role,group_id,is_active FROM users WHERE id=:id"),
                     {"id": principal["id"]}).fetchone()
    if (not row or not row.is_active or row.role != principal.get("role")
            or row.group_id != principal.get("group_id")):
        raise HTTPException(status_code=403, detail="Account changed; authenticate again before uploading")
    fresh = dict(principal)
    if principal.get("_auth_kind") == "user_api_token":
        token = db.execute(text("SELECT user_id,scopes,expires_at FROM api_tokens WHERE id=:id"),
                           {"id": principal.get("_token_id")}).fetchone()
        if not token or token.user_id != row.id:
            raise HTTPException(status_code=403, detail="Upload authorization is no longer available")
        if token.expires_at:
            try:
                expiry = datetime.fromisoformat(token.expires_at) if isinstance(token.expires_at, str) else token.expires_at
                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)
                expired = expiry <= datetime.now(timezone.utc)
            except (TypeError, ValueError):
                expired = True
            if expired:
                raise HTTPException(status_code=403, detail="Upload authorization is no longer available")
        fresh["_token_scopes"] = parse_token_scopes(token.scopes) or []
        if not token_scope_granted(fresh["_token_scopes"], "write:models"):
            raise HTTPException(status_code=403, detail="Upload authorization is no longer available")
    # Re-check the upload.upload permission against the fresh account row (the
    # role matrix is read again, so a change since the request began applies).
    return await _recheck_upload_permission(db, fresh)


async def _recheck_upload_permission(db, fresh):
    from core.rbac import _check_token_scope, _is_platform_superadmin
    from core.rbac_policy import action_roles
    if not _is_platform_superadmin(fresh) and fresh.get("role") not in action_roles(db, "upload.upload"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    _check_token_scope(fresh, "write:models")
    return fresh


def _visible_upload_duplicate(db, file_hash, principal):
    candidates = db.execute(text("SELECT id FROM print_files WHERE file_hash=:hash ORDER BY id"),
                            {"hash": file_hash}).fetchall()
    for candidate in candidates:
        try:
            row = accessible_preview_file(db, candidate.id, principal)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            continue
        return {"duplicate": True, "existing_file_id": row.id, "existing_file_name": row.filename}
    return None


def _own_upload_model(db, normalized_name, raw_name, org_id):
    rows = db.execute(text(
        "SELECT m.id FROM models m WHERE (m.name=:name OR m.name=:raw) "
        "AND (m.org_id=:org OR (m.org_id IS NULL AND :org IS NULL)) "
        "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.model_id=m.id) "
        "AND NOT EXISTS (SELECT 1 FROM print_files p WHERE (p.model_id=m.id OR p.id=m.print_file_id) "
        "AND (EXISTS (SELECT 1 FROM education_submissions s WHERE s.print_file_id=p.id) "
        "OR EXISTS (SELECT 1 FROM education_upload_operations o WHERE o.final_path=p.stored_path AND o.final_path IS NOT NULL))) "
        "ORDER BY m.id LIMIT 2"
    ), {"name": normalized_name, "raw": raw_name, "org": org_id}).fetchall()
    from core.print_file_binding import job_file_id, target_printer
    rows = [row for row in rows if target_printer(db, job_file_id(db, row[0])) is None]
    # An ambiguous namespace requires a new model, not an arbitrary attachment.
    return rows[0] if len(rows) == 1 else None


def _new_upload_path(file_id, filename):
    root = Path(os.environ.get("PRINT_FILE_STORAGE_ROOT") or "/data/print_files")
    if not root.is_absolute():
        raise HTTPException(status_code=400, detail="Print file storage root must be an absolute path")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', filename or "file")
    path = root / f"{file_id}_{uuid.uuid4().hex}_{safe_name}"
    return path



def _normalize_model_name(name: str) -> str:
    """Strip printer model suffixes for variant matching."""
    # Patterns: " (X1C)", " (H2D)", "_P1S", " - A1", etc.
    patterns = [
        r'\s*[\(\[\-_]\s*(X1C?|X1E|H2D|P1[SP]|A1(\s*Mini)?|Kobra\s*S1)\s*[\)\]]?\s*$',
    ]
    result = name
    for p in patterns:
        result = re.sub(p, '', result, flags=re.IGNORECASE)
    return result.strip()




def _studio_print_notes(note: Optional[str], profile_metadata: Optional[dict]) -> Optional[str]:
    pieces = []
    if note:
        pieces.append(note)
    if profile_metadata:
        pieces.append("ODIN Studio profile_metadata: " + json.dumps(profile_metadata, sort_keys=True))
    return "\n\n".join(pieces) if pieces else None

