"""O.D.I.N. — Print File Upload and Management."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, UploadFile, File, Form
from core.rate_limit import limiter
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
import json
import logging
import os
import re
import tempfile

from core.db import get_db
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import AGENT_WRITE_SCOPE, check_org_access, require_any_scope, require_role
from core.responses import build_next_actions, next_action

log = logging.getLogger("odin.api")

router = APIRouter(tags=["Print Files"])

ALLOWED_PRINT_FILE_EXTENSIONS = {".3mf", ".gcode", ".bgcode"}


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


def _parse_studio_profile_metadata(raw: Optional[str]) -> Optional[dict]:
    """Parse Studio's JSON profile_metadata multipart field."""
    if raw in (None, ""):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OdinError(
            ErrorCode.validation_failed,
            f"profile_metadata must be valid JSON: {exc.msg}",
            status=400,
            retriable=False,
        ) from exc
    if not isinstance(parsed, dict):
        raise OdinError(
            ErrorCode.validation_failed,
            "profile_metadata must be a JSON object",
            status=400,
            retriable=False,
        )
    return parsed


def _validate_studio_print_file_name(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_PRINT_FILE_EXTENSIONS:
        raise OdinError(
            ErrorCode.validation_failed,
            "Only .3mf, .gcode, and .bgcode files are supported",
            status=400,
            retriable=False,
        )
    return ext


def _get_accessible_printer(db: Session, printer_id: int, current_user: dict):
    printer = db.execute(
        text("SELECT id, name, org_id, api_type, machine_type FROM printers WHERE id = :id"),
        {"id": printer_id},
    ).fetchone()
    if not printer:
        raise OdinError(
            ErrorCode.printer_not_found,
            f"Printer {printer_id} not found",
            status=404,
            retriable=False,
        )
    if not check_org_access(current_user, printer._mapping.get("org_id")):
        raise OdinError(
            ErrorCode.printer_not_found,
            f"Printer {printer_id} not found",
            status=404,
            retriable=False,
        )
    return printer


def _studio_print_notes(note: Optional[str], profile_metadata: Optional[dict]) -> Optional[str]:
    pieces = []
    if note:
        pieces.append(note)
    if profile_metadata:
        pieces.append("ODIN Studio profile_metadata: " + json.dumps(profile_metadata, sort_keys=True))
    return "\n\n".join(pieces) if pieces else None


# ──────────────────────────────────────────────
# Print Files (3MF upload & management)
# ──────────────────────────────────────────────

@router.post("/print-files/upload")
@limiter.limit("30/minute")
async def upload_3mf(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Upload and parse a print file (.3mf, .gcode, or .bgcode)."""
    from modules.models_library import print_file_meta as pfm

    fname = file.filename or ""
    ext = os.path.splitext(fname)[1].lower()

    if ext not in ALLOWED_PRINT_FILE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only .3mf, .gcode, and .bgcode files are supported")

    # Enforce upload size limit (100 MB)
    MAX_UPLOAD_BYTES = 100 * 1024 * 1024
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large. Maximum upload size is 100 MB.")

    # Compute file hash for duplicate detection
    import hashlib
    file_hash = hashlib.sha256(content).hexdigest()

    # Check for existing file with same hash
    existing = db.execute(
        text("SELECT id, filename FROM print_files WHERE file_hash = :h LIMIT 1"),
        {"h": file_hash},
    ).fetchone()
    duplicate_info = None
    if existing:
        duplicate_info = {"duplicate": True, "existing_file_id": existing[0], "existing_file_name": existing[1]}

    # Save to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        if ext == ".3mf":
            # --- .3mf path: full metadata parse ---
            from modules.models_library.threemf_parser import parse_3mf, extract_objects_from_plate, extract_mesh_from_3mf

            # Zip bomb check: reject files where uncompressed size exceeds 500 MB
            import zipfile as _zf
            MAX_UNCOMPRESSED = 500 * 1024 * 1024
            try:
                with _zf.ZipFile(tmp_path, 'r') as _z:
                    total_size = sum(e.file_size for e in _z.infolist())
                    if total_size > MAX_UNCOMPRESSED:
                        raise HTTPException(status_code=400, detail="File rejected: decompressed size exceeds 500 MB limit.")
            except _zf.BadZipFile:
                raise HTTPException(status_code=400, detail="Invalid .3mf file (bad zip structure).")

            metadata = parse_3mf(tmp_path)
            if not metadata:
                raise HTTPException(status_code=400, detail="Failed to parse .3mf file")

            # Extract objects for quantity counting
            import zipfile
            with zipfile.ZipFile(tmp_path, 'r') as zf:
                plate_objects = extract_objects_from_plate(zf)

            # Extract 3D mesh for viewer
            mesh_data = extract_mesh_from_3mf(tmp_path)
            mesh_json = json.dumps(mesh_data) if mesh_data else None

            # Store in database
            result = db.execute(text("""
                INSERT INTO print_files (
                    filename, project_name, print_time_seconds, total_weight_grams,
                    layer_count, layer_height, nozzle_diameter, printer_model,
                    supports_used, bed_type, filaments_json, thumbnail_b64, mesh_data,
                    file_hash
                ) VALUES (
                    :filename, :project_name, :print_time_seconds, :total_weight_grams,
                    :layer_count, :layer_height, :nozzle_diameter, :printer_model,
                    :supports_used, :bed_type, :filaments_json, :thumbnail_b64, :mesh_json,
                    :file_hash
                )
            """), {
                "filename": file.filename,
                "project_name": metadata.project_name,
                "print_time_seconds": metadata.print_time_seconds,
                "total_weight_grams": metadata.total_weight_grams,
                "layer_count": metadata.layer_count,
                "layer_height": metadata.layer_height,
                "nozzle_diameter": metadata.nozzle_diameter,
                "printer_model": metadata.printer_model,
                "supports_used": metadata.supports_used,
                "bed_type": metadata.bed_type,
                "filaments_json": json.dumps([{
                    "slot": f.slot,
                    "type": f.type,
                    "color": f.color,
                    "used_meters": f.used_meters,
                    "used_grams": f.used_grams
                } for f in metadata.filaments]),
                "thumbnail_b64": metadata.thumbnail_b64,
                "mesh_json": mesh_json,
                "file_hash": file_hash,
            })
            db.commit()

            file_id = result.lastrowid

            # Check for existing model with same name (multi-variant support)
            normalized_name = _normalize_model_name(metadata.project_name)
            existing_model = db.execute(text(
                "SELECT id FROM models WHERE name = :name OR name = :raw_name LIMIT 1"
            ), {"name": normalized_name, "raw_name": metadata.project_name}).fetchone()

            color_req = {}
            for f_item in metadata.filaments:
                color_req[f"slot{f_item.slot}"] = {
                    "color": f_item.color,
                    "grams": round(f_item.used_grams, 2) if f_item.used_grams else 0
                }

            fil_type = "PLA"
            if metadata.filaments:
                fil_type = metadata.filaments[0].type or "PLA"

            if existing_model:
                # Attach as variant to existing model
                model_id = existing_model[0]
                is_new_model = False
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()
            else:
                # Create new model
                model_result = db.execute(text("""
                    INSERT INTO models (
                        name, build_time_hours, default_filament_type,
                        color_requirements, thumbnail_b64, print_file_id, category
                    ) VALUES (
                        :name, :build_time_hours, :filament_type,
                        :color_requirements, :thumbnail_b64, :print_file_id, :category
                    )
                """), {
                    "name": normalized_name,
                    "build_time_hours": round(metadata.print_time_seconds / 3600.0, 2),
                    "filament_type": fil_type,
                    "color_requirements": json.dumps(color_req),
                    "thumbnail_b64": metadata.thumbnail_b64,
                    "print_file_id": file_id,
                    "category": "Uploaded"
                })
                db.commit()
                model_id = model_result.lastrowid
                is_new_model = True
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()

            # Persist file to disk
            import shutil
            file_dir = "/data/print_files"
            os.makedirs(file_dir, exist_ok=True)
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', file.filename)
            stored_path = f"{file_dir}/{file_id}_{safe_name}"
            shutil.copy2(tmp_path, stored_path)
            db.execute(text("UPDATE print_files SET stored_path = :p, original_filename = :fn WHERE id = :id"),
                       {"p": stored_path, "fn": file.filename, "id": file_id})
            db.commit()

            # Extract bed/compatibility metadata and persist
            meta = pfm.extract_print_file_meta(stored_path, ext)
            db.execute(text(
                "UPDATE print_files SET bed_x_mm = :x, bed_y_mm = :y, compatible_api_types = :types WHERE id = :id"
            ), {"x": meta["bed_x_mm"], "y": meta["bed_y_mm"], "types": meta["compatible_api_types"], "id": file_id})
            db.commit()

            return {
                "id": file_id,
                "filename": file.filename,
                "project_name": metadata.project_name,
                "print_time_seconds": metadata.print_time_seconds,
                "print_time_formatted": metadata.print_time_formatted(),
                "total_weight_grams": metadata.total_weight_grams,
                "layer_count": metadata.layer_count,
                "filaments": [{
                    "slot": f.slot,
                    "type": f.type,
                    "color": f.color,
                    "used_grams": f.used_grams
                } for f in metadata.filaments],
                "thumbnail_b64": metadata.thumbnail_b64,
                "is_sliced": metadata.print_time_seconds > 0,
                "model_id": model_id,
                "is_new_model": is_new_model,
                "printer_model": metadata.printer_model,
                "objects": plate_objects,
                "has_mesh": mesh_data is not None,
                "bed_x_mm": meta["bed_x_mm"],
                "bed_y_mm": meta["bed_y_mm"],
                "compatible_api_types": meta["compatible_api_types"],
                "duplicate": duplicate_info,
            }

        else:
            # --- .gcode / .bgcode path: minimal record, no 3mf parsing ---
            import shutil
            project_name = os.path.splitext(fname)[0]
            normalized_name = _normalize_model_name(project_name)

            result = db.execute(text("""
                INSERT INTO print_files (
                    filename, project_name, filaments_json, file_hash
                ) VALUES (
                    :filename, :project_name, :filaments_json, :file_hash
                )
            """), {
                "filename": file.filename,
                "project_name": project_name,
                "filaments_json": json.dumps([]),
                "file_hash": file_hash,
            })
            db.commit()
            file_id = result.lastrowid

            # Persist file to disk
            file_dir = "/data/print_files"
            os.makedirs(file_dir, exist_ok=True)
            safe_name = re.sub(r'[^a-zA-Z0-9._-]', '_', file.filename)
            stored_path = f"{file_dir}/{file_id}_{safe_name}"
            shutil.copy2(tmp_path, stored_path)
            db.execute(text("UPDATE print_files SET stored_path = :p, original_filename = :fn WHERE id = :id"),
                       {"p": stored_path, "fn": file.filename, "id": file_id})
            db.commit()

            # Check for existing model or create new
            existing_model = db.execute(text(
                "SELECT id FROM models WHERE name = :name OR name = :raw_name LIMIT 1"
            ), {"name": normalized_name, "raw_name": project_name}).fetchone()

            if existing_model:
                model_id = existing_model[0]
                is_new_model = False
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()
            else:
                model_result = db.execute(text("""
                    INSERT INTO models (name, default_filament_type, print_file_id, category)
                    VALUES (:name, 'PLA', :print_file_id, 'Uploaded')
                """), {"name": normalized_name, "print_file_id": file_id})
                db.commit()
                model_id = model_result.lastrowid
                is_new_model = True
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
                db.commit()

            # Extract bed/compatibility metadata and persist
            meta = pfm.extract_print_file_meta(stored_path, ext)
            db.execute(text(
                "UPDATE print_files SET bed_x_mm = :x, bed_y_mm = :y, compatible_api_types = :types WHERE id = :id"
            ), {"x": meta["bed_x_mm"], "y": meta["bed_y_mm"], "types": meta["compatible_api_types"], "id": file_id})
            db.commit()

            return {
                "id": file_id,
                "filename": file.filename,
                "project_name": project_name,
                "print_time_seconds": 0,
                "print_time_formatted": "0m",
                "total_weight_grams": None,
                "layer_count": None,
                "filaments": [],
                "thumbnail_b64": None,
                "is_sliced": True,
                "model_id": model_id,
                "is_new_model": is_new_model,
                "printer_model": None,
                "objects": [],
                "has_mesh": False,
                "bed_x_mm": meta["bed_x_mm"],
                "bed_y_mm": meta["bed_y_mm"],
                "compatible_api_types": meta["compatible_api_types"],
                "duplicate": duplicate_info,
            }
    finally:
        # Clean up temp file
        os.unlink(tmp_path)


@router.post("/prints", status_code=status.HTTP_201_CREATED, tags=["Studio"])
async def submit_studio_print(
    request: Request,
    file: UploadFile = File(...),
    target_printer_id: int = Form(...),
    profile_metadata: Optional[str] = Form(None),
    note: Optional[str] = Form(None),
    current_user: dict = Depends(require_role("operator")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE)),
    db: Session = Depends(get_db),
):
    """Queue an ODIN Studio sliced file without starting printer hardware.

    Studio sends multipart `file`, `target_printer_id`, optional
    `profile_metadata`, and optional `note`. This endpoint deliberately queues
    only: it creates a pending job linked to the uploaded print file and never
    calls dispatch/MQTT/FTP printer-start code.
    """
    filename = file.filename or ""
    _validate_studio_print_file_name(filename)
    parsed_profile_metadata = _parse_studio_profile_metadata(profile_metadata)
    printer = _get_accessible_printer(db, target_printer_id, current_user)

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "queue_studio_print",
                "filename": filename,
                "target_printer_id": target_printer_id,
                "profile_metadata": parsed_profile_metadata,
                "note": note,
                "start_mode": "queue_only",
                "dispatch_required": True,
            },
            next_actions=[
                next_action(
                    "submit_print",
                    {"target_printer_id": target_printer_id, "dry_run": False},
                    "queue the sliced file after preview",
                ),
            ],
            notes=(
                "Would persist the uploaded file, create a pending job linked "
                "to it, and assign the target printer. No DB, filesystem, or "
                "printer side effects run on dry-run."
            ),
        )

    uploaded = await upload_3mf(
        request=request,
        file=file,
        current_user=current_user,
        db=db,
    )
    file_id = uploaded["id"]
    filaments = uploaded.get("filaments") or []
    colors = [f.get("color") for f in filaments if f.get("color")]
    duration_seconds = uploaded.get("print_time_seconds") or 0
    duration_hours = duration_seconds / 3600.0 if duration_seconds else 0
    notes = _studio_print_notes(note, parsed_profile_metadata)

    job_result = db.execute(text("""
        INSERT INTO jobs (
            item_name, model_id, duration_hours, colors_required, quantity,
            priority, status, printer_id, hold, is_locked, notes,
            charged_to_user_id, charged_to_org_id, target_type
        ) VALUES (
            :item_name, :model_id, :duration_hours, :colors_required, 1,
            5, 'pending', :printer_id, 0, 0, :notes,
            :charged_to_user_id, :charged_to_org_id, 'specific'
        )
    """), {
        "item_name": uploaded.get("project_name") or filename,
        "model_id": uploaded.get("model_id"),
        "duration_hours": duration_hours,
        "colors_required": ",".join(colors),
        "printer_id": target_printer_id,
        "notes": notes,
        "charged_to_user_id": current_user.get("id") if current_user else None,
        "charged_to_org_id": current_user.get("group_id") if current_user else None,
    })
    db.commit()
    job_id = job_result.lastrowid

    db.execute(
        text("UPDATE print_files SET job_id = :job_id WHERE id = :id"),
        {"job_id": job_id, "id": file_id},
    )
    db.commit()

    queued_position = db.execute(
        text("SELECT COUNT(*) FROM jobs WHERE status IN ('pending', 'scheduled') AND id <= :id"),
        {"id": job_id},
    ).scalar()

    return {
        "id": str(job_id),
        "status": "queued",
        "queued_position": queued_position,
        "job_id": job_id,
        "file_id": file_id,
        "target_printer_id": target_printer_id,
        "target_printer_name": printer._mapping.get("name"),
        "project_name": uploaded.get("project_name"),
        "dispatch_required": True,
        "start_mode": "queue_only",
        "next_actions": build_next_actions(
            next_action("get_job", {"id": job_id}, "confirm the queued Studio job"),
            next_action("dispatch_job", {"job_id": job_id}, "start the physical print only after operator approval"),
        ),
    }


@router.get("/print-files")
def list_print_files(
    limit: int = Query(default=20, ge=1, le=100),
    include_scheduled: bool = False,
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db)
):
    """List uploaded print files."""
    query = """
        SELECT pf.*, j.status as job_status, j.item_name as job_name
        FROM print_files pf
        LEFT JOIN jobs j ON j.id = pf.job_id
    """
    if not include_scheduled:
        query += " WHERE pf.job_id IS NULL"
    query += " ORDER BY pf.uploaded_at DESC LIMIT :limit"

    results = db.execute(text(query), {"limit": limit}).fetchall()

    files = []
    for row in results:
        r = dict(row._mapping)
        r['filaments'] = json.loads(r['filaments_json']) if r['filaments_json'] else []
        del r['filaments_json']
        r.pop('stored_path', None)  # server filesystem path — not for clients
        pts = r['print_time_seconds']
        r['print_time_formatted'] = (f"{pts // 3600}h {(pts % 3600) // 60}m" if pts and pts >= 3600 else f"{pts // 60}m" if pts else None)
        files.append(r)

    return files


@router.get("/print-files/{file_id}")
def get_print_file(file_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get details of a specific print file."""
    result = db.execute(text("SELECT * FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")

    r = dict(result._mapping)
    r['filaments'] = json.loads(r['filaments_json']) if r['filaments_json'] else []
    del r['filaments_json']
    r.pop('stored_path', None)  # server filesystem path — not for clients
    r['print_time_formatted'] = f"{r['print_time_seconds'] // 3600}h {(r['print_time_seconds'] % 3600) // 60}m" if r['print_time_seconds'] >= 3600 else f"{r['print_time_seconds'] // 60}m"

    return r


@router.delete("/print-files/{file_id}")
def delete_print_file(file_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete an uploaded print file."""
    result = db.execute(text("SELECT id FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")

    db.execute(text("DELETE FROM print_files WHERE id = :id"), {"id": file_id})
    db.commit()
    return {"deleted": True}


@router.post("/print-files/{file_id}/schedule")
def schedule_print_file(
    file_id: int,
    printer_id: Optional[int] = None,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Create a job from an uploaded print file."""
    # Get the print file
    result = db.execute(text("SELECT * FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")

    pf = dict(result._mapping)
    if pf['job_id']:
        raise HTTPException(status_code=400, detail="File already scheduled")

    filaments = json.loads(pf['filaments_json']) if pf['filaments_json'] else []
    colors = [f['color'] for f in filaments]

    # Create the job
    job_result = db.execute(text("""
        INSERT INTO jobs (
            item_name, duration_hours, colors_required, quantity, priority, status, printer_id, hold, is_locked
        ) VALUES (
            :item_name, :duration_hours, :colors_required, 1, 5, 'pending', :printer_id, 0, 0
        )
    """), {
        "item_name": pf['project_name'],
        "duration_hours": pf['print_time_seconds'] / 3600.0,
        "colors_required": ','.join(colors),
        "printer_id": printer_id
    })
    db.commit()

    job_id = job_result.lastrowid

    # Link the print file to the job
    db.execute(text("UPDATE print_files SET job_id = :job_id WHERE id = :id"), {
        "job_id": job_id,
        "id": file_id
    })
    db.commit()

    return {
        "job_id": job_id,
        "file_id": file_id,
        "project_name": pf['project_name'],
        "status": "pending"
    }


# ──────────────────────────────────────────────
# Mesh / 3D Viewer
# ──────────────────────────────────────────────

@router.get("/print-files/{file_id}/mesh", tags=["3D Viewer"])
async def get_print_file_mesh(file_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get mesh geometry data for 3D viewer from a print file."""
    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": file_id}).fetchone()

    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available for this file")

    import json as json_stdlib
    return json_stdlib.loads(result[0])


@router.get("/models/{model_id}/mesh", tags=["3D Viewer"])
async def get_model_mesh(model_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get mesh geometry for a model (via its linked print_file)."""
    # Find print_file_id from model
    model = db.execute(text(
        "SELECT print_file_id FROM models WHERE id = :id"
    ), {"id": model_id}).fetchone()

    if not model or not model[0]:
        raise HTTPException(status_code=404, detail="Model has no linked print file")

    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": model[0]}).fetchone()

    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available")

    import json as json_stdlib
    return json_stdlib.loads(result[0])
