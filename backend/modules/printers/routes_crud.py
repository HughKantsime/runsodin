"""Printer CRUD routes — create, read, update, delete, filament slots, test-connection, bulk-update."""
from core.base import slot_filament_value

from modules.inventory.services import assign_slot
from core.rbac import should_filter_org, require_action

from datetime import datetime, timezone
from typing import List, Optional
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text, or_, bindparam
from sqlalchemy.orm import Session
from sqlalchemy.exc import OperationalError

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import (
    AGENT_READ_SCOPE,
    AGENT_WRITE_SCOPE,
    _get_org_filter,
    check_org_access,
    get_org_scope,
    require_any_scope,
    require_role,
)
import core.crypto as crypto
from modules.printers.anycubic_crud import validate_credentials, prepare_update_credentials
from modules.printers.models import Printer, FilamentSlot
from modules.printers.schemas import (
    PrinterCreate, PrinterUpdate, PrinterResponse, FilamentSlotUpdate, FilamentSlotResponse,
)
from core.base import FilamentType
from modules.printers.ledger_display import enrich_printers, ensure_local_filaments
from modules.printers.route_utils import (
    TestConnectionRequest, _check_ssrf_blocklist, _validate_camera_url, camera_url_has_credentials,
)

log = logging.getLogger("odin.api")
router = APIRouter()


# Printers CRUD

@router.get("/printers", response_model=List[PrinterResponse], tags=["Printers"])
def list_printers(
    active_only: bool = False,
    tag: Optional[str] = None,
    org_id: Optional[int] = None,
    # Stacked auth (Phase 2 canonical read shape) — viewer floor for JWT,
    # admin/agent:write/agent:read or the legacy resource-specific read scope.
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE, "read:printers")
    ),
    db: Session = Depends(get_db),
):
    """List all printers, optionally filtered by tag and org."""
    query = db.query(Printer)
    if active_only:
        query = query.filter(Printer.is_active.is_(True))

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter(
            (Printer.org_id == effective_org) | (Printer.org_id == None) | (Printer.shared == True)
        )

    printers = query.order_by(Printer.display_order, Printer.id).all()
    if tag:
        printers = [p for p in printers if p.tags and tag in p.tags]
    return enrich_printers(printers)


@router.get("/printers/tags", tags=["Printers"])
def list_all_tags(db: Session = Depends(get_db), current_user: dict = Depends(require_role("viewer", scope="read:printers"))):
    """Get all unique tags across all printers."""
    query = db.query(Printer).filter(Printer.tags.isnot(None))
    org = get_org_scope(current_user)
    if should_filter_org(current_user, org):
        query = query.filter((Printer.org_id == org) | Printer.org_id.is_(None) | Printer.shared.is_(True))
    printers = query.all()
    tags = set()
    for p in printers:
        if p.tags:
            tags.update(p.tags)
    return sorted(tags)


@router.post("/printers", response_model=PrinterResponse, status_code=status.HTTP_201_CREATED, tags=["Printers"])
def create_printer(
    printer: PrinterCreate,
    current_user: dict = Depends(require_action("printers.add", scope="write:printers")), db: Session = Depends(get_db)
):
    """Create a new printer."""
    if printer.api_host:
        _check_ssrf_blocklist(printer.api_host)
        from core.webhook_utils import normalize_device_host
        printer.api_host = normalize_device_host(printer.api_host)
    camera_url = None
    if printer.camera_url:
        camera_url = _validate_camera_url(printer.camera_url)
        if camera_url_has_credentials(camera_url):
            camera_url = crypto.encrypt(camera_url)

    from license_manager import check_printer_limit

    current_count = db.query(Printer).count()
    check_printer_limit(current_count)

    existing = db.query(Printer).filter(Printer.name == printer.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Printer '{printer.name}' already exists")

    validate_credentials(printer.api_type, printer.api_key)

    encrypted_api_key = None
    if hasattr(printer, 'api_key') and printer.api_key:
        encrypted_api_key = crypto.encrypt(printer.api_key)

    db_printer = Printer(
        name=printer.name,
        model=printer.model,
        slot_count=printer.slot_count,
        is_active=printer.is_active,
        api_type=printer.api_type,
        api_host=printer.api_host,
        api_key=encrypted_api_key,
        shared=getattr(printer, 'shared', False),
        org_id=current_user.get("group_id") if current_user else None,
        bed_x_mm=printer.bed_x_mm,
        bed_y_mm=printer.bed_y_mm,
        # Without a URL the model default applies (Bambu printers have a built-in camera).
        **({"camera_url": camera_url, "camera_enabled": True} if camera_url else {}),
    )
    db.add(db_printer)
    db.flush()

    for i in range(1, printer.slot_count + 1):
        slot_data = None
        if printer.initial_slots:
            slot_data = next((s for s in printer.initial_slots if s.slot_number == i), None)

        slot = FilamentSlot(
            printer_id=db_printer.id,
            slot_number=i,
            filament_type=FilamentType(slot_filament_value(slot_data.filament_type if slot_data else None)),
            color=slot_data.color if slot_data else None,
            color_hex=slot_data.color_hex if slot_data else None,
        )
        db.add(slot)

    db.flush()
    db.refresh(db_printer)
    log_audit(db, "printer.created", "printer", db_printer.id,
              {"name": db_printer.name, "api_type": db_printer.api_type})
    db.commit()
    db.refresh(db_printer)

    if camera_url:
        try:
            from modules.printers.route_utils import sync_go2rtc_config
            sync_go2rtc_config(db)
        except Exception as e:
            log.debug(f"Failed to sync go2rtc config: {e}")
    return db_printer


class ReorderPrintersRequest(PydanticBaseModel):
    printer_ids: List[int] = []


@router.post("/printers/reorder", tags=["Printers"])
def reorder_printers(
    data: ReorderPrintersRequest,
    current_user: dict = Depends(require_action("printers.reorder", scope="write:printers")), db: Session = Depends(get_db)
):
    """Update printer display order."""
    printer_ids = data.printer_ids
    if len(set(printer_ids)) != len(printer_ids):
        raise HTTPException(status_code=400, detail="Printer IDs must be unique")
    printers = db.query(Printer).filter(Printer.id.in_(printer_ids)).all()
    if len(printers) != len(printer_ids) or any(
        not check_org_access(current_user, printer.org_id) for printer in printers
    ):
        raise HTTPException(status_code=404, detail="Printer not found")
    for idx, printer_id in enumerate(printer_ids):
        db.execute(
            text("UPDATE printers SET display_order = :order WHERE id = :id"),
            {"order": idx, "id": printer_id}
        )
    db.commit()
    return {"success": True, "order": printer_ids}


# Static route registered before /printers/{printer_id} to prevent FastAPI
# from treating "live-status" as a printer_id integer.
@router.get("/printers/live-status", tags=["Printers"])
def get_all_printers_live_status_early(
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
    db: Session = Depends(get_db),
):
    """Get real-time status from all Bambu printers."""
    from modules.printers.routes_status import _fetch_printer_live_status
    query = db.query(Printer).filter(
        Printer.api_host.isnot(None),
        or_(Printer.api_key.isnot(None), Printer.api_type == "anycubic_lan"),
    )
    scope = get_org_scope(current_user)
    if should_filter_org(current_user, scope):
        query = query.filter(or_(Printer.org_id == scope, Printer.org_id.is_(None), Printer.shared.is_(True)))
    printers = query.all()
    return [_fetch_printer_live_status(printer.id, db) for printer in printers]


@router.get("/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def get_printer(
    printer_id: int,
    current_user: dict = Depends(require_role("viewer")),
    _agent_scope: dict = Depends(
        require_any_scope("admin", AGENT_WRITE_SCOPE, AGENT_READ_SCOPE, "read:printers")
    ),
    db: Session = Depends(get_db),
):
    """Get a specific printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Printer not found")
    return enrich_printers([printer])[0]


@router.patch("/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def update_printer(
    printer_id: int,
    updates: PrinterUpdate,
    current_user: dict = Depends(require_action("printers.edit", scope="write:printers")), db: Session = Depends(get_db)
):
    """Update a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    update_data = updates.model_dump(exclude_unset=True)
    if 'slot_count' in update_data and update_data['slot_count'] != printer.slot_count:
        ensure_local_filaments(printer_id)

    prepare_update_credentials(update_data, printer.api_type)

    if 'api_host' in update_data and update_data['api_host']:
        _check_ssrf_blocklist(update_data['api_host'])
        from core.webhook_utils import normalize_device_host
        update_data['api_host'] = normalize_device_host(update_data['api_host'])

    if 'camera_url' in update_data:
        from modules.printers.route_utils import resolve_masked_camera_url
        # GET returns the effective URL masked (stored, or generated from the
        # printer's credentials), so masked values resolve against that.
        from modules.printers.route_utils import effective_camera_url, camera_url_is_masked
        effective = effective_camera_url(printer)
        if (not printer.camera_url and camera_url_is_masked(update_data['camera_url'])
                and resolve_masked_camera_url(update_data['camera_url'], effective) is not None):
            # The generated camera (Bambu access code) is derived at read time; a masked
            # edit would freeze the access code into camera_url.
            raise HTTPException(status_code=400, detail="This camera comes from the printer's access code; "
                                "enter a full camera URL to override it")
        if update_data['camera_url'] and resolve_masked_camera_url(update_data['camera_url'], effective) is None:
            # The masked URL from GET sent back unchanged: keep the stored value.
            del update_data['camera_url']
        elif update_data['camera_url']:
            update_data['camera_url'] = resolve_masked_camera_url(update_data['camera_url'], effective)
            update_data['camera_url'] = _validate_camera_url(update_data['camera_url'])
            if camera_url_has_credentials(update_data['camera_url']):
                update_data['camera_url'] = crypto.encrypt(update_data['camera_url'])
            update_data['camera_enabled'] = True
        else:
            update_data['camera_enabled'] = False

    if 'api_key' in update_data and update_data['api_key']:
        update_data['api_key'] = crypto.encrypt(update_data['api_key'])

    if 'slot_count' in update_data and update_data['slot_count'] != printer.slot_count:
        new_count = update_data['slot_count']
        # slot_count covers the four-tray slots 1-16 only. AMS HT slots (17+)
        # come from AMS sync and are never created or deleted by slot_count.
        from modules.printers.telemetry.bambu.ams_identity import AMS_HT_FIRST_SLOT
        current_slots = {s.slot_number: s for s in printer.filament_slots if s.slot_number < AMS_HT_FIRST_SLOT}
        current_count = len(current_slots)

        if new_count > current_count:
            for i in range(current_count + 1, new_count + 1):
                if i not in current_slots:
                    slot = FilamentSlot(
                        printer_id=printer.id,
                        slot_number=i,
                        filament_type=FilamentType(slot_filament_value(FilamentType.EMPTY)),
                    )
                    db.add(slot)
        elif new_count < current_count:
            for slot in printer.filament_slots:
                if new_count < slot.slot_number < AMS_HT_FIRST_SLOT:
                    db.delete(slot)

    for field, value in update_data.items():
        setattr(printer, field, value)

    log_audit(db, "printer.updated", "printer", printer_id,
              {"fields": list(update_data.keys())})
    db.commit()
    db.refresh(printer)

    if 'camera_url' in update_data:
        try:
            from modules.printers.route_utils import sync_go2rtc_config
            sync_go2rtc_config(db)
        except Exception as e:
            log.debug(f"Failed to sync go2rtc config: {e}")

    return printer


@router.delete("/printers/{printer_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Printers"])
def delete_printer(printer_id: int, current_user: dict = Depends(require_action("printers.delete", scope="write:printers")), db: Session = Depends(get_db)):
    """Delete a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    from core.registry import registry
    education_policy = registry.get_provider("EducationPolicyProvider")
    if education_policy is None:
        raise HTTPException(status_code=503, detail="Education policy unavailable")
    education_policy.assert_printer_tenant_change_or_delete_allowed(db, printer_id)
    printer_name = printer.name
    # Jobs pinned to this printer must not silently follow a reused id: mark
    # their pin deleted (they wait for a new choice) and tell their owners.
    from modules.jobs.services import clear_pins_for_deleted_printer
    from core.base import AlertType, AlertSeverity
    from modules.notifications.alert_dispatcher import stage_alert, deliver_staged_alert
    pins = clear_pins_for_deleted_printer(db, printer_id)
    live_jobs = pins.live
    # One alert per owner of a still-schedulable job (not per job), in bounded chunks.
    owners = {}
    for start in range(0, len(live_jobs), 500):
        for row in db.execute(text("SELECT id,charged_to_user_id,submitted_by FROM jobs WHERE id IN :ids")
                              .bindparams(bindparam("ids", expanding=True)), {"ids": live_jobs[start:start + 500]}).all():
            owner = row[1] or row[2]
            if owner is not None:
                owners.setdefault(owner, []).append(row[0])
    plans = []
    for owner, job_ids in sorted(owners.items()):
        count = len(job_ids)
        plan = stage_alert(db, AlertType.QUEUE_SKIPPED,
            AlertSeverity.WARNING, f"Pinned printer deleted: {printer_name}",
            f"{printer_name} was deleted. {count} queued job{'s' if count != 1 else ''} pinned to it "
            f"wait for a new printer choice.",
            job_id=job_ids[0],  # scopes the alert to this owner's job audience
            metadata={"reason": "pinned_printer_deleted", "job_ids": job_ids[:50], "job_count": count},
            target_user_ids=[owner])
        if plan is not None:
            plans.append(plan)
    db.delete(printer)
    log_audit(db, "printer.deleted", "printer", printer_id,
              {"name": printer_name, "pinned_job_count": len(pins.marked), "queued_pinned_jobs": live_jobs[:200]})
    db.commit()
    for plan in plans:
        deliver_staged_alert(db, plan)


# Filament Slots

@router.get("/printers/{printer_id}/slots", response_model=List[FilamentSlotResponse], tags=["Filament"])
def list_filament_slots(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")), db: Session = Depends(get_db)):
    """List filament slots for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or (not check_org_access(current_user, printer.org_id) and not printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    enriched = enrich_printers([printer])[0]
    return enriched.filament_slots if hasattr(enriched, "filament_slots") else printer.filament_slots


@router.patch("/printers/{printer_id}/slots/{slot_number}", response_model=FilamentSlotResponse, tags=["Filament"])
def update_filament_slot(
    printer_id: int,
    slot_number: int,
    updates: FilamentSlotUpdate,
    current_user: dict = Depends(require_action("printers.slots", scope="write:printers")), db: Session = Depends(get_db)
):
    """Update a filament slot (e.g., load new filament)."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()

    if not slot:
        raise HTTPException(status_code=404, detail="Filament slot not found")
    ensure_local_filaments(printer_id)

    values = updates.model_dump(exclude_unset=True)
    if 'assigned_spool_id' in values:
        assign_slot(db, slot, values.pop('assigned_spool_id'), current_user)
    for field, value in values.items():
        setattr(slot, field, FilamentType(slot_filament_value(value)) if field == "filament_type" else value)

    if slot.filament_type and slot.filament_type.value == "empty":
        slot.color = None
        slot.color_hex = None
        slot.spoolman_spool_id = None
        assign_slot(db, slot, None, current_user)
        slot.spool_confirmed = False

    slot.loaded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(slot)
    return slot


# Overlay (public, no auth)

@router.get("/overlay/{printer_id}", tags=["Overlay"])
def get_overlay_data(printer_id: int, db: Session = Depends(get_db)):
    """Public endpoint for OBS streaming overlay. Returns cached printer status."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    # Public endpoint: never expose a stored camera URL (it can embed credentials
    # and may be stored encrypted). Point at the overlay MJPEG relay, which is
    # public in path only: it requires the scoped camera token the copied
    # overlay URL carries (?token=), checked per stream.
    from modules.printers.route_utils import get_camera_url
    camera_url = None
    if printer.is_active and printer.camera_enabled and get_camera_url(printer):
        camera_url = f"/api/overlay/{printer_id}/camera"
    job_row = db.execute(
        text(
            "SELECT job_name, progress_percent, current_layer, total_layers, remaining_minutes "
            "FROM print_jobs WHERE printer_id = :pid AND status = 'running' "
            "ORDER BY started_at DESC LIMIT 1"
        ),
        {"pid": printer_id},
    ).fetchone()
    return {
        "printer_id": printer.id,
        "printer_name": printer.nickname or printer.name,
        "model": printer.model,
        "gcode_state": printer.gcode_state,
        "print_stage": printer.print_stage,
        "print_progress": job_row[1] if job_row else None,
        "current_layer": job_row[2] if job_row else None,
        "total_layers": job_row[3] if job_row else None,
        "time_remaining_min": job_row[4] if job_row else None,
        "nozzle_temp": printer.nozzle_temp,
        "nozzle_target_temp": printer.nozzle_target_temp,
        "bed_temp": printer.bed_temp,
        "bed_target_temp": printer.bed_target_temp,
        "job_name": job_row[0] if job_row else None,
        "camera_url": camera_url,
    }


# Bulk Update

@router.post("/printers/bulk-update", tags=["Printers"])
async def bulk_update_printers(body: dict, current_user: dict = Depends(require_role("admin", scope="write:printers")), db: Session = Depends(get_db)):
    """Atomically process unique authorized printers; affected counts targets."""
    ids = body.get("printer_ids")
    action = body.get("action")
    if (not isinstance(ids, list) or not ids or len(ids) > 1000
            or any(type(pid) is not int or pid <= 0 or pid > 2147483647 for pid in ids)):
        raise HTTPException(400, "A list of 1 to 1000 positive printer IDs is required")
    if action not in ("enable", "disable", "add_tag"):
        raise HTTPException(400, "Invalid bulk action")
    tag = body.get("tag")
    if action == "add_tag":
        if not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 128:
            raise HTTPException(400, "A tag of 1 to 128 characters is required")
        tag = tag.strip()
    ids = sorted(set(ids))
    try:
        # SQLite has no row locks. Acquire its writer boundary before reading
        # ownership, then refresh any identity-map entries. No device I/O occurs.
        if db.bind.dialect.name == "sqlite":
            db.execute(text("UPDATE printers SET name=name WHERE id=:id"), {"id": ids[0]})
        printers = (db.query(Printer).filter(Printer.id.in_(ids)).order_by(Printer.id)
                    .with_for_update(nowait=True).populate_existing().all())
        if len(printers) != len(ids) or any(not check_org_access(current_user, p.org_id) for p in printers):
            raise HTTPException(404, "Printer not found")
        # Validate every stored value before modifying any target.
        copied_tags = {}
        if action == "add_tag":
            for printer in printers:
                tags = printer.tags
                if tags is None:
                    tags = []
                if not isinstance(tags, list) or any(not isinstance(value, str) for value in tags):
                    raise HTTPException(409, "Printer tags require repair before bulk updates")
                copied_tags[printer.id] = list(tags)
        for printer in printers:
            if action == "add_tag":
                tags = copied_tags[printer.id]
                if tag not in tags:
                    printer.tags = tags + [tag]
            else:
                printer.is_active = action == "enable"
        db.commit()
        return {"status": "ok", "affected": len(printers)}
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, "sqlstate", None) == "55P03" or getattr(exc.orig, "sqlite_errorcode", None) in (5, 6, 517):
            raise HTTPException(409, "Printer updates are busy; retry") from None
        raise HTTPException(503, "Printer update could not be completed") from None
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        # Includes corrupt stored JSON and commit failures; never echo stored data.
        raise HTTPException(503, "Printer update could not be completed") from None


# Test Connection

@router.post("/printers/test-connection", tags=["Printers"])
def test_printer_connection(request: TestConnectionRequest, current_user: dict = Depends(require_action("printers.edit", scope="write:printers"))):
    """Test connection to a printer without saving."""
    _check_ssrf_blocklist(request.api_host)
    api_type = request.api_type.lower()

    if api_type == "bambu":
        if not request.serial or not request.access_code:
            raise HTTPException(status_code=400, detail="Serial and access_code required for Bambu printers")

        from modules.printers.telemetry.feature_flag import is_v2_enabled
        if is_v2_enabled():
            from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
            from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
            from modules.printers.telemetry.bambu.session import read_status_once
            from modules.printers.telemetry.bambu.status_view import ams_slots_from_section

            endpoint = resolve_bambu_broker_config(request.api_host, printer_id="crud-test")
            config = BambuAdapterConfig(
                printer_id="crud-test",
                serial=request.serial,
                host=endpoint.host,
                port=endpoint.port,
                use_tls=endpoint.use_tls,
                access_code=request.access_code,
            )
            try:
                result = read_status_once(config, timeout=8.0)
            except Exception as e:
                log.warning("Bambu V2 test-connection failed: %s", e)
                return {"success": False, "error": "Connection failed. Check IP, serial, and access code."}
            if not result.success:
                return {"success": False, "error": result.error or "Failed to connect."}
            view = result.view
            ams_slots = ams_slots_from_section(result.section) if result.section else []
            try:
                from modules.printers.printer_models import normalize_model_name
                detected_model = normalize_model_name("bambu", view.printer_type)
            except Exception:
                detected_model = None
            return {
                "success": True, "state": view.state,
                "bed_temp": view.bed_temp, "nozzle_temp": view.nozzle_temp,
                "ams_slots": len(ams_slots), "model": detected_model,
            }

        # Legacy path
        try:
            from modules.printers.adapters.bambu import BambuPrinter
            bambu = BambuPrinter(ip=request.api_host, serial=request.serial, access_code=request.access_code)
            if not bambu.connect():
                return {"success": False, "error": "Failed to connect. Check IP, serial, and access code."}
            time.sleep(2)
            bambu_status = bambu.get_status()
            bambu.disconnect()
            try:
                from modules.printers.printer_models import normalize_model_name
                detected_model = normalize_model_name("bambu", bambu_status.printer_type)
            except Exception:
                detected_model = None
            return {
                "success": True, "state": bambu_status.state.value,
                "bed_temp": bambu_status.bed_temp, "nozzle_temp": bambu_status.nozzle_temp,
                "ams_slots": len(bambu_status.ams_slots), "model": detected_model,
            }
        except ImportError:
            raise HTTPException(status_code=500, detail="bambu_adapter not installed")
        except Exception as e:
            log.warning("Bambu test-connection failed: %s", e)
            return {"success": False, "error": "Connection failed. Check IP, serial, and access code."}

    elif api_type == "moonraker":
        import httpx as httpx_client
        from modules.printers.route_utils import pinned_device_get
        try:
            r = pinned_device_get(httpx_client, f"http://{request.api_host}/printer/info", timeout=5)  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host -- verified safe — admin-gated by require_role and SSRF-checked by _check_ssrf_blocklist (or setup-locked)
            if r.status_code == 200:
                info = r.json().get("result", {})
                detected_model = None
                try:
                    cfg_r = pinned_device_get(httpx_client, f"http://{request.api_host}/server/config", timeout=3)  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host -- verified safe — admin-gated by require_role and SSRF-checked by _check_ssrf_blocklist (or setup-locked)
                    if cfg_r.status_code == 200:
                        kinematics = (
                            cfg_r.json().get("result", {}).get("config", {}).get("printer", {}).get("kinematics", "") or ""
                        )
                        if kinematics.lower() == "corexy":
                            detected_model = "Voron"
                except Exception as e:
                    log.debug(f"Failed to detect Moonraker kinematics: {e}")
                if detected_model is None:
                    try:
                        hostname = (info.get("hostname") or "").lower()
                        if "voron" in hostname:
                            detected_model = "Voron"
                        elif "trident" in hostname:
                            detected_model = "Voron Trident"
                        elif "switchwire" in hostname:
                            detected_model = "Voron Switchwire"
                        elif "v0" in hostname:
                            detected_model = "Voron V0"
                    except Exception as e:
                        log.debug(f"Failed to detect model from hostname: {e}")
                return {"success": True, "state": info.get("state", "unknown"), "bed_temp": 0, "nozzle_temp": 0, "ams_slots": 0, "model": detected_model}
            return {"success": False, "error": f"Moonraker returned HTTP {r.status_code}"}
        except Exception as e:
            log.warning("Moonraker test-connection failed: %s", e)
            return {"success": False, "error": "Connection failed. Check printer IP and Moonraker configuration."}

    elif api_type == "prusalink":
        from modules.printers.route_utils import prusalink_test_get
        try:
            r = prusalink_test_get(request.api_host, request.api_key)
            if r.status_code == 200:
                # Official PrusaLink Version schema defines `printer` as a
                # software-version string, not a hardware-model identifier.
                return {"success": True, "state": "connected", "bed_temp": 0, "nozzle_temp": 0, "ams_slots": 0, "model": None}
            return {"success": False, "error": f"PrusaLink returned HTTP {r.status_code}"}
        except Exception:
            log.warning("PrusaLink test-connection failed")
            return {"success": False, "error": "Connection failed. Check printer IP and PrusaLink configuration."}

    elif api_type == "anycubic_lan":
        from modules.printers.route_utils import test_anycubic_lan_connection
        return test_anycubic_lan_connection(request.api_host)

    elif api_type == "elegoo":
        import httpx as httpx_client
        from modules.printers.route_utils import pinned_device_get
        import socket
        import json as _json
        reachable = False
        try:
            pinned_device_get(httpx_client, f"http://{request.api_host}:3030", timeout=5)  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host -- verified safe — admin-gated by require_role and SSRF-checked by _check_ssrf_blocklist (or setup-locked)
            reachable = True
        except Exception as e:
            log.debug(f"Failed to reach Elegoo printer: {e}")
        if not reachable:
            return {"success": False, "error": "Cannot reach Elegoo printer on port 3030"}
        detected_model = None
        try:
            from modules.printers.printer_models import normalize_model_name
            # Unicast SDCP probe to the policy-validated IP literal.
            from core.webhook_utils import device_connect_ip
            probe_ip = device_connect_ip(request.api_host, 3000)
            sock = socket.socket(socket.AF_INET6 if ":" in probe_ip else socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3.0)
            try:
                sock.sendto(b"M99999", (probe_ip, 3000))
                data, _ = sock.recvfrom(4096)
                info = _json.loads(data.decode("utf-8"))
                if "Data" in info:
                    attrs = info["Data"].get("Attributes", info["Data"])
                else:
                    attrs = info
                machine_name = attrs.get("MachineName", "") or attrs.get("Name", "") or ""
                detected_model = normalize_model_name("elegoo", machine_name)
            except Exception:
                detected_model = None
            finally:
                sock.close()
        except Exception:
            detected_model = None
        return {"success": True, "state": "connected", "bed_temp": 0, "nozzle_temp": 0, "ams_slots": 0, "model": detected_model}

    else:
        return {"success": False, "error": f"Unknown printer type: {request.api_type}"}
