"""O.D.I.N. — Project Routes

Group related print archives into named projects.
CRUD, bulk archive assignment, ZIP export/import.
"""

from core.rbac import should_filter_org

# Domain: archives
# Depends on: core, organizations
# Owns tables: projects

import io
import json
import logging
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text, bindparam
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from core.db import get_db
from core.db_compat import execute_insert_returning_id
from core.dependencies import get_current_user
from core.rbac import check_org_access, get_org_scope, require_role
from core.interfaces.education_policy import generic_archive_audience_sql
from modules.archives.project_zip_admission import MAX_COMPRESSED_BYTES, validate_project_zip
from modules.archives.project_zip_io import (
    ProjectZipExportError,
    build_project_zip,
    discard_import_dir,
    stage_import_files,
)

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


def _check_project_org_access(db, project_row, current_user):
    """Check if user can access a project via its org_id."""
    org_id = project_row.org_id if hasattr(project_row, "org_id") else dict(project_row._mapping).get("org_id")
    if not check_org_access(current_user, org_id):
        raise HTTPException(status_code=404, detail="Project not found")


def _locked_project(db, project_id):
    dialect = db.get_bind().dialect.name
    if dialect == 'sqlite':
        # Even an existing deferred transaction must acquire a real write lock.
        try:
            db.execute(text("UPDATE projects SET id = id WHERE 1 = 0"))
        except OperationalError:
            db.rollback()
            raise HTTPException(status_code=409, detail="Project changed concurrently; retry") from None
    suffix = " FOR UPDATE" if dialect == 'postgresql' else ""
    return db.execute(text("SELECT * FROM projects WHERE id = :id" + suffix), {"id": project_id}).fetchone()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant dialect lock suffix is appended; values are bound


def _lock_assignment_owners(db, archive_ids):
    # Ownership is anchored on the archive, not a mutable printer row.
    for archive_id in sorted(archive_ids):
        db.execute(text("UPDATE print_archives SET id=id WHERE id=:id"), {"id": archive_id})


from modules.archives.access import archive_visibility as _archive_visibility, assert_archive_access


def _check_assignment_archive(db, row, user):
    assert_archive_access(db, row.id, user)


# ──────────────────────────────────────────────
# CRUD
# ──────────────────────────────────────────────

@router.get("/projects", tags=["Projects"])
def list_projects(
    status: Optional[str] = Query(None),
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """List all projects with archive counts."""
    conditions = []
    params = {}
    if status:
        conditions.append("p.status = :status")
        params["status"] = status

    # Org scoping — projects have org_id directly
    org = get_org_scope(user)
    if should_filter_org(user, org):
        conditions.append("(p.org_id = :_org OR p.org_id IS NULL)")
        params["_org"] = org

    archive_scope, archive_params = _archive_visibility(user)
    params.update(archive_params)
    where = " AND ".join(conditions) if conditions else "1=1"

    rows = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
            f"SELECT p.*, "
            f"(SELECT COUNT(*) FROM print_archives a LEFT JOIN printers ap ON ap.id = a.printer_id "
            f"WHERE a.project_id = p.id AND {archive_scope}) AS archive_count "
            f"FROM projects p WHERE {where} ORDER BY p.updated_at DESC"
        ),
        params,
    ).fetchall()
    return [dict(r._mapping) for r in rows]


@router.post("/projects", tags=["Projects"])
def create_project(
    body: ProjectCreate,
    user=Depends(require_role("operator", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Create a new project."""
    uid = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    # Default org_id to user's org scope if not explicitly provided
    org_id = body.org_id if body.org_id is not None else get_org_scope(user)
    if not check_org_access(user, org_id):
        raise HTTPException(status_code=404, detail="Organization not found")
    pid = execute_insert_returning_id(
        db,
        """
            INSERT INTO projects (name, description, color, expected_parts, created_by, org_id)
            VALUES (:name, :desc, :color, :parts, :uid, :org)
        """,
        {
            "name": body.name, "desc": body.description, "color": body.color,
            "parts": body.expected_parts, "uid": uid, "org": org_id,
        },
    )
    db.commit()
    return {"id": pid, "name": body.name, "status": "active"}


@router.get("/projects/{project_id}", tags=["Projects"])
def get_project(
    project_id: int,
    user=Depends(require_role("viewer", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Get project detail with linked archives."""
    row = db.execute(
        text("SELECT * FROM projects WHERE id = :id"), {"id": project_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_org_access(db, row, user)
    d = dict(row._mapping)

    archive_scope, archive_params = _archive_visibility(user)
    archives = db.execute(
        text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- predicate is code-built from constant SQL fragments; principal values are bound
            "SELECT a.*, p.name AS printer_name, p.nickname AS printer_nickname "
            "FROM print_archives a "
            "LEFT JOIN printers p ON p.id = a.printer_id "
            "LEFT JOIN printers ap ON ap.id = a.printer_id "
            f"WHERE a.project_id = :pid AND {archive_scope} ORDER BY a.created_at DESC, a.id DESC"
        ),
        {"pid": project_id, **archive_params},
    ).fetchall()
    d["archives"] = [dict(a._mapping) for a in archives]
    d["archive_count"] = len(archives)
    return d


@router.put("/projects/{project_id}", tags=["Projects"])
def update_project(
    project_id: int,
    body: ProjectUpdate,
    user=Depends(require_role("operator", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Update a project."""
    existing = _locked_project(db, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_org_access(db, existing, user)

    updates = []
    params = {"id": project_id}
    for field in ("name", "description", "color", "status", "expected_parts"):
        val = getattr(body, field, None)
        if val is not None:
            updates.append(f"{field} = :{field}")
            params[field] = val
    updates.append("updated_at = CURRENT_TIMESTAMP")

    if updates:
        db.execute(text(f"UPDATE projects SET {', '.join(updates)} WHERE id = :id"), params)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        db.commit()
    return {"status": "updated"}


@router.delete("/projects/{project_id}", tags=["Projects"])
def delete_project(
    project_id: int,
    user=Depends(require_role("admin", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Soft-delete a project (set status to 'archived'). Admin only."""
    existing = _locked_project(db, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_org_access(db, existing, user)
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
    user=Depends(require_role("operator", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Bulk assign archives to a project."""
    existing = _locked_project(db, project_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_org_access(db, existing, user)

    if not body.archive_ids:
        raise HTTPException(status_code=400, detail="archive_ids cannot be empty")

    archive_ids = sorted(set(body.archive_ids))
    _lock_assignment_owners(db, archive_ids)
    found = db.execute(
        text("SELECT a.id, a.printer_id, ap.id AS owner_printer_id, ap.org_id AS owner_org "
             "FROM print_archives a LEFT JOIN printers ap ON ap.id = a.printer_id "
             "WHERE a.id IN :archive_ids").bindparams(bindparam("archive_ids", expanding=True)),
        {"archive_ids": archive_ids},
    ).fetchall()
    if len(found) != len(archive_ids):
        raise HTTPException(status_code=404, detail="Archive not found")
    for row in found:
        _check_assignment_archive(db, row, user)
    db.execute(
        text("UPDATE print_archives SET project_id = :pid WHERE id IN :archive_ids")
        .bindparams(bindparam("archive_ids", expanding=True)),
        {"pid": project_id, "archive_ids": archive_ids},
    )
    db.commit()
    return {"assigned": len(archive_ids)}


# ──────────────────────────────────────────────
# Export / Import
# ──────────────────────────────────────────────

@router.get("/projects/{project_id}/export", tags=["Projects"])
def export_project(
    project_id: int,
    user=Depends(require_role("operator", scope="read:jobs")),
    db: Session = Depends(get_db),
):
    """Export project as a v2 ZIP: closed manifest plus hash-bound print files."""
    row = db.execute(
        text("SELECT * FROM projects WHERE id = :id"), {"id": project_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    _check_project_org_access(db, row, user)

    project = dict(row._mapping)
    archive_scope, archive_params = _archive_visibility(user)
    archives = db.execute(
        text(f"SELECT a.*, CASE WHEN {generic_archive_audience_sql()} THEN 0 ELSE 1 END AS education_linked "  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- predicate is code-built from constant SQL fragments; principal values are bound
             "FROM print_archives a LEFT JOIN printers ap ON ap.id = a.printer_id "
             f"WHERE a.project_id = :pid AND {archive_scope} ORDER BY a.id"),
        {"pid": project_id, **archive_params},
    ).fetchall()

    try:
        payload = build_project_zip(project, [dict(a._mapping) for a in archives])
    except ProjectZipExportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in project["name"])[:50]
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=project_{safe_name}.zip"},
    )


@router.post("/projects/import", tags=["Projects"])
def import_project(
    file: UploadFile = File(...),
    user=Depends(require_role("operator", scope="write:jobs")),
    db: Session = Depends(get_db),
):
    """Restore a v2 project ZIP as a new project owned by the importer."""
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload must be a .zip file")

    # Sync handler: validation, hashing and fsync run in the threadpool, not the event loop.
    contents = file.file.read(MAX_COMPRESSED_BYTES + 1)
    try:
        validated = validate_project_zip(contents)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid, unsupported or oversized project ZIP") from None
    manifest = validated.manifest
    if any(a.protection != "generic" for a in manifest.archives):
        raise HTTPException(status_code=400, detail="Education-protected history cannot be imported")
    if any("," in tag for a in manifest.archives for tag in a.display.tags):
        raise HTTPException(status_code=400, detail="Archive tags cannot contain commas")

    uid = user.get("id") if isinstance(user, dict) else getattr(user, "id", None)
    org_id = get_org_scope(user)
    import_dir, placed = None, {}
    try:
        import_dir, placed = stage_import_files(contents, manifest)
        project_id = execute_insert_returning_id(
            db,
            """
                INSERT INTO projects (name, description, color, expected_parts, created_by, status, org_id)
                VALUES (:name, :desc, :color, :parts, :uid, 'active', :org)
            """,
            {
                "name": manifest.project.name,
                "desc": manifest.project.description,
                "color": manifest.project.color,
                "parts": manifest.project.expected_parts,
                "uid": uid,
                "org": org_id,
            },
        )
        for archive in manifest.archives:
            display = archive.display
            path, digest = placed.get(archive.archive_key, (None, None))
            completed = display.printed_at.replace("T", " ").rstrip("Z") if display.printed_at else None
            db.execute(
                text(
                    "INSERT INTO print_archives (print_name, status, completed_at, actual_duration_seconds, "
                    "filament_used_grams, notes, tags, file_path, file_hash, project_id, user_id, org_id, "
                    "ownership_state, ownership_source, accounting_state, accounting_reason) "
                    "VALUES (:name, :status, :completed, :seconds, :grams, :notes, :tags, :path, :hash, "
                    ":project, :uid, :org, 'known', 'project_import', 'unresolved', 'project_import')"
                ),
                {
                    "name": display.print_name,
                    "status": display.status,
                    "completed": completed,
                    "seconds": int(display.actual_print_seconds) if display.actual_print_seconds is not None else None,
                    "grams": display.filament_used_g,
                    "notes": display.notes,
                    "tags": ",".join(display.tags),
                    "path": path,
                    "hash": digest,
                    "project": project_id,
                    "uid": uid,
                    "org": org_id,
                },
            )
        db.commit()
    except BaseException:
        db.rollback()
        discard_import_dir(import_dir)
        raise
    return {
        "id": project_id,
        "name": manifest.project.name,
        "status": "imported",
        "archives": len(manifest.archives),
        "files": len(placed),
    }
