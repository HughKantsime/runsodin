"""Printer CRUD routes — create, read, update, delete, filament slots, test-connection, bulk-update."""

from datetime import datetime, timezone
from typing import List, Optional
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user, log_audit
from core.rbac import require_role, _get_org_filter, get_org_scope, check_org_access
import core.crypto as crypto
from modules.printers.models import Printer, FilamentSlot
from modules.printers.schemas import (
    PrinterCreate, PrinterUpdate, PrinterResponse, FilamentSlotUpdate, FilamentSlotResponse,
)
from core.base import FilamentType
from modules.printers.route_utils import (
    TestConnectionRequest, _check_ssrf_blocklist, _validate_camera_url,
)

log = logging.getLogger("odin.api")
router = APIRouter()


# ====================================================================
# Printers CRUD
# ====================================================================

@router.get("/printers", response_model=List[PrinterResponse], tags=["Printers"])
def list_printers(
    active_only: bool = False,
    tag: Optional[str] = None,
    org_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all printers, optionally filtered by tag and org."""
    query = db.query(Printer)
    if active_only:
        query = query.filter(Printer.is_active.is_(True))

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if effective_org is not None:
        query = query.filter(
            (Printer.org_id == effective_org) | (Printer.org_id == None) | (Printer.shared == True)
        )

    printers = query.order_by(Printer.display_order, Printer.id).all()
    if tag:
        printers = [p for p in printers if p.tags and tag in p.tags]
    return printers


@router.get("/printers/tags", tags=["Printers"])
def list_all_tags(db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Get all unique tags across all printers."""
    printers = db.query(Printer).filter(Printer.tags.isnot(None)).all()
    tags = set()
    for p in printers:
        if p.tags:
            tags.update(p.tags)
    return sorted(tags)


@router.post("/printers", response_model=PrinterResponse, status_code=status.HTTP_201_CREATED, tags=["Printers"])
def create_printer(
    printer: PrinterCreate,
    current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)
):
    """Create a new printer."""
    if printer.api_host:
        _check_ssrf_blocklist(printer.api_host)
    if printer.camera_url:
        printer.camera_url = _validate_camera_url(printer.camera_url)
        if '@' in printer.camera_url:
            printer.camera_url = crypto.encrypt(printer.camera_url)

    from license_manager import check_printer_limit

    current_count = db.query(Printer).count()
    check_printer_limit(current_count)

    existing = db.query(Printer).filter(Printer.name == printer.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Printer '{printer.name}' already exists")

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
            filament_type=slot_data.filament_type if slot_data else None,
            color=slot_data.color if slot_data else None,
            color_hex=slot_data.color_hex if slot_data else None,
        )
        db.add(slot)

    db.commit()
    db.refresh(db_printer)
    return db_printer


@router.post("/printers/reorder", tags=["Printers"])
def reorder_printers(
    data: dict,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Update printer display order."""
    printer_ids = data.get("printer_ids", [])
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
def get_all_printers_live_status_early(db: Session = Depends(get_db)):
    """Get real-time status from all Bambu printers."""
    from modules.printers.routes_status import _fetch_printer_live_status
    printers = db.query(Printer).filter(
        Printer.api_host.isnot(None),
        Printer.api_key.isnot(None),
    ).all()
    return [_fetch_printer_live_status(printer.id, db) for printer in printers]


@router.get("/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def get_printer(printer_id: int, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Get a specific printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if current_user and not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


@router.patch("/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def update_printer(
    printer_id: int,
    updates: PrinterUpdate,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Update a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    update_data = updates.model_dump(exclude_unset=True)

    if 'camera_url' in update_data and update_data['camera_url']:
        update_data['camera_url'] = _validate_camera_url(update_data['camera_url'])
        if '@' in update_data['camera_url']:
            update_data['camera_url'] = crypto.encrypt(update_data['camera_url'])

    if 'api_key' in update_data and update_data['api_key']:
        update_data['api_key'] = crypto.encrypt(update_data['api_key'])

    if 'slot_count' in update_data and update_data['slot_count'] != printer.slot_count:
        new_count = update_data['slot_count']
        current_slots = {s.slot_number: s for s in printer.filament_slots}
        current_count = len(current_slots)

        if new_count > current_count:
            for i in range(current_count + 1, new_count + 1):
                if i not in current_slots:
                    slot = FilamentSlot(
                        printer_id=printer.id,
                        slot_number=i,
                        filament_type=FilamentType.EMPTY,
                    )
                    db.add(slot)
        elif new_count < current_count:
            for slot in printer.filament_slots:
                if slot.slot_number > new_count:
                    db.delete(slot)

    for field, value in update_data.items():
        setattr(printer, field, value)

    db.commit()
    db.refresh(printer)
    return printer


@router.delete("/printers/{printer_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Printers"])
def delete_printer(printer_id: int, current_user: dict = Depends(require_role("operator", scope="write")), db: Session = Depends(get_db)):
    """Delete a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    db.delete(printer)
    db.commit()


# ====================================================================
# Filament Slots
# ====================================================================

@router.get("/printers/{printer_id}/slots", response_model=List[FilamentSlotResponse], tags=["Filament"])
def list_filament_slots(printer_id: int, db: Session = Depends(get_db)):
    """List filament slots for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer.filament_slots


@router.patch("/printers/{printer_id}/slots/{slot_number}", response_model=FilamentSlotResponse, tags=["Filament"])
def update_filament_slot(
    printer_id: int,
    slot_number: int,
    updates: FilamentSlotUpdate,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Update a filament slot (e.g., load new filament)."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()

    if not slot:
        raise HTTPException(status_code=404, detail="Filament slot not found")

    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(slot, field, value)

    if slot.filament_type and slot.filament_type.value == "empty":
        slot.color = None
        slot.color_hex = None
        slot.spoolman_spool_id = None
        slot.assigned_spool_id = None
        slot.spool_confirmed = False

    slot.loaded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(slot)
    return slot


# ====================================================================
# Overlay (public, no auth)
# ====================================================================

@router.get("/overlay/{printer_id}", tags=["Overlay"])
def get_overlay_data(printer_id: int, db: Session = Depends(get_db)):
    """Public endpoint for OBS streaming overlay. Returns cached printer status."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    camera_url = None
    if printer.api_type == "bambu" and printer.api_host:
        camera_url = f"/api/cameras/{printer_id}/stream"
    elif printer.camera_url:
        camera_url = printer.camera_url
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


# ====================================================================
# Bulk Update
# ====================================================================

@router.post("/printers/bulk-update", tags=["Printers"])
async def bulk_update_printers(body: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Bulk update printer fields for multiple printers."""
    printer_ids = body.get("printer_ids", [])
    if not printer_ids or not isinstance(printer_ids, list):
        raise HTTPException(status_code=400, detail="printer_ids list is required")

    action = body.get("action", "")
    count = 0

    if action == "enable":
        for pid in printer_ids:
            db.execute(text("UPDATE printers SET is_active = 1 WHERE id = :id"), {"id": pid})
            count += 1
    elif action == "disable":
        for pid in printer_ids:
            db.execute(text("UPDATE printers SET is_active = 0 WHERE id = :id"), {"id": pid})
            count += 1
    elif action == "add_tag":
        tag = body.get("tag", "").strip()
        if not tag:
            raise HTTPException(status_code=400, detail="Tag is required")
        for pid in printer_ids:
            printer = db.query(Printer).filter(Printer.id == pid).first()
            if not printer:
                continue
            current_tags = printer.tags if isinstance(printer.tags, list) else json.loads(printer.tags or "[]")
            if tag not in current_tags:
                current_tags.append(tag)
                printer.tags = current_tags
            count += 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    db.commit()
    return {"status": "ok", "affected": count}


# ====================================================================
# Test Connection
# ====================================================================

@router.post("/printers/test-connection", tags=["Printers"])
def test_printer_connection(request: TestConnectionRequest, current_user: dict = Depends(require_role("operator"))):
    """Test connection to a printer without saving."""
    _check_ssrf_blocklist(request.api_host)
    api_type = request.api_type.lower()

    if api_type == "bambu":
        if not request.serial or not request.access_code:
            raise HTTPException(status_code=400, detail="Serial and access_code required for Bambu printers")
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
            return {"success": False, "error": str(e)}

    elif api_type == "moonraker":
        import httpx as httpx_client
        try:
            r = httpx_client.get(f"http://{request.api_host}/printer/info", timeout=5)
            if r.status_code == 200:
                info = r.json().get("result", {})
                detected_model = None
                try:
                    cfg_r = httpx_client.get(f"http://{request.api_host}/server/config", timeout=3)
                    if cfg_r.status_code == 200:
                        kinematics = (
                            cfg_r.json().get("result", {}).get("config", {}).get("printer", {}).get("kinematics", "") or ""
                        )
                        if kinematics.lower() == "corexy":
                            detected_model = "Voron"
                except Exception:
                    pass
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
                    except Exception:
                        pass
                return {"success": True, "state": info.get("state", "unknown"), "bed_temp": 0, "nozzle_temp": 0, "ams_slots": 0, "model": detected_model}
            return {"success": False, "error": f"Moonraker returned HTTP {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif api_type == "prusalink":
        import httpx as httpx_client
        try:
            r = httpx_client.get(f"http://{request.api_host}/api/version", timeout=5)
            if r.status_code == 200:
                info = r.json()
                detected_model = None
                try:
                    from modules.printers.printer_models import normalize_model_name
                    printer_field = info.get("printer", None)
                    if isinstance(printer_field, dict):
                        raw_type = printer_field.get("type", "") or ""
                    elif isinstance(printer_field, str):
                        raw_type = printer_field
                    else:
                        raw_type = ""
                    detected_model = normalize_model_name("prusalink", raw_type)
                except Exception:
                    detected_model = None
                return {"success": True, "state": "connected", "bed_temp": 0, "nozzle_temp": 0, "ams_slots": 0, "model": detected_model}
            return {"success": False, "error": f"PrusaLink returned HTTP {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif api_type == "elegoo":
        import httpx as httpx_client
        import socket
        import json as _json
        reachable = False
        try:
            httpx_client.get(f"http://{request.api_host}:3030", timeout=5)
            reachable = True
        except Exception:
            pass
        if not reachable:
            return {"success": False, "error": "Cannot reach Elegoo printer on port 3030"}
        detected_model = None
        try:
            from modules.printers.printer_models import normalize_model_name
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(3.0)
            try:
                sock.sendto(b"M99999", (request.api_host, 3000))
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
