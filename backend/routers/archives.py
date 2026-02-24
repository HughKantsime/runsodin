"""O.D.I.N. — Print Archive Routes

CRUD for print archives: searchable history of completed prints.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import get_db, get_current_user, require_role

log = logging.getLogger("odin.api")
router = APIRouter()


class ArchiveNotesUpdate(PydanticBaseModel):
    notes: Optional[str] = None


@router.get("/archives", tags=["Archives"])
def list_archives(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    printer_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    user=Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """List print archives with pagination and filters."""
    conditions = []
    params = {}

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

    items = []
    for r in rows:
        d = dict(r._mapping)
        d["printer_display"] = d.get("printer_nickname") or d.get("printer_name") or f"Printer {d.get('printer_id')}"
        items.append(d)

    return {"items": items, "total": total, "page": page, "per_page": per_page}


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
    d = dict(row._mapping)
    d["printer_display"] = d.get("printer_nickname") or d.get("printer_name") or f"Printer {d.get('printer_id')}"
    return d


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
