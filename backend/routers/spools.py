"""O.D.I.N. — Spool & Filament Routes

Spool CRUD, load/unload/use/weigh, QR codes, label generation,
filament library, Spoolman integration, drying logs, scan-assign,
and bulk operations.
"""

from datetime import datetime
from io import BytesIO
from typing import List, Optional
import logging
import os

import httpx
import qrcode
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request
from fastapi.responses import StreamingResponse
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel as PydanticBaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from deps import (
    get_db, get_current_user, require_role, log_audit,
    _get_org_filter, SessionLocal,
)
from models import (
    Spool, SpoolUsage, SpoolStatus, FilamentSlot, FilamentLibrary,
    Printer, DryingLog, HYGROSCOPIC_TYPES,
)
from schemas import SpoolmanSpool, SpoolmanSyncResult
from config import settings
import crypto

log = logging.getLogger("odin.api")
router = APIRouter()


# ====================================================================
# Inline Pydantic models
# ====================================================================

class SpoolCreate(PydanticBaseModel):
    filament_id: int
    initial_weight_g: float = 1000.0
    spool_weight_g: float = 250.0
    price: Optional[float] = None
    purchase_date: Optional[datetime] = None
    vendor: Optional[str] = None
    lot_number: Optional[str] = None
    storage_location: Optional[str] = None
    notes: Optional[str] = None


class SpoolUpdate(PydanticBaseModel):
    remaining_weight_g: Optional[float] = None
    status: Optional[str] = None
    storage_location: Optional[str] = None
    notes: Optional[str] = None
    price: Optional[float] = None
    vendor: Optional[str] = None
    lot_number: Optional[str] = None


class SpoolResponse(PydanticBaseModel):
    id: int
    filament_id: int
    qr_code: Optional[str]
    initial_weight_g: float
    remaining_weight_g: float
    spool_weight_g: float
    percent_remaining: float
    price: Optional[float]
    purchase_date: Optional[datetime]
    vendor: Optional[str]
    lot_number: Optional[str]
    status: str
    location_printer_id: Optional[int]
    location_slot: Optional[int]
    storage_location: Optional[str]
    notes: Optional[str]
    created_at: datetime
    # Include filament info
    filament_brand: Optional[str] = None
    filament_name: Optional[str] = None
    filament_material: Optional[str] = None
    filament_color_hex: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SpoolLoadRequest(PydanticBaseModel):
    printer_id: int
    slot_number: int


class SpoolUseRequest(PydanticBaseModel):
    weight_used_g: float
    job_id: Optional[int] = None
    notes: Optional[str] = None


class SpoolWeighRequest(PydanticBaseModel):
    gross_weight_g: float  # Total weight including spool


class FilamentCreateRequest(PydanticBaseModel):
    brand: str
    name: str
    material: str = "PLA"
    color_hex: Optional[str] = None


class FilamentUpdateRequest(PydanticBaseModel):
    brand: Optional[str] = None
    name: Optional[str] = None
    material: Optional[str] = None
    color_hex: Optional[str] = None


class ScanAssignRequest(PydanticBaseModel):
    qr_code: str
    printer_id: int
    slot: int  # 0-indexed slot/gate number


class ScanAssignResponse(PydanticBaseModel):
    success: bool
    message: str
    spool_id: Optional[int] = None
    spool_name: Optional[str] = None
    printer_name: Optional[str] = None
    slot: Optional[int] = None


# ====================================================================
# Helper
# ====================================================================

def generate_single_label(spool, width, height):
    """Generate a single label image for a spool."""
    img = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(img)

    # QR code
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(spool.qr_code)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")

    qr_size = min(height - 20, width // 2 - 20)
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)
    img.paste(qr_img, (10, (height - qr_size) // 2))

    # Text
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except:
        font_large = font_medium = font_small = ImageFont.load_default()

    text_x = qr_size + 30
    y = 15

    brand = spool.filament.brand if spool.filament else "Unknown"
    name = spool.filament.name if spool.filament else "Unknown"
    material = spool.filament.material if spool.filament else "?"
    color_hex = spool.filament.color_hex if spool.filament else None

    # Color swatch
    if color_hex:
        hex_clean = color_hex.replace("#", "")
        try:
            rgb = tuple(int(hex_clean[i:i+2], 16) for i in (0, 2, 4))
            draw.rectangle([text_x, y, text_x + 40, y + 40], fill=rgb, outline="black")
        except:
            pass
        title_x = text_x + 50
    else:
        title_x = text_x

    draw.text((title_x, y), f"{brand} - {name}", fill="black", font=font_large)
    y += 45
    draw.text((text_x, y), f"Material: {material}", fill="black", font=font_medium)
    y += 35
    draw.text((text_x, y), f"Weight: {spool.initial_weight_g:.0f}g", fill="black", font=font_medium)
    y += 35
    draw.text((text_x, y), f"ID: {spool.qr_code}", fill="gray", font=font_small)

    draw.rectangle([0, 0, width-1, height-1], outline="black", width=2)

    return img


# ====================================================================
# Spoolman Integration
# ====================================================================

@router.post("/spoolman/sync", response_model=SpoolmanSyncResult, tags=["Spoolman"])
async def sync_spoolman(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Sync filament data from Spoolman."""
    if not settings.spoolman_url:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=10)
            resp.raise_for_status()
            spools = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {e}")

    # For now, just return what we found - actual slot matching would need user mapping
    return SpoolmanSyncResult(
        success=True,
        spools_found=len(spools),
        slots_updated=0,
        message=f"Found {len(spools)} spools in Spoolman. Use the UI to assign spools to printer slots.",
    )


@router.get("/spoolman/spools", response_model=List[SpoolmanSpool], tags=["Spoolman"])
async def list_spoolman_spools():
    """List available spools from Spoolman."""
    if not settings.spoolman_url:
        raise HTTPException(status_code=400, detail="Spoolman URL not configured")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=10)
            resp.raise_for_status()
            spools_data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {e}")

    spools = []
    for s in spools_data:
        filament = s.get("filament", {})
        spools.append(SpoolmanSpool(
            id=s.get("id"),
            filament_name=filament.get("name", "Unknown"),
            filament_type=filament.get("material", "PLA"),
            color_name=filament.get("color_name"),
            color_hex=filament.get("color_hex"),
            remaining_weight=s.get("remaining_weight"),
        ))

    return spools


@router.get("/spoolman/filaments", tags=["Spoolman"])
async def get_spoolman_filaments():
    """Fetch all filament types from Spoolman."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{settings.spoolman_url}/api/v1/filament", timeout=10.0)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {str(e)}")


# ====================================================================
# Filament Library
# ====================================================================

@router.get("/filaments", tags=["Filaments"])
def list_filaments(
    brand: Optional[str] = None,
    material: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get filaments from library."""
    query = db.query(FilamentLibrary)
    if brand:
        query = query.filter(FilamentLibrary.brand == brand)
    if material:
        query = query.filter(FilamentLibrary.material == material)

    library_filaments = query.all()
    result = []
    for f in library_filaments:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })
    return result


@router.post("/filaments", tags=["Filaments"])
def add_custom_filament(data: FilamentCreateRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Add a custom filament to the library."""
    filament = FilamentLibrary(
        brand=data.brand,
        name=data.name,
        material=data.material,
        color_hex=data.color_hex,
        is_custom=True,
    )
    db.add(filament)
    db.commit()
    return {"id": filament.id, "brand": filament.brand, "name": filament.name, "message": "Filament added"}


@router.get("/filaments/combined", tags=["Filaments"])
async def get_combined_filaments(db: Session = Depends(get_db)):
    """Get filaments from both Spoolman (if available) and local library."""
    result = []

    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/spool", timeout=5)
                if resp.status_code == 200:
                    spools = resp.json()
                    for spool in spools:
                        filament = spool.get("filament", {})
                        result.append({
                            "id": f"spool_{spool['id']}",
                            "source": "spoolman",
                            "brand": filament.get("vendor", {}).get("name", "Unknown"),
                            "name": filament.get("name", "Unknown"),
                            "material": filament.get("material", "PLA"),
                            "color_hex": filament.get("color_hex"),
                            "remaining_weight": spool.get("remaining_weight"),
                            "display_name": f"{filament.get('name')} ({filament.get('material')}) - {int(spool.get('remaining_weight', 0))}g",
                        })
        except:
            pass

    library = db.query(FilamentLibrary).all()
    for f in library:
        result.append({
            "id": f"lib_{f.id}",
            "source": "library",
            "brand": f.brand,
            "name": f.name,
            "material": f.material,
            "color_hex": f.color_hex,
            "display_name": f"{f.brand} {f.name} ({f.material})",
        })

    return result


@router.get("/filaments/{filament_id}", tags=["Filaments"])
def get_filament(filament_id: str, db: Session = Depends(get_db)):
    """Get a specific filament from the library."""
    fid_str = filament_id.replace("lib_", "")
    try:
        fid = int(fid_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filament ID")

    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == fid).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")
    return {
        "id": f"lib_{filament.id}",
        "source": "library",
        "brand": filament.brand,
        "name": filament.name,
        "material": filament.material,
        "color_hex": filament.color_hex,
        "is_custom": getattr(filament, 'is_custom', False),
        "display_name": f"{filament.brand} {filament.name} ({filament.material})",
    }


@router.patch("/filaments/{filament_id}", tags=["Filaments"])
def update_filament(filament_id: str, updates: FilamentUpdateRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a filament in the library."""
    fid_str = filament_id.replace("lib_", "")
    try:
        fid = int(fid_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filament ID")

    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == fid).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")

    update_data = updates.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(filament, field, value)

    db.commit()
    return {
        "id": f"lib_{filament.id}",
        "brand": filament.brand,
        "name": filament.name,
        "material": filament.material,
        "color_hex": filament.color_hex,
        "message": "Filament updated",
    }


@router.delete("/filaments/{filament_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Filaments"])
def delete_filament(filament_id: str, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a filament from the library."""
    fid_str = filament_id.replace("lib_", "")
    try:
        fid = int(fid_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filament ID")

    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == fid).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")
    db.delete(filament)
    db.commit()


# ====================================================================
# Spool CRUD
# ====================================================================

@router.get("/spools", tags=["Spools"])
def list_spools(
    status: Optional[str] = None,
    filament_id: Optional[int] = None,
    printer_id: Optional[int] = None,
    org_id: Optional[int] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List all spools with optional filters."""
    query = db.query(Spool)

    if status:
        query = query.filter(Spool.status == status)
    if filament_id:
        query = query.filter(Spool.filament_id == filament_id)
    if printer_id:
        query = query.filter(Spool.location_printer_id == printer_id)

    effective_org = _get_org_filter(current_user, org_id)
    if effective_org is not None:
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
        }
        result.append(spool_dict)

    return result


@router.get("/spools/{spool_id}", tags=["Spools"])
def get_spool(spool_id: int, db: Session = Depends(get_db)):
    """Get a single spool with details."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
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


@router.post("/spools", tags=["Spools"])
def create_spool(spool: SpoolCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new spool."""
    # Verify filament exists
    filament = db.query(FilamentLibrary).filter(FilamentLibrary.id == spool.filament_id).first()
    if not filament:
        raise HTTPException(status_code=404, detail="Filament not found")

    # Generate QR code
    import uuid
    qr_code = f"SPL-{uuid.uuid4().hex[:8].upper()}"

    db_spool = Spool(
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
        status=SpoolStatus.ACTIVE,
        org_id=current_user.get("group_id") if current_user else None,
    )
    db.add(db_spool)
    db.commit()
    db.refresh(db_spool)

    log_audit(db, "create", "spool", db_spool.id, {"filament_id": spool.filament_id, "qr_code": db_spool.qr_code})
    return {
        "id": db_spool.id,
        "qr_code": db_spool.qr_code,
        "message": "Spool created",
    }


@router.patch("/spools/{spool_id}", tags=["Spools"])
def update_spool(spool_id: int, updates: SpoolUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update spool details."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    update_data = updates.model_dump(exclude_unset=True)

    if "status" in update_data:
        update_data["status"] = SpoolStatus(update_data["status"])

    for field, value in update_data.items():
        setattr(spool, field, value)

    db.commit()
    db.refresh(spool)

    return {"success": True, "id": spool.id}


@router.delete("/spools/{spool_id}", tags=["Spools"])
def delete_spool(spool_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a spool (or archive it)."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Archive instead of delete
    spool.status = SpoolStatus.ARCHIVED
    db.commit()

    return {"success": True, "message": "Spool archived"}


# ====================================================================
# Spool Actions (load/unload/use/weigh)
# ====================================================================

@router.post("/spools/{spool_id}/load", tags=["Spools"])
def load_spool(spool_id: int, request: SpoolLoadRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Load a spool into a printer slot."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    printer = db.query(Printer).filter(Printer.id == request.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    if request.slot_number < 1 or request.slot_number > printer.slot_count:
        raise HTTPException(status_code=400, detail=f"Invalid slot number (1-{printer.slot_count})")

    # Unload any existing spool in that slot
    existing = db.query(Spool).filter(
        Spool.location_printer_id == request.printer_id,
        Spool.location_slot == request.slot_number,
    ).first()
    if existing and existing.id != spool_id:
        existing.location_printer_id = None
        existing.location_slot = None

    # Update spool location
    spool.location_printer_id = request.printer_id
    spool.location_slot = request.slot_number
    spool.storage_location = None

    # Update filament slot assignment
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == request.printer_id,
        FilamentSlot.slot_number == request.slot_number,
    ).first()
    if slot:
        slot.assigned_spool_id = spool_id
        slot.spool_confirmed = True

    db.commit()

    return {
        "success": True,
        "spool_id": spool_id,
        "printer": printer.name,
        "slot": request.slot_number,
    }


@router.post("/spools/{spool_id}/unload", tags=["Spools"])
def unload_spool(
    spool_id: int,
    storage_location: Optional[str] = None,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Unload a spool from printer to storage."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Clear slot assignment
    if spool.location_printer_id and spool.location_slot:
        slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == spool.location_printer_id,
            FilamentSlot.slot_number == spool.location_slot,
        ).first()
        if slot and slot.assigned_spool_id == spool_id:
            slot.assigned_spool_id = None
            slot.spool_confirmed = False

    spool.location_printer_id = None
    spool.location_slot = None
    spool.storage_location = storage_location

    db.commit()

    return {"success": True, "message": "Spool unloaded"}


@router.post("/spools/{spool_id}/use", tags=["Spools"])
def use_spool(spool_id: int, request: SpoolUseRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Record filament usage from a spool."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Deduct weight
    spool.remaining_weight_g = max(0, spool.remaining_weight_g - request.weight_used_g)

    # Check if empty
    if spool.remaining_weight_g <= 0:
        spool.status = SpoolStatus.EMPTY

    # Record usage
    usage = SpoolUsage(
        spool_id=spool.id,
        job_id=request.job_id,
        weight_used_g=request.weight_used_g,
        notes=request.notes,
    )
    db.add(usage)
    db.commit()

    log_audit(db, "use", "spool", spool_id, {"weight_used_g": request.weight_used_g, "remaining": spool.remaining_weight_g})
    return {
        "success": True,
        "remaining_weight_g": spool.remaining_weight_g,
        "percent_remaining": spool.percent_remaining,
        "status": spool.status.value,
    }


@router.post("/spools/{spool_id}/weigh", tags=["Spools"])
def weigh_spool(spool_id: int, request: SpoolWeighRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update spool weight from scale measurement."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Calculate net filament weight
    old_weight = spool.remaining_weight_g
    new_weight = max(0, request.gross_weight_g - spool.spool_weight_g)
    spool.remaining_weight_g = new_weight

    # Record as usage if weight decreased
    if new_weight < old_weight:
        usage = SpoolUsage(
            assigned_spool_id=spool.id,
            weight_used_g=old_weight - new_weight,
            notes="Manual weigh adjustment",
        )
        db.add(usage)

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
# QR Codes & Labels
# ====================================================================

@router.get("/spools/{spool_id}/qr", tags=["Spools"])
def get_spool_qr(spool_id: int, db: Session = Depends(get_db)):
    """Get QR code data for a spool."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    return {
        "qr_code": spool.qr_code,
        "spool_id": spool.id,
        "filament": f"{spool.filament.brand} {spool.filament.name}" if spool.filament else "Unknown",
        "material": spool.filament.material if spool.filament else "Unknown",
        "color_hex": spool.filament.color_hex if spool.filament else None,
    }


@router.get("/spools/lookup/{qr_code}", tags=["Spools"])
def lookup_spool_by_qr(qr_code: str, db: Session = Depends(get_db)):
    """Look up spool details by QR code."""
    spool = db.query(Spool).filter(Spool.qr_code == qr_code).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    return {
        "id": spool.id,
        "qr_code": spool.qr_code,
        "brand": spool.filament.brand if spool.filament else None,
        "name": spool.filament.name if spool.filament else None,
        "material": spool.filament.material if spool.filament else None,
        "color_hex": spool.filament.color_hex if spool.filament else None,
        "remaining_weight": spool.remaining_weight_g,
        "initial_weight": spool.initial_weight_g,
        "location_printer_id": spool.location_printer_id,
        "location_slot": spool.location_slot,
    }


@router.get("/spools/{spool_id}/label", tags=["Spools"])
def generate_spool_label(
    spool_id: int,
    size: str = "small",  # small (2x1"), medium (3x2"), large (4x3")
    db: Session = Depends(get_db)
):
    """Generate a printable QR label for a spool."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Label dimensions (at 300 DPI)
    sizes = {
        "small": (600, 300),   # 2" x 1"
        "medium": (900, 600),  # 3" x 2"
        "large": (1200, 900),  # 4" x 3"
    }
    width, height = sizes.get(size, sizes["small"])

    # Create white background
    img = Image.new('RGB', (width, height), 'white')
    draw = ImageDraw.Draw(img)

    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(spool.qr_code)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white")

    # Resize QR to fit
    qr_size = min(height - 20, width // 2 - 20)
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)

    # Paste QR on left side
    qr_x = 10
    qr_y = (height - qr_size) // 2
    img.paste(qr_img, (qr_x, qr_y))

    # Text area starts after QR
    text_x = qr_size + 30
    text_width = width - text_x - 10

    # Try to load a font, fall back to default
    try:
        font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 36)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
    except:
        font_large = ImageFont.load_default()
        font_medium = font_large
        font_small = font_large

    # Get filament info
    brand = spool.filament.brand if spool.filament else "Unknown"
    name = spool.filament.name if spool.filament else "Unknown"
    material = spool.filament.material if spool.filament else "?"
    color_hex = spool.filament.color_hex if spool.filament else None

    # Draw color swatch
    if color_hex:
        swatch_size = 40
        swatch_x = text_x
        swatch_y = 15
        hex_clean = color_hex.replace("#", "")
        try:
            rgb = tuple(int(hex_clean[i:i+2], 16) for i in (0, 2, 4))
            draw.rectangle([swatch_x, swatch_y, swatch_x + swatch_size, swatch_y + swatch_size], fill=rgb, outline="black")
        except:
            pass
        text_start_x = swatch_x + swatch_size + 10
    else:
        text_start_x = text_x
        swatch_y = 15

    # Draw text
    y = swatch_y

    # Brand - Name
    title = f"{brand} - {name}"
    draw.text((text_start_x, y), title, fill="black", font=font_large)
    y += 45

    # Material
    draw.text((text_x, y), f"Material: {material}", fill="black", font=font_medium)
    y += 35

    # Weight
    weight_text = f"Weight: {spool.initial_weight_g:.0f}g"
    draw.text((text_x, y), weight_text, fill="black", font=font_medium)
    y += 35

    # Spool ID
    draw.text((text_x, y), f"ID: {spool.qr_code}", fill="gray", font=font_small)

    # Add border
    draw.rectangle([0, 0, width-1, height-1], outline="black", width=2)

    # Return as PNG
    buffer = BytesIO()
    img.save(buffer, format="PNG", dpi=(300, 300))
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="image/png",
        headers={"Content-Disposition": f"inline; filename=spool_{spool_id}_label.png"},
    )


@router.get("/spools/labels/batch", tags=["Spools"])
def generate_batch_labels(
    spool_ids: str,  # Comma-separated IDs
    size: str = "small",
    db: Session = Depends(get_db)
):
    """Generate a page of labels for multiple spools."""
    ids = [int(x.strip()) for x in spool_ids.split(",") if x.strip().isdigit()]
    if not ids:
        raise HTTPException(status_code=400, detail="No valid spool IDs provided")

    spools = db.query(Spool).filter(Spool.id.in_(ids)).all()
    if not spools:
        raise HTTPException(status_code=404, detail="No spools found")

    # Label dimensions
    sizes = {
        "small": (600, 300),
        "medium": (900, 600),
        "large": (1200, 900),
    }
    label_w, label_h = sizes.get(size, sizes["small"])

    # Page layout (Letter size at 300 DPI = 2550 x 3300)
    page_w, page_h = 2550, 3300
    margin = 75

    # Calculate grid
    cols = (page_w - 2 * margin) // label_w
    rows = (page_h - 2 * margin) // label_h
    labels_per_page = cols * rows

    # Create page
    page = Image.new('RGB', (page_w, page_h), 'white')

    for idx, spool in enumerate(spools[:labels_per_page]):
        row = idx // cols
        col = idx % cols

        x = margin + col * label_w
        y = margin + row * label_h

        # Generate individual label
        label = generate_single_label(spool, label_w, label_h)
        page.paste(label, (x, y))

    buffer = BytesIO()
    page.save(buffer, format="PNG", dpi=(300, 300))
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="image/png",
        headers={"Content-Disposition": f"inline; filename=spool_labels_batch.png"},
    )


# ====================================================================
# Slot Assignment
# ====================================================================

@router.post("/printers/{printer_id}/slots/{slot_number}/assign", tags=["Spools"])
def assign_spool_to_slot(
    printer_id: int,
    slot_number: int,
    spool_id: int,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Assign a spool to a printer slot."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    # Update slot
    slot.assigned_spool_id = spool_id
    slot.spool_confirmed = False  # Needs confirmation

    # Update spool location
    spool.location_printer_id = printer_id
    spool.location_slot = slot_number
    spool.storage_location = None

    db.commit()

    return {"success": True, "message": "Spool assigned, awaiting confirmation"}


@router.post("/printers/{printer_id}/slots/{slot_number}/confirm", tags=["Spools"])
def confirm_slot_assignment(
    printer_id: int,
    slot_number: int,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Confirm the spool assignment for a slot."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number,
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")

    if not slot.assigned_spool_id:
        raise HTTPException(status_code=400, detail="No spool assigned to confirm")

    slot.spool_confirmed = True
    db.commit()

    return {"success": True, "message": "Spool assignment confirmed"}


@router.get("/printers/{printer_id}/slots/needs-attention", tags=["Spools"])
def get_slots_needing_attention(printer_id: int, db: Session = Depends(get_db)):
    """Get slots that need spool confirmation or have mismatches."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    issues = []
    for slot in printer.filament_slots:
        slot_issues = []

        # No spool assigned but slot has filament
        if not slot.assigned_spool_id and slot.color_hex:
            slot_issues.append("No spool assigned")

        # Spool assigned but not confirmed
        if slot.assigned_spool_id and not slot.spool_confirmed:
            slot_issues.append("Awaiting confirmation")

        # Spool assigned but type/color mismatch
        if slot.assigned_spool and slot.assigned_spool.filament:
            spool_fil = slot.assigned_spool.filament
            if slot.color_hex and spool_fil.color_hex:
                # Simple mismatch check
                if slot.color_hex.lower().replace("#", "") != spool_fil.color_hex.lower().replace("#", ""):
                    slot_issues.append(f"Color mismatch: slot={slot.color_hex}, spool={spool_fil.color_hex}")

        if slot_issues:
            issues.append({
                "slot_number": slot.slot_number,
                "issues": slot_issues,
                "current_type": slot.filament_type.value if slot.filament_type else None,
                "current_color": slot.color,
                "current_color_hex": slot.color_hex,
                "assigned_spool_id": slot.assigned_spool_id,
                "spool_confirmed": slot.spool_confirmed,
            })

    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "slots_needing_attention": len(issues),
        "slots": issues,
    }


# ====================================================================
# Drying Log
# ====================================================================

@router.post("/spools/{spool_id}/dry", tags=["Spools"])
def log_drying_session(
    spool_id: int,
    duration_hours: float,
    temp_c: Optional[float] = None,
    method: str = "dryer",
    notes: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Log a filament drying session for a spool."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")

    drying_log = DryingLog(
        spool_id=spool_id,
        duration_hours=duration_hours,
        temp_c=temp_c,
        method=method,
        notes=notes,
    )
    db.add(drying_log)
    db.commit()
    db.refresh(drying_log)

    log_audit(db, "create", "drying_log", drying_log.id, {"spool_id": spool_id, "duration_hours": duration_hours, "method": method})

    return {
        "id": drying_log.id,
        "spool_id": drying_log.spool_id,
        "dried_at": drying_log.dried_at.isoformat() if drying_log.dried_at else None,
        "duration_hours": drying_log.duration_hours,
        "temp_c": drying_log.temp_c,
        "method": drying_log.method,
        "notes": drying_log.notes,
    }


@router.get("/spools/{spool_id}/drying-history", tags=["Spools"])
def get_drying_history(spool_id: int, db: Session = Depends(get_db)):
    """Get drying session history for a spool."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
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


# ====================================================================
# Scan Assign
# ====================================================================

@router.post("/spools/scan-assign", response_model=ScanAssignResponse, tags=["Spools"])
def scan_assign_spool(
    data: ScanAssignRequest,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """
    Assign a spool to a printer slot by scanning its QR code.

    Used for:
    - Non-RFID printers (Kobra S1 with ACE)
    - Third-party filaments in Bambu AMS
    """
    # Find spool by QR code
    spool = db.query(Spool).filter(Spool.qr_code == data.qr_code).first()
    if not spool:
        return ScanAssignResponse(
            success=False,
            message=f"Spool not found: {data.qr_code}",
        )

    # Find printer
    printer = db.query(Printer).filter(Printer.id == data.printer_id).first()
    if not printer:
        return ScanAssignResponse(
            success=False,
            message=f"Printer not found: {data.printer_id}",
        )

    # Validate slot number
    if data.slot < 1 or data.slot > (printer.slot_count or 4):
        return ScanAssignResponse(
            success=False,
            message=f"Invalid slot {data.slot} for {printer.name} (has {printer.slot_count or 4} slots)",
        )

    # Check if slot already has a spool assigned
    existing_slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == data.printer_id,
        FilamentSlot.slot_number == data.slot,
    ).first()

    if existing_slot:
        # Update existing slot
        existing_slot.assigned_spool_id = spool.id
        existing_slot.spool_confirmed = True
        existing_slot.filament_type = spool.filament.material if spool.filament else None
        existing_slot.color = spool.filament.name if spool.filament else None
        existing_slot.color_hex = spool.filament.color_hex if spool.filament else None
    else:
        # Create new slot entry
        new_slot = FilamentSlot(
            printer_id=data.printer_id,
            slot_number=data.slot,
            assigned_spool_id=spool.id,
            filament_type=spool.filament.material if spool.filament else None,
            color=spool.filament.name if spool.filament else None,
            color_hex=spool.filament.color_hex if spool.filament else None,
            spool_confirmed=True,
        )
        db.add(new_slot)

    # Update spool location
    spool.location_printer_id = data.printer_id
    spool.location_slot = data.slot

    # Clear any previous slot assignment for this spool on OTHER printers
    db.query(FilamentSlot).filter(
        FilamentSlot.assigned_spool_id == spool.id,
        FilamentSlot.printer_id != data.printer_id,
    ).update({FilamentSlot.assigned_spool_id: None})

    db.commit()

    spool_name = f"{spool.filament.brand} {spool.filament.name}" if spool.filament else spool.qr_code

    return ScanAssignResponse(
        success=True,
        message=f"Assigned {spool_name} to {printer.name} slot {data.slot}",
        spool_id=spool.id,
        spool_name=spool_name,
        printer_name=printer.name,
        slot=data.slot,
    )


# ====================================================================
# Bulk Update
# ====================================================================

@router.post("/spools/bulk-update", tags=["Spools"])
async def bulk_update_spools(body: dict, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Bulk update spool fields for multiple spools."""
    spool_ids = body.get("spool_ids", [])
    if not spool_ids or not isinstance(spool_ids, list):
        raise HTTPException(status_code=400, detail="spool_ids list is required")
    if len(spool_ids) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 spools per batch")

    action = body.get("action", "")
    count = 0

    if action == "archive":
        for sid in spool_ids:
            db.execute(text("UPDATE spools SET status = 'archived' WHERE id = :id AND status != 'archived'"),
                       {"id": sid})
            count += 1
    elif action == "activate":
        for sid in spool_ids:
            db.execute(text("UPDATE spools SET status = 'active' WHERE id = :id"),
                       {"id": sid})
            count += 1
    elif action == "delete":
        for sid in spool_ids:
            db.execute(text("DELETE FROM spools WHERE id = :id AND status IN ('archived', 'empty')"),
                       {"id": sid})
            count += 1
    else:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")

    db.commit()
    log_audit(db, f"bulk_{action}", "spools", details=f"{count} spools")
    return {"status": "ok", "affected": count}
