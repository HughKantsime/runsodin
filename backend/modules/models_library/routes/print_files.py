"""O.D.I.N. — Print File Upload and Management."""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, UploadFile, File, Form
from starlette.responses import JSONResponse
from core.rate_limit import limiter
from sqlalchemy.orm import Session
from sqlalchemy import text
from typing import Optional
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from sqlalchemy.exc import OperationalError
from core.dependencies import log_audit

from core.quota import charge_job_quota
from core.db import get_db
from core.db_compat import execute_insert_returning_id
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import AGENT_WRITE_SCOPE, check_org_access, require_any_scope, require_role
from core.responses import build_next_actions, next_action
from modules.models_library.upload_storage import _fresh_upload_actor, _visible_upload_duplicate, _own_upload_model, _new_upload_path

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
        text("SELECT id, name, org_id, api_type, machine_type, is_active, shared, bed_x_mm, bed_y_mm FROM printers WHERE id = :id"),
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
    current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)
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

    duplicate_info = None

    # Save to temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    stored_path = None
    file_id = None
    final_created = False
    commit_started = False
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

            metadata = parse_3mf(tmp_path, display_name=os.path.basename(file.filename or "") or None)
            if not metadata:
                raise HTTPException(status_code=400, detail="Failed to parse .3mf file")

            # Extract objects for quantity counting
            import zipfile
            with zipfile.ZipFile(tmp_path, 'r') as zf:
                plate_objects = extract_objects_from_plate(zf)

            # Extract 3D mesh for viewer
            mesh_data = extract_mesh_from_3mf(tmp_path)
            mesh_json = json.dumps(mesh_data) if mesh_data else None

            current_user = await _fresh_upload_actor(db, current_user)
            duplicate_info = _visible_upload_duplicate(db, file_hash, current_user)

            # Store in database
            file_id = execute_insert_returning_id(db, """
                INSERT INTO print_files (
                    filename, project_name, print_time_seconds, total_weight_grams,
                    layer_count, layer_height, nozzle_diameter, printer_model,
                    supports_used, bed_type, filaments_json, thumbnail_b64, mesh_data,
                    file_hash, org_id, created_by
                ) VALUES (
                    :filename, :project_name, :print_time_seconds, :total_weight_grams,
                    :layer_count, :layer_height, :nozzle_diameter, :printer_model,
                    :supports_used, :bed_type, :filaments_json, :thumbnail_b64, :mesh_json,
                    :file_hash, :org_id, :created_by
                )
            """, {
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
                "org_id": current_user.get("group_id"),
                "created_by": current_user["id"],
            })

            # Check for existing model with same name (multi-variant support)
            normalized_name = _normalize_model_name(metadata.project_name)
            existing_model = _own_upload_model(db, normalized_name, metadata.project_name, current_user.get("group_id"))

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
            else:
                # Create new model
                model_id = execute_insert_returning_id(db, """
                    INSERT INTO models (
                        name, build_time_hours, default_filament_type,
                        color_requirements, thumbnail_b64, print_file_id, category, org_id
                    ) VALUES (
                        :name, :build_time_hours, :filament_type,
                        :color_requirements, :thumbnail_b64, :print_file_id, :category, :org_id
                    )
                """, {
                    "name": normalized_name,
                    "build_time_hours": round(metadata.print_time_seconds / 3600.0, 2),
                    "filament_type": fil_type,
                    "color_requirements": json.dumps(color_req),
                    "thumbnail_b64": metadata.thumbnail_b64,
                    "print_file_id": file_id,
                    "category": "Uploaded",
                    "org_id": current_user.get("group_id")
                })
                is_new_model = True
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})

            # Persist file to disk
            import shutil
            destination = _new_upload_path(file_id, file.filename)
            with open(destination, "xb") as output:
                stored_path = str(destination)
                final_created = True
                with open(tmp_path, "rb") as source:
                    shutil.copyfileobj(source, output)
            db.execute(text("UPDATE print_files SET stored_path = :p, original_filename = :fn WHERE id = :id"),
                       {"p": stored_path, "fn": file.filename, "id": file_id})

            # Extract bed/compatibility metadata and persist
            meta = pfm.extract_print_file_meta(stored_path, ext)
            db.execute(text(
                "UPDATE print_files SET bed_x_mm = :x, bed_y_mm = :y, "
                "compatible_api_types = :types, compatibility_facts_json = :facts WHERE id = :id"
            ), {
                "x": meta["bed_x_mm"],
                "y": meta["bed_y_mm"],
                "types": meta["compatible_api_types"],
                "facts": json.dumps(meta["safety_facts"], sort_keys=True),
                "id": file_id,
            })

            log_audit(db, "print_file_uploaded", "print_file", file_id)
            db.flush()
            commit_started = True
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
                "compatibility_facts": meta["safety_facts"],
                "duplicate": duplicate_info,
            }

        else:
            # --- .gcode / .bgcode path: minimal record, no 3mf parsing ---
            import shutil
            project_name = os.path.splitext(fname)[0]
            normalized_name = _normalize_model_name(project_name)
            current_user = await _fresh_upload_actor(db, current_user)
            duplicate_info = _visible_upload_duplicate(db, file_hash, current_user)

            file_id = execute_insert_returning_id(db, """
                INSERT INTO print_files (
                    filename, project_name, filaments_json, file_hash, org_id, created_by
                ) VALUES (
                    :filename, :project_name, :filaments_json, :file_hash, :org_id, :created_by
                )
            """, {
                "filename": file.filename,
                "project_name": project_name,
                "filaments_json": json.dumps([]),
                "file_hash": file_hash,
                "org_id": current_user.get("group_id"),
                "created_by": current_user["id"],
            })

            # Persist file to disk
            destination = _new_upload_path(file_id, file.filename)
            with open(destination, "xb") as output:
                stored_path = str(destination)
                final_created = True
                with open(tmp_path, "rb") as source:
                    shutil.copyfileobj(source, output)
            db.execute(text("UPDATE print_files SET stored_path = :p, original_filename = :fn WHERE id = :id"),
                       {"p": stored_path, "fn": file.filename, "id": file_id})

            # Check for existing model or create new
            existing_model = _own_upload_model(db, normalized_name, project_name, current_user.get("group_id"))

            if existing_model:
                model_id = existing_model[0]
                is_new_model = False
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})
            else:
                model_id = execute_insert_returning_id(db, """
                    INSERT INTO models (name, default_filament_type, print_file_id, category, org_id)
                    VALUES (:name, 'PLA', :print_file_id, 'Uploaded', :org_id)
                """, {"name": normalized_name, "print_file_id": file_id, "org_id": current_user.get("group_id")})
                is_new_model = True
                db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                           {"mid": model_id, "fid": file_id})

            # Extract bed/compatibility metadata and persist
            meta = pfm.extract_print_file_meta(stored_path, ext)
            db.execute(text(
                "UPDATE print_files SET bed_x_mm = :x, bed_y_mm = :y, "
                "compatible_api_types = :types, compatibility_facts_json = :facts WHERE id = :id"
            ), {
                "x": meta["bed_x_mm"],
                "y": meta["bed_y_mm"],
                "types": meta["compatible_api_types"],
                "facts": json.dumps(meta["safety_facts"], sort_keys=True),
                "id": file_id,
            })

            log_audit(db, "print_file_uploaded", "print_file", file_id)
            db.flush()
            commit_started = True
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
                "compatibility_facts": meta["safety_facts"],
                "duplicate": duplicate_info,
            }
    except Exception as exc:
        rollback_failed = False
        try:
            db.rollback()
        except Exception:
            rollback_failed = True
        uncertain = commit_started or rollback_failed
        if final_created and not uncertain:
            try:
                Path(stored_path).unlink(missing_ok=True)
            except OSError:
                log.warning("Upload cleanup failed for file %s", file_id)
        if uncertain:
            log.warning("Upload transaction outcome unknown for file %s; payload retained", file_id)
            reference = f" with file reference {file_id}" if file_id is not None else ""
            raise HTTPException(status_code=500, detail=f"Upload outcome is uncertain. Contact support{reference} before retrying.") from None
        if isinstance(exc, HTTPException):
            raise
        busy = isinstance(exc, OperationalError) and (
            getattr(getattr(exc, "orig", None), "sqlstate", None) == "55P03"
            or "database is locked" in str(getattr(exc, "orig", "")))
        if busy:
            raise HTTPException(status_code=409, detail="Another upload is in progress; retry this upload") from None
        log.warning("Print file upload failed (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Unable to save print file upload") from None
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
    from modules.models_library.queue_payload import TARGET_FIELDS, capture
    initial_target = capture(printer, TARGET_FIELDS)
    compatible_suffixes = {'bambu': ('.3mf',), 'moonraker': ('.gcode',), 'prusalink': ('.gcode', '.bgcode')}
    if (not printer.is_active or printer.api_type not in compatible_suffixes
            or Path(filename).suffix.lower() not in compatible_suffixes[printer.api_type]):
        raise HTTPException(400, 'Uploaded file is incompatible with the selected printer API')

    if is_dry_run(request):
        return JSONResponse(status_code=200, content=dry_run_preview(
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
        ))

    from core.interfaces.queue_creation import require_queue_creation_provider
    queue_creator = require_queue_creation_provider()

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

    from core.model_preview_access import accessible_preview_file
    from modules.models_library.file_management import (
        _assert_generic_payload, _generic_parent, _rollback_file_transaction,
        assert_generic_model_reference_allowed,
    )
    from modules.models_library.queue_payload import (
        FILE_FIELDS, lock_queue_reference, assert_unchanged,
        verified_queue_payload, assert_payload_unchanged, file_snapshot_predicate,
    )
    try:
        db.execute(text('UPDATE print_files SET id=id WHERE id=:id'), {'id': file_id})
        row = accessible_preview_file(db, file_id, current_user)
        _assert_generic_payload(db, row)
        if row.job_id is not None or row.model_id != uploaded.get('model_id'):
            raise HTTPException(409, 'Uploaded file changed; refresh before scheduling')
        if row.model_id is not None:
            lock_queue_reference(db, 'models', row.model_id, ('id', 'org_id'))
        parent = _generic_parent(db, row, current_user)
        assert_generic_model_reference_allowed(db, row.model_id)
        target = lock_queue_reference(db, 'printers', target_printer_id, TARGET_FIELDS)
        if target is None or not check_org_access(current_user, target.org_id):
            raise HTTPException(404, 'Printer not found')
        target_capture = capture(target, TARGET_FIELDS)
        if target_capture != initial_target:
            raise HTTPException(409, 'Selected printer changed during upload; refresh before scheduling')
        file_capture = capture(row, FILE_FIELDS)
        parent_capture = capture(parent, ('id', 'org_id'))
        with verified_queue_payload(row, target) as payload_hash:
            assert_unchanged(db, 'print_files', file_capture)
            assert_unchanged(db, 'models', parent_capture)
            assert_unchanged(db, 'printers', target_capture)
        charge_job_quota(db, current_user)

        job = queue_creator.stage_values(db, {
            "item_name": uploaded.get("project_name") or filename,
            "model_id": row.model_id, "print_file_id": row.id,
            "payload_sha256": payload_hash, "plate_index": 0, "duration_hours": duration_hours,
            "colors_required": ",".join(colors), "quantity": 1, "priority": 5,
            "status": "pending", "printer_id": target_printer_id,
            "hold": False, "is_locked": False, "notes": notes,
            "charged_to_user_id": current_user.get("id") if current_user else None,
            "charged_to_org_id": current_user.get("group_id") if current_user else None,
            "target_type": "specific",
        }, current_user)
        job_id = job.id
        assert_unchanged(db, 'print_files', file_capture)
        assert_unchanged(db, 'models', parent_capture)
        assert_unchanged(db, 'printers', target_capture)
        _assert_generic_payload(db, row)
        assert_generic_model_reference_allowed(db, row.model_id)
        assert_payload_unchanged(row.stored_path, payload_hash)
        predicate, parameters = file_snapshot_predicate(file_capture)
        linked = db.execute(text(f'UPDATE print_files SET job_id=:job WHERE {predicate} AND job_id IS NULL'),
                            {**parameters, 'job': job_id})
        if linked.rowcount != 1:
            raise HTTPException(409, 'Print file changed; refresh before scheduling')
        db.commit()
    except (HTTPException, OdinError):
        _rollback_file_transaction(db, file_id)
        raise
    except Exception as exc:
        _rollback_file_transaction(db, file_id)
        log.warning('Studio queue failed for file %s (%s)', file_id, type(exc).__name__)
        raise HTTPException(500, 'Unable to queue uploaded print file. Refresh before retrying.') from None

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
def list_print_files(limit: int = Query(default=20, ge=1, le=100), include_scheduled: bool = False,
                     current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    from modules.models_library.file_management import list_generic_files
    return list_generic_files(db, current_user, limit, include_scheduled)


@router.get("/print-files/{file_id}")
def get_print_file(file_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    from modules.models_library.file_management import read_generic_file
    return read_generic_file(db, file_id, current_user)


@router.delete("/print-files/{file_id}")
def delete_print_file(file_id: int, current_user: dict = Depends(require_role("operator", scope="write:models")), db: Session = Depends(get_db)):
    from modules.models_library.file_management import delete_generic_file
    return delete_generic_file(db, file_id, current_user)


@router.post("/print-files/{file_id}/schedule")
def schedule_print_file(file_id: int, printer_id: Optional[int] = None,
                        current_user: dict = Depends(require_role("operator", scope="write:jobs")), db: Session = Depends(get_db)):
    from modules.models_library.file_management import schedule_generic_file
    return schedule_generic_file(db, file_id, printer_id, current_user)


# ──────────────────────────────────────────────
# Mesh / 3D Viewer
# ──────────────────────────────────────────────

@router.get("/print-files/{file_id}/mesh", tags=["3D Viewer"])
async def get_print_file_mesh(file_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """Get geometry only after file and Education audience authorization."""
    from core.model_preview_access import accessible_preview_file, preview_not_found
    row = accessible_preview_file(db, file_id, current_user)
    if not row.mesh_data:
        raise preview_not_found()
    try:
        return json.loads(row.mesh_data)
    except (TypeError, ValueError):
        raise preview_not_found() from None


@router.get("/models/{model_id}/mesh", tags=["3D Viewer"])
async def get_model_mesh(model_id: int, current_user: dict = Depends(require_role("viewer", scope="read:models")), db: Session = Depends(get_db)):
    """Authorize both the model and its linked original file."""
    from core.model_preview_access import preview_not_found
    model = db.execute(text("SELECT print_file_id,org_id FROM models WHERE id=:id"), {"id": model_id}).fetchone()
    if not model or not model.print_file_id or not check_org_access(current_user, model.org_id):
        raise preview_not_found()
    return await get_print_file_mesh(model.print_file_id, current_user, db)
