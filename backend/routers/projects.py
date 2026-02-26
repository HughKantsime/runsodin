"""O.D.I.N. — Project Routes

Group related print archives into named projects.
CRUD, bulk archive assignment, ZIP export/import.
"""

# Domain: archives
# Depends on: core, organizations
# Owns tables: projects

import io
import json
import logging
import os
import zipfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_user, require_role

log = logging.getLogger("odin.api")
router = APIRouter()


# ──────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────

class ProjectCreate(PydanticBaseModel):
    name: str
    description: Optional[str] = None
    color: str = "#6366f1"
    expected_parts: Optional[int] = None
    org_id: Optional[int] = None


class ProjectUpdate(PydanticBaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None
    status: Optional[str] = None
    expected_parts: Optional[int] = None


class BulkAssign(PydanticBaseModel):
    archive_ids: List[int]


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def _project_row_to_dict(r, archive_count=None):
    d = dict(r._mapping)
    if archive_count is not None:
        d["archive_count"] = archive_count
    return d


# ──────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────

@router.get("/projects", tags=["Projects"])
def list_projects(
    status: Optional[str] = Query(None),
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """List all projects with archive counts."""
    conditions = []
    params = {}
    if status:
        conditions.append("p.status = :status")
        params["status"] = status
    where = " AND ".join(conditions) if conditions else "1=1"

    rows = db.execute(
        text(
            f"SELECT p.*, "
            f"(SELECT COUNT(*) FROM print_archives a WHERE a.project_id = p.id) AS archive_count "
            f"FROM projects p WHERE {where} ORDER BY p.updated_at DESC"
        ),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/projects", tags=["Projects"])
def create_project(
    body: ProjectCreate,
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Create a new project."""
    uid = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    result = db.execute(
        text("""
            INSERT INTO projects (name, description, color, expected_parts, created_by, org_id)
            VALUES (:name, :desc, :color, :parts, :uid, :org)
        """),
        {
            "name": body.name, "desc": body.description, "color": body.color,
            "parts": body.expected_parts, "uid": uid, "org": body.org_id,
        },
    )
    db.commit()
    pid = result.lastrowid
    return {"id": pid, "name": body.name, "status": "active"}


@router.get("/projects/{project_id}", tags=["Projects"])
def get_project(
    project_id: int,
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Get project detail with linked archives."""
    row = db.execute(
        text("SELECT * FROM projects WHERE id = :id"), {"id": project_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    d = dict(row._mapping)

    archives = db.execute(
        text(
            "SELECT a.*, p.name AS printer_name, p.nickname AS printer_nickname "
            "FROM print_archives a "
            "LEFT JOIN printers p ON p.id = a.printer_id "
            "WHERE a.project_id = :pid ORDER BY a.created_at DESC"
        ),
        {"pid": project_id},
    ).fetchall()
    d["archives"] = [dict(a._mapping) for a in archives]
    d["archive_count"] = len(archives)
    return d


@router.put("/projects/{project_id}", tags=["Projects"])
def update_project(
    project_id: int,
    body: ProjectUpdate,
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Update a project."""
    existing = db.execute(
        text("SELECT id FROM projects WHERE id = :id"), {"id": project_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")

    updates = []
    params = {"id": project_id}
    for field in ("name", "description", "color", "status", "expected_parts"):
        val = getattr(body, field, None)
        if val is not None:
            updates.append(f"{field} = :{field}")
            params[field] = val
    updates.append("updated_at = CURRENT_TIMESTAMP")

    if updates:
        db.execute(text(f"UPDATE projects SET {', '.join(updates)} WHERE id = :id"), params)
        db.commit()
    return {"status": "updated"}


@router.delete("/projects/{project_id}", tags=["Projects"])
def delete_project(
    project_id: int,
    user=Depends(require_role("admin")),
    db: Session = Depends(get_db),
):
    """Soft-delete a project (set status to 'archived'). Admin only."""
    existing = db.execute(
        text("SELECT id FROM projects WHERE id = :id"), {"id": project_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    db.execute(
        text("UPDATE projects SET status = 'archived', updated_at = CURRENT_TIMESTAMP WHERE id = :id"),
        {"id": project_id},
    )
    db.commit()
    return {"status": "archived"}


# ──────────────────────────────────────────────
# Bulk archive assignment
# ──────────────────────────────────────────────

@router.post("/projects/{project_id}/archives", tags=["Projects"])
def assign_archives(
    project_id: int,
    body: BulkAssign,
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Bulk assign archives to a project."""
    existing = db.execute(
        text("SELECT id FROM projects WHERE id = :id"), {"id": project_id}
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")

    if not body.archive_ids:
        raise HTTPException(status_code=400, detail="archive_ids cannot be empty")

    # Validate all archive IDs exist
    placeholders = ",".join(str(int(aid)) for aid in body.archive_ids)
    found = db.execute(
        text(f"SELECT id FROM print_archives WHERE id IN ({placeholders})")
    ).fetchall()
    found_ids = {r[0] for r in found}
    missing = set(body.archive_ids) - found_ids
    if missing:
        raise HTTPException(status_code=400, detail=f"Archive IDs not found: {sorted(missing)}")

    db.execute(
        text(f"UPDATE print_archives SET project_id = :pid WHERE id IN ({placeholders})"),
        {"pid": project_id},
    )
    db.commit()
    return {"assigned": len(body.archive_ids)}


# ──────────────────────────────────────────────
# Export / Import
# ──────────────────────────────────────────────

@router.get("/projects/{project_id}/export", tags=["Projects"])
def export_project(
    project_id: int,
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Export project as ZIP containing metadata JSON and linked 3MF files."""
    row = db.execute(
        text("SELECT * FROM projects WHERE id = :id"), {"id": project_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")

    project = dict(row._mapping)
    archives = db.execute(
        text("SELECT * FROM print_archives WHERE project_id = :pid"),
        {"pid": project_id},
    ).fetchall()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        project["archives"] = [dict(a._mapping) for a in archives]
        # Serialize datetimes to strings
        meta = json.dumps(project, default=str, indent=2)
        zf.writestr("project.json", meta)

        for a in archives:
            ad = dict(a._mapping)
            fp = ad.get("file_path")
            if fp and os.path.exists(fp):
                zf.write(fp, f"files/{os.path.basename(fp)}")

    buf.seek(0)
    safe_name = project["name"].replace(" ", "_")[:50]
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=project_{safe_name}.zip"},
    )


@router.post("/projects/import", tags=["Projects"])
async def import_project(
    file: UploadFile = File(...),
    user=Depends(require_role("operator")),
    db: Session = Depends(get_db),
):
    """Import a project from a ZIP export."""
    if not file.filename or not file.filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload must be a .zip file")

    contents = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(contents))
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="Invalid ZIP file")

    if "project.json" not in zf.namelist():
        raise HTTPException(status_code=400, detail="ZIP must contain project.json")

    meta = json.loads(zf.read("project.json"))
    uid = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)

    result = db.execute(
        text("""
            INSERT INTO projects (name, description, color, expected_parts, created_by, status)
            VALUES (:name, :desc, :color, :parts, :uid, 'active')
        """),
        {
            "name": meta.get("name", "Imported Project"),
            "desc": meta.get("description"),
            "color": meta.get("color", "#6366f1"),
            "parts": meta.get("expected_parts"),
            "uid": uid,
        },
    )
    db.commit()
    return {"id": result.lastrowid, "name": meta.get("name"), "status": "imported"}
