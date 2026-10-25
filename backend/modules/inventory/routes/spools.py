"""Spool CRUD and action endpoints (load/unload/use/weigh/drying)."""

from modules.inventory.slot_assignments import assign_slot, lock_slot_graph, unload_spool_graph
from core.spool_purchase_evidence import read_purchase, write_purchase, require_financial_owner
from core.rbac import should_filter_org, require_action

from typing import Optional
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.errors import ErrorCode, OdinError
from core.middleware.dry_run import dry_run_preview, is_dry_run
from core.rbac import (
    AGENT_READ_SCOPE,
    AGENT_WRITE_SCOPE,
    _get_org_filter,
    check_org_access,
    get_org_scope,
    require_any_scope,
    require_role,
)
from core.responses import build_next_actions, next_action
from core.base import SpoolStatus
from modules.inventory.models import Spool
from modules.inventory.services import OTHER_ORG_SPOOL, spoolman_link_conflicts, visible_spoolman_link_conflicts  # noqa: F401
from modules.printers.models import FilamentSlot, Printer
from core.interfaces.filament_ownership import ensure_local_filament_management
from ._helpers import (
    SpoolCreate, SpoolUpdate,
    SpoolLoadRequest, SpoolUseRequest, SpoolWeighRequest,
)

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/spools", tags=["Spools"])


# ====================================================================
# Spool CRUD
# ====================================================================

@router.get("/low-stock", tags=["Spools"])
def low_stock_spools(
    current_user: dict = Depends(require_role("viewer", scope="read:spools")),
    db: Session = Depends(get_db),
):
    """List spools below their low-stock threshold."""
    query = db.query(Spool).filter(Spool.status == SpoolStatus.ACTIVE)
    effective_org = get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter((Spool.org_id == effective_org) | (Spool.org_id == None))
    spools = query.all()
    result = []
    for s in spools:
        threshold = s.low_stock_threshold_g if s.low_stock_threshold_g is not None else 50
        remaining = s.remaining_weight_g or 0
        if remaining < threshold:
            result.append({
                "id": s.id,
                "filament_brand": s.filament.brand if s.filament else None,
                "filament_name": s.filament.name if s.filament else None,
                "filament_material": s.filament.material if s.filament else None,
                "remaining_weight_g": remaining,
                "low_stock_threshold_g": threshold,
                "location_printer_id": s.location_printer_id,
            })
    return result


@router.get("", tags=["Spools"])
def list_spools(
    status: Optional[str] = None,
    filament_id: Optional[int] = None,
    printer_id: Optional[int] = None,
    org_id: Optional[int] = None,
    # Stacked auth (Phase 2 canonical read shape).
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE, "read:spools")
    ),
    db: Session = Depends(get_db),
):
    """List all spools with optional filters."""
    query = db.query(Spool)

    if status:
        query = query.filter(Spool.status == status)
    if filament_id:
        query = query.filter(Spool.filament_id == filament_id)
    if printer_id:
        query = query.filter(Spool.location_printer_id == printer_id)

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter((Spool.org_id == effective_org) | (Spool.org_id == None))

    spools = query.all()

    result = []
    for s in spools:
        spool_dict = {
            "id": s.id,
            "filament_id": s.filament_id,
            "qr_code": s.qr_code,
            "initial_weight_g": s.initial_weight_g,
            "remaining_weight_g": s.remaining_weight_g,
            "spool_weight_g": s.spool_weight_g,
            "percent_remaining": s.percent_remaining,
            "price": s.price,
            "purchase_date": s.purchase_date,
            "vendor": s.vendor,
            "lot_number": s.lot_number,
            "status": s.status.value if s.status else None,
            "location_printer_id": s.location_printer_id,
            "location_slot": s.location_slot,
            "storage_location": s.storage_location,
            "notes": s.notes,
            "created_at": s.created_at,
            "filament_brand": s.filament.brand if s.filament else None,
            "filament_name": s.filament.name if s.filament else None,
            "filament_material": s.filament.material if s.filament else None,
            "filament_color_hex": s.color_hex or (s.filament.color_hex if s.filament else None),
            "pa_profile": s.pa_profile,
            "low_stock_threshold_g": s.low_stock_threshold_g,
            "spoolman_spool_id": s.spoolman_spool_id,
            "is_low_stock": (s.remaining_weight_g or 0) < (s.low_stock_threshold_g if s.low_stock_threshold_g is not None else 50),
        }
        result.append(spool_dict)

    return result


SPOOLMAN_LINK_LOCK_CLASS = 1330071887  # pg_advisory_xact_lock(class, spoolman_spool_id) namespace


def _serialize_spoolman_link(db: Session, spoolman_spool_id) -> None:
    """Serialise check-then-write for one Spoolman spool id until this transaction ends.

    PostgreSQL: a transaction-scoped advisory lock keyed by the (int32) Spoolman id.
    SQLite: BEGIN IMMEDIATE takes the single writer lock before the check (a transaction
    that already wrote holds it). There is deliberately no unique index: 1.9.x data may
    hold duplicates, which are reported to admins and never rewritten.
    """
    if spoolman_spool_id is None:
        return
    connection = db.connection()
    dialect = connection.dialect.name
    if dialect == "postgresql":
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock_class, :key)"),
                           {"lock_class": SPOOLMAN_LINK_LOCK_CLASS, "key": int(spoolman_spool_id)})
    elif dialect == "sqlite":
        raw = connection.connection.driver_connection
        if not raw.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        raise HTTPException(status_code=500, detail="Unsupported database for Spoolman link serialisation")


def _refuse_taken_spoolman_link(db: Session, spoolman_spool_id, own_id=None, current_user=None) -> None:
    """A Spoolman spool links to at most one ODIN spool install-wide (one Spoolman per install).
    Call after _serialize_spoolman_link in the same transaction; 409 names the holder."""
    if spoolman_spool_id is None:
        return
    query = db.query(Spool.id, Spool.qr_code, Spool.org_id).filter(Spool.spoolman_spool_id == spoolman_spool_id)
    if own_id is not None:
        query = query.filter(Spool.id != own_id)
    holder = query.order_by(Spool.id).first()
    if holder is None:
        return
    if current_user is not None and check_org_access(current_user, holder.org_id):
        where = f"spool #{holder.id} ({holder.qr_code})"
    else:
        where = OTHER_ORG_SPOOL
    raise HTTPException(status_code=409, detail=f"Spoolman spool {spoolman_spool_id} is already linked to {where}")


@router.get("/spoolman-link-conflicts", tags=["Spools"])
def get_spoolman_link_conflicts(current_user: dict = Depends(require_role("admin", scope="read:spools")), db: Session = Depends(get_db)):
    """Admin warning: Spoolman spools linked to more than one ODIN spool (usage would be pushed twice)."""
    return visible_spoolman_link_conflicts(db, current_user)


@router.post("", tags=["Spools"])
def create_spool(spool: SpoolCreate, current_user: dict = Depends(require_action("spools.edit", scope="write:spools")), db: Session = Depends(get_db)):
    """Create a new spool."""
    return _create_spool_record(spool, current_user, db)


def _create_spool_record(spool, current_user, db, *, commit=True):
    from modules.inventory.models import FilamentLibrary
    # Verify filament exists
    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == spool.filament_id).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")

    _serialize_spoolman_link(db, spool.spoolman_spool_id)
    _refuse_taken_spoolman_link(db, spool.spoolman_spool_id, current_user=current_user)

    # Generate QR code
    import uuid
    qr_code = f"SPL-{uuid.uuid4().hex[:8].upper()}"

    values = dict(
        filament_id=spool.filament_id,
        qr_code=qr_code,
        initial_weight_g=spool.initial_weight_g,
        remaining_weight_g=spool.initial_weight_g,
        spool_weight_g=spool.spool_weight_g,
        price=spool.price,
        purchase_date=spool.purchase_date,
        vendor=spool.vendor,
        lot_number=spool.lot_number,
        storage_location=spool.storage_location,
        notes=spool.notes,
        pa_profile=spool.pa_profile,
        low_stock_threshold_g=spool.low_stock_threshold_g,
        spoolman_spool_id=spool.spoolman_spool_id,
        status=SpoolStatus.ACTIVE,
        org_id=current_user.get("group_id") if current_user else None,
    )
    # Preserve mapped bind semantics (notably SQLite datetime formatting) while
    # the reviewed factory owns the fresh INSERT and immutable birth evidence.
    dialect = db.get_bind().dialect
    for name, value in list(values.items()):
        processor = Spool.__table__.c[name].type.dialect_impl(dialect).bind_processor(dialect)
        if processor is not None:
            values[name] = processor(value)
    from core.economic_source_creation import _create_spool
    owner = values["org_id"]
    actor_org = current_user.get("group_id")
    global_authority = current_user.get("role") == "admin" and actor_org is None
    attestation = {}
    if type(owner) is int and owner > 0 and (
        (type(actor_org) is int and actor_org == owner) or global_authority
    ):
        attestation = {"owner_org_id": owner}
    elif owner is None and global_authority:
        attestation = {"owner_org_id": None, "global_authority": True}
    created = _create_spool(db, values, **attestation)
    if spool.purchase_evidence is not None:
        write_purchase(db, created.id, current_user, spool.purchase_evidence, 0)
    db_spool = db.get(Spool, created.id)
    if db_spool is None:
        raise RuntimeError("Fresh spool could not be loaded")
    if spool.spoolman_spool_id is not None:
        # Not an economic birth field; linked after the factory INSERT in the same transaction.
        db_spool.spoolman_spool_id = spool.spoolman_spool_id

    log_audit(db, "create", "spool", db_spool.id, {"filament_id": spool.filament_id, "qr_code": db_spool.qr_code})
    if commit:
        db.commit()
    return {
        "id": db_spool.id,
        "qr_code": db_spool.qr_code,
        "message": "Spool created",
    }


@router.get("/{spool_id}/purchase-evidence", tags=["Spools"])
def get_spool_purchase_evidence(spool_id: int, current_user: dict = Depends(require_role("viewer", scope="read:spools")), db: Session = Depends(get_db)):
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool or not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")
    return read_purchase(db, spool_id, current_user)


@router.get("/{spool_id}", tags=["Spools"])
def get_spool(spool_id: int, current_user: dict = Depends(require_role("viewer", scope="read:spools")), db: Session = Depends(get_db)):
    """Get a single spool with details."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    return {
        "id": spool.id,
        "filament_id": spool.filament_id,
        "qr_code": spool.qr_code,
        "initial_weight_g": spool.initial_weight_g,
        "remaining_weight_g": spool.remaining_weight_g,
        "spool_weight_g": spool.spool_weight_g,
        "percent_remaining": spool.percent_remaining,
        "price": spool.price,
        "purchase_date": spool.purchase_date,
        "vendor": spool.vendor,
        "lot_number": spool.lot_number,
        "status": spool.status.value if spool.status else None,
        "location_printer_id": spool.location_printer_id,
        "location_slot": spool.location_slot,
        "storage_location": spool.storage_location,
        "notes": spool.notes,
        "pa_profile": spool.pa_profile,
        "low_stock_threshold_g": spool.low_stock_threshold_g,
        "spoolman_spool_id": spool.spoolman_spool_id,
        "is_low_stock": (spool.remaining_weight_g or 0) < (spool.low_stock_threshold_g if spool.low_stock_threshold_g is not None else 50),
        "spoolman_spool_id": spool.spoolman_spool_id,
        "created_at": spool.created_at,
        "updated_at": spool.updated_at,
        "filament_brand": spool.filament.brand if spool.filament else None,
        "filament_name": spool.filament.name if spool.filament else None,
        "filament_material": spool.filament.material if spool.filament else None,
        "filament_color_hex": spool.filament.color_hex if spool.filament else None,
        "usage_history": [
            {
                "id": u.id,
                "weight_used_g": u.weight_used_g,
                "used_at": u.used_at,
                "job_id": u.job_id,
                "notes": u.notes,
            }
            for u in spool.usage_history
        ],
    }


@router.patch("/{spool_id}", tags=["Spools"])
def update_spool(spool_id: int, updates: SpoolUpdate, current_user: dict = Depends(require_action("spools.edit", scope="write:spools")), db: Session = Depends(get_db)):
    """Update spool details."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    update_data = updates.model_dump(exclude_unset=True)
    # Lock order on every spool write path: the Spoolman-link lock first, then the
    # purchase/financial locks (write_purchase, require_financial_owner).
    if "spoolman_spool_id" in update_data:
        if update_data["spoolman_spool_id"] == spool.spoolman_spool_id:
            # Unchanged link (even an existing duplicate): not a link write.
            del update_data["spoolman_spool_id"]
        else:
            _serialize_spoolman_link(db, update_data["spoolman_spool_id"])
            _refuse_taken_spoolman_link(db, update_data["spoolman_spool_id"], spool.id, current_user)
    purchase_operation = "purchase_evidence" in update_data
    purchase = update_data.pop("purchase_evidence", None)
    expected = update_data.pop("expected_purchase_revision", None)
    if "price" in update_data and update_data["price"] != spool.price:
        require_financial_owner(db, spool_id, current_user, legacy_allowed=True)
    if purchase_operation:
        legacy_fields = {"price": update_data.pop("price")} if "price" in update_data else None
        write_purchase(db, spool_id, current_user, purchase, expected, legacy_fields=legacy_fields)
        log_audit(db, "spool.purchase_evidence_updated", "spool", spool_id,
                  {"state": "cleared" if purchase is None else "recorded"})


    if "status" in update_data:
        update_data["status"] = SpoolStatus(update_data["status"])

    for field, value in update_data.items():
        setattr(spool, field, value)

    db.commit()
    db.refresh(spool)

    return {"success": True, "id": spool.id}


@router.delete("/{spool_id}", tags=["Spools"])
def delete_spool(spool_id: int, current_user: dict = Depends(require_action("spools.delete", scope="write:spools")), db: Session = Depends(get_db)):
    """Delete a spool (or archive it)."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    # Archive instead of delete
    spool.status = SpoolStatus.ARCHIVED
    db.commit()

    return {"success": True, "message": "Spool archived"}


# ====================================================================
# Spool Actions (load/unload/use/weigh)
# ====================================================================

@router.post("/{spool_id}/load", tags=["Spools"])
def load_spool(spool_id: int, request: SpoolLoadRequest, current_user: dict = Depends(require_action("spools.edit", scope="write:spools")), db: Session = Depends(get_db)):
    """Load a spool into a printer slot."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    printer = db.query(Printer).filter(Printer.id == request.printer_id).first()
    if not printer or not (check_org_access(current_user, printer.org_id) or printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    ensure_local_filament_management(request.printer_id)

    if request.slot_number < 1 or request.slot_number > printer.slot_count:
        raise HTTPException(status_code=400, detail=f"Invalid slot number (1-{printer.slot_count})")

    lock_slot_graph(db)
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == request.printer_id,
        FilamentSlot.slot_number == request.slot_number,
    ).first()
    if not slot:
        slot = FilamentSlot(printer_id=request.printer_id, slot_number=request.slot_number)
        db.add(slot)
    assign_slot(db, slot, spool_id, current_user)
    slot.spool_confirmed = True

    db.commit()

    return {
        "success": True,
        "spool_id": spool_id,
        "printer": printer.name,
        "slot": request.slot_number,
    }


@router.post("/{spool_id}/unload", tags=["Spools"])
def unload_spool(
    spool_id: int,
    storage_location: Optional[str] = None,
    current_user: dict = Depends(require_action("spools.edit", scope="write:spools")), db: Session = Depends(get_db)
):
    """Unload a spool from printer to storage."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    unload_spool_graph(db, spool, current_user)
    spool.storage_location = storage_location

    db.commit()

    return {"success": True, "message": "Spool unloaded"}


@router.post("/{spool_id}/use", tags=["Spools"])
@router.patch("/{spool_id}/use", tags=["Spools"])
def use_spool(
    spool_id: int,
    body: SpoolUseRequest,
    request: Request,
    # Stacked auth (Phase 2 canonical) — param name `body` so `request` is
    # the raw FastAPI Request (middleware dry-run flag lives here).
    current_user: dict = Depends(require_action("spools.edit")),
    _agent_scope: dict = Depends(require_any_scope("admin", AGENT_WRITE_SCOPE, "write:spools")),
    db: Session = Depends(get_db),
):
    """Record filament usage from a spool (consume_spool).

    Agent-surface v1.9.0 Phase 2. Accepts both POST (legacy) and PATCH
    (MCP's consume_spool shape) on the same handler. Body accepts either
    `weight_used_g` (legacy) or `grams` (MCP) — resolved before use.
    """
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool or not check_org_access(current_user, spool.org_id):
        raise OdinError(
            ErrorCode.spool_not_found,
            f"Spool {spool_id} not found",
            status=404,
        )

    try:
        grams = body.resolved_grams
    except ValueError as e:
        raise OdinError(
            ErrorCode.validation_failed,
            str(e),
            status=400,
            retriable=False,
        )

    import math
    if not math.isfinite(grams) or grams <= 0:
        raise OdinError(
            ErrorCode.validation_failed,
            "Filament usage must be a positive number of grams",
            status=400,
            retriable=False,
        )

    current_remaining = spool.remaining_weight_g
    new_remaining = max(0, current_remaining - grams)
    will_deplete = new_remaining <= 0

    if is_dry_run(request):
        return dry_run_preview(
            would_execute={
                "action": "consume_spool",
                "spool_id": spool_id,
                "grams_consumed": grams,
                "current_remaining_g": current_remaining,
                "new_remaining_g": new_remaining,
                "would_deplete_to_empty": will_deplete,
                "would_insert_usage_row": True,
                "job_id": body.job_id,
            },
            next_actions=[
                next_action("list_spools", {}, "verify remaining weights"),
            ],
            notes="Would update spools.remaining_weight_g and insert a spool_usage row.",
        )

    # The filament ledger records it as a manual adjustment: it never counts as
    # the named job's automatic deduction (that job's completion still deducts).
    from core.filament_ledger import record_manual_usage
    from core.print_attempts import sqlalchemy_runner
    db.flush()
    record_manual_usage(sqlalchemy_runner(db), spool.id, grams, job_id=body.job_id, notes=body.notes)
    db.refresh(spool)
    if will_deplete:
        spool.status = SpoolStatus.EMPTY
    log_audit(db, "use", "spool", spool_id, {"weight_used_g": grams, "remaining": spool.remaining_weight_g})
    db.commit()
    return {
        "success": True,
        "remaining_weight_g": spool.remaining_weight_g,
        "percent_remaining": spool.percent_remaining,
        "status": spool.status.value,
        "next_actions": build_next_actions(
            next_action("list_spools", {}, "verify remaining weights"),
        ),
    }


@router.post("/{spool_id}/weigh", tags=["Spools"])
def weigh_spool(spool_id: int, request: SpoolWeighRequest, current_user: dict = Depends(require_action("spools.edit", scope="write:spools")), db: Session = Depends(get_db)):
    """Update spool weight from scale measurement."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    # Calculate net filament weight
    old_weight = spool.remaining_weight_g
    new_weight = max(0, request.gross_weight_g - spool.spool_weight_g)
    # The ledger sets the net weight and records a decrease as a manual adjustment.
    from core.filament_ledger import record_weighed_spool
    from core.print_attempts import sqlalchemy_runner
    db.flush()
    record_weighed_spool(sqlalchemy_runner(db), spool.id, new_weight)
    db.refresh(spool)

    # Check if empty
    if spool.remaining_weight_g <= 10:  # Less than 10g = effectively empty
        spool.status = SpoolStatus.EMPTY

    db.commit()

    return {
        "success": True,
        "old_weight_g": old_weight,
        "new_weight_g": new_weight,
        "percent_remaining": spool.percent_remaining,
    }


# ====================================================================
# Drying
# ====================================================================

@router.post("/{spool_id}/dry", tags=["Spools"])
def log_drying_session(
    spool_id: int,
    duration_hours: float,
    temp_c: Optional[float] = None,
    method: str = "dryer",
    notes: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_action("spools.edit", scope="write:spools")),
):
    """Log a filament drying session for a spool."""
    from modules.inventory.models import DryingLog
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    drying_log = DryingLog(
        spool_id=spool_id,
        duration_hours=duration_hours,
        temp_c=temp_c,
        method=method,
        notes=notes,
    )
    db.add(drying_log)
    db.flush()
    db.refresh(drying_log)

    log_audit(db, "create", "drying_log", drying_log.id, {"spool_id": spool_id, "duration_hours": duration_hours, "method": method})
    db.commit()

    return {
        "id": drying_log.id,
        "spool_id": drying_log.spool_id,
        "dried_at": drying_log.dried_at.isoformat() if drying_log.dried_at else None,
        "duration_hours": drying_log.duration_hours,
        "temp_c": drying_log.temp_c,
        "method": drying_log.method,
        "notes": drying_log.notes,
    }


@router.get("/{spool_id}/drying-history", tags=["Spools"])
def get_drying_history(spool_id: int, current_user: dict = Depends(require_role("viewer", scope="read:spools")), db: Session = Depends(get_db)):
    """Get drying session history for a spool."""
    from modules.inventory.models import DryingLog
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    if not check_org_access(current_user, spool.org_id):
        raise HTTPException(status_code=404, detail="Spool not found")

    logs = (
        db.query(DryingLog)
        .filter(DryingLog.spool_id == spool_id)
        .order_by(DryingLog.dried_at.desc())
        .all()
    )
    return [
        {
            "id": l.id,
            "dried_at": l.dried_at.isoformat() if l.dried_at else None,
            "duration_hours": l.duration_hours,
            "temp_c": l.temp_c,
            "method": l.method,
            "notes": l.notes,
        }
        for l in logs
    ]
