"""O.D.I.N. — Print Archive Routes

CRUD for print archives: searchable history of completed prints.
Tag management, print log, archive comparison, reprint with AMS mapping.
"""

# Domain: archives
# Depends on: core, printers, jobs, organizations
# Owns tables: print_archives, projects

import csv
import io
import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_user, require_role

log = logging.getLogger("odin.api")
router = APIRouter()


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
    plate_index: int = 0


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


# ──────────────────────────────────────────────
# Archive CRUD
# ──────────────────────────────────────────────

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
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """List print archives with pagination and filters."""
    conditions = []
    params = {}
    _build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, None, tag)
    where = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * per_page

    total = db.execute(
        text(f"SELECT COUNT(*) FROM print_archives a WHERE {where}"), params
    ).scalar() or 0

    rows = db.execute(
        text(
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
    user=Depends(require_role("viewer")),
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
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Chronological print log — flat rows sorted by completed_at desc."""
    conditions = []
    params = {}
    _build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, user_id, tag)
    where = " AND ".join(conditions) if conditions else "1=1"
    offset = (page - 1) * per_page

    total = db.execute(
        text(f"SELECT COUNT(*) FROM print_archives a WHERE {where}"), params
    ).scalar() or 0

    rows = db.execute(
        text(
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
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Export print log as CSV."""
    conditions = []
    params = {}
    _build_archive_query(conditions, params, printer_id, status, search,
                         start_date, end_date, user_id, tag)
    where = " AND ".join(conditions) if conditions else "1=1"

    rows = db.execute(
        text(
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
    user=Depends(require_role("viewer")),
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
    return _archive_row_to_dict(row)


@router.patch("/archives/{archive_id}", tags=["Archives"])
def update_archive(
    archive_id: int,
    body: ArchiveNotesUpdate,
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Update archive notes."""
    existing = db.execute(
        text("SELECT id FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Archive not found")

    db.execute(
        text("UPDATE print_archives SET notes = :notes WHERE id = :id"),
        {"notes": body.notes, "id": archive_id},
    )
    db.commit()
    return {"status": "updated"}


@router.delete("/archives/{archive_id}", tags=["Archives"])
def delete_archive(
    archive_id: int,
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Delete a print archive. Admin only."""
    existing = db.execute(
        text("SELECT id FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Archive not found")

    db.execute(text("DELETE FROM print_archives WHERE id = :id"), {"id": archive_id})
    db.commit()
    return {"status": "deleted"}


# ──────────────────────────────────────────────
# Tag Management
# ──────────────────────────────────────────────

@router.patch("/archives/{archive_id}/tags", tags=["Archives"])
def update_archive_tags(
    archive_id: int,
    body: TagsUpdate,
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Set tags on an archive entry."""
    existing = db.execute(
        text("SELECT id FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Archive not found")

    # Store as comma-separated, trimmed, deduplicated
    clean = list(dict.fromkeys(t.strip() for t in body.tags if t.strip()))
    tags_str = ", ".join(clean) if clean else ""

    db.execute(
        text("UPDATE print_archives SET tags = :tags WHERE id = :id"),
        {"tags": tags_str, "id": archive_id},
    )
    db.commit()
    return {"tags": clean}


@router.get("/tags", tags=["Archives"])
def list_tags(
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Return all unique tags across archives with usage counts."""
    rows = db.execute(
        text("SELECT tags FROM print_archives WHERE tags IS NOT NULL AND tags != ''")
    ).fetchall()

    counts = {}
    for r in rows:
        for t in r[0].split(","):
            tag = t.strip()
            if tag:
                counts[tag] = counts.get(tag, 0) + 1

    return {"tags": [{"name": k, "count": v} for k, v in sorted(counts.items())]}


@router.post("/tags/rename", tags=["Archives"])
def rename_tag(
    body: TagRename,
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Bulk-rename a tag across all archives. Admin only."""
    if not body.old.strip() or not body.new.strip():
        raise HTTPException(status_code=400, detail="Tag names cannot be empty")

    old_tag = body.old.strip()
    new_tag = body.new.strip()

    rows = db.execute(
        text("SELECT id, tags FROM print_archives WHERE tags IS NOT NULL AND tags != ''")
    ).fetchall()

    updated = 0
    for r in rows:
        tags = [t.strip() for t in r[1].split(",") if t.strip()]
        if old_tag in tags:
            tags = [new_tag if t == old_tag else t for t in tags]
            # Deduplicate after rename
            tags = list(dict.fromkeys(tags))
            db.execute(
                text("UPDATE print_archives SET tags = :tags WHERE id = :id"),
                {"tags": ", ".join(tags), "id": r[0]},
            )
            updated += 1

    db.commit()
    return {"updated": updated}


@router.delete("/tags/{tag}", tags=["Archives"])
def delete_tag(
    tag: str,
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Remove a tag from all archives. Admin only."""
    tag = tag.strip()
    if not tag:
        raise HTTPException(status_code=400, detail="Tag name cannot be empty")

    rows = db.execute(
        text("SELECT id, tags FROM print_archives WHERE tags IS NOT NULL AND tags != ''")
    ).fetchall()

    updated = 0
    for r in rows:
        tags = [t.strip() for t in r[1].split(",") if t.strip()]
        if tag in tags:
            tags = [t for t in tags if t != tag]
            db.execute(
                text("UPDATE print_archives SET tags = :tags WHERE id = :id"),
                {"tags": ", ".join(tags), "id": r[0]},
            )
            updated += 1

    db.commit()
    return {"updated": updated}


# ──────────────────────────────────────────────
# Reprint with AMS Mapping
# ──────────────────────────────────────────────

@router.get("/archives/{archive_id}/ams-preview", tags=["Archives"])
def ams_preview(
    archive_id: int,
    printer_id: Optional[int] = Query(None),
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Return filament requirements from an archive's file alongside a printer's AMS state."""
    archive = db.execute(
        text("SELECT * FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not archive:
        raise HTTPException(status_code=404, detail="Archive not found")

    ad = dict(archive._mapping)

    # Try to find the associated print_file for filament metadata
    required = []
    file_id = ad.get("print_file_id")
    if file_id:
        pf = db.execute(
            text("SELECT filaments_json FROM print_files WHERE id = :id"),
            {"id": file_id},
        ).fetchone()
        if pf and pf[0]:
            try:
                filaments = json.loads(pf[0])
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
        from models import Printer, FilamentSlot
        printer = db.query(Printer).filter(Printer.id == printer_id).first()
        if printer:
            slots = db.query(FilamentSlot).filter(
                FilamentSlot.printer_id == printer_id
            ).order_by(FilamentSlot.slot_number).all()
            for s in slots:
                available.append({
                    "slot": s.slot_number,
                    "color": s.color or "",
                    "material": s.material_type or "",
                    "spool_id": s.spool_id,
                })

    # Simple auto-match: exact type+color match
    suggested = []
    for req in required:
        best = None
        confidence = "none"
        for avail in available:
            if (req["material"].lower() == avail["material"].lower()
                    and req["color"].lower() == avail["color"].lower()):
                best = avail["slot"]
                confidence = "exact"
                break
            elif req["material"].lower() == avail["material"].lower():
                if best is None:
                    best = avail["slot"]
                    confidence = "material_only"
        if best is not None:
            suggested.append({
                "source_slot": req["slot"],
                "target_slot": best,
                "confidence": confidence,
            })

    return {"required": required, "available": available, "suggested_mapping": suggested}


@router.post("/archives/{archive_id}/reprint", tags=["Archives"])
def reprint_archive(
    archive_id: int,
    body: ReprintRequest,
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Reprint an archived job on a specified printer with optional AMS remapping."""
    archive = db.execute(
        text("SELECT * FROM print_archives WHERE id = :id"), {"id": archive_id}
    ).fetchone()
    if not archive:
        raise HTTPException(status_code=404, detail="Archive not found")

    ad = dict(archive._mapping)

    # Verify we have a file to reprint (check before printer to give better error)
    file_path = ad.get("file_path")
    if not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=400,
            detail="Original print file not available for reprint",
        )

    # Verify target printer exists
    from models import Printer
    printer = db.query(Printer).filter(Printer.id == body.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Create a new job pointing to the same file
    from models import Job
    new_job = Job(
        item_name=f"Reprint: {ad.get('print_name', 'Unknown')}",
        printer_id=body.printer_id,
        priority=5,
        status="pending",
        user_id=user.get("id") if isinstance(user, dict) else getattr(user, "id", None),
    )
    db.add(new_job)
    db.flush()

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
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Stream a 3MF or STL file for 3D model preview."""
    row = db.execute(
        text("SELECT stored_path, original_filename FROM print_files WHERE id = :id"),
        {"id": file_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="File not found")

    stored_path = row[0]
    original_name = row[1] or "model"

    if not stored_path or not os.path.exists(stored_path):
        raise HTTPException(status_code=404, detail="File not available on disk")

    # Validate path is under /data/
    real_path = os.path.realpath(stored_path)
    if not real_path.startswith("/data/"):
        raise HTTPException(status_code=400, detail="Invalid file path")

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
