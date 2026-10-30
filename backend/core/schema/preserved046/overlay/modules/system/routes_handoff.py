"""
modules/system/routes_handoff.py — NSUserActivity Handoff state endpoint.

Used by native Apple apps to populate NSUserActivity for Handoff between
iPhone, iPad, and macOS. Returns a minimal payload sufficient to restore
the exact screen on the receiving device without an additional fetch.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import text

from core.db import get_db
from core.dependencies import get_current_user

log = logging.getLogger("odin.handoff")
router = APIRouter(tags=["Handoff"])


@router.get("/handoff")
def get_handoff_state(
    resource: str = Query(..., pattern="^(printer|job|model|spool|order|archive)$"),
    id: int = Query(..., gt=0),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return a minimal NSUserActivity payload for Apple Handoff.

    The client emits an NSUserActivity when the user views a resource detail screen.
    When Handoff fires on another device, it calls this endpoint to get the data
    needed to restore the same screen.

    Response: {resource, id, title, subtitle, deep_link_path}
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")

    payload = _fetch_handoff_payload(resource, id, db)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"{resource} {id} not found")

    return payload


def _fetch_handoff_payload(resource: str, resource_id: int, db: Session) -> Optional[dict]:
    handlers = {
        "printer": _printer_payload,
        "job": _job_payload,
        "model": _model_payload,
        "spool": _spool_payload,
        "order": _order_payload,
        "archive": _archive_payload,
    }
    return handlers[resource](resource_id, db)


def _printer_payload(printer_id: int, db: Session) -> Optional[dict]:
    row = db.execute(
        text("SELECT id, name, model, gcode_state FROM printers WHERE id = :id"),
        {"id": printer_id},
    ).fetchone()
    if not row:
        return None
    return {
        "resource": "printer",
        "id": row.id,
        "title": row.name,
        "subtitle": f"{row.model or 'Printer'} · {row.gcode_state or 'Unknown'}",
        "deep_link_path": f"/fleet/{row.id}",
    }


def _job_payload(job_id: int, db: Session) -> Optional[dict]:
    row = db.execute(
        text("""
            SELECT j.id, j.item_name, j.status, p.name AS printer_name
            FROM jobs j
            LEFT JOIN printers p ON j.printer_id = p.id
            WHERE j.id = :id
        """),
        {"id": job_id},
    ).fetchone()
    if not row:
        return None
    return {
        "resource": "job",
        "id": row.id,
        "title": row.item_name or f"Job #{row.id}",
        "subtitle": f"{row.printer_name or 'Unassigned'} · {row.status}",
        "deep_link_path": f"/queue/{row.id}",
    }


def _model_payload(model_id: int, db: Session) -> Optional[dict]:
    row = db.execute(
        text("SELECT id, name, filament_type FROM models WHERE id = :id"),
        {"id": model_id},
    ).fetchone()
    if not row:
        return None
    return {
        "resource": "model",
        "id": row.id,
        "title": row.name or f"Model #{row.id}",
        "subtitle": row.filament_type or "Unknown material",
        "deep_link_path": f"/models/{row.id}",
    }


def _spool_payload(spool_id: int, db: Session) -> Optional[dict]:
    row = db.execute(
        text("""
            SELECT s.id, fl.name AS filament_name, s.remaining_weight_g, s.status
            FROM spools s
            LEFT JOIN filament_library fl ON s.filament_id = fl.id
            WHERE s.id = :id
        """),
        {"id": spool_id},
    ).fetchone()
    if not row:
        return None
    remaining = f"{round(row.remaining_weight_g)}g remaining" if row.remaining_weight_g else ""
    return {
        "resource": "spool",
        "id": row.id,
        "title": row.filament_name or f"Spool #{row.id}",
        "subtitle": remaining,
        "deep_link_path": f"/inventory/spools/{row.id}",
    }


def _order_payload(order_id: int, db: Session) -> Optional[dict]:
    row = db.execute(
        text("SELECT id, order_number, customer_name, status FROM orders WHERE id = :id"),
        {"id": order_id},
    ).fetchone()
    if not row:
        return None
    return {
        "resource": "order",
        "id": row.id,
        "title": f"Order {row.order_number or row.id}",
        "subtitle": f"{row.customer_name or 'Customer'} · {row.status}",
        "deep_link_path": f"/orders/{row.id}",
    }


def _archive_payload(archive_id: int, db: Session) -> Optional[dict]:
    row = db.execute(
        text("""
            SELECT a.id, a.model_name, a.status, p.name AS printer_name
            FROM archives a
            LEFT JOIN printers p ON a.printer_id = p.id
            WHERE a.id = :id
        """),
        {"id": archive_id},
    ).fetchone()
    if not row:
        return None
    return {
        "resource": "archive",
        "id": row.id,
        "title": row.model_name or f"Archive #{row.id}",
        "subtitle": f"{row.printer_name or 'Unknown'} · {row.status}",
        "deep_link_path": f"/archives/{row.id}",
    }
