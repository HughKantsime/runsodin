"""AMS routes — AMS sync (multi-protocol: Bambu, Moonraker MMU)."""
from core.base import slot_filament_value

from modules.inventory.services import assign_slot, lock_slot_graph
from core.interfaces.rfid_identity import rfid_identity

from datetime import datetime, timezone
from typing import List, Optional
import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.orm import Session
from sqlalchemy import text, or_
from sqlalchemy.exc import OperationalError, IntegrityError

from core.spoolman_settings import get_spoolman_url
from core.db import get_db
from core.dependencies import log_audit
from core.models import AuditLog
from core.rbac import require_role, check_org_access, require_action
from core.config import settings
import core.crypto as crypto
from modules.printers.models import Printer, FilamentSlot
from modules.inventory.models import Spool, FilamentLibrary
from modules.printers.schemas import FilamentSlotResponse
from modules.printers.ledger_display import ensure_local_filaments
from core.base import FilamentType

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

def _ams_mutation_graph(db, printer_id, tags, uuids=(), trays=()):
    rfid = rfid_identity()
    slots = db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).populate_existing().all()
    matches = db.query(Spool).filter(or_(Spool.rfid_tag.in_(tags), Spool.tray_uuid.in_(uuids))).populate_existing().all() if tags or uuids else []
    org = db.query(Printer.org_id).filter(Printer.id == printer_id).scalar()
    candidates = rfid.candidate_rows(db, org) if trays else []
    spool_ids = {slot.assigned_spool_id for slot in slots if slot.assigned_spool_id is not None}
    spool_ids.update(spool.id for spool in matches)
    spool_ids.update(row[0] for row in candidates)
    displaced = db.query(Spool).filter(Spool.location_printer_id == printer_id).populate_existing().all()
    spool_ids.update(spool.id for spool in displaced)
    previous = db.query(FilamentSlot).filter(FilamentSlot.assigned_spool_id.in_(spool_ids)).populate_existing().all()
    slots = list({slot.id: slot for slot in slots + previous}.values())
    spools = db.query(Spool).filter(Spool.id.in_(spool_ids)).populate_existing().all() if spool_ids else []
    if {spool.id for spool in spools} != spool_ids:
        raise HTTPException(409, "Spool association unavailable for this organization")
    for spool_id in spool_ids:
        linked = [slot for slot in slots if slot.assigned_spool_id == spool_id]
        if len(linked) > 1:
            raise HTTPException(409, f"AMS association ambiguous: spool_id={spool_id} filament_slot_ids={sorted(s.id for s in linked)}; use Resolve inventory conflicts")
    return (
        {slot.id: (slot.assigned_spool_id, slot.printer_id, slot.slot_number) for slot in slots},
        {spool.id: (spool.location_printer_id, spool.location_slot) for spool in spools},
        {**{spool.rfid_tag: spool.id for spool in matches if spool.rfid_tag},
         **{"uuid:" + spool.tray_uuid: spool.id for spool in matches if spool.tray_uuid}},
        candidates,
    )


def _lock_ams_mutation(db, printer_id, current_user, tags, acquired_connection, uuids=(), trays=()):
    """Lock and recheck the complete known mutation graph after acquisition.

    PostgreSQL locks sorted printer, slot, then spool rows with NOWAIT. SQLite
    takes a writer boundary before fresh reads. Linkage changes abort the
    complete mutation phase before writes; locks never span network acquisition.
    """
    rfid = rfid_identity()
    try:
        lock_slot_graph(db)
        if db.bind.dialect.name == "sqlite":
            db.execute(text("UPDATE printers SET name=name WHERE id=:id"), {"id": printer_id})
        db.expire_all()
        before = _ams_mutation_graph(db, printer_id, tags, uuids, trays)
        printer_ids = {printer_id} | {value[1] for value in before[0].values()} | {value[0] for value in before[1].values() if value[0] is not None}
        printers = db.query(Printer).filter(Printer.id.in_(printer_ids)).order_by(Printer.id).with_for_update(nowait=True).populate_existing().all()
        db.query(FilamentSlot).filter(FilamentSlot.id.in_(before[0])).order_by(FilamentSlot.id).with_for_update(nowait=True).populate_existing().all()
        spools = db.query(Spool).filter(Spool.id.in_(before[1])).order_by(Spool.id).with_for_update(nowait=True).populate_existing().all()
        db.expire_all()
        if _ams_mutation_graph(db, printer_id, tags, uuids, trays) != before:
            raise HTTPException(409, "AMS associations changed; retry synchronization")
        printers = {row.id: row for row in printers}
        printer = printers.get(printer_id)
        if not printer or not check_org_access(current_user, printer.org_id):
            raise HTTPException(404, "Printer not found")
        if (printer.api_type, printer.api_host, printer.api_key) != acquired_connection:
            raise HTTPException(409, "Printer configuration changed; retry synchronization")
        if set(printers) != printer_ids:
            raise HTTPException(409, "Spool association unavailable for this organization")
        if (tags or uuids) and any(p.org_id is not None and p.org_id != printer.org_id for p in printers.values()):
            raise HTTPException(409, "Spool association unavailable for this organization")
        for spool in spools:
            if (spool.org_id is not None and spool.org_id != printer.org_id) or not check_org_access(current_user, spool.org_id):
                raise HTTPException(409, "Spool association unavailable for this organization")
            if spool.location_printer_id is not None and not check_org_access(current_user, printers[spool.location_printer_id].org_id):
                raise HTTPException(409, "Spool association unavailable for this organization")
        from modules.inventory.services import repair_stale_locations
        repair_stale_locations(db, spools, current_user['id'])
        by_id = {spool.id: spool for spool in spools}
        return printer, rfid.LockedMatches({key: by_id[value] for key, value in before[2].items()}, before[3])
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, "sqlstate", None) == "55P03" or getattr(exc.orig, "sqlite_errorcode", None) in (5, 6, 517):
            raise HTTPException(409, "AMS synchronization is busy; retry") from None
        raise
    except HTTPException:
        db.rollback()
        raise


@router.post("/printers/{printer_id}/sync-ams", tags=["Printers"])
def sync_ams_state(printer_id: int, current_user: dict = Depends(require_action("printers.edit", scope="write:printers")), db: Session = Depends(get_db)):
    """Sync AMS filament state from printer."""
    rfid = rfid_identity()
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")
    ensure_local_filaments(printer_id)

    def authorize_spool(spool):
        if not spool:
            return
        if not check_org_access(current_user, spool.org_id):
            raise HTTPException(status_code=409, detail="Spool association unavailable for this organization")
        if spool.location_printer_id is not None:
            previous = db.query(Printer).filter(Printer.id == spool.location_printer_id).first()
            if previous is None or not check_org_access(current_user, previous.org_id):
                raise HTTPException(status_code=409, detail="Spool association unavailable for this organization")

    # Validate legacy/poisoned associations before any acquisition or write.
    for existing_slot in db.query(FilamentSlot).filter(FilamentSlot.printer_id == printer_id).all():
        authorize_spool(existing_slot.assigned_spool)

    if not printer.api_type:
        raise HTTPException(status_code=400, detail="Printer api_type not configured")
    if not printer.api_host:
        raise HTTPException(status_code=400, detail="Printer api_host (IP) not configured")

    acquired_connection = (printer.api_type, printer.api_host, printer.api_key)

    # ---- Stock Anycubic ACE read-only acquisition ----
    if printer.api_type.lower() == "anycubic_lan":
        from modules.printers.adapters.anycubic_lan import AnycubicLANPrinter
        from modules.printers.parsing.anycubic_lan import filament_slots
        client = AnycubicLANPrinter(printer.api_host)
        try:
            if not client.connect() or not client.poll():
                raise HTTPException(502, "Anycubic LAN connection failed")
            deadline = time.monotonic() + 5
            slots = []
            while time.monotonic() < deadline:
                slots = filament_slots(client.get_status().boxes)
                if slots:
                    break
                time.sleep(0.1)
            if not slots:
                raise HTTPException(502, "No ACE slot report received")
        finally:
            client.disconnect()
        printer, _ = _lock_ams_mutation(db, printer_id, current_user, [], acquired_connection)
        from core.interfaces.inventory_moves import inventory_moves
        inventory_moves().observe_slots(db, printer_id,
            [(s['slot_number'], s['material'] == 'EMPTY', None) for s in slots], set(), current_user)
        updated = []
        for observed in slots:
            slot_num = observed['slot_number']
            slot = db.query(FilamentSlot).filter_by(printer_id=printer_id, slot_number=slot_num).first()
            if slot is None:
                slot = FilamentSlot(printer_id=printer_id, slot_number=slot_num)
                db.add(slot)
            slot.filament_type = FilamentType(slot_filament_value(observed['material']))
            slot.color = observed['material'] if observed['material'] != 'EMPTY' else None
            if observed['color_hex'] is not None or observed['material'] == 'EMPTY':
                slot.color_hex = observed['color_hex'] if observed['material'] != 'EMPTY' else None
            updated.append({"slot": slot_num, "type": observed['material'], "color": observed['color_hex'],
                            "empty": observed['material'] == 'EMPTY', "loaded": observed['loaded'],
                            "active": observed['active'], "remaining_percent": observed['remaining'],
                            "ams_id": observed['box_id'], "tray_id": observed['slot_index'],
                            "box_model": observed['box_model']})
        printer.slot_count = max(s['slot_number'] for s in slots)
        log_audit(db, "sync", "printer", printer_id, {"slots_synced": len(updated), "source": "anycubic_lan"})
        db.commit()
        return {"success": True, "printer_id": printer_id, "printer_name": printer.name,
                "slots_synced": len(updated), "slots": updated, "mismatches": []}

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
        printer, _ = _lock_ams_mutation(db, printer_id, current_user, [], acquired_connection)
        from core.interfaces.inventory_moves import inventory_moves
        inventory_moves().observe_slots(db, printer_id,
            [(s.gate + 1, not s.loaded, None) for s in mk_status.filament_slots], set(), current_user)
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
            ftype = FilamentType(slot_filament_value(material_key))
            color_hex = mmu_slot.color_hex[:6] if mmu_slot.color_hex else None
            color_name = mmu_slot.name or mmu_slot.material or None
            if mmu_slot.loaded:
                db_slot.filament_type = FilamentType(slot_filament_value(ftype))
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.loaded_at = datetime.now(timezone.utc)
                updated_slots.append({
                    "slot": slot_num, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "matched": "mmu_gate"})
            else:
                db_slot.filament_type = FilamentType(slot_filament_value(FilamentType.EMPTY))
                db_slot.color = None
                db_slot.color_hex = None
                updated_slots.append({"slot": slot_num, "type": "EMPTY", "color": None, "empty": True})
        max_slot = len(mk_status.filament_slots)
        db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number > max_slot,
        ).delete()
        log_audit(db, "sync", "printer", printer_id, {"slots_synced": len(updated_slots), "source": "moonraker_mmu"})
        db.commit()
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

    from modules.printers.telemetry.feature_flag import is_v2_enabled

    if is_v2_enabled():
        from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
        from modules.printers.telemetry.bambu.broker_policy import resolve_bambu_broker_config
        from modules.printers.telemetry.bambu.session import read_status_once
        from modules.printers.telemetry.bambu.status_view import ams_slots_from_section

        endpoint = resolve_bambu_broker_config(
            printer.api_host, printer_id=f"ams-sync-{printer_id}",
        )
        config = BambuAdapterConfig(
            printer_id=f"ams-sync-{printer_id}",
            serial=serial,
            host=endpoint.host,
            port=endpoint.port,
            use_tls=endpoint.use_tls,
            access_code=access_code,
        )
        # Printers take 5-10s to emit their first MQTT report.
        try:
            result = read_status_once(config, timeout=20.0)
        except Exception as e:
            log.error(f"Printer connection error (Bambu V2 sync): {e}")
            raise HTTPException(status_code=503, detail="Printer connection error. Check printer IP and credentials.")

        if not result.success:
            raise HTTPException(status_code=503, detail=result.error or "Failed to connect to printer")

        slots = ams_slots_from_section(result.section) if result.section else []
        if not slots:
            return {"success": True, "printer_id": printer_id, "printer_name": printer.name,
                    "slots_synced": 0, "slots": [], "mismatches": [],
                    "message": "No AMS data received. Printer may not have an AMS or timed out."}

        # Construct a minimal status-like object with .ams_slots for the
        # downstream code to iterate. Everything else that code reads is
        # on the slot, not the status.
        class _StatusShim:
            def __init__(self, slots):
                self.ams_slots = slots
        bambu_status = _StatusShim(slots)

    else:
        # Legacy path
        try:
            from modules.printers.adapters.bambu import BambuPrinter

            bambu = BambuPrinter(ip=printer.api_host, serial=serial, access_code=access_code)
            if not bambu.connect():
                raise HTTPException(status_code=503, detail="Failed to connect to printer")
            # Poll for AMS data — printers take 5-10s to send first MQTT report
            bambu_status = None
            for _ in range(20):
                time.sleep(1)
                bambu_status = bambu.get_status()
                if bambu_status.ams_slots:
                    break
            bambu.disconnect()
            if not bambu_status or not bambu_status.ams_slots:
                return {"success": True, "printer_id": printer_id, "printer_name": printer.name,
                        "slots_synced": 0, "slots": [], "mismatches": [],
                        "message": "No AMS data received. Printer may not have an AMS or timed out."}

        except ImportError:
            raise HTTPException(status_code=500, detail="bambu_adapter not installed")
        except Exception as e:
            log.error(f"Printer connection error (Bambu sync): {e}")
            raise HTTPException(status_code=503, detail="Printer connection error. Check printer IP and credentials.")


    # Normalize placeholders before authorization, graph matching or creation.
    trays = [rfid.slot_tray(slot) for slot in bambu_status.ams_slots]
    tags = [t['tag_uid'] for t in trays if t['tag_uid'] and t['tray_type']]
    uuids = [t['tray_uuid'] for t in trays if t['tray_uuid'] and t['tray_type']]
    if len(tags) != len(set(tags)) or len(uuids) != len(set(uuids)):
        raise HTTPException(409, "AMS identities are ambiguous; retry synchronization")
    for tray in trays:
        if tray['tray_uuid']:
            authorize_spool(db.query(Spool).filter(Spool.tray_uuid == tray['tray_uuid']).first())
        if tray['tag_uid']:
            authorize_spool(db.query(Spool).filter(Spool.rfid_tag == tray['tag_uid']).first())

    local_library = db.query(FilamentLibrary).all()

    spoolman_spools = []
    spoolman_url = get_spoolman_url(db)
    if spoolman_url:
        try:
            from core.spoolman_client import fetch_spools
            spoolman_spools = fetch_spools(5)  # validated, pinned, projected (O1)
        except Exception as e:
            log.debug(f"Failed to fetch spoolman spools: {e}")

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
            filament = spool.get("filament") or {}
            spool_hex = (filament.get("color_hex") or "").lower()
            spool_material = (filament.get("material") or "").upper()
            if spool_hex and hex_lower:
                if spool_hex == hex_lower:
                    if spool_material == filament_type.upper():
                        return spool
                    return spool
        return None

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

    printer, locked_rfid = _lock_ams_mutation(
        db, printer_id, current_user, tags, acquired_connection, uuids, trays)
    sealed_rows = locked_rfid.candidates
    known = [rfid.match_id(locked_rfid, tray) for tray in trays if tray['tray_type']]
    known_ids = [spool.id for spool in known if spool is not None]
    if len(known_ids) != len(set(known_ids)):
        raise HTTPException(409, "AMS identities are ambiguous; retry synchronization")
    from core.interfaces.inventory_moves import inventory_moves
    inventory_moves().observe_slots(db, printer_id,
        [(s.slot_number, s.empty, t['tag_uid']) for s, t in zip(bambu_status.ams_slots, trays)],
        set(known_ids), current_user)
    updated_slots = []
    used_candidates = set()
    for ams_slot, tray in zip(bambu_status.ams_slots, trays):
        db_slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == ams_slot.slot_number,
        ).first()
        if not db_slot:
            db_slot = FilamentSlot(
                printer_id=printer_id,
                slot_number=ams_slot.slot_number,
            )
            db.add(db_slot)
            db.flush()

        color_hex = ams_slot.color_hex[:6] if ams_slot.color_hex else None
        ftype = FilamentType(slot_filament_value(ams_slot.filament_type))

        if not ams_slot.empty:
            rfid_match = rfid.match_id(locked_rfid, tray)
            candidates, possible = [], []
            bound = False
            if rfid_match is None and (tray['tag_uid'] or tray['tray_uuid']):
                candidates, possible = rfid.choose_candidates(sealed_rows, tray, used_candidates)
                if candidates:
                    rfid_match = db.get(Spool, candidates[0])
                    used_candidates.add(rfid_match.id)
                    rfid_match.rfid_tag = tray['tag_uid']
                    rfid_match.tray_uuid = tray['tray_uuid']
                    rfid_match.color_hex = color_hex
                    bound = True
                    log_audit(db, 'inventory.rfid_bound', 'spool', rfid_match.id,
                              rfid.audit_details(rfid_match.id, tray, printer_id, ams_slot.slot_number, candidates))
            if rfid_match and not rfid_match.tray_uuid and tray['tray_uuid']:
                rfid_match.tray_uuid = tray['tray_uuid']

            if rfid_match:
                if not bound and not db.query(AuditLog.id).filter(AuditLog.entity_type == 'spool',
                        AuditLog.entity_id == rfid_match.id,
                        AuditLog.action.in_(['inventory.rfid_bound', 'inventory.rfid_created', 'inventory.rfid_identified', 'inventory.rfid_merged'])).first():
                    log_audit(db, 'inventory.rfid_identified', 'spool', rfid_match.id,
                              rfid.audit_details(rfid_match.id, tray, printer_id, ams_slot.slot_number))
                color_name = f"{rfid_match.filament.brand} {rfid_match.filament.name}".strip() if rfid_match.filament else "Unknown"
                db_slot.filament_type = FilamentType(slot_filament_value(ftype))
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                assign_slot(db, db_slot, rfid_match.id, current_user)
                db_slot.spool_confirmed = True
                db_slot.loaded_at = datetime.now(timezone.utc)
                rfid_match.location_printer_id = printer_id
                rfid_match.location_slot = ams_slot.slot_number
                rfid_match.storage_location = None
                rfid.accept_measurement(db, rfid_match, printer, ams_slot.remaining_percent)
                updated_slots.append({
                    "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "spool_id": rfid_match.id,
                    "rfid": tray["tag_uid"], "tray_uuid": rfid_match.tray_uuid,
                    "matched": "rfid_bound" if bound else "rfid",
                    "remaining_percent": ams_slot.remaining_percent,
                })
                continue

            if (tray['tag_uid'] or tray['tray_uuid']) and not rfid_match:
                import uuid
                from core.material_identity import canonical_reported_material
                material = canonical_reported_material(ams_slot.filament_type) or ftype.value
                sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                library_entry = db.query(FilamentLibrary).filter(
                    FilamentLibrary.brand == "Bambu Lab",
                    FilamentLibrary.name == sub_brand,
                    FilamentLibrary.material == material,
                ).first()
                if not library_entry:
                    sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                    existing = db.query(FilamentLibrary).filter(
                        FilamentLibrary.brand == "Bambu Lab",
                        FilamentLibrary.name == sub_brand,
                        FilamentLibrary.material == material,
                    ).first()
                    if existing:
                        library_entry = existing
                    else:
                        sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                        new_lib = FilamentLibrary(
                            brand="Bambu Lab", name=sub_brand,
                            material=material, color_hex=color_hex,
                        )
                        db.add(new_lib)
                        db.flush()
                        library_entry = new_lib

                from core.filament_ledger import create_rfid_spool
                from core.print_attempts import sqlalchemy_runner
                try:
                    new_id = create_rfid_spool(sqlalchemy_runner(db),
                        filament_id=library_entry.id, qr_code=f"SPL-{uuid.uuid4().hex[:8].upper()}",
                        tag=tray["tag_uid"], tray_uuid=tray["tray_uuid"],
                        color_name=rfid.color_name(tray), color_hex=color_hex, org_id=printer.org_id)
                except IntegrityError as exc:
                    db.rollback()
                    if getattr(exc.orig, "sqlstate", None) == "23505" or getattr(exc.orig, "sqlite_errorcode", None) == 2067:
                        raise HTTPException(409, "Spool association changed; retry synchronization") from None
                    raise
                new_spool = db.get(Spool, new_id)
                rfid.accept_measurement(db, new_spool, printer, ams_slot.remaining_percent)
                log_audit(db, "inventory.rfid_created", "spool", new_spool.id,
                          rfid.audit_details(new_spool.id, tray, printer_id, ams_slot.slot_number, possible=possible))
                color_name = f"{library_entry.brand} {library_entry.name}".strip()
                db_slot.filament_type = FilamentType(slot_filament_value(ftype))
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                assign_slot(db, db_slot, new_spool.id, current_user)
                db_slot.spool_confirmed = True
                db_slot.loaded_at = datetime.now(timezone.utc)
                updated_slots.append({
                    "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "spool_id": new_spool.id,
                    "rfid": tray["tag_uid"], "tray_uuid": new_spool.tray_uuid, "matched": "rfid_auto_created",
                    "possible_duplicate_spool_ids": possible,
                })
                continue

            library_match = find_library_match(color_hex, ams_slot.filament_type)
            if library_match:
                color_name = f"{library_match.brand} {library_match.name}".strip()
                db_slot.filament_type = FilamentType(slot_filament_value(ftype))
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.loaded_at = datetime.now(timezone.utc)
                updated_slots.append({
                    "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                    "color_hex": color_hex, "matched": "library",
                })
                continue

            color_name = get_color_name(color_hex)
            db_slot.filament_type = FilamentType(slot_filament_value(ftype))
            db_slot.color = color_name
            db_slot.color_hex = color_hex
            db_slot.loaded_at = datetime.now(timezone.utc)
            updated_slots.append({
                "slot": ams_slot.slot_number, "type": ftype.value, "color": color_name,
                "color_hex": color_hex, "matched": "color_analysis",
            })
        else:
            db_slot.color = None
            db_slot.color_hex = None
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
                    except Exception as e:
                        log.debug(f"Failed to compute color distance: {e}")
                if mismatch and not spool.rfid_tag:
                    db_slot.spool_confirmed = False
                    mismatches.append({
                        "slot_number": db_slot.slot_number,
                        "assigned_spool_id": spool.id,
                        "reasons": mismatch_reason,
                    })

    log_audit(db, "sync", "printer", printer_id, {"slots_synced": len(updated_slots), "mismatches": len(mismatches)})
    db.commit()
    return {
        "success": True, "printer_id": printer_id, "printer_name": printer.name,
        "slots_synced": len(updated_slots), "slots": updated_slots, "mismatches": mismatches,
    }
