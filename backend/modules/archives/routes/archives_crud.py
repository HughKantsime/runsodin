"""O.D.I.N. — Print Archive CRUD, Tags, AMS Preview, Reprint, and File Preview."""

from core.rbac import should_filter_org

# Domain: archives
# Depends on: core, printers, jobs, organizations
# Owns tables: print_archives

import hashlib
import hmac
import csv
import io
import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel as PydanticBaseModel, Field, StrictInt
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import check_org_access, get_org_scope, require_action, require_role, require_superadmin

log = logging.getLogger("odin.api")
router = APIRouter()
PREVIEW_STORAGE_ROOT = "/data"


# ──────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────

class ArchiveNotesUpdate(PydanticBaseModel):
    notes: Optional[str] = None


class TagsUpdate(PydanticBaseModel):
    tags: List[str]


class TagRename(PydanticBaseModel):
    old: str
    new: str


class ReprintRequest(PydanticBaseModel):
    printer_id: int
    ams_mapping: Optional[List[dict]] = None
    plate_index: StrictInt = Field(default=0, ge=0)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _archive_row_to_dict(r):
    """Convert a raw archive row to a response dict with printer display name."""
    d = dict(r._mapping)
    d["printer_display"] = (
        d.get("printer_nickname") or d.get("printer_name")
        or f"Printer {d.get('printer_id')}"
    )
    # Parse tags from comma-separated string
    raw_tags = d.get("tags") or ""
    d["tags"] = [t.strip() for t in raw_tags.split(",") if t.strip()] if raw_tags else []
    return d


def _build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, user_id, tag):
    """Build WHERE clause for archive queries."""
    if printer_id is not None:
        conditions.append("a.printer_id = :printer_id")
        params["printer_id"] = printer_id
    if status:
        conditions.append("a.status = :status")
        params["status"] = status
    if search:
        conditions.append("a.print_name LIKE :search")
        params["search"] = f"%{search}%"
    if start_date:
        conditions.append("a.completed_at >= :start_date")
        params["start_date"] = start_date
    if end_date:
        conditions.append("a.completed_at <= :end_date")
        params["end_date"] = end_date
    if user_id is not None:
        conditions.append("a.user_id = :user_id")
        params["user_id"] = user_id
    if tag:
        # Match comma-separated tag field (exact tag match, not substring)
        conditions.append(
            "(a.tags = :tag_exact OR a.tags LIKE :tag_start "
            "OR a.tags LIKE :tag_mid OR a.tags LIKE :tag_end)"
        )
        params["tag_exact"] = tag
        params["tag_start"] = f"{tag},%"
        params["tag_mid"] = f"%, {tag},%"
        params["tag_end"] = f"%, {tag}"


from modules.archives.access import archive_visibility, assert_archive_access


def _check_archive_org_access(db, archive_id, current_user):
    assert_archive_access(db, archive_id, current_user)


def _apply_org_scope(conditions, params, current_user, join_clauses=None):
    predicate, bindings = archive_visibility(current_user)
    conditions.append(predicate)
    params.update(bindings)
    return ""


# ──────────────────────────────────────────────
# Archive CRUD
# ──────────────────────────────────────────────

class OwnershipReconciliation(PydanticBaseModel):
    limit: StrictInt = Field(default=100, ge=1, le=100)
    after_archive_id: StrictInt = Field(default=0, ge=0)


@router.post("/archives/reconcile-ownership", tags=["Archives"])
def reconcile_archive_ownership(body: OwnershipReconciliation,
    user=Depends(require_superadmin(scope="write:jobs")), db: Session=Depends(get_db)):
    from modules.archives.legacy_ownership import resolve_legacy_archive_ownership
    try:
        result=resolve_legacy_archive_ownership(db,limit=body.limit,after_archive_id=body.after_archive_id)
        db.commit()
        return result
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, 'sqlstate', None) == '55P03':
            raise HTTPException(409, 'Archive repair is temporarily busy. Retry after current activity finishes.') from None
        raise
    except Exception:
        db.rollback()
        raise


@router.get("/archives", tags=["Archives"])
def list_archives(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    printer_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """List print archives with pagination and filters."""
    conditions = []
    params = {}
    _build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, None, tag)

    # Org scoping — main query already JOINs printers as p
    org = get_org_scope(user)
    predicate, bindings = archive_visibility(user)
    conditions.append(predicate)
    params.update(bindings)

    where = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * per_page

    # Count query needs the printer JOIN for org scoping
    count_join = " LEFT JOIN printers p ON a.printer_id = p.id" if should_filter_org(user, org) else ""
    total = db.execute(
        text(f"SELECT COUNT(*) FROM print_archives a{count_join} WHERE {where}"), params  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    ).scalar() or 0

    rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
            f"SELECT a.*, p.name AS printer_name, p.nickname AS printer_nickname "
            f"FROM print_archives a "
            f"LEFT JOIN printers p ON p.id = a.printer_id "
            f"WHERE {where} "
            f"ORDER BY a.created_at DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": per_page, "offset": offset},
    ).fetchall()

    return {
        "items": [_archive_row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@router.get("/archives/compare", tags=["Archives"])
def compare_archives(
    a: int = Query(..., description="First archive ID"),
    b: int = Query(..., description="Second archive ID"),
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Side-by-side comparison of two archive entries."""
    row_a = db.execute(
        text(
            "SELECT a.*, p.name AS printer_name, p.nickname AS printer_nickname "
            "FROM print_archives a LEFT JOIN printers p ON p.id = a.printer_id "
            "WHERE a.id = :id"
        ),
        {"id": a},
    ).fetchone()
    row_b = db.execute(
        text(
            "SELECT a.*, p.name AS printer_name, p.nickname AS printer_nickname "
            "FROM print_archives a LEFT JOIN printers p ON p.id = a.printer_id "
            "WHERE a.id = :id"
        ),
        {"id": b},
    ).fetchone()

    if not row_a or not row_b:
        raise HTTPException(status_code=404, detail="One or both archives not found")

    _check_archive_org_access(db, dict(row_a._mapping)["id"], user)
    _check_archive_org_access(db, dict(row_b._mapping)["id"], user)

    da = _archive_row_to_dict(row_a)
    db_dict = _archive_row_to_dict(row_b)

    # Build diff — fields that differ between a and b
    compare_fields = [
        "printer_display", "status", "actual_duration_seconds",
        "filament_used_grams", "cost_estimate", "started_at", "completed_at",
    ]
    diff = {}
    for f in compare_fields:
        va = da.get(f)
        vb = db_dict.get(f)
        if va != vb:
            diff[f] = {"a": va, "b": vb}

    return {"a": da, "b": db_dict, "diff": diff}


@router.get("/archives/log", tags=["Archives"])
def archive_log(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    printer_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Chronological print log — flat rows sorted by completed_at desc."""
    conditions = []
    params = {}
    _build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, user_id, tag)

    # Org scoping — main query already JOINs printers as p
    org = get_org_scope(user)
    predicate, bindings = archive_visibility(user)
    conditions.append(predicate)
    params.update(bindings)

    where = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * per_page

    count_join = " LEFT JOIN printers p ON a.printer_id = p.id" if should_filter_org(user, org) else ""
    total = db.execute(
        text(f"SELECT COUNT(*) FROM print_archives a{count_join} WHERE {where}"), params  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
    ).scalar() or 0

    rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
            f"SELECT a.id, a.print_name, a.printer_id, a.user_id, a.status, "
            f"a.started_at, a.completed_at, a.actual_duration_seconds, "
            f"a.filament_used_grams, a.cost_estimate, a.tags, "
            f"p.name AS printer_name, p.nickname AS printer_nickname, "
            f"u.username AS user_name "
            f"FROM print_archives a "
            f"LEFT JOIN printers p ON p.id = a.printer_id "
            f"LEFT JOIN users u ON u.id = a.user_id "
            f"WHERE {where} "
            f"ORDER BY a.completed_at DESC LIMIT :limit OFFSET :offset"
        ),
        {**params, "limit": per_page, "offset": offset},
    ).fetchall()

    items = []
    for r in rows:
        d = dict(r._mapping)
        d["printer_display"] = (
            d.get("printer_nickname") or d.get("printer_name")
            or f"Printer {d.get('printer_id')}"
        )
        raw_tags = d.get("tags") or ""
        d["tags"] = [t.strip() for t in raw_tags.split(",") if t.strip()] if raw_tags else []
        items.append(d)

    return {"items": items, "total": total, "page": page, "per_page": per_page}


@router.get("/archives/log/export", tags=["Archives"])
def export_archive_log(
    printer_id: Optional[int] = Query(None),
    user_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    tag: Optional[str] = Query(None),
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Export print log as CSV."""
    conditions = []
    params = {}
    _build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, user_id, tag)

    # Org scoping — main query already JOINs printers as p
    org = get_org_scope(user)
    predicate, bindings = archive_visibility(user)
    conditions.append(predicate)
    params.update(bindings)

    where = " AND ".join(conditions) if conditions else "1=1"

    rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
            f"SELECT a.print_name, a.status, a.started_at, a.completed_at, "
            f"a.actual_duration_seconds, a.filament_used_grams, a.cost_estimate, "
            f"a.tags, p.name AS printer_name, u.username AS user_name "
            f"FROM print_archives a "
            f"LEFT JOIN printers p ON p.id = a.printer_id "
            f"LEFT JOIN users u ON u.id = a.user_id "
            f"WHERE {where} "
            f"ORDER BY a.completed_at DESC"
        ),
        params,
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Print Name", "Printer", "User", "Status",
        "Started", "Completed", "Duration (s)",
        "Filament (g)", "Cost", "Tags",
    ])
    for r in rows:
        d = dict(r._mapping)
        writer.writerow([
            d.get("print_name", ""),
            d.get("printer_name", ""),
            d.get("user_name", ""),
            d.get("status", ""),
            d.get("started_at", ""),
            d.get("completed_at", ""),
            d.get("actual_duration_seconds", ""),
            d.get("filament_used_grams", ""),
            d.get("cost_estimate", ""),
            d.get("tags", ""),
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=print_log.csv"},
    )


@router.get("/archives/{archive_id}", tags=["Archives"])
def get_archive(
    archive_id: int,
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Get a single print archive with full detail."""
    row = db.execute(
        text(
            "SELECT a.*, p.name AS printer_name, p.nickname AS printer_nickname "
            "FROM print_archives a "
            "LEFT JOIN printers p ON p.id = a.printer_id "
            "WHERE a.id = :id"
        ),
        {"id": archive_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Archive not found")
    _check_archive_org_access(db, dict(row._mapping)["id"], user)
    return _archive_row_to_dict(row)


@router.patch("/archives/{archive_id}", tags=["Archives"])
def update_archive(
    archive_id: int,
    body: ArchiveNotesUpdate,
    user=Depends(require_role("operator", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Update archive notes."""
    existing = db.execute(
        text("SELECT id, printer_id FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Archive not found")
    _check_archive_org_access(db, existing.id, user)

    db.execute(
        text("UPDATE print_archives SET notes = :notes WHERE id = :id"),
        {"notes": body.notes, "id": archive_id},
    )
    db.commit()
    return {"status": "updated"}


@router.delete("/archives/{archive_id}", tags=["Archives"])
def delete_archive(
    archive_id: int,
    user=Depends(require_role("admin", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Delete a print archive. Admin only."""
    existing = db.execute(
        text("SELECT id, printer_id FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Archive not found")
    _check_archive_org_access(db, existing.id, user)

    db.execute(text("DELETE FROM print_archives WHERE id = :id"), {"id": archive_id})
    db.commit()
    return {"status": "deleted"}


# ──────────────────────────────────────────────
# Tag Management (per-archive)
# ──────────────────────────────────────────────

@router.patch("/archives/{archive_id}/tags", tags=["Archives"])
def update_archive_tags(
    archive_id: int,
    body: TagsUpdate,
    user=Depends(require_role("operator", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Set tags on an archive entry."""
    existing = db.execute(
        text("SELECT id, printer_id FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Archive not found")
    _check_archive_org_access(db, existing.id, user)

    # Store as comma-separated, trimmed, deduplicated
    clean = list(dict.fromkeys(t.strip() for t in body.tags if t.strip()))
    tags_str = ", ".join(clean) if clean else ""

    db.execute(
        text("UPDATE print_archives SET tags = :tags WHERE id = :id"),
        {"tags": tags_str, "id": archive_id},
    )
    db.commit()
    return {"tags": clean}


# ──────────────────────────────────────────────
# Reprint with AMS Mapping
# ──────────────────────────────────────────────

@router.get("/archives/{archive_id}/ams-preview", tags=["Archives"])
def ams_preview(
    archive_id: int,
    printer_id: Optional[int] = Query(None),
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Return filament requirements from an archive's file alongside a printer's AMS state."""
    archive = db.execute(
        text("SELECT * FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not archive:
        raise HTTPException(status_code=404, detail="Archive not found")

    ad = dict(archive._mapping)
    _check_archive_org_access(db, ad["id"], user)

    # Try to find the associated print_file for filament metadata
    required = []
    file_id = ad.get("print_file_id")
    if file_id:
        from core.model_preview_access import accessible_preview_file
        pf = accessible_preview_file(db, file_id, user)
        if pf.filaments_json:
            try:
                filaments = json.loads(pf.filaments_json)
                for i, f in enumerate(filaments):
                    required.append({
                        "slot": i,
                        "color": f.get("color", ""),
                        "material": f.get("type", f.get("material", "")),
                    })
            except (json.JSONDecodeError, TypeError):
                pass

    # Get target printer's AMS state if provided
    available = []
    if printer_id:
        from modules.printers.models import Printer, FilamentSlot
        printer = db.query(Printer).filter(Printer.id == printer_id).first()
        if not printer or not (check_org_access(user, printer.org_id) or printer.shared):
            raise HTTPException(404, "Printer not found")
        if printer:
            slots = db.query(FilamentSlot).filter(
                FilamentSlot.printer_id == printer_id
            ).order_by(FilamentSlot.slot_number).all()
            for s in slots:
                available.append({
                    "slot": s.slot_number,
                    "color": s.color or "",
                    "color_hex": s.color_hex or "",
                    "material": s.material_type or "",
                    "spool_id": s.assigned_spool_id,
                })

    from modules.archives.ams_mapping import suggest_mapping
    suggested = suggest_mapping(required, available)
    return {"required": required, "available": available, "suggested_mapping": suggested}


@router.post("/archives/{archive_id}/reprint", tags=["Archives"])
def reprint_archive(
    archive_id: int,
    body: ReprintRequest,
    user=Depends(require_action("jobs.create", scope="write:jobs")),  # a reprint creates a job
    db: Session = Depends(get_db),
):
    """Reprint an archived job on a specified printer with optional AMS remapping."""
    from core.interfaces.queue_creation import require_queue_creation_provider
    queue_creator = require_queue_creation_provider()
    archive = db.execute(
        text("SELECT * FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not archive:
        raise HTTPException(status_code=404, detail="Archive not found")

    ad = dict(archive._mapping)
    _check_archive_org_access(db, ad["id"], user)

    from modules.printers.models import Printer, FilamentSlot
    from modules.models_library.models import Model
    from modules.jobs.models import Job
    from core.print_payload import validate_plate, validate_mapping
    from core.registry import registry

    printer = db.query(Printer).filter(Printer.id == body.printer_id).first()
    if not printer or not (check_org_access(user, printer.org_id) or printer.shared):
        raise HTTPException(404, "Printer not found")
    file = db.execute(text("SELECT * FROM print_files WHERE id=:id"),
                      {"id": ad.get("print_file_id")}).mappings().first()
    model = db.query(Model).filter(Model.id == file["model_id"]).first() if file else None
    if (not file or not model or not check_org_access(user, model.org_id)
            or (file["org_id"] is not None and not check_org_access(user, file["org_id"]))):
        raise HTTPException(400, "Original print file or model not available for reprint")
    policy = registry.get_provider("EducationPolicyProvider")
    if policy is None:
        raise HTTPException(503, "Education policy unavailable")
    policy.assert_generic_model_reference_allowed(db, model.id)
    if db.execute(text("SELECT 1 FROM education_submissions WHERE print_file_id=:id"),
                  {"id": file["id"]}).first():
        raise HTTPException(409, "Education files require the Education approval workflow")
    file_path = file["stored_path"]
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(400, "Original print file not available for reprint")
    digest = hashlib.sha256()
    try:
        with open(file_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise HTTPException(400, "Original print file is unreadable") from None
    payload_hash = digest.hexdigest()
    for expected in (ad.get("file_hash"), file["file_hash"]):
        if expected and not hmac.compare_digest(str(expected).lower(), payload_hash):
            raise HTTPException(400, "Original print file contents changed")
    validate_plate(file_path, body.plate_index, printer.api_type)
    slots = {slot.slot_number: getattr(slot.filament_type, "value", slot.filament_type) for slot in db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer.id).all()}
    mapping = validate_mapping(body.ams_mapping, file["filaments_json"], slots, printer.api_type)
    identity = user if isinstance(user, dict) else vars(user)
    new_job = Job(
        item_name=f"Reprint: {ad.get('print_name') or 'Unknown'}",
        model_id=model.id, print_file_id=file["id"], payload_sha256=payload_hash, plate_index=body.plate_index,
        ams_mapping=mapping, printer_id=printer.id, priority=5, status="pending",
        submitted_by=identity.get("id"), charged_to_user_id=identity.get("id"),
        charged_to_org_id=identity.get("group_id"), quantity=1, hold=False,
        # The reprint dialog's printer (and its AMS mapping) is an explicit choice: pin it.
        is_locked=False, target_type="specific", target_filter=str(printer.id),
    )

    queue_creator.stage_job(db, new_job, identity)
    if mapping is not None:
        from core.ams_mapping import mapping_audit_details
        db.flush()
        log_audit(db, 'job.ams_mapping_changed', 'job', new_job.id, mapping_audit_details(new_job))
    db.commit()
    db.refresh(new_job)

    return {
        "status": "created",
        "job_id": new_job.id,
        "message": f"Reprint job created for printer {printer.name or printer.id}",
    }


# ──────────────────────────────────────────────
# File Preview (3D model serving)
# ──────────────────────────────────────────────

@router.get("/files/{file_id}/preview-model", tags=["Archives"])
def preview_model(
    file_id: int,
    user=Depends(require_role("viewer", scope="read:models")),
    db: Session = Depends(get_db),
):
    """Stream a 3MF or STL file for 3D model preview."""
    from core.model_preview_access import accessible_preview_file, preview_not_found
    row = accessible_preview_file(db, file_id, user)
    stored_path = row.stored_path
    original_name = row.original_filename or "model"
    if not stored_path:
        raise preview_not_found()
    from pathlib import Path
    real_path = Path(stored_path).resolve()
    roots = [Path(PREVIEW_STORAGE_ROOT).resolve()]
    configured_root = os.environ.get("PRINT_FILE_STORAGE_ROOT")
    if configured_root:
        configured = Path(configured_root)
        if not configured.is_absolute():
            raise HTTPException(status_code=400, detail="Print file storage root must be an absolute path")
        roots.append(configured.resolve())
    if not any(real_path.is_relative_to(root) for root in roots):
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not os.path.exists(real_path):
        raise preview_not_found()

    # Determine media type
    ext = os.path.splitext(original_name)[1].lower()
    media_types = {
        ".3mf": "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
        ".stl": "application/sla",
        ".obj": "text/plain",
    }
    media_type = media_types.get(ext, "application/octet-stream")

    return FileResponse(
        real_path,
        media_type=media_type,
        filename=original_name,
    )
