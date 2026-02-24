"""O.D.I.N. — Slicer & Printer Profile Routes

Central profile library for Klipper, OrcaSlicer, Bambu Studio, and PrusaSlicer.
Klipper profiles can be applied live; slicer profiles are stored and exported.
"""

import configparser
import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_user, require_role, log_audit

log = logging.getLogger("odin.api")
router = APIRouter()

VALID_SLICERS = {"klipper", "orca", "bambu_studio", "prusa", "generic"}
VALID_CATEGORIES_SLICER = {"printer", "filament", "process"}
VALID_CATEGORIES_KLIPPER = {"temperature", "speed", "macro_set", "custom"}


def _profile_row_to_dict(row) -> dict:
    return {
        "id": row.id, "created_by": row.created_by,
        "printer_id": row.printer_id, "org_id": row.org_id,
        "name": row.name, "description": row.description,
        "slicer": row.slicer, "category": row.category,
        "file_format": row.file_format, "filament_type": row.filament_type,
        "is_shared": row.is_shared, "is_default": row.is_default,
        "tags": row.tags,
        "last_applied_at": row.last_applied_at.isoformat() if row.last_applied_at else None,
        "last_applied_printer_id": row.last_applied_printer_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/profiles", tags=["Profiles"])
def list_profiles(
    slicer: Optional[str] = None,
    category: Optional[str] = None,
    printer_id: Optional[int] = None,
    filament_type: Optional[str] = None,
    search: Optional[str] = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """List profiles with optional filters."""
    if not current_user:
        raise HTTPException(status_code=401)
    clauses = ["1=1"]
    params = {}
    if slicer:
        clauses.append("slicer = :slicer")
        params["slicer"] = slicer
    if category:
        clauses.append("category = :category")
        params["category"] = category
    if printer_id:
        clauses.append("printer_id = :printer_id")
        params["printer_id"] = printer_id
    if filament_type:
        clauses.append("filament_type = :filament_type")
        params["filament_type"] = filament_type
    if search:
        clauses.append("(name LIKE :search OR tags LIKE :search)")
        params["search"] = f"%{search}%"

    where = " AND ".join(clauses)
    total = db.execute(text(f"SELECT COUNT(*) FROM printer_profiles WHERE {where}"), params).scalar()
    offset = (page - 1) * per_page
    rows = db.execute(
        text(f"SELECT * FROM printer_profiles WHERE {where} ORDER BY updated_at DESC LIMIT :limit OFFSET :offset"),
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
    current_user: dict = Depends(require_role("operator")),
):
    """Create a profile from JSON body."""
    body = await request.json()
    name = body.get("name")
    slicer = body.get("slicer")
    category = body.get("category")
    raw_content = body.get("raw_content", "")
    if not name or not slicer or not category:
        raise HTTPException(status_code=400, detail="name, slicer, and category are required")
    if slicer not in VALID_SLICERS:
        raise HTTPException(status_code=400, detail=f"slicer must be one of {VALID_SLICERS}")

    file_format = "ini" if slicer == "prusa" else "json"
    db.execute(text("""
        INSERT INTO printer_profiles
            (created_by, printer_id, org_id, name, description, slicer, category,
             file_format, filament_type, raw_content, is_shared, is_default, tags)
        VALUES (:created_by, :printer_id, :org_id, :name, :description, :slicer, :category,
                :file_format, :filament_type, :raw_content, :is_shared, :is_default, :tags)
    """), {
        "created_by": current_user.get("id"),
        "printer_id": body.get("printer_id"),
        "org_id": body.get("org_id"),
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
    })
    db.commit()
    pid = db.execute(text("SELECT last_insert_rowid()")).scalar()
    log_audit(db, "create", "profile", pid, {"name": name, "slicer": slicer})
    return {"id": pid, "name": name}


def _parse_orca_json(content: str) -> dict:
    """Parse OrcaSlicer / Bambu Studio JSON profile."""
    data = json.loads(content)
    name = data.get("name", "Imported Profile")
    ptype = data.get("type", "").lower()
    category = {"machine": "printer", "filament": "filament", "process": "process"}.get(ptype, "process")
    filament_type = data.get("filament_type", [None])[0] if isinstance(data.get("filament_type"), list) else data.get("filament_type")
    slicer = "bambu_studio" if data.get("from") == "system" else "orca"
    return {"name": name, "category": category, "filament_type": filament_type, "slicer": slicer, "file_format": "json"}


def _parse_prusa_ini(content: str) -> dict:
    """Parse PrusaSlicer INI profile."""
    cp = configparser.ConfigParser()
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
    current_user: dict = Depends(require_role("operator")),
):
    """Import profile from file upload (.json, .ini, .3mf)."""
    MAX_SIZE = 50 * 1024 * 1024  # 50 MB
    content_bytes = await file.read(MAX_SIZE + 1)
    if len(content_bytes) > MAX_SIZE:
        raise HTTPException(status_code=413, detail="File exceeds 50 MB limit")

    filename = file.filename or ""
    profiles_created = []

    if filename.endswith(".3mf"):
        # Extract embedded profiles from 3MF ZIP
        try:
            zf = zipfile.ZipFile(io.BytesIO(content_bytes))
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid .3mf file")
        for name in zf.namelist():
            if name.endswith(".ini") or name.endswith(".json"):
                inner = zf.read(name).decode("utf-8", errors="replace")
                if name.endswith(".json"):
                    meta = _parse_orca_json(inner)
                else:
                    meta = _parse_prusa_ini(inner)
                meta["raw_content"] = inner
                profiles_created.append(meta)
        if not profiles_created:
            raise HTTPException(status_code=400, detail="No profiles found in .3mf archive")

    elif filename.endswith(".json"):
        content = content_bytes.decode("utf-8")
        meta = _parse_orca_json(content)
        meta["raw_content"] = content
        profiles_created.append(meta)

    elif filename.endswith(".ini"):
        content = content_bytes.decode("utf-8")
        meta = _parse_prusa_ini(content)
        meta["raw_content"] = content
        profiles_created.append(meta)

    else:
        raise HTTPException(status_code=400, detail="Unsupported file format. Use .json, .ini, or .3mf")

    ids = []
    for p in profiles_created:
        db.execute(text("""
            INSERT INTO printer_profiles
                (created_by, name, slicer, category, file_format, filament_type, raw_content, is_shared)
            VALUES (:created_by, :name, :slicer, :category, :file_format, :filament_type, :raw_content, 1)
        """), {
            "created_by": current_user.get("id"),
            "name": p["name"],
            "slicer": p["slicer"],
            "category": p["category"],
            "file_format": p["file_format"],
            "filament_type": p.get("filament_type"),
            "raw_content": p["raw_content"],
        })
        db.commit()
        pid = db.execute(text("SELECT last_insert_rowid()")).scalar()
        ids.append(pid)
        log_audit(db, "create", "profile", pid, {"name": p["name"], "slicer": p["slicer"], "source": "import"})

    return {"imported": len(ids), "ids": ids}


@router.get("/profiles/{profile_id}", tags=["Profiles"])
def get_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Get single profile with raw_content."""
    if not current_user:
        raise HTTPException(status_code=401)
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    d = _profile_row_to_dict(row)
    d["raw_content"] = row.raw_content
    return d


@router.put("/profiles/{profile_id}", tags=["Profiles"])
async def update_profile(
    profile_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Update profile metadata."""
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    # Operator can edit own; admin can edit any
    if current_user.get("role") != "admin" and row.created_by != current_user.get("id"):
        raise HTTPException(status_code=403, detail="Can only edit your own profiles")
    body = await request.json()
    allowed = {"name", "description", "tags", "is_shared", "printer_id", "filament_type"}
    updates = {k: v for k, v in body.items() if k in allowed}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid fields")
    sets = ", ".join(f"{k} = :{k}" for k in updates)
    updates["id"] = profile_id
    db.execute(text(f"UPDATE printer_profiles SET {sets}, updated_at = datetime('now') WHERE id = :id"), updates)
    db.commit()
    log_audit(db, "update", "profile", profile_id, updates)
    return {"updated": True}


@router.delete("/profiles/{profile_id}", tags=["Profiles"], status_code=204)
def delete_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Delete a profile."""
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    if current_user.get("role") != "admin" and row.created_by != current_user.get("id"):
        raise HTTPException(status_code=403, detail="Can only delete your own profiles")
    db.execute(text("DELETE FROM printer_profiles WHERE id = :id"), {"id": profile_id})
    db.commit()
    log_audit(db, "delete", "profile", profile_id, {"name": row.name})


@router.get("/profiles/{profile_id}/export", tags=["Profiles"])
def export_profile(
    profile_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """Download profile in native format."""
    if not current_user:
        raise HTTPException(status_code=401)
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    ext = ".ini" if row.file_format == "ini" else ".json"
    safe_name = re.sub(r'[^\w\-]', '_', row.name)[:80] + ext
    media = "text/plain" if row.file_format == "ini" else "application/json"
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
    current_user: dict = Depends(require_role("operator")),
):
    """Apply a Klipper profile to a printer via Moonraker GCode API."""
    row = db.execute(text("SELECT * FROM printer_profiles WHERE id = :id"), {"id": profile_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Profile not found")
    if row.slicer != "klipper":
        raise HTTPException(
            status_code=400,
            detail="Only Klipper profiles can be applied directly. Download this profile and import it into your slicer.",
        )

    body = await request.json()
    target_printer_id = body.get("printer_id")
    if not target_printer_id:
        raise HTTPException(status_code=400, detail="printer_id is required")

    printer = db.execute(
        text("SELECT id, name, api_type, api_host FROM printers WHERE id = :id"),
        {"id": target_printer_id},
    ).fetchone()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if printer.api_type != "moonraker":
        raise HTTPException(status_code=400, detail="Profile can only be applied to Klipper/Moonraker printers")

    # Parse profile content
    try:
        profile_data = json.loads(row.raw_content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid profile JSON content")

    # Build GCode commands based on category
    from moonraker_adapter import MoonrakerPrinter
    adapter = MoonrakerPrinter(printer.api_host)

    gcodes = []
    category = row.category
    if category == "temperature":
        nozzle = profile_data.get("nozzle_temp")
        bed = profile_data.get("bed_temp")
        if nozzle is not None:
            gcodes.append(f"SET_HEATER_TEMPERATURE HEATER=extruder TARGET={int(nozzle)}")
        if bed is not None:
            gcodes.append(f"SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET={int(bed)}")
    elif category == "speed":
        velocity = profile_data.get("print_speed")
        accel = profile_data.get("accel")
        accel_to_decel = profile_data.get("accel_to_decel")
        parts = []
        if velocity:
            parts.append(f"VELOCITY={int(velocity)}")
        if accel:
            parts.append(f"ACCEL={int(accel)}")
        if accel_to_decel:
            parts.append(f"ACCEL_TO_DECEL={int(accel_to_decel)}")
        if parts:
            gcodes.append("SET_VELOCITY_LIMIT " + " ".join(parts))
    elif category == "macro_set":
        macros = profile_data.get("macros", [])
        for macro in macros:
            if isinstance(macro, str):
                gcodes.append(macro)
            elif isinstance(macro, dict):
                gcodes.append(macro.get("gcode", ""))
    elif category == "custom":
        custom = profile_data.get("gcode", "")
        if custom:
            gcodes.extend(custom.strip().split("\n"))

    if not gcodes:
        raise HTTPException(status_code=400, detail="Profile has no commands to apply")

    # Send each GCode
    failures = []
    for gcode in gcodes:
        gcode = gcode.strip()
        if not gcode:
            continue
        success = adapter.send_gcode(gcode)
        if not success:
            failures.append(gcode)

    # Update last_applied
    db.execute(text("""
        UPDATE printer_profiles
        SET last_applied_at = datetime('now'), last_applied_printer_id = :pid
        WHERE id = :id
    """), {"pid": target_printer_id, "id": profile_id})
    db.commit()
    log_audit(db, "apply", "profile", profile_id, {
        "printer_id": target_printer_id, "printer_name": printer.name,
        "commands_sent": len(gcodes), "failures": len(failures),
    })

    if failures:
        return {"applied": True, "warnings": f"{len(failures)} command(s) failed", "failed_commands": failures}
    return {"applied": True, "commands_sent": len(gcodes)}
