"""Spool label/QR generation, CSV export, lookup, scan-assign, and bulk operations."""

from io import BytesIO
from typing import Optional
import logging

import qrcode
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_role
from modules.inventory.models import Spool
from modules.printers.models import FilamentSlot, Printer
from ._helpers import ScanAssignRequest, ScanAssignResponse, generate_single_label

log = logging.getLogger("odin.api")
router = APIRouter(prefix="/spools", tags=["Spools"])


@router.get("/export", tags=["Spools"])
def export_spools_csv(
    current_user: dict = Depends(require_role("viewer")),
    db: Session = Depends(get_db),
):
    """Export all spools as CSV."""
    import csv as csv_mod
    import io as io_mod

    spools = db.query(Spool).all()
    output = io_mod.StringIO()
    writer = csv_mod.writer(output)
    writer.writerow([
        "ID", "Brand", "Name", "Material", "Color", "Initial Weight (g)",
        "Remaining Weight (g)", "% Remaining", "Status", "Vendor", "Price",
        "Storage Location", "Notes",
    ])
    for s in spools:
        writer.writerow([
            s.id,
            s.filament.brand if s.filament else "",
            s.filament.name if s.filament else "",
            s.filament.material if s.filament else "",
            s.color_hex or (s.filament.color_hex if s.filament else ""),
            s.initial_weight_g,
            s.remaining_weight_g,
            s.percent_remaining,
            s.status.value if s.status else "",
            s.vendor or "",
            s.price or "",
            s.storage_location or "",
            s.notes or "",
        ])
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spools_export.csv"},
    )


@router.get("/labels/batch", tags=["Spools"])
def generate_batch_labels(
    spool_ids: str,  # Comma-separated IDs
    size: str = "small",
    current_user: dict = Depends(require_role("viewer")),
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


@router.get("/lookup/{qr_code}", tags=["Spools"])
def lookup_spool_by_qr(qr_code: str, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
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


@router.post("/scan-assign", response_model=ScanAssignResponse, tags=["Spools"])
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


@router.post("/bulk-update", tags=["Spools"])
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


@router.get("/{spool_id}/qr", tags=["Spools"])
def get_spool_qr(spool_id: int, current_user: dict = Depends(require_role("viewer")), db: Session = Depends(get_db)):
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


@router.get("/{spool_id}/label", tags=["Spools"])
def generate_spool_label(
    spool_id: int,
    size: str = "small",  # small (2x1"), medium (3x2"), large (4x3")
    current_user: dict = Depends(require_role("viewer")),
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
    except Exception:
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
        except Exception:
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
