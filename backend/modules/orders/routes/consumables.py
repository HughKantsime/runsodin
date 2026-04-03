"""O.D.I.N. — Consumables CRUD and Stock Management."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import logging

from core.db import get_db
from core.rbac import require_role
from modules.inventory.models import Consumable, ConsumableUsage
from modules.orders.schemas import (
    ConsumableCreate, ConsumableUpdate, ConsumableResponse, ConsumableAdjust,
)

log = logging.getLogger("odin.api")

router = APIRouter(prefix="/consumables", tags=["Consumables"])


@router.get("")
def list_consumables(status: str = None, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """List all consumables with low-stock flag."""
    query = db.query(Consumable)
    if status:
        query = query.filter(Consumable.status == status)
    items = query.order_by(Consumable.name).all()
    result = []
    for c in items:
        resp = ConsumableResponse.model_validate(c)
        resp.is_low_stock = c.current_stock < c.min_stock if c.min_stock and c.min_stock > 0 else False
        result.append(resp)
    return result


@router.post("")
def create_consumable(data: ConsumableCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new consumable item."""
    item = Consumable(**data.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    resp = ConsumableResponse.model_validate(item)
    resp.is_low_stock = item.current_stock < item.min_stock if item.min_stock and item.min_stock > 0 else False
    return resp


@router.get("/low-stock")
def low_stock_consumables(current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get consumables below their minimum stock threshold."""
    items = db.query(Consumable).filter(
        Consumable.status == "active",
        Consumable.min_stock > 0,
        Consumable.current_stock < Consumable.min_stock
    ).all()
    result = []
    for c in items:
        resp = ConsumableResponse.model_validate(c)
        resp.is_low_stock = True
        result.append(resp)
    return result


@router.get("/{consumable_id}")
def get_consumable(consumable_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get a consumable with recent usage history."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    resp = ConsumableResponse.model_validate(item)
    resp.is_low_stock = item.current_stock < item.min_stock if item.min_stock and item.min_stock > 0 else False
    # Include recent usage
    usage = db.query(ConsumableUsage).filter(
        ConsumableUsage.consumable_id == consumable_id
    ).order_by(ConsumableUsage.used_at.desc()).limit(50).all()
    return {
        **resp.model_dump(),
        "usage_history": [{"id": u.id, "quantity_used": u.quantity_used, "used_at": u.used_at,
                           "order_id": u.order_id, "notes": u.notes} for u in usage]
    }


@router.patch("/{consumable_id}")
def update_consumable(consumable_id: int, data: ConsumableUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a consumable."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(item, key, val)
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(item)
    resp = ConsumableResponse.model_validate(item)
    resp.is_low_stock = item.current_stock < item.min_stock if item.min_stock and item.min_stock > 0 else False
    return resp


@router.delete("/{consumable_id}")
def delete_consumable(consumable_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a consumable."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    db.delete(item)
    db.commit()
    return {"success": True}


@router.post("/{consumable_id}/adjust")
def adjust_consumable_stock(consumable_id: int, data: ConsumableAdjust, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Manual stock adjustment (restock or deduct)."""
    item = db.query(Consumable).filter(Consumable.id == consumable_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Consumable not found")
    if data.type == "restock":
        item.current_stock += data.quantity
    else:
        item.current_stock = max(0, item.current_stock - data.quantity)
    usage = ConsumableUsage(
        consumable_id=consumable_id,
        quantity_used=data.quantity if data.type == "deduct" else -data.quantity,
        notes=data.notes or f"Manual {data.type}"
    )
    db.add(usage)
    item.updated_at = datetime.now(timezone.utc)
    db.commit()
    return {"success": True, "new_stock": item.current_stock}
