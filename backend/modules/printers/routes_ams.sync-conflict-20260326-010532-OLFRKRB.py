"""AMS routes — AMS sync (multi-protocol: Bambu, Moonraker MMU)."""

from datetime import datetime, timezone
from typing import List, Optional
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role
from core.config import settings
import core.crypto as crypto
from modules.printers.models import Printer, FilamentSlot
from modules.inventory.models import Spool, FilamentLibrary
from modules.printers.schemas import FilamentSlotResponse
from core.base import FilamentType, SpoolStatus

# Bambu Lab Integration
try:
    from modules.printers.bambu_integration import (
        test_bambu_connection, sync_ams_filaments, slot_to_dict,
        map_bambu_filament_type, BAMBU_FILAMENT_TYPE_MAP, MQTT_AVAILABLE,
    )
    BAMBU_AVAILABLE = MQTT_AVAILABLE
except ImportError:
    BAMBU_AVAILABLE = False

log = logging.getLogger("odin.api")
router = APIRouter()


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
# AMS Sync
# ====================================================================

@router.post("/printers/{printer_id}/sync-ams", tags=["Printers"])
def sync_ams_state(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Sync AMS filament state from printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if not printer.api_type:
        raise HTTPException(status_code=400, detail="Printer api_type not configured")
    if not printer.api_host:
        raise HTTPException(status_code=400, detail="Printer api_host (IP) not configured")

    # ---- Moonraker / Klipper MMU sync ----
    if printer.api_type.lower() == "moonraker":
        from modules.printers.adapters.moonraker import MoonrakerPrinter
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

    decrypted_key = crypto.decrypt(printer.api_key)
    if "|" not in decrypted_key:
        raise HTTPException(status_code=400, detail="Invalid api_key format. Expected 'serial|access_code'")

    serial, access_code = decrypted_key.split("|", 1)

    try:
        from modules.printers.adapters.bambu import BambuPrinter

        bambu = BambuPrinter(ip=printer.api_host, serial=serial, access_code=access_code)
        if not bambu.connect():
            raise HTTPException(status_code=503, detail="Failed to connect to printer")
        time.sleep(2)
        bambu_status = bambu.get_status()
        bambu.disconnect()

    except ImportError:
        raise HTTPException(status_code=500, detail="bambu_adapter not installed")
    except Exception as e:
        log.error(f"Printer connection error (Bambu sync): {e}")
        raise HTTPException(status_code=503, detail="Printer connection error. Check printer IP and credentials.")

    filament_type_map = {
        "PLA": FilamentType.PLA, "PETG": FilamentType.PETG, "ABS": FilamentType.ABS,
        "ASA": FilamentType.ASA, "TPU": FilamentType.TPU, "PA": FilamentType.PA,
        "PC": FilamentType.PC, "PVA": FilamentType.PVA,
        "PLA-S": FilamentType.PLA_SUPPORT, "PA-S": FilamentType.PLA_SUPPORT,
        "PETG-S": FilamentType.PLA_SUPPORT, "PA-CF": FilamentType.NYLON_CF,
        "PA-GF": FilamentType.NYLON_GF, "PET-CF": FilamentType.PETG_CF,
        "PLA-CF": FilamentType.PLA_CF,
    }

    local_library = db.query(FilamentLibrary).all()

    spoolman_spools = []
    if settings.spoolman_url:
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{settings.spoolman_url}/api/v1/spool")
                if resp.status_code == 200:
                    spoolman_spools = resp.json()
        except Exception:
            pass

    def find_library_match(hex_code, filament_type):
        if not hex_code:
            return None
        hex_lower = hex_code.lower()
        for f in local_library:
            if f.color_hex and f.color_hex.lower() == hex_lower:
                if f.material and f.material.upper() == filament_type.upper():
                    return f
        for f in local_library:
            if f.color_hex and f.color_hex.lower() == hex_lower:
                return f
        return None

    def find_spoolman_match(hex_code, filament_type):
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
        if not rfid_tag:
            return None
        return db.query(Spool).filter(Spool.rfid_tag == rfid_tag).first()

    def get_color_name(hex_code):
        if not hex_code:
            return None
        hex_lower = hex_code.lower()
        color_map = {
            "000000": "Black", "ffffff": "White", "f5f5f5": "Off White",
            "ff0000": "Red", "00ff00": "Green", "0000ff": "Blue",
            "ffff00": "Yellow", "ff00ff": "Magenta", "00ffff": "Cyan",
            "ffa500": "Orange", "800080": "Purple", "ffc0cb": "Pink",
            "808080": "Gray", "c0c0c0": "Silver",
        }
        if hex_lower in color_map:
            return color_map[hex_lower]
        try:
            r = int(hex_lower[0:2], 16)
            g = int(hex_lower[2:4], 16)
            b = int(hex_lower[4:6], 16)
            if abs(r - g) < 25 and abs(g - b) < 25 and abs(r - b) < 25:
                avg = (r + g + b) // 3
                if avg < 40: return "Black"
                elif avg < 100: return "Dark Gray"
                elif avg < 160: return "Gray"
                elif avg < 220: return "Light Gray"
                else: return "White"
            max_val = max(r, g, b)
            if r == max_val and r > g + 30 and r > b + 30:
                if g > 150: return "Orange" if g < 200 else "Yellow"
                elif b > 100: return "Pink"
                return "Red"
            elif g == max_val and g > b:
                if r > 80 and g > 80 and b < g and r < g: return "Olive Green"
                elif b > 150: return "Teal"
                return "Green"
            elif b == max_val and b > r + 30 and b > g + 30:
                if r > 100: return "Purple"
                return "Blue"
            elif r > 200 and g > 200 and b < 100: return "Yellow"
            elif r > 200 and g < 150 and b > 200: return "Magenta"
            elif r < 100 and g > 200 and b > 200: return "Cyan"
            return f"#{hex_code.upper()}"
        except Exception:
            return f"#{hex_code.upper()}"

    updated_slots = []
    for ams_slot in bambu_status.ams_slots:
        db_slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == ams_slot.slot_number,
        ).first()
        if not db_slot:
            continue

        color_hex = ams_slot.color_hex[:6] if ams_slot.color_hex else None
        ftype = filament_type_map.get(ams_slot.filament_type.upper(), FilamentType.PLA)

        if not ams_slot.empty:
            rfid_match = find_spool_by_rfid(ams_slot.rfid_tag)

            if rfid_match:
                color_name = f"{rfid_match.filament.brand} {rfid_match.filament.name}".strip() if rfid_match.filament else "Unknown"
                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.assigned_spool_id = rfid_match.id
                db_slot.spool_confirmed = True
                db_slot.loaded_at = datetime.now(timezone.utc)
                rfid_match.location_printer_id = printer_id
                rfid_match.location_slot = ams_slot.slot_number
                rfid_match.storage_location = None
                if ams_slot.remaining_percent >= 0:
                    rfid_match.remaining_weight_g = rfid_match.initial_weight_g * (ams_slot.remaining_percent / 100)
                updated_slots.append({
                    "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "spool_id": rfid_match.id,
                    "rfid": ams_slot.rfid_tag, "matched": "rfid",
                    "remaining_percent": ams_slot.remaining_percent,
                })
                continue

            if ams_slot.rfid_tag and not rfid_match:
                import uuid
                sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                library_entry = db.query(FilamentLibrary).filter(
                    FilamentLibrary.brand == "Bambu Lab",
                    FilamentLibrary.name == sub_brand,
                    FilamentLibrary.material == ftype.value,
                ).first()
                if not library_entry:
                    sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                    existing = db.query(FilamentLibrary).filter(
                        FilamentLibrary.brand == "Bambu Lab",
                        FilamentLibrary.name == sub_brand,
                        FilamentLibrary.material == ftype.value,
                    ).first()
                    if existing:
                        library_entry = existing
                    else:
                        sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                        new_lib = FilamentLibrary(
                            brand="Bambu Lab", name=sub_brand,
                            material=ftype.value, color_hex=color_hex,
                        )
                        db.add(new_lib)
                        db.flush()
                        library_entry = new_lib

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
                    "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "spool_id": new_spool.id,
                    "rfid": ams_slot.rfid_tag, "matched": "rfid_auto_created",
                })
                continue

            library_match = find_library_match(color_hex, ams_slot.filament_type)
            if library_match:
                color_name = f"{library_match.brand} {library_match.name}".strip()
                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.spoolman_spool_id = None
                db_slot.loaded_at = datetime.now(timezone.utc)
                updated_slots.append({
                    "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "matched": "library",
                })
                continue

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
                    "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "spoolman_id": spoolman_id, "matched": "spoolman",
                })
                continue

            color_name = get_color_name(color_hex)
            db_slot.filament_type = ftype
            db_slot.color = color_name
            db_slot.color_hex = color_hex
            db_slot.spoolman_spool_id = None
            db_slot.loaded_at = datetime.now(timezone.utc)
            updated_slots.append({
                "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                "color_hex": color_hex, "matched": "color_analysis",
            })
        else:
            db_slot.color = None
            db_slot.color_hex = None
            db_slot.spoolman_spool_id = None
            updated_slots.append({
                "slot": ams_slot.slot_number, "type": db_slot.filament_type.value,
                "color": None, "empty": True,
            })

    mismatches = []
    for db_slot in db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).all():
        if db_slot.assigned_spool_id and db_slot.assigned_spool:
            spool = db_slot.assigned_spool
            if spool.filament:
                spool_hex = (spool.filament.color_hex or "").lower().replace("#", "")
                slot_hex = (db_slot.color_hex or "").lower().replace("#", "")
                mismatch = False
                mismatch_reason = []
                if spool_hex and slot_hex and spool_hex != slot_hex:
                    try:
                        r1, g1, b1 = int(spool_hex[0:2], 16), int(spool_hex[2:4], 16), int(spool_hex[4:6], 16)
                        r2, g2, b2 = int(slot_hex[0:2], 16), int(slot_hex[2:4], 16), int(slot_hex[4:6], 16)
                        distance = ((r1-r2)**2 + (g1-g2)**2 + (b1-b2)**2) ** 0.5
                        if distance > 60:
                            mismatch = True
                            mismatch_reason.append(f"Color: spool={spool_hex}, slot={slot_hex}")
                    except Exception:
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
        "success": True, "printer_id": printer_id, "printer_name": printer.name,
        "slots_synced": len(updated_slots), "slots": updated_slots, "mismatches": mismatches,
    }
