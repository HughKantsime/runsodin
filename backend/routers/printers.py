"""O.D.I.N. — Printer Routes

CRUD for printers, filament slots, AMS sync, Bambu integration,
live status, printer commands (stop/pause/resume), lights,
smart plug control, AMS environment, telemetry, and nozzle lifecycle.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
import json
import logging
import os
import re
import time

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import (
    get_db, get_current_user, require_role, log_audit,
    _get_org_filter, get_org_scope, check_org_access,
    compute_printer_online, get_printer_api_key,
    SessionLocal,
)
from models import (
    Printer, FilamentSlot, FilamentType, FilamentLibrary,
    Spool, SpoolStatus, NozzleLifecycle,
)
from schemas import (
    PrinterCreate, PrinterUpdate, PrinterResponse, PrinterSummary,
    FilamentSlotCreate, FilamentSlotUpdate, FilamentSlotResponse,
    NozzleInstall, NozzleLifecycleResponse,
)
from config import settings
import crypto

# Bambu Lab Integration
try:
    from bambu_integration import (
        test_bambu_connection, sync_ams_filaments, slot_to_dict,
        map_bambu_filament_type, BAMBU_FILAMENT_TYPE_MAP, MQTT_AVAILABLE,
    )
    BAMBU_AVAILABLE = MQTT_AVAILABLE
except ImportError:
    BAMBU_AVAILABLE = False

import smart_plug

log = logging.getLogger("odin.api")
router = APIRouter()

GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/app/go2rtc/go2rtc.yaml")


# ====================================================================
# Inline Pydantic models
# ====================================================================

class TestConnectionRequest(PydanticBaseModel):
    """Request body for testing printer connection."""
    api_type: str
    api_host: str
    serial: Optional[str] = None
    access_code: Optional[str] = None


class BambuConnectionTest(PydanticBaseModel):
    ip_address: str
    serial_number: str
    access_code: str


class AMSSlotResponse(PydanticBaseModel):
    ams_id: int
    tray_id: int
    slot_number: int
    filament_type_raw: str
    filament_type: str
    color_hex: Optional[str]
    remaining_percent: Optional[int]
    brand: Optional[str]
    is_empty: bool
    match_source: Optional[str] = None
    color_name: Optional[str] = None
    matched_filament_id: Optional[str] = None
    matched_filament_name: Optional[str] = None


class BambuSyncResult(PydanticBaseModel):
    success: bool
    printer_name: Optional[str] = None
    slots: List[AMSSlotResponse] = []
    message: str
    slots_updated: int = 0
    unmatched_slots: List[int] = []


class ManualSlotAssignment(PydanticBaseModel):
    filament_library_id: Optional[int] = None
    filament_type: Optional[str] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    brand: Optional[str] = None


# ====================================================================
# Camera / go2rtc helpers (used by printers + cameras routers)
# ====================================================================

def get_camera_url(printer):
    """Get camera URL for a printer - from DB field or auto-generated from credentials.

    Works for all printer types:
    - Bambu: auto-generates RTSP URL from serial|access_code credentials
    - Moonraker/PrusaLink/Elegoo: uses camera_url populated by monitor auto-discovery
    """
    if printer.camera_url:
        return printer.camera_url
    # Auto-generate RTSP URL for Bambu printers with built-in cameras
    if printer.api_type == "bambu" and printer.api_key and printer.api_host:
        RTSP_MODELS = {'X1C', 'X1 Carbon', 'X1E', 'X1 Carbon Combo', 'H2D'}
        model = (printer.model or '').strip()
        if model not in RTSP_MODELS:
            return None
        try:
            parts = crypto.decrypt(printer.api_key).split("|")
            if len(parts) == 2:
                return f"rtsps://bblp:{parts[1]}@{printer.api_host}:322/streaming/live/1"
        except Exception:
            pass
    return None


def sanitize_camera_url(url: str) -> str:
    """Strip credentials from RTSP URLs for API responses."""
    if not url:
        return url
    # rtsps://bblp:ACCESS_CODE@192.168.x.x:322/... -> rtsps://***@192.168.x.x:322/...
    return re.sub(r'(rtsps?://)([^@]+)@', r'\1***@', url)


def _get_lan_ip():
    """Auto-detect LAN IP for WebRTC ICE candidates."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def sync_go2rtc_config(db: Session):
    """Regenerate go2rtc config from printer camera URLs."""
    printers = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True)).all()
    streams = {}
    for p in printers:
        url = get_camera_url(p)
        if url:
            # HTTP streams (MJPEG) need ffmpeg transcoding to H264 for WebRTC;
            # RTSP streams already carry H264 and can be proxied directly.
            if url.startswith(("http://", "https://")):
                streams[f"printer_{p.id}"] = f"ffmpeg:{url}#video=h264"
            else:
                streams[f"printer_{p.id}"] = url
            # Save generated URL back to DB if not already set
            if not p.camera_url and url:
                p.camera_url = url
                p.camera_discovered = True
    db.commit()
    webrtc_config = {"listen": "0.0.0.0:8555"}
    # Priority: env var > system_config > auto-detect
    lan_ip = os.environ.get("ODIN_HOST_IP")
    if not lan_ip:
        row = db.execute(text("SELECT value FROM system_config WHERE key = 'host_ip'")).fetchone()
        if row:
            lan_ip = row[0]
    if not lan_ip:
        lan_ip = _get_lan_ip()
    if lan_ip:
        webrtc_config["candidates"] = [f"{lan_ip}:8555"]
        log.info(f"go2rtc WebRTC ICE candidate: {lan_ip}:8555")
    config = {
        "api": {"listen": "0.0.0.0:1984"},
        "webrtc": webrtc_config,
        "streams": streams,
    }
    with open(GO2RTC_CONFIG, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    # Restart go2rtc to pick up config changes
    try:
        import subprocess
        subprocess.run(["supervisorctl", "restart", "go2rtc"], capture_output=True, timeout=5)
    except Exception:
        pass


def sync_go2rtc_config_standalone():
    """Regenerate go2rtc config (callable without a DB session)."""
    db = SessionLocal()
    try:
        sync_go2rtc_config(db)
    finally:
        db.close()


# ====================================================================
# Printer command helpers
# ====================================================================

# Allowlist of adapter methods that may be invoked via command dispatch.
# Prevents arbitrary method execution through getattr.
_ALLOWED_COMMANDS = frozenset({"pause_print", "resume_print", "stop_print", "cancel_print"})


def _call_adapter_method(adapter, action: str, *args):
    """Dispatch an allowed command to an adapter via explicit allowlist."""
    if action not in _ALLOWED_COMMANDS:
        raise ValueError(f"Unknown printer command: {action}")
    method = getattr(adapter, action, None)
    if method is None:
        raise ValueError(f"Adapter {type(adapter).__name__} does not support '{action}'")
    return method(*args)


def _bambu_command(printer, action: str) -> bool:
    """Send a command to a Bambu printer via a short-lived MQTT connection.

    Uses a unique client_id so we don't collide with the monitor daemon's
    persistent connection on the same broker.
    """
    from bambu_adapter import BambuPrinter
    import time as _time
    try:
        creds = crypto.decrypt(printer.api_key)
        serial, access_code = creds.split("|", 1)
        adapter = BambuPrinter(
            printer.api_host, serial, access_code,
            client_id=f"odin_cmd_{printer.id}_{int(_time.time())}"
        )
        if adapter.connect():
            success = _call_adapter_method(adapter, action)
            _time.sleep(0.3)  # let the ACK settle
            adapter.disconnect()
            return success
    except Exception as e:
        log.error(f"Bambu {action} failed for printer {printer.id}: {e}")
    return False


def _prusalink_command(printer, action: str) -> bool:
    """Send a command to a PrusaLink printer."""
    from prusalink_adapter import PrusaLinkPrinter
    try:
        decrypted = crypto.decrypt(printer.api_key) if printer.api_key else ""
        if "|" in decrypted:
            username, password = decrypted.split("|", 1)
            adapter = PrusaLinkPrinter(printer.api_host, username=username, password=password)
        else:
            adapter = PrusaLinkPrinter(printer.api_host, api_key=decrypted)
        # PrusaLink needs the current job_id for pause/resume/stop
        status = adapter.get_status()
        if not status or not status.job_id:
            log.error(f"PrusaLink {action}: no active job_id for printer {printer.id}")
            return False
        return _call_adapter_method(adapter, action, status.job_id)
    except Exception as e:
        log.error(f"PrusaLink {action} failed for printer {printer.id}: {e}")
    return False


def _elegoo_command(printer, action: str) -> bool:
    """Send a command to an Elegoo printer."""
    from elegoo_adapter import ElegooPrinter
    try:
        mainboard_id = crypto.decrypt(printer.api_key) if printer.api_key else ""
        adapter = ElegooPrinter(printer.api_host, mainboard_id=mainboard_id)
        if adapter.connect():
            success = _call_adapter_method(adapter, action)
            adapter.disconnect()
            return success
    except Exception as e:
        log.error(f"Elegoo {action} failed for printer {printer.id}: {e}")
    return False


def _send_printer_command(printer, action: str) -> bool:
    """Route a command to the correct adapter based on printer type."""
    if action not in _ALLOWED_COMMANDS:
        log.error(f"Rejected unknown printer command: {action}")
        return False
    if printer.api_type == "moonraker":
        from moonraker_adapter import MoonrakerPrinter
        adapter = MoonrakerPrinter(printer.api_host)
        return _call_adapter_method(adapter, action)
    elif printer.api_type == "prusalink":
        return _prusalink_command(printer, action)
    elif printer.api_type == "elegoo":
        return _elegoo_command(printer, action)
    else:
        return _bambu_command(printer, action)


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

    # Org scoping: explicit org_id param takes precedence, else implicit scope
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
def list_all_tags(db: Session = Depends(get_db)):
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Create a new printer."""
    from license_manager import check_printer_limit

    # Check license printer limit
    current_count = db.query(Printer).count()
    check_printer_limit(current_count)

    # Check for duplicate name
    existing = db.query(Printer).filter(Printer.name == printer.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Printer '{printer.name}' already exists")

    # Encrypt api_key if provided
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

    # Create empty filament slots
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

    # Encrypt api_key if being updated
    if 'api_key' in update_data and update_data['api_key']:
        update_data['api_key'] = crypto.encrypt(update_data['api_key'])

    # Handle slot_count changes
    if 'slot_count' in update_data and update_data['slot_count'] != printer.slot_count:
        new_count = update_data['slot_count']
        current_slots = {s.slot_number: s for s in printer.filament_slots}
        current_count = len(current_slots)

        if new_count > current_count:
            # Add new slots
            for i in range(current_count + 1, new_count + 1):
                if i not in current_slots:
                    slot = FilamentSlot(
                        printer_id=printer.id,
                        slot_number=i,
                        filament_type=FilamentType.EMPTY,
                    )
                    db.add(slot)
        elif new_count < current_count:
            # Remove extra slots
            for slot in printer.filament_slots:
                if slot.slot_number > new_count:
                    db.delete(slot)

    # Apply all updates
    for field, value in update_data.items():
        setattr(printer, field, value)

    db.commit()
    db.refresh(printer)
    return printer


@router.delete("/printers/{printer_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Printers"])
def delete_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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

    # If slot is set to empty, clear all filament data
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
# AMS Sync
# ====================================================================

@router.post("/printers/{printer_id}/sync-ams", tags=["Printers"])
def sync_ams_state(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """
    Sync AMS filament state from printer.

    Connects to the printer, reads current AMS state, and updates
    the filament slots in the database.

    Requires printer to have:
    - api_type = "bambu"
    - api_host = printer IP address
    - api_key = "serial|access_code"
    """
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Check printer has required config
    if not printer.api_type:
        raise HTTPException(status_code=400, detail="Printer api_type not configured")
    if not printer.api_host:
        raise HTTPException(status_code=400, detail="Printer api_host (IP) not configured")

    # ---- Moonraker / Klipper MMU sync ----
    if printer.api_type.lower() == "moonraker":
        from moonraker_adapter import MoonrakerPrinter
        api_host = (printer.api_host or "").strip()
        host, port = api_host, 80
        if ":" in api_host:
            h, prt = api_host.rsplit(":", 1)
            host = h.strip() or host
            try:
                port = int(prt)
            except Exception:
                port = 80
        api_key = ""
        if printer.api_key:
            try:
                api_key = crypto.decrypt(printer.api_key)
            except Exception:
                api_key = printer.api_key
        mk = MoonrakerPrinter(host=host, port=port, api_key=api_key)
        if not mk.connect():
            raise HTTPException(status_code=503, detail="Failed to connect to Moonraker")
        mk_status = mk.get_status()
        mk.disconnect()
        if not mk_status.filament_slots:
            return {"success": True, "printer_id": printer_id, "printer_name": printer.name,
                    "slots_synced": 0, "slots": [], "mismatches": [],
                    "message": "No MMU/ACE detected on this printer"}
        filament_type_map_mk = {
            "PLA": FilamentType.PLA, "PETG": FilamentType.PETG, "ABS": FilamentType.ABS,
            "ASA": FilamentType.ASA, "TPU": FilamentType.TPU, "PA": FilamentType.PA,
            "PC": FilamentType.PC, "PVA": FilamentType.PVA,
            "PA-CF": FilamentType.NYLON_CF, "PA-GF": FilamentType.NYLON_GF,
            "PET-CF": FilamentType.PETG_CF, "PLA-CF": FilamentType.PLA_CF,
        }
        updated_slots = []
        for mmu_slot in mk_status.filament_slots:
            slot_num = mmu_slot.gate + 1
            db_slot = db.query(FilamentSlot).filter(
                FilamentSlot.printer_id == printer_id,
                FilamentSlot.slot_number == slot_num,
            ).first()
            if not db_slot:
                db_slot = FilamentSlot(printer_id=printer_id, slot_number=slot_num)
                db.add(db_slot)
            material_key = (mmu_slot.material or "").upper()
            ftype = filament_type_map_mk.get(material_key, FilamentType.PLA)
            color_hex = mmu_slot.color_hex[:6] if mmu_slot.color_hex else None
            color_name = mmu_slot.name or mmu_slot.material or None
            if mmu_slot.loaded:
                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.loaded_at = datetime.now(timezone.utc)
                updated_slots.append({
                    "slot": slot_num, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "matched": "mmu_gate"})
            else:
                db_slot.filament_type = FilamentType.EMPTY
                db_slot.color = None
                db_slot.color_hex = None
                updated_slots.append({"slot": slot_num, "type": "EMPTY", "color": None, "empty": True})
        # Remove stale slots
        max_slot = len(mk_status.filament_slots)
        db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number > max_slot,
        ).delete()
        db.commit()
        log_audit(db, "sync", "printer", printer_id, {"slots_synced": len(updated_slots), "source": "moonraker_mmu"})
        return {"success": True, "printer_id": printer_id, "printer_name": printer.name,
                "slots_synced": len(updated_slots), "slots": updated_slots, "mismatches": []}

    if not printer.api_key:
        raise HTTPException(status_code=400, detail="Printer api_key not configured")

    # ---- Bambu AMS sync ----
    if printer.api_type.lower() != "bambu":
        raise HTTPException(status_code=400, detail=f"Sync not supported for {printer.api_type}")

    # Parse credentials (format: "serial|access_code")
    decrypted_key = crypto.decrypt(printer.api_key)
    if "|" not in decrypted_key:
        raise HTTPException(
            status_code=400,
            detail="Invalid api_key format. Expected 'serial|access_code'"
        )

    serial, access_code = decrypted_key.split("|", 1)

    # Connect to printer
    try:
        from bambu_adapter import BambuPrinter

        bambu = BambuPrinter(
            ip=printer.api_host,
            serial=serial,
            access_code=access_code,
        )

        if not bambu.connect():
            raise HTTPException(status_code=503, detail="Failed to connect to printer")

        # Wait for status update
        time.sleep(2)
        bambu_status = bambu.get_status()
        bambu.disconnect()

    except ImportError:
        raise HTTPException(status_code=500, detail="bambu_adapter not installed")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Printer connection error: {str(e)}")

    # Map filament types
    filament_type_map = {
        "PLA": FilamentType.PLA,
        "PETG": FilamentType.PETG,
        "ABS": FilamentType.ABS,
        "ASA": FilamentType.ASA,
        "TPU": FilamentType.TPU,
        "PA": FilamentType.PA,
        "PC": FilamentType.PC,
        "PVA": FilamentType.PVA,
        "PLA-S": FilamentType.PLA_SUPPORT,
        "PA-S": FilamentType.PLA_SUPPORT,
        "PETG-S": FilamentType.PLA_SUPPORT,
        "PA-CF": FilamentType.NYLON_CF,
        "PA-GF": FilamentType.NYLON_GF,
        "PET-CF": FilamentType.PETG_CF,
        "PLA-CF": FilamentType.PLA_CF,
    }

    # Load local filament library
    local_library = db.query(FilamentLibrary).all()

    # Try to fetch Spoolman spools for matching (secondary source)
    spoolman_spools = []
    if settings.spoolman_url:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{settings.spoolman_url}/api/v1/spool")
                if resp.status_code == 200:
                    spoolman_spools = resp.json()
        except:
            pass  # Spoolman not available, continue without it

    def find_library_match(hex_code, filament_type):
        """Find a local library filament matching the hex code and material type."""
        if not hex_code:
            return None

        hex_lower = hex_code.lower()

        # First pass: exact hex + material match
        for f in local_library:
            if f.color_hex and f.color_hex.lower() == hex_lower:
                if f.material and f.material.upper() == filament_type.upper():
                    return f

        # Second pass: just hex match (any material)
        for f in local_library:
            if f.color_hex and f.color_hex.lower() == hex_lower:
                return f

        return None

    def find_spoolman_match(hex_code, filament_type):
        """Find a Spoolman spool matching the hex code and material type."""
        if not hex_code or not spoolman_spools:
            return None

        hex_lower = hex_code.lower()

        for spool in spoolman_spools:
            filament = spool.get("filament", {})
            spool_hex = filament.get("color_hex", "").lower()
            spool_material = filament.get("material", "").upper()

            if spool_hex and hex_lower:
                if spool_hex == hex_lower:
                    if spool_material == filament_type.upper():
                        return spool
                    return spool

        return None

    def find_spool_by_rfid(rfid_tag):
        """Find a tracked spool by RFID tag."""
        if not rfid_tag:
            return None
        return db.query(Spool).filter(Spool.rfid_tag == rfid_tag).first()

    def get_color_name(hex_code):
        """Get a human-readable color name from hex code."""
        if not hex_code:
            return None

        hex_lower = hex_code.lower()

        # Common color mappings
        color_map = {
            "000000": "Black",
            "ffffff": "White",
            "f5f5f5": "Off White",
            "ff0000": "Red",
            "00ff00": "Green",
            "0000ff": "Blue",
            "ffff00": "Yellow",
            "ff00ff": "Magenta",
            "00ffff": "Cyan",
            "ffa500": "Orange",
            "800080": "Purple",
            "ffc0cb": "Pink",
            "808080": "Gray",
            "c0c0c0": "Silver",
        }

        if hex_lower in color_map:
            return color_map[hex_lower]

        # Analyze the color components
        try:
            r = int(hex_lower[0:2], 16)
            g = int(hex_lower[2:4], 16)
            b = int(hex_lower[4:6], 16)

            # Check for grayscale (r ~ g ~ b)
            if abs(r - g) < 25 and abs(g - b) < 25 and abs(r - b) < 25:
                avg = (r + g + b) // 3
                if avg < 40:
                    return "Black"
                elif avg < 100:
                    return "Dark Gray"
                elif avg < 160:
                    return "Gray"
                elif avg < 220:
                    return "Light Gray"
                else:
                    return "White"

            # Find dominant color
            max_val = max(r, g, b)

            if r == max_val and r > g + 30 and r > b + 30:
                if g > 150:
                    return "Orange" if g < 200 else "Yellow"
                elif b > 100:
                    return "Pink"
                return "Red"
            elif g == max_val and g > b:
                if r > 80 and g > 80 and b < g and r < g:
                    return "Olive Green"
                elif b > 150:
                    return "Teal"
                return "Green"
            elif b == max_val and b > r + 30 and b > g + 30:
                if r > 100:
                    return "Purple"
                return "Blue"
            elif r > 200 and g > 200 and b < 100:
                return "Yellow"
            elif r > 200 and g < 150 and b > 200:
                return "Magenta"
            elif r < 100 and g > 200 and b > 200:
                return "Cyan"

            # Default to hex if we can't determine
            return f"#{hex_code.upper()}"

        except:
            return f"#{hex_code.upper()}"

    # Update slots from AMS state
    updated_slots = []
    for ams_slot in bambu_status.ams_slots:
        # Find matching slot in database
        db_slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == ams_slot.slot_number,
        ).first()

        if not db_slot:
            continue

        # Parse color hex (Bambu returns 8 char with alpha, we want 6)
        color_hex = ams_slot.color_hex[:6] if ams_slot.color_hex else None

        # Map filament type
        ftype = filament_type_map.get(ams_slot.filament_type.upper(), FilamentType.PLA)

        # Update slot
        if not ams_slot.empty:
            # Priority 0: Match by RFID tag (most reliable)
            rfid_match = find_spool_by_rfid(ams_slot.rfid_tag)

            if rfid_match:
                color_name = f"{rfid_match.filament.brand} {rfid_match.filament.name}".strip() if rfid_match.filament else "Unknown"

                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.assigned_spool_id = rfid_match.id
                db_slot.spool_confirmed = True
                db_slot.loaded_at = datetime.now(timezone.utc)

                # Update spool location
                rfid_match.location_printer_id = printer_id
                rfid_match.location_slot = ams_slot.slot_number
                rfid_match.storage_location = None
                # Update weight from AMS data
                if ams_slot.remaining_percent >= 0:
                    rfid_match.remaining_weight_g = rfid_match.initial_weight_g * (ams_slot.remaining_percent / 100)

                updated_slots.append({
                    "slot": ams_slot.slot_number,
                    "type": ftype.value,
                    "color": color_name,
                    "color_hex": color_hex,
                    "spool_id": rfid_match.id,
                    "rfid": ams_slot.rfid_tag,
                    "matched": "rfid",
                    "remaining_percent": ams_slot.remaining_percent,
                })
                continue

            # Auto-create spool if RFID exists but not tracked
            if ams_slot.rfid_tag and not rfid_match:
                import uuid

                # Find or create filament library entry - check sub_brand FIRST
                sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                library_entry = db.query(FilamentLibrary).filter(
                    FilamentLibrary.brand == "Bambu Lab",
                    FilamentLibrary.name == sub_brand,
                    FilamentLibrary.material == ftype.value,
                ).first()

                if not library_entry:
                    # Check for existing entry by brand+name+material
                    sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                    existing = db.query(FilamentLibrary).filter(
                        FilamentLibrary.brand == "Bambu Lab",
                        FilamentLibrary.name == sub_brand,
                        FilamentLibrary.material == ftype.value,
                    ).first()
                    if existing:
                        library_entry = existing
                    else:
                        # Create new library entry from AMS data
                        sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                        new_lib = FilamentLibrary(
                            brand="Bambu Lab",
                            name=sub_brand,
                            material=ftype.value,
                            color_hex=color_hex,
                        )
                        db.add(new_lib)
                        db.flush()
                        library_entry = new_lib

                # Create spool
                new_spool = Spool(
                    filament_id=library_entry.id,
                    qr_code=f"SPL-{uuid.uuid4().hex[:8].upper()}",
                    rfid_tag=ams_slot.rfid_tag,
                    color_hex=color_hex,
                    remaining_weight_g=max(0, 1000.0 * (ams_slot.remaining_percent / 100)),
                    status=SpoolStatus.ACTIVE,
                    location_printer_id=printer_id,
                    location_slot=ams_slot.slot_number,
                )
                db.add(new_spool)
                db.flush()

                color_name = f"{library_entry.brand} {library_entry.name}".strip()
                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.assigned_spool_id = new_spool.id
                db_slot.spool_confirmed = True
                db_slot.loaded_at = datetime.now(timezone.utc)

                updated_slots.append({
                    "slot": ams_slot.slot_number,
                    "type": ftype.value,
                    "color": color_name,
                    "color_hex": color_hex,
                    "spool_id": new_spool.id,
                    "rfid": ams_slot.rfid_tag,
                    "matched": "rfid_auto_created",
                })
                continue

            # Priority 1: Match against local filament library
            library_match = find_library_match(color_hex, ams_slot.filament_type)

            if library_match:
                color_name = f"{library_match.brand} {library_match.name}".strip()

                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.spoolman_spool_id = None
                db_slot.loaded_at = datetime.now(timezone.utc)
                updated_slots.append({
                    "slot": ams_slot.slot_number,
                    "type": ftype.value,
                    "color": color_name,
                    "color_hex": color_hex,
                    "matched": "library",
                })
                continue

            # Priority 2: Match against Spoolman (if configured)
            spoolman_match = find_spoolman_match(color_hex, ams_slot.filament_type)

            if spoolman_match:
                filament = spoolman_match.get("filament", {})
                vendor = filament.get("vendor", {})
                vendor_name = vendor.get("name", "") if vendor else ""
                filament_name = filament.get("name", "")

                color_name = f"{vendor_name} {filament_name}".strip() if vendor_name else filament_name
                spoolman_id = spoolman_match.get("id")

                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.spoolman_spool_id = spoolman_id
                db_slot.loaded_at = datetime.now(timezone.utc)
                updated_slots.append({
                    "slot": ams_slot.slot_number,
                    "type": ftype.value,
                    "color": color_name,
                    "color_hex": color_hex,
                    "spoolman_id": spoolman_id,
                    "matched": "spoolman",
                })
                continue

            # Priority 3: Fall back to color name detection
            color_name = get_color_name(color_hex)

            db_slot.filament_type = ftype
            db_slot.color = color_name
            db_slot.color_hex = color_hex
            db_slot.spoolman_spool_id = None
            db_slot.loaded_at = datetime.now(timezone.utc)
            updated_slots.append({
                "slot": ams_slot.slot_number,
                "type": ftype.value,
                "color": color_name,
                "color_hex": color_hex,
                "matched": "color_analysis",
            })
        else:
            # Empty slot
            db_slot.color = None
            db_slot.color_hex = None
            db_slot.spoolman_spool_id = None
            updated_slots.append({
                "slot": ams_slot.slot_number,
                "type": db_slot.filament_type.value,
                "color": None,
                "empty": True,
            })

    # Check for mismatches with assigned spools
    mismatches = []
    for db_slot in db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).all():
        if db_slot.assigned_spool_id and db_slot.assigned_spool:
            spool = db_slot.assigned_spool
            if spool.filament:
                # Check color mismatch
                spool_hex = (spool.filament.color_hex or "").lower().replace("#", "")
                slot_hex = (db_slot.color_hex or "").lower().replace("#", "")

                # Color distance check (allow some tolerance)
                mismatch = False
                mismatch_reason = []

                if spool_hex and slot_hex and spool_hex != slot_hex:
                    # Calculate color distance
                    try:
                        r1, g1, b1 = int(spool_hex[0:2], 16), int(spool_hex[2:4], 16), int(spool_hex[4:6], 16)
                        r2, g2, b2 = int(slot_hex[0:2], 16), int(slot_hex[2:4], 16), int(slot_hex[4:6], 16)
                        distance = ((r1-r2)**2 + (g1-g2)**2 + (b1-b2)**2) ** 0.5
                        if distance > 60:  # Threshold for "different color"
                            mismatch = True
                            mismatch_reason.append(f"Color: spool={spool_hex}, slot={slot_hex}")
                    except:
                        pass

                if mismatch and not spool.rfid_tag:
                    db_slot.spool_confirmed = False
                    mismatches.append({
                        "slot_number": db_slot.slot_number,
                        "assigned_spool_id": spool.id,
                        "reasons": mismatch_reason,
                    })

    db.commit()

    log_audit(db, "sync", "printer", printer_id, {"slots_synced": len(updated_slots), "mismatches": len(mismatches)})
    return {
        "success": True,
        "printer_id": printer_id,
        "printer_name": printer.name,
        "slots_synced": len(updated_slots),
        "slots": updated_slots,
        "mismatches": mismatches,
    }


# ====================================================================
# Lights
# ====================================================================

@router.post("/printers/{printer_id}/lights", tags=["Printers"])
def toggle_printer_lights(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Toggle chamber lights on/off for a Bambu printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if not printer.api_type or printer.api_type.lower() != "bambu":
        raise HTTPException(status_code=400, detail="Light control only supported for Bambu printers")
    if not printer.api_host or not printer.api_key:
        raise HTTPException(status_code=400, detail="Printer connection not configured")

    decrypted_key = crypto.decrypt(printer.api_key)
    if "|" not in decrypted_key:
        raise HTTPException(status_code=400, detail="Invalid api_key format")

    serial, access_code = decrypted_key.split("|", 1)

    # Determine desired state (toggle from current)
    turn_on = not printer.lights_on

    try:
        from bambu_adapter import BambuPrinter

        bambu = BambuPrinter(
            ip=printer.api_host,
            serial=serial,
            access_code=access_code,
        )

        if not bambu.connect():
            raise HTTPException(status_code=503, detail="Failed to connect to printer")

        time.sleep(3)

        payload = {
            'system': {
                'sequence_id': '0',
                'command': 'ledctrl',
                'led_node': 'chamber_light',
                'led_mode': 'on' if turn_on else 'off',
            }
        }
        success = bambu._publish(payload)

        time.sleep(1)
        bambu.disconnect()

        if not success:
            raise HTTPException(status_code=503, detail="Failed to send light command")

        # Update DB immediately + set cooldown so monitor doesn't overwrite
        printer.lights_on = turn_on
        printer.lights_toggled_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(printer)

        return {"lights_on": turn_on, "message": f"Lights {'on' if turn_on else 'off'}"}

    except ImportError:
        raise HTTPException(status_code=500, detail="bambu_adapter not installed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Printer connection error: {str(e)}")


# ====================================================================
# Test Connection
# ====================================================================

@router.post("/printers/test-connection", tags=["Printers"])
def test_printer_connection(request: TestConnectionRequest, current_user: dict = Depends(require_role("operator"))):
    """
    Test connection to a printer without saving.

    Used by the UI to validate credentials before saving.
    """
    api_type = request.api_type.lower()

    if api_type == "bambu":
        if not request.serial or not request.access_code:
            raise HTTPException(status_code=400, detail="Serial and access_code required for Bambu printers")

        try:
            from bambu_adapter import BambuPrinter

            bambu = BambuPrinter(
                ip=request.api_host,
                serial=request.serial,
                access_code=request.access_code,
            )

            if not bambu.connect():
                return {
                    "success": False,
                    "error": "Failed to connect. Check IP, serial, and access code.",
                }

            # Wait for status
            time.sleep(2)
            bambu_status = bambu.get_status()
            bambu.disconnect()

            return {
                "success": True,
                "state": bambu_status.state.value,
                "bed_temp": bambu_status.bed_temp,
                "nozzle_temp": bambu_status.nozzle_temp,
                "ams_slots": len(bambu_status.ams_slots),
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
                return {
                    "success": True,
                    "state": info.get("state", "unknown"),
                    "bed_temp": 0,
                    "nozzle_temp": 0,
                    "ams_slots": 0,
                }
            return {"success": False, "error": f"Moonraker returned HTTP {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif api_type == "prusalink":
        import httpx as httpx_client
        try:
            r = httpx_client.get(f"http://{request.api_host}/api/version", timeout=5)
            if r.status_code == 200:
                info = r.json()
                return {
                    "success": True,
                    "state": "connected",
                    "bed_temp": 0,
                    "nozzle_temp": 0,
                    "ams_slots": 0,
                }
            return {"success": False, "error": f"PrusaLink returned HTTP {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif api_type == "elegoo":
        import httpx as httpx_client
        try:
            r = httpx_client.get(f"http://{request.api_host}:3030", timeout=5)
            return {
                "success": True,
                "state": "connected",
                "bed_temp": 0,
                "nozzle_temp": 0,
                "ams_slots": 0,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    else:
        return {"success": False, "error": f"Unknown printer type: {request.api_type}"}


# ====================================================================
# Bambu Integration
# ====================================================================

@router.post("/bambu/test-connection", tags=["Bambu"])
async def test_bambu_printer_connection(request: BambuConnectionTest, current_user: dict = Depends(require_role("operator"))):
    """Test connection to a Bambu Lab printer via local MQTT."""
    if not BAMBU_AVAILABLE:
        raise HTTPException(status_code=501, detail="Bambu integration not available. Install: pip install paho-mqtt")

    result = test_bambu_connection(
        ip_address=request.ip_address,
        serial_number=request.serial_number,
        access_code=request.access_code,
    )
    return result


@router.post("/printers/{printer_id}/bambu/sync-ams", response_model=BambuSyncResult, tags=["Bambu"])
async def sync_bambu_ams(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Sync AMS filament slots from a Bambu Lab printer."""
    if not BAMBU_AVAILABLE:
        raise HTTPException(status_code=501, detail="Bambu integration not available")

    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if printer.api_type != "bambu":
        raise HTTPException(status_code=400, detail=f"Printer is type '{printer.api_type}', not 'bambu'")

    if not printer.api_host:
        raise HTTPException(status_code=400, detail="Printer has no Bambu config (api_host empty)")

    try:
        parts = crypto.decrypt(printer.api_key).split("|")
        if len(parts) != 2:
            raise ValueError()
        serial_number, access_code = parts; ip_address = printer.api_host
    except:
        raise HTTPException(status_code=400, detail="Invalid Bambu config. Expected: ip|serial|access_code")

    library_filaments = db.query(FilamentLibrary).all()
    library_list = [
        {"id": f"lib_{f.id}", "brand": f.brand, "name": f.name, "material": f.material, "color_hex": f.color_hex}
        for f in library_filaments
    ]

    spoolman_list = []
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=5)
                if resp.status_code == 200:
                    for spool in resp.json():
                        filament = spool.get("filament", {})
                        spoolman_list.append({
                            "id": f"spool_{spool['id']}",
                            "brand": filament.get("vendor", {}).get("name", "Unknown"),
                            "name": filament.get("name", "Unknown"),
                            "material": filament.get("material", "PLA"),
                            "color_hex": filament.get("color_hex"),
                        })
        except:
            pass

    result = sync_ams_filaments(
        ip_address=ip_address,
        serial_number=serial_number,
        access_code=access_code,
        library_filaments=library_list,
        spoolman_spools=spoolman_list,
    )

    if not result.success:
        raise HTTPException(status_code=502, detail=result.message)

    slots_updated = 0
    unmatched_slots = []
    slot_responses = []

    for slot_info in result.slots:
        slot_dict = slot_to_dict(slot_info)
        slot_responses.append(AMSSlotResponse(**slot_dict))

        if slot_info.is_empty:
            continue

        db_slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == slot_info.slot_number,
        ).first()

        if not db_slot:
            db_slot = FilamentSlot(
                printer_id=printer_id,
                slot_number=slot_info.slot_number,
                filament_type=FilamentType.EMPTY,
            )
            db.add(db_slot)

        try:
            db_slot.filament_type = FilamentType(slot_info.mapped_type)
        except ValueError:
            try:
                db_slot.filament_type = FilamentType.from_bambu_code(slot_info.filament_type)
            except:
                db_slot.filament_type = FilamentType.OTHER
            unmatched_slots.append(slot_info.slot_number)

        # Override color for support materials (Bambu reports black but they are natural/white)
        if slot_info.mapped_type in ["PLA_SUPPORT", "SUPPORT", "PVA", "HIPS", "BVOH"]:
            db_slot.color_hex = "#F5F5F5"
            db_slot.color = "Natural"
        else:
            db_slot.color_hex = slot_info.color_hex
            db_slot.color = slot_info.color_name or slot_info.brand
        db_slot.loaded_at = datetime.now(timezone.utc)

        if slot_info.matched_filament:
            db_slot.spoolman_id = slot_info.matched_filament.get('id')

        slots_updated += 1

    db.commit()

    return BambuSyncResult(
        success=True,
        printer_name=result.printer_name,
        slots=slot_responses,
        message=f"Synced {slots_updated} slots from AMS",
        slots_updated=slots_updated,
        unmatched_slots=unmatched_slots,
    )


@router.get("/bambu/filament-types", tags=["Bambu"])
async def list_bambu_filament_types():
    """List Bambu filament type codes and their mappings."""
    if not BAMBU_AVAILABLE:
        return {"error": "Bambu integration not available"}
    return {"bambu_to_normalized": BAMBU_FILAMENT_TYPE_MAP}


@router.patch("/printers/{printer_id}/slots/{slot_number}/manual-assign", tags=["Bambu"])
async def manual_slot_assignment(
    printer_id: int,
    slot_number: int,
    assignment: ManualSlotAssignment,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Manually assign filament to a slot when auto-matching fails."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()

    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    if assignment.filament_library_id:
        lib_entry = db.query(FilamentLibrary).filter(
            FilamentLibrary.id == assignment.filament_library_id,
        ).first()
        if not lib_entry:
            raise HTTPException(status_code=404, detail="Library filament not found")
        try:
            slot.filament_type = FilamentType(lib_entry.material.upper())
        except ValueError:
            slot.filament_type = FilamentType.OTHER
        slot.color = lib_entry.name
        slot.color_hex = lib_entry.color_hex
        slot.spoolman_id = f"lib_{lib_entry.id}"
    else:
        if assignment.filament_type:
            try:
                slot.filament_type = FilamentType(assignment.filament_type.upper())
            except ValueError:
                slot.filament_type = FilamentType.OTHER
        if assignment.color:
            slot.color = assignment.color
        if assignment.color_hex:
            slot.color_hex = assignment.color_hex

    slot.loaded_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(slot)

    return {
        "success": True,
        "slot_number": slot_number,
        "filament_type": slot.filament_type.value if slot.filament_type else None,
        "color": slot.color,
        "color_hex": slot.color_hex,
    }


@router.get("/printers/{printer_id}/unmatched-slots", tags=["Bambu"])
async def get_unmatched_slots(printer_id: int, db: Session = Depends(get_db)):
    """Get slots that need manual filament assignment."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    unmatched = []
    for slot in printer.filament_slots:
        needs_attention = False
        reason = []
        if slot.filament_type == FilamentType.OTHER:
            needs_attention = True
            reason.append("Unknown filament type")
        if not slot.spoolman_spool_id and slot.color_hex:
            needs_attention = True
            reason.append("No library match")
        if needs_attention:
            unmatched.append({
                "slot_number": slot.slot_number,
                "current_type": slot.filament_type.value if slot.filament_type else None,
                "color": slot.color,
                "color_hex": slot.color_hex,
                "reason": ", ".join(reason),
            })

    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "unmatched_count": len(unmatched),
        "slots": unmatched,
    }


# ====================================================================
# Live Status
# ====================================================================

@router.get("/printers/{printer_id}/live-status", tags=["Printers"])
def get_printer_live_status(printer_id: int, db: Session = Depends(get_db)):
    """Get real-time status from printer via MQTT."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if not printer.api_host or not printer.api_key:
        return {"error": "Printer not configured for MQTT"}

    try:
        parts = crypto.decrypt(printer.api_key).split("|")
        if len(parts) != 2:
            return {"error": "Invalid credentials format"}
        serial, access_code = parts
    except:
        return {"error": "Could not decrypt credentials"}

    # Quick MQTT connection to get status
    from bambu_adapter import BambuPrinter

    status_data = {}
    def on_status(s):
        nonlocal status_data
        status_data = s.raw_data.get('print', {})

    try:
        bp = BambuPrinter(
            ip=printer.api_host,
            serial=serial,
            access_code=access_code,
            on_status_update=on_status,
        )
        if bp.connect():
            # Wait for first status
            timeout = 5
            start = time.time()
            while not status_data and (time.time() - start) < timeout:
                time.sleep(0.2)
            bp.disconnect()

            if status_data:
                return {
                    "printer_id": printer_id,
                    "printer_name": printer.name,
                    "gcode_state": status_data.get('gcode_state'),
                    "job_name": status_data.get('subtask_name'),
                    "progress": status_data.get('mc_percent'),
                    "layer": status_data.get('layer_num'),
                    "total_layers": status_data.get('total_layer_num'),
                    "time_remaining": status_data.get('mc_remaining_time'),
                    "bed_temp": status_data.get('bed_temper'),
                    "bed_target": status_data.get('bed_target_temper'),
                    "nozzle_temp": status_data.get('nozzle_temper'),
                    "nozzle_target": status_data.get('nozzle_target_temper'),
                    "wifi_signal": status_data.get('wifi_signal'),
                }
            else:
                return {"error": "Timeout waiting for status"}
        else:
            return {"error": "Connection failed"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/printers/live-status", tags=["Printers"])
def get_all_printers_live_status(db: Session = Depends(get_db)):
    """Get real-time status from all Bambu printers."""
    printers = db.query(Printer).filter(
        Printer.api_host.isnot(None),
        Printer.api_key.isnot(None),
    ).all()

    results = []
    for printer in printers:
        printer_status = get_printer_live_status(printer.id, db)
        results.append(printer_status)

    return results


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
            existing = db.execute(text("SELECT 1 FROM printer_tags WHERE printer_id = :pid AND tag = :tag"),
                                  {"pid": pid, "tag": tag}).fetchone()
            if not existing:
                db.execute(text("INSERT INTO printer_tags (printer_id, tag) VALUES (:pid, :tag)"),
                           {"pid": pid, "tag": tag})
            count += 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    db.commit()
    return {"status": "ok", "affected": count}


# ====================================================================
# Printer Commands (Stop / Pause / Resume)
# ====================================================================

@router.post("/printers/{printer_id}/stop", tags=["Printers"])
async def stop_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Emergency stop - cancel current print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Moonraker uses cancel_print, others use stop_print
    action = "cancel_print" if printer.api_type == "moonraker" else "stop_print"
    if _send_printer_command(printer, action):
        db.execute(text("UPDATE printers SET gcode_state = 'IDLE', print_stage = 'Idle' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print stopped"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


@router.post("/printers/{printer_id}/pause", tags=["Printers"])
async def pause_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Pause current print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if _send_printer_command(printer, "pause_print"):
        db.execute(text("UPDATE printers SET gcode_state = 'PAUSED' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print paused"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


@router.post("/printers/{printer_id}/resume", tags=["Printers"])
async def resume_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Resume paused print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if _send_printer_command(printer, "resume_print"):
        db.execute(text("UPDATE printers SET gcode_state = 'RUNNING' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print resumed"}
    raise HTTPException(status_code=503, detail="Printer unreachable or command failed")


# ====================================================================
# AMS Environment
# ====================================================================

@router.get("/printers/{printer_id}/ams/environment", tags=["AMS"])
async def get_ams_environment(
    printer_id: int,
    hours: int = Query(default=24, ge=1, le=168),
    unit: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """
    Get AMS humidity/temperature history for charts.
    Returns time-series data for the specified time window.
    Default: last 24 hours. Max: 7 days (168 hours).
    """
    query = """
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM ams_telemetry
        WHERE printer_id = :printer_id
        AND recorded_at >= datetime('now', :hours_ago)
    """
    params = {
        "printer_id": printer_id,
        "hours_ago": f"-{hours} hours",
    }

    if unit is not None:
        query += " AND ams_unit = :unit"
        params["unit"] = unit

    query += " ORDER BY recorded_at ASC"

    rows = db.execute(text(query), params).fetchall()

    # Group by AMS unit
    units = {}
    for row in rows:
        u = row[0]
        if u not in units:
            units[u] = []
        units[u].append({
            "humidity": row[1],
            "temperature": row[2],
            "time": row[3],
        })

    return {
        "printer_id": printer_id,
        "hours": hours,
        "units": units,
    }


@router.get("/printers/{printer_id}/ams/current", tags=["AMS"])
async def get_ams_current(printer_id: int, db: Session = Depends(get_db)):
    """
    Get latest AMS environmental readings for a printer.
    Returns the most recent humidity/temperature per AMS unit.
    """
    rows = db.execute(text("""
        SELECT ams_unit, humidity, temperature, recorded_at
        FROM ams_telemetry
        WHERE printer_id = :pid
        AND recorded_at = (
            SELECT MAX(recorded_at) FROM ams_telemetry t2
            WHERE t2.printer_id = ams_telemetry.printer_id
            AND t2.ams_unit = ams_telemetry.ams_unit
        )
        ORDER BY ams_unit
    """), {"pid": printer_id}).fetchall()

    units = []
    for row in rows:
        # Map Bambu humidity scale: 1=dry, 5=wet
        hum = row[1]
        hum_label = {1: "Dry", 2: "Low", 3: "Moderate", 4: "High", 5: "Wet"}.get(hum, "Unknown") if hum else "N/A"
        units.append({
            "unit": row[0],
            "humidity": hum,
            "humidity_label": hum_label,
            "temperature": row[2],
            "recorded_at": row[3],
        })

    return {"printer_id": printer_id, "units": units}


# ====================================================================
# Smart Plug Control
# ====================================================================

@router.get("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def get_plug_config(printer_id: int, db: Session = Depends(get_db)):
    """Get smart plug configuration for a printer."""
    result = db.execute(text("""
        SELECT plug_type, plug_host, plug_entity_id, plug_auto_on, plug_auto_off,
               plug_cooldown_minutes, plug_power_state, plug_energy_kwh
        FROM printers WHERE id = :id
    """), {"id": printer_id}).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Printer not found")

    return {
        "type": result[0],
        "host": result[1],
        "entity_id": result[2],
        "auto_on": bool(result[3]) if result[3] is not None else True,
        "auto_off": bool(result[4]) if result[4] is not None else True,
        "cooldown_minutes": result[5] or 5,
        "power_state": result[6],
        "energy_kwh": result[7] or 0,
        "configured": result[0] is not None,
    }


@router.put("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def update_plug_config(printer_id: int, request: Request, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update smart plug configuration for a printer."""
    data = await request.json()

    # Validate plug type
    plug_type = data.get("type")
    if plug_type and plug_type not in ("tasmota", "homeassistant", "mqtt"):
        raise HTTPException(400, "Invalid plug type. Use: tasmota, homeassistant, mqtt")

    db.execute(text("""
        UPDATE printers SET
            plug_type = :plug_type,
            plug_host = :plug_host,
            plug_entity_id = :plug_entity_id,
            plug_auth_token = :plug_auth_token,
            plug_auto_on = :plug_auto_on,
            plug_auto_off = :plug_auto_off,
            plug_cooldown_minutes = :plug_cooldown_minutes
        WHERE id = :id
    """), {
        "id": printer_id,
        "plug_type": plug_type,
        "plug_host": data.get("host"),
        "plug_entity_id": data.get("entity_id"),
        "plug_auth_token": data.get("auth_token"),
        "plug_auto_on": data.get("auto_on", True),
        "plug_auto_off": data.get("auto_off", True),
        "plug_cooldown_minutes": data.get("cooldown_minutes", 5),
    })
    db.commit()

    return {"status": "ok", "message": "Smart plug configuration updated"}


@router.delete("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def remove_plug_config(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Remove smart plug configuration from a printer."""
    db.execute(text("""
        UPDATE printers SET
            plug_type = NULL, plug_host = NULL, plug_entity_id = NULL,
            plug_auth_token = NULL, plug_auto_on = 1, plug_auto_off = 1,
            plug_cooldown_minutes = 5, plug_power_state = NULL
        WHERE id = :id
    """), {"id": printer_id})
    db.commit()
    return {"status": "ok"}


@router.post("/printers/{printer_id}/plug/on", tags=["Smart Plug"])
async def plug_power_on(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Turn on a printer's smart plug."""
    result = smart_plug.power_on(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@router.post("/printers/{printer_id}/plug/off", tags=["Smart Plug"])
async def plug_power_off(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Turn off a printer's smart plug."""
    result = smart_plug.power_off(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@router.post("/printers/{printer_id}/plug/toggle", tags=["Smart Plug"])
async def plug_power_toggle(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Toggle a printer's smart plug."""
    result = smart_plug.power_toggle(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@router.get("/printers/{printer_id}/plug/energy", tags=["Smart Plug"])
async def plug_energy(printer_id: int):
    """Get current energy data from smart plug."""
    data = smart_plug.get_energy(printer_id)
    if data is None:
        raise HTTPException(400, "No energy data available")
    return data


@router.get("/printers/{printer_id}/plug/state", tags=["Smart Plug"])
async def plug_state(printer_id: int):
    """Query current power state from smart plug."""
    state = smart_plug.get_power_state(printer_id)
    if state is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": state}


@router.get("/settings/energy-rate", tags=["Smart Plug"])
async def get_energy_rate(db: Session = Depends(get_db)):
    """Get energy cost per kWh."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")).fetchone()
    return {"energy_cost_per_kwh": float(result[0]) if result else 0.12}


@router.put("/settings/energy-rate", tags=["Smart Plug"])
async def set_energy_rate(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set energy cost per kWh."""
    data = await request.json()
    rate = data.get("energy_cost_per_kwh", 0.12)
    db.execute(text(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('energy_cost_per_kwh', :rate)"
    ), {"rate": str(rate)})
    db.commit()
    return {"energy_cost_per_kwh": rate}


# ====================================================================
# Telemetry
# ====================================================================

@router.get("/printers/{printer_id}/telemetry", tags=["Telemetry"])
def get_printer_telemetry(printer_id: int, hours: int = Query(24, ge=1, le=168),
                          current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get timeseries telemetry data for a printer (recorded during prints)."""
    rows = db.execute(text(
        "SELECT recorded_at, bed_temp, nozzle_temp, bed_target, nozzle_target, fan_speed "
        "FROM printer_telemetry WHERE printer_id = :pid AND recorded_at > datetime('now', :cutoff) "
        "ORDER BY recorded_at ASC"
    ), {"pid": printer_id, "cutoff": f"-{hours} hours"}).fetchall()
    return [{"recorded_at": r[0], "bed_temp": r[1], "nozzle_temp": r[2],
             "bed_target": r[3], "nozzle_target": r[4], "fan_speed": r[5]} for r in rows]


@router.get("/printers/{printer_id}/hms-history", tags=["Telemetry"])
def get_hms_error_history(printer_id: int, days: int = Query(30, ge=1, le=90),
                          current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
    """Get HMS error history with occurrence timestamps."""
    rows = db.execute(text(
        "SELECT id, printer_id, code, message, severity, source, occurred_at "
        "FROM hms_error_history WHERE printer_id = :pid AND occurred_at > datetime('now', :cutoff) "
        "ORDER BY occurred_at DESC"
    ), {"pid": printer_id, "cutoff": f"-{days} days"}).fetchall()
    entries = [{"id": r[0], "printer_id": r[1], "code": r[2], "message": r[3],
                "severity": r[4], "source": r[5], "occurred_at": r[6]} for r in rows]
    # Frequency summary
    freq = {}
    for e in entries:
        key = e["code"]
        freq[key] = freq.get(key, 0) + 1
    return {"entries": entries, "frequency": freq, "total": len(entries)}


# ====================================================================
# Nozzle Lifecycle
# ====================================================================

@router.get("/printers/{printer_id}/nozzle", tags=["Telemetry"])
def get_current_nozzle(printer_id: int, current_user: dict = Depends(require_role("viewer")),
                       db: Session = Depends(get_db)):
    """Get the currently installed nozzle for a printer."""
    nozzle = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
        NozzleLifecycle.removed_at.is_(None),
    ).first()
    if not nozzle:
        return None
    return NozzleLifecycleResponse.model_validate(nozzle)


@router.post("/printers/{printer_id}/nozzle", tags=["Telemetry"])
def install_nozzle(printer_id: int, data: NozzleInstall,
                   current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Install a new nozzle (auto-retires the previous one)."""
    # Verify printer exists
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    # Retire current nozzle if any
    current = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
        NozzleLifecycle.removed_at.is_(None),
    ).first()
    if current:
        current.removed_at = datetime.now(timezone.utc)
    # Install new nozzle
    nozzle = NozzleLifecycle(
        printer_id=printer_id,
        nozzle_type=data.nozzle_type,
        nozzle_diameter=data.nozzle_diameter,
        notes=data.notes,
    )
    db.add(nozzle)
    db.commit()
    db.refresh(nozzle)
    return NozzleLifecycleResponse.model_validate(nozzle)


@router.patch("/printers/{printer_id}/nozzle/{nozzle_id}/retire", tags=["Telemetry"])
def retire_nozzle(printer_id: int, nozzle_id: int,
                  current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Retire a specific nozzle."""
    nozzle = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.id == nozzle_id,
        NozzleLifecycle.printer_id == printer_id,
    ).first()
    if not nozzle:
        raise HTTPException(status_code=404, detail="Nozzle not found")
    if nozzle.removed_at:
        raise HTTPException(status_code=400, detail="Nozzle already retired")
    nozzle.removed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(nozzle)
    return NozzleLifecycleResponse.model_validate(nozzle)


@router.get("/printers/{printer_id}/nozzle/history", tags=["Telemetry"])
def get_nozzle_history(printer_id: int, current_user: dict = Depends(require_role("viewer")),
                       db: Session = Depends(get_db)):
    """Get all nozzles (past and present) for a printer."""
    nozzles = db.query(NozzleLifecycle).filter(
        NozzleLifecycle.printer_id == printer_id,
    ).order_by(NozzleLifecycle.installed_at.desc()).all()
    return [NozzleLifecycleResponse.model_validate(n) for n in nozzles]
