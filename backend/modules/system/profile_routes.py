"""O.D.I.N. — Slicer & Printer Profile Routes

Central profile library for Klipper, OrcaSlicer, Bambu Studio, and PrusaSlicer.
Klipper profiles can be applied live; slicer profiles are stored and exported.
"""

from modules.system import profile_history
from core.rbac import should_filter_org

# Domain: system
# Depends on: core, printers, organizations
# Owns tables: printer_profiles

import configparser
import io
import json
import logging
import math
import os
import re
import zipfile
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import execute_insert_returning_id, sql
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, get_org_scope, check_org_access

log = logging.getLogger("odin.api")
router = APIRouter()

VALID_SLICERS = {"klipper", "orca", "bambu_studio", "prusa", "generic"}
VALID_CATEGORIES_SLICER = {"printer", "filament", "process"}
VALID_CATEGORIES_KLIPPER = {"temperature", "speed", "macro_set", "custom"}



async def _profile_request_body(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="Profile body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Profile body must be a JSON object")
    for key in ("is_shared", "is_default"):
        # INTEGER 0/1 columns on both dialects: JSON booleans are stored as 1/0 (PostgreSQL
        # refuses a boolean bound into an integer column); anything else is refused.
        if key in body:
            value = body[key]
            if value is True or value is False or (type(value) is int and value in (0, 1)):
                body[key] = int(value)
            else:
                raise HTTPException(status_code=422, detail=f"{key} must be true/false or 0/1")
    return body


def _accessible_profile_printer(db: Session, printer_id, current_user: dict):
    if isinstance(printer_id, bool) or not isinstance(printer_id, int) or printer_id <= 0:
        raise HTTPException(status_code=400, detail="printer_id must be a positive integer")
    printer = db.execute(text(
        "SELECT id, name, org_id, api_type, api_host FROM printers WHERE id = :id"
    ), {"id": printer_id}).fetchone()
    if printer is None or not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer



def _profile_command_number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise HTTPException(status_code=400, detail="Profile command values must be finite nonnegative numbers")
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("Invalid command number")
        return int(number)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail="Profile command values must be finite nonnegative numbers")


def _profile_commands(category: str, data) -> list[str]:
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Profile command content must be a JSON object")
    commands = []
    if category == "temperature":
        for key, heater in (("nozzle_temp", "extruder"), ("bed_temp", "heater_bed")):
            if data.get(key) is not None:
                commands.append(f"SET_HEATER_TEMPERATURE HEATER={heater} TARGET={_profile_command_number(data[key])}")
    elif category == "speed":
        parts = []
        for key, parameter in (("print_speed", "VELOCITY"), ("accel", "ACCEL"), ("accel_to_decel", "ACCEL_TO_DECEL")):
            if data.get(key) is not None:
                parts.append(f"{parameter}={_profile_command_number(data[key])}")
        if parts:
            commands.append("SET_VELOCITY_LIMIT " + " ".join(parts))
    elif category == "macro_set":
        macros = data.get("macros", [])
        if not isinstance(macros, list):
            raise HTTPException(status_code=400, detail="Profile macros must be a list")
        for macro in macros:
            command = macro.get("gcode") if isinstance(macro, dict) else macro
            if not isinstance(command, str):
                raise HTTPException(status_code=400, detail="Profile macro commands must be text")
            commands.append(command)
    elif category == "custom":
        content = data.get("gcode", "")
        if not isinstance(content, str):
            raise HTTPException(status_code=400, detail="Profile GCode must be text")
        commands.extend(content.splitlines())
    commands = [command.strip() for command in commands if command.strip()]
    if not commands:
        raise HTTPException(status_code=400, detail="Profile has no commands to apply")
    return commands


def _profile_row_to_dict(row) -> dict:
    return {
        "id": row.id, "created_by": row.created_by,
        "printer_id": row.printer_id, "org_id": row.org_id,
        "name": row.name, "description": row.description,
        "slicer": row.slicer, "category": row.category,
        "file_format": row.file_format, "filament_type": row.filament_type,
        "is_shared": row.is_shared, "is_default": row.is_default,
        "tags": row.tags,
        "last_applied_at": row.last_applied_at if isinstance(row.last_applied_at, str) else (row.last_applied_at.isoformat() if row.last_applied_at else None),
        "last_applied_printer_id": row.last_applied_printer_id,
        "created_at": row.created_at if isinstance(row.created_at, str) else (row.created_at.isoformat() if row.created_at else None),
        "updated_at": row.updated_at if isinstance(row.updated_at, str) else (row.updated_at.isoformat() if row.updated_at else None),
    }


@router.get("/profiles", tags=["Profiles"])
def list_profiles(
    slicer: Optional[str] = None,
    category: Optional[str] = None,
    printer_id: Optional[int] = None,
    filament_type: Optional[str] = None,
    printer_model: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:models")),
):
    """List profiles with optional filters."""
    if not current_user:
        raise HTTPException(status_code=401)
    org = get_org_scope(current_user)
    clauses = ["1=1"]
    params = {}
    if should_filter_org(current_user, org):
        clauses.append("(org_id = :org OR org_id IS NULL)")
        params["org"] = org
    if slicer:
        clauses.append("slicer = :slicer")
        params["slicer"] = slicer
    if category:
        clauses.append("category = :category")
        params["category"] = category
    if printer_id:
        clauses.append("printer_id = :printer_id")
        params["printer_id"] = printer_id
    if printer_model:
        printer_scope = " AND (org_id = :org OR org_id IS NULL)" if should_filter_org(current_user, org) else ""
        clauses.append("printer_id IN (SELECT id FROM printers WHERE model = :printer_model" + printer_scope + ")")
        params["printer_model"] = printer_model
    if filament_type:
        clauses.append("filament_type = :filament_type")
        params["filament_type"] = filament_type
    if search:
        clauses.append("(name LIKE :search OR tags LIKE :search)")
        params["search"] = f"%{search}%"

    where = " AND ".join(clauses)
    total = db.execute(text(f"SELECT COUNT(*) FROM printer_profiles WHERE {where}"), params).scalar()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    offset = (page - 1) * per_page
    rows = db.execute(
        text(f"SELECT * FROM printer_profiles WHERE {where} ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        {**params, "limit": per_page, "offset": offset},
    ).fetchall()
    return {
        "total": total, "page": page, "per_page": per_page,
        "profiles": [_profile_row_to_dict(r) for r in rows],
    }


@router.post("/profiles", tags=["Profiles"], status_code=201)
async def create_profile(
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator", scope="write:models")),
):
    """Create a profile from JSON body."""
    body = await _profile_request_body(request)
    name = body.get("name")
    slicer = body.get("slicer")
    category = body.get("category")
    raw_content = body.get("raw_content", "")
    if not name or not slicer or not category:
        raise HTTPException(status_code=400, detail="name, slicer, and category are required")
    if not isinstance(slicer, str) or slicer not in VALID_SLICERS:
        raise HTTPException(status_code=400, detail=f"slicer must be one of {VALID_SLICERS}")

    valid_categories = VALID_CATEGORIES_KLIPPER if slicer == "klipper" else VALID_CATEGORIES_SLICER
    if not isinstance(category, str) or category not in valid_categories:
        raise HTTPException(status_code=400, detail="Invalid profile category")
    if not isinstance(name, str) or not name.strip() or not isinstance(raw_content, str):
        raise HTTPException(status_code=400, detail="Profile name and raw_content must be text")
    if body.get("printer_id") is not None:
        _accessible_profile_printer(db, body["printer_id"], current_user)
    file_format = "ini" if slicer == "prusa" else "json"
    insert_sql = """
        INSERT INTO printer_profiles
            (created_by, printer_id, org_id, name, description, slicer, category,
             file_format, filament_type, raw_content, is_shared, is_default, tags)
        VALUES (:created_by, :printer_id, :org_id, :name, :description, :slicer, :category,
                :file_format, :filament_type, :raw_content, :is_shared, :is_default, :tags)
    """
    params = {
        "created_by": current_user.get("id"),
        "printer_id": body.get("printer_id"),
        "org_id": current_user.get("group_id"),
        "name": name,
        "description": body.get("description"),
        "slicer": slicer,
        "category": category,
        "file_format": file_format,
        "filament_type": body.get("filament_type"),
        "raw_content": raw_content,
        "is_shared": body.get("is_shared", 1),
        "is_default": body.get("is_default", 0),
        "tags": body.get("tags"),
    }
    pid = execute_insert_returning_id(db, insert_sql, params)
    db.flush()
    log_audit(db, "create", "profile", pid, {"name": name, "slicer": slicer})
    db.commit()
    return {"id": pid, "name": name}


def _parse_orca_json(content: str) -> dict:
    """Parse OrcaSlicer / Bambu Studio JSON profile."""
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("Profile JSON must be an object")
    name = data.get("name", "Imported Profile")
    ptype = data.get("type", "").lower()
    category = {"machine": "printer", "filament": "filament", "process": "process"}.get(ptype, "process")
    filament_type = data.get("filament_type")
    if isinstance(filament_type, list):
        filament_type = filament_type[0] if filament_type else None
    slicer = "bambu_studio" if data.get("from") == "system" else "orca"
    return {"name": name, "category": category, "filament_type": filament_type, "slicer": slicer, "file_format": "json"}


def _parse_prusa_ini(content: str) -> dict:
    """Parse PrusaSlicer INI profile."""
    cp = configparser.ConfigParser(interpolation=None)
    meaningful = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith(("#", ";"))]
    if meaningful and not meaningful[0].startswith("["):
        content = "[profile]\n" + content
    cp.read_string(content)
    # PrusaSlicer INIs don't always have sections; check first non-comment line
    name = "Imported Profile"
    category = "process"
    filament_type = None
    for section in cp.sections():
        sl = section.lower()
        if "printer" in sl:
            category = "printer"
        elif "filament" in sl:
            category = "filament"
        elif "print" in sl:
            category = "process"
    # Try to get name from the config
    for section in cp.sections():
        if cp.has_option(section, "name"):
            name = cp.get(section, "name")
            break
    # Check filament type
    for section in cp.sections():
        if cp.has_option(section, "filament_type"):
            filament_type = cp.get(section, "filament_type")
            break
    return {"name": name, "category": category, "filament_type": filament_type, "slicer": "prusa", "file_format": "ini"}


@router.post("/profiles/import", tags=["Profiles"], status_code=201)
async def import_profile(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator", scope="write:models")),
):
    """Import profile from file upload (.json, .ini, .cfg, .3mf)."""
    MAX_SIZE = 50 * 1024 * 1024  # 50 MB
    content_bytes = await file.read(MAX_SIZE + 1)
    if len(content_bytes) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit")

    filename = file.filename or ""
    profiles_created = []

    try:
        if filename.lower().endswith(".3mf"):
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as archive:
                members = archive.infolist()
                if len(members) > 128 or sum(member.file_size for member in members) > MAX_SIZE:
                    raise HTTPException(status_code=413, detail="Profile archive exceeds extraction limits")
                for member in members:
                    extension = os.path.splitext(member.filename)[1].lower()
                    if extension not in {".json", ".ini"} or member.is_dir():
                        continue
                    inner = archive.read(member).decode("utf-8")
                    meta = _parse_orca_json(inner) if extension == ".json" else _parse_prusa_ini(inner)
                    meta["raw_content"] = inner
                    profiles_created.append(meta)
            if not profiles_created:
                raise HTTPException(status_code=400, detail="No profiles found in .3mf archive")
        elif filename.lower().endswith(".cfg"):
            content = content_bytes.decode("utf-8")
            if "\x00" in content:
                raise ValueError("Invalid configuration text")
            parser = configparser.RawConfigParser(interpolation=None)
            parser.read_string(content)
            if not parser.sections():
                raise ValueError("Configuration requires a section")
            profiles_created.append({"name": os.path.splitext(os.path.basename(filename))[0],
                                     "slicer": "klipper", "category": "printer",
                                     "file_format": "cfg", "raw_content": content})
        elif filename.lower().endswith((".json", ".ini")):
            content = content_bytes.decode("utf-8")
            meta = _parse_orca_json(content) if filename.lower().endswith(".json") else _parse_prusa_ini(content)
            meta["raw_content"] = content
            profiles_created.append(meta)
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format. Use .json, .ini, .cfg, or .3mf")
    except (ValueError, TypeError, IndexError, AttributeError, UnicodeError, configparser.Error, zipfile.BadZipFile, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid profile file content")
    for profile in profiles_created:
        if not isinstance(profile["name"], str) or not profile["name"].strip():
            raise HTTPException(status_code=400, detail="Profile name must be text")
        if profile.get("filament_type") is not None and not isinstance(profile["filament_type"], str):
            raise HTTPException(status_code=400, detail="Profile filament_type must be text")

    ids = []
    try:
        for p in profiles_created:
            import_insert_sql = """
                INSERT INTO printer_profiles
                    (created_by, org_id, name, slicer, category, file_format, filament_type, raw_content, is_shared)
                VALUES (:created_by, :org_id, :name, :slicer, :category, :file_format, :filament_type, :raw_content, 1)
            """
            import_params = {
                "created_by": current_user.get("id"),
                "org_id": current_user.get("group_id"),
                "name": p["name"],
                "slicer": p["slicer"],
                "category": p["category"],
                "file_format": p["file_format"],
                "filament_type": p.get("filament_type"),
                "raw_content": p["raw_content"],
            }
            pid = execute_insert_returning_id(db, import_insert_sql, import_params)
            db.flush()
            ids.append(pid)
            log_audit(db, "create", "profile", pid, {"name": p["name"], "slicer": p["slicer"], "source": "import"})
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"imported": len(ids), "ids": ids}


@router.get("/profiles/{profile_id}", tags=["Profiles"])
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:models")),
):
    """Get single profile with raw_content."""
    if not current_user:
        raise HTTPException(status_code=401)
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not check_org_access(current_user, row.org_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    d = _profile_row_to_dict(row)
    d["raw_content"] = row.raw_content
    return d


@router.put("/profiles/{profile_id}", tags=["Profiles"])
async def update_profile(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator", scope="write:models")),
):
    """Update profile metadata."""
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not check_org_access(current_user, row.org_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    # Operator can edit own; admin can edit any
    if current_user.get("role") != "admin" and row.created_by != current_user.get("id"):
        raise HTTPException(status_code=403, detail="Can only edit your own profiles")
    body = await _profile_request_body(request)
    allowed = {"name", "description", "tags", "is_shared", "printer_id", "filament_type"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if "raw_content" in body:
        if updates.get("printer_id") is not None:
            _accessible_profile_printer(db, updates["printer_id"], current_user)
        result = profile_history.write_revision(db, profile_id, current_user,
            content=body["raw_content"], changelog=body.get("changelog"), updates=updates)
        return {"updated": True, **result}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields")
    sets = ", ".join(f"{k} = :{k}" for k in updates)
    if updates.get("printer_id") is not None:
        _accessible_profile_printer(db, updates["printer_id"], current_user)
    updates["id"] = profile_id
    db.execute(text(f"UPDATE printer_profiles SET {sets}, updated_at = {sql.now()} WHERE id = :id"), updates)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    log_audit(db, "update", "profile", profile_id, updates)
    db.commit()
    return {"updated": True}


@router.delete("/profiles/{profile_id}", tags=["Profiles"], status_code=204)
def delete_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator", scope="write:models")),
):
    """Delete a profile."""
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not check_org_access(current_user, row.org_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    if current_user.get("role") != "admin" and row.created_by != current_user.get("id"):
        raise HTTPException(status_code=403, detail="Can only delete your own profiles")
    db.execute(text("DELETE FROM printer_profiles WHERE id = :id"), {"id": profile_id})
    log_audit(db, "delete", "profile", profile_id, {"name": row.name})
    db.commit()


@router.get("/profiles/{profile_id}/export", tags=["Profiles"])
def export_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("viewer", scope="read:models")),
):
    """Download profile in native format."""
    if not current_user:
        raise HTTPException(status_code=401)
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not check_org_access(current_user, row.org_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    ext = "." + row.file_format if row.file_format in {"ini", "cfg"} else ".json"
    safe_name = re.sub(r'[^\w\-]', '_', row.name)[:80] + ext
    media = "text/plain" if row.file_format in {"ini", "cfg"} else "application/json"
    return Response(
        content=row.raw_content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@router.post("/profiles/{profile_id}/apply", tags=["Profiles"])
async def apply_profile(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator", scope="write:printers")),
    _profile_reader: dict = Depends(require_role("viewer", scope="read:models")),
):
    """Apply a Klipper profile to a printer via Moonraker GCode API."""
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not check_org_access(current_user, row.org_id):
        raise HTTPException(status_code=404, detail="Profile not found")
    if row.file_format == "cfg":
        raise HTTPException(400, "Configuration profiles support import and export only")
    if row.slicer != "klipper":
        raise HTTPException(
            status_code=400,
            detail="Only Klipper profiles can be applied directly. Download this profile and import it into your slicer.",
        )

    body = await _profile_request_body(request)
    target_printer_id = body.get("printer_id")
    if not target_printer_id:
        raise HTTPException(status_code=400, detail="printer_id is required")

    printer = _accessible_profile_printer(db, target_printer_id, current_user)
    if printer.api_type != "moonraker":
        raise HTTPException(status_code=400, detail="Profile can only be applied to Klipper/Moonraker printers")

    # Parse profile content
    try:
        profile_data = json.loads(row.raw_content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid profile JSON content")

    gcodes = _profile_commands(row.category, profile_data)
    from core.auth import SECRET_KEY
    from core.dispatch_identity import build_identity
    from core.dispatch_target_lock import acquire_target_lock
    from core.dispatch_target_guard import unresolved_target
    from modules.printers.dispatch import _get_printer_info
    key=SECRET_KEY.encode() if isinstance(SECRET_KEY,str) else None
    initial=build_identity(_get_printer_info(target_printer_id,connection=db),key)
    if initial is None:
        raise HTTPException(409,'Profile target configuration could not be verified')
    # Release read-only request transaction before holding a target over I/O.
    engine=db.get_bind();db.rollback()
    acquired=acquire_target_lock(engine,initial.target_key)
    if acquired.status!='acquired':
        raise HTTPException(409,'Profile target is busy or uncertain; reconcile before retrying')
    target_handle=acquired.handle
    try:
        current_row=db.execute(text('SELECT * FROM printer_profiles WHERE id=:id'),{'id':profile_id}).fetchone()
        current_printer=_accessible_profile_printer(db,target_printer_id,current_user)
        current_identity=build_identity(_get_printer_info(target_printer_id,connection=db),key)
        if not current_row or not check_org_access(current_user,current_row.org_id):
            raise HTTPException(404,'Profile not found')
        if (current_identity!=initial or current_printer.api_type!='moonraker'
                or current_row.slicer!=row.slicer or current_row.file_format!=row.file_format
                or current_row.category!=row.category or current_row.raw_content!=row.raw_content):
            raise HTTPException(409,'Profile authority or configuration changed; no command was sent')
        db.rollback()
        if not target_handle.held() or unresolved_target(engine,target_printer_id,initial.target_key,include_claims=True):
            raise HTTPException(409,'Unresolved dispatch or sender blocks profile commands; reconcile before retrying')
        return _apply_profile_commands(db,profile_id,target_printer_id,current_row,current_printer,gcodes,target_handle,current_user,key,initial)
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise HTTPException(409,'Profile outcome requires reconciliation before retrying') from None
    finally:
        if not target_handle.close().released:
            db.rollback()
            raise HTTPException(409,'Profile target cleanup requires reconciliation before retrying')


def _profile_snapshot_current(db,profile_id,printer_id,row,current_user,key,expected_identity,*,recording=False):
    from core.dispatch_identity import build_identity
    from modules.printers.dispatch import _get_printer_info
    try:
        dialect=db.get_bind().dialect.name
        # Only the final recording phase takes short authority locks, after
        # every command has returned. Never hold these over device I/O.
        if recording and dialect=='sqlite':
            db.execute(text('BEGIN IMMEDIATE'))
        suffix=' FOR UPDATE NOWAIT' if recording and dialect=='postgresql' else ''
        fresh=db.execute(text('SELECT * FROM printer_profiles WHERE id=:id'+suffix),{'id':profile_id}).fetchone()
        if recording:
            db.execute(text('SELECT id FROM printers WHERE id=:id'+suffix),{'id':printer_id}).first()
        printer=_accessible_profile_printer(db,printer_id,current_user)
        identity=build_identity(_get_printer_info(printer_id,connection=db),key)
        if (not fresh or not check_org_access(current_user,fresh.org_id)
                or identity!=expected_identity
                or any(fresh._mapping[name]!=row._mapping[name] for name in ('org_id','raw_content','category','file_format','slicer','created_by','created_at','updated_at'))):
            raise HTTPException(409,'Profile authority or configuration changed; command outcome requires reconciliation')
    except HTTPException:
        db.rollback()
        raise HTTPException(409,'Profile authority or configuration changed; command outcome requires reconciliation') from None
    except Exception:
        db.rollback()
        raise HTTPException(409,'Profile authority could not be verified; command outcome requires reconciliation') from None
    if not recording:db.rollback()


def _apply_profile_commands(db,profile_id,target_printer_id,row,printer,gcodes,target_handle,current_user,key,expected_identity):
    from modules.printers.adapters.moonraker import MoonrakerPrinter
    if not target_handle.held():
        raise HTTPException(409,'Profile target ownership requires reconciliation before retrying')
    adapter = MoonrakerPrinter(printer.api_host)

    # Send each GCode
    failures = []
    for gcode in gcodes:
        _profile_snapshot_current(db,profile_id,target_printer_id,row,current_user,key,expected_identity)
        if not target_handle.held():
            raise HTTPException(409,'Profile target ownership changed; reconcile before retrying')
        try:
            success = adapter.send_gcode(gcode)
        except Exception as exc:
            log.warning("Profile command delivery unconfirmed profile=%s printer=%s error=%s", profile_id, target_printer_id, type(exc).__name__)
            success = False
        if not success:
            failures.append(gcode)

    # Only a fully successful application advances the success anchor.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    if not target_handle.held():
        raise HTTPException(409,'Profile target ownership changed; reconcile before retrying')
    _profile_snapshot_current(db,profile_id,target_printer_id,row,current_user,key,expected_identity,recording=True)
    if not failures:
        changed=db.execute(text(f"""
            UPDATE printer_profiles
            SET last_applied_at = {sql.now()}, last_applied_printer_id = :pid
            WHERE id = :id
              AND (org_id=:org OR (org_id IS NULL AND :org IS NULL))
              AND raw_content=:content AND category=:category
              AND file_format=:format AND slicer=:slicer
        """), {"pid": target_printer_id, "id": profile_id,"org":row.org_id,
                 "content":row.raw_content,"category":row.category,"format":row.file_format,"slicer":row.slicer})
        if changed.rowcount!=1:
            db.rollback()
            raise HTTPException(409,'Profile success recording changed; command outcome requires reconciliation')
    log_audit(db, "apply", "profile", profile_id, {
        "printer_id": target_printer_id, "printer_name": printer.name,
        "commands_sent": len(gcodes) - len(failures), "failures": len(failures),
    })
    # Keep writes pending until lock retirement is confirmed; a lost close
    # must not advance the prior fully successful application anchor.
    if not target_handle.held() or not target_handle.close().released:
        db.rollback()
        raise HTTPException(409,'Profile target cleanup requires reconciliation before retrying')
    db.commit()

    if failures:
        return {"applied": False, "outcome": "failed" if len(failures) == len(gcodes) else "partial", "commands_sent": len(gcodes) - len(failures), "commands_failed": len(failures), "warnings": f"{len(failures)} command(s) failed", "failed_commands": failures}
    return {"applied": True, "outcome": "applied", "commands_sent": len(gcodes), "commands_failed": 0}


@router.get("/profiles/{profile_id}/revisions", tags=["Profiles"])
def list_profile_revisions(profile_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:models"))):
    return profile_history.revisions(db, profile_id, current_user)


@router.get("/profiles/{profile_id}/revisions/{revision_number}", tags=["Profiles"])
def get_profile_revision(profile_id: int, revision_number: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:models"))):
    return profile_history.revision(db, profile_id, revision_number, current_user)


@router.get("/profiles/{profile_id}/diff", tags=["Profiles"])
def diff_profile_revisions(profile_id: int, from_revision: int, to_revision: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:models"))):
    return profile_history.content_diff(db, profile_id, from_revision, to_revision, current_user)


@router.post("/profiles/{profile_id}/revisions", tags=["Profiles"])
async def create_profile_revision(profile_id: int, request: Request, db: Session = Depends(get_db), current_user: dict = Depends(require_role("operator", scope="write:models"))):
    body = await _profile_request_body(request)
    return profile_history.write_revision(db, profile_id, current_user, content=body.get("raw_content"), changelog=body.get("changelog"))


@router.post("/profiles/{profile_id}/revert/{revision_number}", tags=["Profiles"])
def revert_profile_revision(profile_id: int, revision_number: int, db: Session = Depends(get_db), current_user: dict = Depends(require_role("operator", scope="write:models"))):
    return profile_history.write_revision(db, profile_id, current_user, revert_to=revision_number)
