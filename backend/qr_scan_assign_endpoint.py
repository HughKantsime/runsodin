"""
QR Scan-to-Assign Endpoint

Add this to main.py to enable scanning QR codes to assign spools to printer slots.

Endpoint: POST /api/spools/scan-assign
Body: { "qr_code": "SPL-CEDB74F3", "printer_id": 5, "slot": 1 }

This handles both:
- Kobra S1 / ACE slots (Moonraker printers)
- Bambu AMS slots with third-party (non-RFID) filament
"""

# Add this endpoint to main.py:

# ============== QR Scan-to-Assign ==============

class ScanAssignRequest(BaseModel):
    qr_code: str
    printer_id: int
    slot: int  # 0-indexed slot/gate number


class ScanAssignResponse(BaseModel):
    success: bool
    message: str
    spool_id: Optional[int] = None
    spool_name: Optional[str] = None
    printer_name: Optional[str] = None
    slot: Optional[int] = None


@app.post("/api/spools/scan-assign", response_model=ScanAssignResponse, tags=["Spools"])
def scan_assign_spool(
    data: ScanAssignRequest,
    db: Session = Depends(get_db)
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
            message=f"Spool not found: {data.qr_code}"
        )
    
    # Find printer
    printer = db.query(Printer).filter(Printer.id == data.printer_id).first()
    if not printer:
        return ScanAssignResponse(
            success=False,
            message=f"Printer not found: {data.printer_id}"
        )
    
    # Validate slot number
    if data.slot < 0 or data.slot >= printer.slot_count:
        return ScanAssignResponse(
            success=False,
            message=f"Invalid slot {data.slot} for {printer.name} (has {printer.slot_count} slots)"
        )
    
    # Check if slot already has a spool assigned
    existing_slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == data.printer_id,
        FilamentSlot.slot_index == data.slot
    ).first()
    
    if existing_slot:
        # Update existing slot
        existing_slot.spool_id = spool.id
        existing_slot.filament_type = spool.filament.material if spool.filament else None
        existing_slot.color_name = spool.filament.name if spool.filament else None
        existing_slot.color_hex = spool.filament.color_hex if spool.filament else None
    else:
        # Create new slot entry
        new_slot = FilamentSlot(
            printer_id=data.printer_id,
            slot_index=data.slot,
            spool_id=spool.id,
            filament_type=spool.filament.material if spool.filament else None,
            color_name=spool.filament.name if spool.filament else None,
            color_hex=spool.filament.color_hex if spool.filament else None,
        )
        db.add(new_slot)
    
    # Update spool location
    spool.location_printer_id = data.printer_id
    spool.location_slot = data.slot
    
    # Clear any previous slot assignment for this spool on OTHER printers
    db.query(FilamentSlot).filter(
        FilamentSlot.spool_id == spool.id,
        FilamentSlot.printer_id != data.printer_id
    ).update({FilamentSlot.spool_id: None})
    
    db.commit()
    
    spool_name = f"{spool.filament.brand} {spool.filament.name}" if spool.filament else spool.qr_code
    
    return ScanAssignResponse(
        success=True,
        message=f"Assigned {spool_name} to {printer.name} slot {data.slot + 1}",
        spool_id=spool.id,
        spool_name=spool_name,
        printer_name=printer.name,
        slot=data.slot
    )


@app.get("/api/spools/lookup/{qr_code}", tags=["Spools"])
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
        "remaining_weight": spool.remaining_weight,
        "initial_weight": spool.initial_weight_g,
        "location_printer_id": spool.location_printer_id,
        "location_slot": spool.location_slot,
    }
