"""
PrintFarm Scheduler API

FastAPI application providing REST endpoints for managing
printers, jobs, and the scheduling engine.
"""

from datetime import datetime, timedelta
from typing import List, Optional
from contextlib import asynccontextmanager
import re
import os

from pydantic import BaseModel as PydanticBaseModel, field_validator, ConfigDict
from fastapi import FastAPI, Depends, HTTPException, Query, status, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
import httpx

from models import (Spool, SpoolUsage, SpoolStatus, AuditLog, 
    Base, Printer, FilamentSlot, Model, Job, JobStatus,
    FilamentType, SchedulerRun, init_db, FilamentLibrary
)

# Bambu Lab Integration
try:
    from bambu_integration import (
        test_bambu_connection, sync_ams_filaments, slot_to_dict,
        map_bambu_filament_type, BAMBU_FILAMENT_TYPE_MAP, MQTT_AVAILABLE
    )
    BAMBU_AVAILABLE = MQTT_AVAILABLE
except ImportError:
    BAMBU_AVAILABLE = False
from schemas import (
    PrinterCreate, PrinterUpdate, PrinterResponse, PrinterSummary,
    FilamentSlotCreate, FilamentSlotUpdate, FilamentSlotResponse,
    ModelCreate, ModelUpdate, ModelResponse,
    JobCreate, JobUpdate, JobResponse, JobSummary,
    SchedulerConfig as SchedulerConfigSchema, ScheduleResult, SchedulerRunResponse,
    TimelineResponse, TimelineSlot,
    SpoolmanSpool, SpoolmanSyncResult,
    HealthCheck
)
from scheduler import Scheduler, SchedulerConfig, run_scheduler
from config import settings
import crypto


# Database setup
engine = create_engine(settings.database_url, echo=settings.debug)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """Dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def log_audit(db: Session, action: str, entity_type: str = None, entity_id: int = None, details: dict = None, ip: str = None):
    """Log an action to the audit log."""
    entry = AuditLog(
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
        ip_address=ip
    )
    db.add(entry)
    db.commit()


def verify_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """Verify API key if authentication is enabled."""
    # If no API key configured, auth is disabled (trusted network mode)
    if not settings.api_key:
        return None
    
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    return x_api_key


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize database on startup."""
    Base.metadata.create_all(bind=engine)
    yield


# Create FastAPI app
app = FastAPI(
    title="PrintFarm Scheduler",
    description="Smart job scheduling for 3D print farms",
    version="0.1.0",
    lifespan=lifespan
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# API Key authentication middleware
@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    """Check API key for all routes except health check."""
    # Skip auth for health endpoint and OPTIONS (CORS preflight)
    if request.url.path == "/health" or "/label" in request.url.path or request.method == "OPTIONS":
        return await call_next(request)
    
    # If no API key configured, auth is disabled
    if not settings.api_key:
        return await call_next(request)
    
    # Check the API key
    api_key = request.headers.get("X-API-Key")
    if not api_key or api_key != settings.api_key:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"}
        )
    
    return await call_next(request)


# ============== Helper Functions ==============

def get_printer_api_key(printer: Printer) -> Optional[str]:
    """Get decrypted API key for a printer. For internal use only."""
    if not printer.api_key:
        return None
    return crypto.decrypt(printer.api_key)


def mask_api_key(api_key: Optional[str]) -> Optional[str]:
    """Mask an API key for safe display (e.g., '••••••••abc123')."""
    if not api_key:
        return None
    if len(api_key) <= 6:
        return "••••••••"
    return "••••••••" + api_key[-6:]


# ============== Health Check ==============

@app.get("/health", response_model=HealthCheck, tags=["System"])
async def health_check():
    """Check API health and connectivity."""
    spoolman_ok = False
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=5)
                spoolman_ok = resp.status_code == 200
        except:
            pass
    
    return HealthCheck(
        status="ok",
        version="0.1.0",
        database=settings.database_url.split("///")[-1],
        spoolman_connected=spoolman_ok
    )


# ============== Printers ==============

@app.get("/api/printers", response_model=List[PrinterResponse], tags=["Printers"])
def list_printers(
    active_only: bool = False,
    db: Session = Depends(get_db)
):
    """List all printers."""
    query = db.query(Printer)
    if active_only:
        query = query.filter(Printer.is_active == True)
    return query.order_by(Printer.display_order, Printer.id).all()


@app.post("/api/printers", response_model=PrinterResponse, status_code=status.HTTP_201_CREATED, tags=["Printers"])
def create_printer(
    printer: PrinterCreate,
    db: Session = Depends(get_db)
):
    """Create a new printer."""
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
        api_key=encrypted_api_key
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
            filament_type=slot_data.filament_type if slot_data else FilamentType.PLA,
            color=slot_data.color if slot_data else None,
            color_hex=slot_data.color_hex if slot_data else None
        )
        db.add(slot)
    
    db.commit()
    db.refresh(db_printer)
    return db_printer



@app.post("/api/printers/reorder", tags=["Printers"])
def reorder_printers(
    data: dict,
    db: Session = Depends(get_db)
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

@app.get("/api/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def get_printer(printer_id: int, db: Session = Depends(get_db)):
    """Get a specific printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


@app.patch("/api/printers/{printer_id}", response_model=PrinterResponse, tags=["Printers"])
def update_printer(
    printer_id: int,
    updates: PrinterUpdate,
    db: Session = Depends(get_db)
):
    """Update a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
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
                        filament_type=FilamentType.PLA
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
@app.delete("/api/printers/{printer_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Printers"])
def delete_printer(printer_id: int, db: Session = Depends(get_db)):
    """Delete a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    db.delete(printer)
    db.commit()


# ============== Filament Slots ==============

@app.get("/api/printers/{printer_id}/slots", response_model=List[FilamentSlotResponse], tags=["Filament"])
def list_filament_slots(printer_id: int, db: Session = Depends(get_db)):
    """List filament slots for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer.filament_slots


@app.patch("/api/printers/{printer_id}/slots/{slot_number}", response_model=FilamentSlotResponse, tags=["Filament"])
def update_filament_slot(
    printer_id: int,
    slot_number: int,
    updates: FilamentSlotUpdate,
    db: Session = Depends(get_db)
):
    """Update a filament slot (e.g., load new filament)."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number
    ).first()
    
    if not slot:
        raise HTTPException(status_code=404, detail="Filament slot not found")
    
    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(slot, field, value)
    
    slot.loaded_at = datetime.utcnow()
    db.commit()
    db.refresh(slot)
    return slot


@app.post("/api/printers/{printer_id}/sync-ams", tags=["Printers"])
def sync_ams_state(printer_id: int, db: Session = Depends(get_db)):
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
    if not printer.api_key:
        raise HTTPException(status_code=400, detail="Printer api_key not configured")
    
    # Currently only Bambu is supported
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
        import time
        
        bambu = BambuPrinter(
            ip=printer.api_host,
            serial=serial,
            access_code=access_code
        )
        
        if not bambu.connect():
            raise HTTPException(status_code=503, detail="Failed to connect to printer")
        
        # Wait for status update
        time.sleep(2)
        status = bambu.get_status()
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
    
    # Import FilamentLibrary model
    from models import FilamentLibrary
    
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
            
            # Check for grayscale (r ≈ g ≈ b)
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
    for ams_slot in status.ams_slots:
        # Find matching slot in database
        db_slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == printer_id,
            FilamentSlot.slot_number == ams_slot.slot_number
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
                db_slot.loaded_at = datetime.utcnow()
                
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
                    "remaining_percent": ams_slot.remaining_percent
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
                    FilamentLibrary.material == ftype.value
                ).first()
                
                
                if not library_entry:
                    # Check for existing entry by brand+name+material
                    sub_brand = ams_slot.sub_brand or ams_slot.filament_type
                    existing = db.query(FilamentLibrary).filter(
                        FilamentLibrary.brand == "Bambu Lab",
                        FilamentLibrary.name == sub_brand,
                        FilamentLibrary.material == ftype.value
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
                            color_hex=color_hex
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
                    location_slot=ams_slot.slot_number
                )
                db.add(new_spool)
                db.flush()
                
                color_name = f"{library_entry.brand} {library_entry.name}".strip()
                db_slot.filament_type = ftype
                db_slot.color = color_name
                db_slot.color_hex = color_hex
                db_slot.assigned_spool_id = new_spool.id
                db_slot.spool_confirmed = True
                db_slot.loaded_at = datetime.utcnow()
                
                updated_slots.append({
                    "slot": ams_slot.slot_number,
                    "type": ftype.value,
                    "color": color_name,
                    "color_hex": color_hex,
                    "spool_id": new_spool.id,
                    "rfid": ams_slot.rfid_tag,
                    "matched": "rfid_auto_created"
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
                db_slot.loaded_at = datetime.utcnow()
                updated_slots.append({
                    "slot": ams_slot.slot_number,
                    "type": ftype.value,
                    "color": color_name,
                    "color_hex": color_hex,
                    "matched": "library"
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
                db_slot.loaded_at = datetime.utcnow()
                updated_slots.append({
                    "slot": ams_slot.slot_number,
                    "type": ftype.value,
                    "color": color_name,
                    "color_hex": color_hex,
                    "spoolman_id": spoolman_id,
                    "matched": "spoolman"
                })
                continue
            
            # Priority 3: Fall back to color name detection
            color_name = get_color_name(color_hex)
            
            db_slot.filament_type = ftype
            db_slot.color = color_name
            db_slot.color_hex = color_hex
            db_slot.spoolman_spool_id = None
            db_slot.loaded_at = datetime.utcnow()
            updated_slots.append({
                "slot": ams_slot.slot_number,
                "type": ftype.value,
                "color": color_name,
                "color_hex": color_hex,
                "matched": "color_analysis"
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
                "empty": True
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
                        "reasons": mismatch_reason
                    })

    db.commit()
    
    
    log_audit(db, "sync", "printer", printer_id, {"slots_synced": len(updated_slots), "mismatches": len(mismatches)})
    return {
        "success": True,
        "printer_id": printer_id,
        "printer_name": printer.name,
        "slots_synced": len(updated_slots),
        "slots": updated_slots,
        "mismatches": mismatches
    }


class TestConnectionRequest(PydanticBaseModel):
    """Request body for testing printer connection."""
    api_type: str
    api_host: str
    serial: Optional[str] = None
    access_code: Optional[str] = None


@app.post("/api/printers/test-connection", tags=["Printers"])
def test_printer_connection(request: TestConnectionRequest):
    """
    Test connection to a printer without saving.
    
    Used by the UI to validate credentials before saving.
    """
    if request.api_type.lower() != "bambu":
        raise HTTPException(status_code=400, detail=f"Test not supported for {request.api_type}")
    
    if not request.serial or not request.access_code:
        raise HTTPException(status_code=400, detail="Serial and access_code required for Bambu printers")
    
    try:
        from bambu_adapter import BambuPrinter
        import time
        
        bambu = BambuPrinter(
            ip=request.api_host,
            serial=request.serial,
            access_code=request.access_code
        )
        
        if not bambu.connect():
            return {
                "success": False,
                "error": "Failed to connect. Check IP, serial, and access code."
            }
        
        # Wait for status
        time.sleep(2)
        status = bambu.get_status()
        bambu.disconnect()
        
        return {
            "success": True,
            "state": status.state.value,
            "bed_temp": status.bed_temp,
            "nozzle_temp": status.nozzle_temp,
            "ams_slots": len(status.ams_slots)
        }
        
    except ImportError:
        raise HTTPException(status_code=500, detail="bambu_adapter not installed")
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


# ============== Models ==============

@app.get("/api/models", response_model=List[ModelResponse], tags=["Models"])
def list_models(
    category: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all print models."""
    query = db.query(Model)
    if category:
        query = query.filter(Model.category == category)
    return query.order_by(Model.name).all()


@app.post("/api/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED, tags=["Models"])
def create_model(model: ModelCreate, db: Session = Depends(get_db)):
    """Create a new model definition."""
    # Convert color requirements to dict format
    color_req = None
    if model.color_requirements:
        color_req = {k: v.model_dump() for k, v in model.color_requirements.items()}
    
    db_model = Model(
        name=model.name,
        build_time_hours=model.build_time_hours,
        default_filament_type=model.default_filament_type,
        color_requirements=color_req,
        category=model.category,
        thumbnail_url=model.thumbnail_url,
        notes=model.notes,
        cost_per_item=model.cost_per_item
    )
    db.add(db_model)
    db.commit()
    db.refresh(db_model)
    return db_model


@app.get("/api/models/{model_id}", response_model=ModelResponse, tags=["Models"])
def get_model(model_id: int, db: Session = Depends(get_db)):
    """Get a specific model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


@app.patch("/api/models/{model_id}", response_model=ModelResponse, tags=["Models"])
def update_model(model_id: int, updates: ModelUpdate, db: Session = Depends(get_db)):
    """Update a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    update_data = updates.model_dump(exclude_unset=True)
    if "color_requirements" in update_data and update_data["color_requirements"]:
        update_data["color_requirements"] = {
            k: v.model_dump() for k, v in update_data["color_requirements"].items()
        }
    
    for field, value in update_data.items():
        setattr(model, field, value)
    
    db.commit()
    db.refresh(model)
    return model


@app.delete("/api/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Models"])
def delete_model(model_id: int, db: Session = Depends(get_db)):
    """Delete a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    db.delete(model)
    db.commit()


# ============== Jobs ==============

@app.get("/api/jobs", response_model=List[JobResponse], tags=["Jobs"])
def list_jobs(
    status: Optional[JobStatus] = None,
    printer_id: Optional[int] = None,
    limit: int = Query(default=100, le=500),
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """List jobs with optional filters."""
    query = db.query(Job)
    
    if status:
        query = query.filter(Job.status == status)
    if printer_id:
        query = query.filter(Job.printer_id == printer_id)
    
    return query.order_by(Job.priority, Job.created_at).offset(offset).limit(limit).all()


@app.post("/api/jobs", response_model=JobResponse, status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_job(job: JobCreate, db: Session = Depends(get_db)):
    """Create a new print job."""
    db_job = Job(
        item_name=job.item_name,
        model_id=job.model_id,
        quantity=job.quantity,
        priority=job.priority,
        duration_hours=job.duration_hours,
        colors_required=job.colors_required,
        filament_type=job.filament_type,
        notes=job.notes,
        hold=job.hold,
        status=JobStatus.PENDING
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)
    return db_job


@app.post("/api/jobs/bulk", response_model=List[JobResponse], status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_jobs_bulk(jobs: List[JobCreate], db: Session = Depends(get_db)):
    """Create multiple jobs at once."""
    db_jobs = []
    for job in jobs:
        db_job = Job(
            item_name=job.item_name,
            model_id=job.model_id,
            quantity=job.quantity,
            priority=job.priority,
            duration_hours=job.duration_hours,
            colors_required=job.colors_required,
            filament_type=job.filament_type,
            notes=job.notes,
            hold=job.hold,
            status=JobStatus.PENDING
        )
        db.add(db_job)
        db_jobs.append(db_job)
    
    db.commit()
    for job in db_jobs:
        db.refresh(job)
    return db_jobs


@app.get("/api/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
def get_job(job_id: int, db: Session = Depends(get_db)):
    """Get a specific job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.patch("/api/jobs/{job_id}", response_model=JobResponse, tags=["Jobs"])
def update_job(job_id: int, updates: JobUpdate, db: Session = Depends(get_db)):
    """Update a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    for field, value in updates.model_dump(exclude_unset=True).items():
        setattr(job, field, value)
    
    db.commit()
    db.refresh(job)
    return job


@app.delete("/api/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Jobs"])
def delete_job(job_id: int, db: Session = Depends(get_db)):
    """Delete a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    db.delete(job)
    db.commit()


@app.post("/api/jobs/{job_id}/start", response_model=JobResponse, tags=["Jobs"])
def start_job(job_id: int, db: Session = Depends(get_db)):
    """Mark a job as started (printing)."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status not in [JobStatus.SCHEDULED, JobStatus.PENDING]:
        raise HTTPException(status_code=400, detail=f"Cannot start job in {job.status} status")
    
    job.status = JobStatus.PRINTING
    job.actual_start = datetime.utcnow()
    job.is_locked = True
    
    db.commit()
    db.refresh(job)
    return job


@app.post("/api/jobs/{job_id}/complete", response_model=JobResponse, tags=["Jobs"])
def complete_job(job_id: int, db: Session = Depends(get_db)):
    """Mark a job as completed."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job.status = JobStatus.COMPLETED
    job.actual_end = datetime.utcnow()
    job.is_locked = True
    
    # Update printer's loaded colors based on this job
    if job.printer_id and job.colors_list:
        printer = db.query(Printer).filter(Printer.id == job.printer_id).first()
        if printer:
            for i, color in enumerate(job.colors_list[:printer.slot_count]):
                slot = next((s for s in printer.filament_slots if s.slot_number == i + 1), None)
                if slot:
                    slot.color = color
    
    db.commit()
    db.refresh(job)
    return job


@app.post("/api/jobs/{job_id}/fail", response_model=JobResponse, tags=["Jobs"])
def fail_job(job_id: int, notes: Optional[str] = None, db: Session = Depends(get_db)):
    """Mark a job as failed."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job.status = JobStatus.FAILED
    job.actual_end = datetime.utcnow()
    job.is_locked = True
    if notes:
        job.notes = f"{job.notes or ''}\nFailed: {notes}".strip()
    
    db.commit()
    db.refresh(job)
    return job


@app.post("/api/jobs/{job_id}/reset", response_model=JobResponse, tags=["Jobs"])
def reset_job(job_id: int, db: Session = Depends(get_db)):
    """Reset a job back to pending status."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job.status = JobStatus.PENDING
    job.printer_id = None
    job.scheduled_start = None
    job.scheduled_end = None
    job.actual_start = None
    job.actual_end = None
    job.match_score = None
    job.is_locked = False
    
    db.commit()
    db.refresh(job)
    return job


# ============== Scheduler ==============

@app.post("/api/scheduler/run", response_model=ScheduleResult, tags=["Scheduler"])
def run_scheduler_endpoint(
    config: Optional[SchedulerConfigSchema] = None,
    db: Session = Depends(get_db)
):
    """Run the scheduler to assign pending jobs to printers."""
    scheduler_config = None
    if config:
        scheduler_config = SchedulerConfig.from_time_strings(
            blackout_start=config.blackout_start,
            blackout_end=config.blackout_end,
            setup_duration_slots=config.setup_duration_slots,
            horizon_days=config.horizon_days
        )
    
    result = run_scheduler(db, scheduler_config)
    
    # Get the run ID from the most recent log
    run_log = db.query(SchedulerRun).order_by(SchedulerRun.id.desc()).first()
    
    # Get scheduled job summaries
    scheduled_jobs = db.query(Job).filter(
        Job.status == JobStatus.SCHEDULED
    ).order_by(Job.scheduled_start).all()
    
    job_summaries = []
    for job in scheduled_jobs:
        printer_name = None
        if job.printer:
            printer_name = job.printer.name
        job_summaries.append(JobSummary(
            id=job.id,
            item_name=job.item_name,
            status=job.status,
            priority=job.priority,
            printer_id=job.printer_id,
            printer_name=printer_name,
            scheduled_start=job.scheduled_start,
            scheduled_end=job.scheduled_end,
            duration_hours=job.effective_duration,
            colors_list=job.colors_list,
            match_score=job.match_score
        ))
    
    return ScheduleResult(
        success=result.success,
        run_id=run_log.id if run_log else 0,
        scheduled=result.scheduled_count,
        skipped=result.skipped_count,
        setup_blocks=result.setup_blocks,
        message=f"Scheduled {result.scheduled_count} jobs, skipped {result.skipped_count}",
        jobs=job_summaries
    )


@app.get("/api/scheduler/runs", response_model=List[SchedulerRunResponse], tags=["Scheduler"])
def list_scheduler_runs(
    limit: int = Query(default=30, le=100),
    db: Session = Depends(get_db)
):
    """Get scheduler run history."""
    return db.query(SchedulerRun).order_by(SchedulerRun.run_at.desc()).limit(limit).all()


# ============== Timeline ==============

@app.get("/api/timeline", response_model=TimelineResponse, tags=["Timeline"])
def get_timeline(
    start_date: Optional[datetime] = None,
    days: int = Query(default=7, ge=1, le=30),
    db: Session = Depends(get_db)
):
    """Get timeline view data for the scheduler."""
    if start_date is None:
        start_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    
    end_date = start_date + timedelta(days=days)
    slot_duration = 30  # minutes
    
    # Get printers
    printers = db.query(Printer).filter(Printer.is_active == True).all()
    printer_summaries = [
        PrinterSummary(
            id=p.id,
            name=p.name,
            model=p.model,
            is_active=p.is_active,
            loaded_colors=p.loaded_colors
        )
        for p in printers
    ]
    
    # Get scheduled/printing/completed jobs in range
    jobs = db.query(Job).filter(
        Job.scheduled_start.isnot(None),
        Job.scheduled_start < end_date,
        Job.scheduled_end > start_date,
        Job.status.in_([JobStatus.SCHEDULED, JobStatus.PRINTING, JobStatus.COMPLETED])
    ).all()
    
    # Build timeline slots
    slots = []
    for job in jobs:
        if job.printer_id is None:
            continue
        
        printer = next((p for p in printers if p.id == job.printer_id), None)
        if not printer:
            continue
        
        slots.append(TimelineSlot(
            start=job.scheduled_start,
            end=job.scheduled_end,
            printer_id=job.printer_id,
            printer_name=printer.name,
            job_id=job.id,
            item_name=job.item_name,
            status=job.status,
            is_setup=False,
            colors=job.colors_list
        ))
    

    # Add MQTT-tracked print jobs to timeline
    mqtt_jobs_query = text("""
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE pj.started_at < :end_date
        AND (pj.ended_at > :start_date OR pj.ended_at IS NULL)
    """)
    mqtt_jobs = db.execute(mqtt_jobs_query, {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat()
    }).fetchall()
    
    for mj in mqtt_jobs:
        row = dict(mj._mapping)
        printer = next((p for p in printers if p.id == row["printer_id"]), None)
        if not printer:
            continue
        start_time = datetime.fromisoformat(row["started_at"])
        end_time = datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else datetime.now()
        mqtt_status = row["status"]
        if mqtt_status == "running":
            job_status = JobStatus.PRINTING
        elif mqtt_status == "completed":
            job_status = JobStatus.COMPLETED
        elif mqtt_status == "failed":
            job_status = JobStatus.FAILED
        else:
            job_status = JobStatus.COMPLETED
        slots.append(TimelineSlot(
            start=start_time,
            end=end_time,
            printer_id=row["printer_id"],
            printer_name=printer.name,
            job_id=None,
            item_name=row["job_name"] or "MQTT Print",
            status=job_status,
            is_setup=False,
            colors=[]
        ))
    return TimelineResponse(
        start_date=start_date,
        end_date=end_date,
        slot_duration_minutes=slot_duration,
        printers=printer_summaries,
        slots=slots
    )


# ============== Spoolman Integration ==============

@app.post("/api/spoolman/sync", response_model=SpoolmanSyncResult, tags=["Spoolman"])
async def sync_spoolman(db: Session = Depends(get_db)):
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
        message=f"Found {len(spools)} spools in Spoolman. Use the UI to assign spools to printer slots."
    )


@app.get("/api/spoolman/spools", response_model=List[SpoolmanSpool], tags=["Spoolman"])
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
            remaining_weight=s.get("remaining_weight")
        ))
    
    return spools


# ============== Stats ==============

@app.get("/api/stats", tags=["Stats"])
async def get_stats(db: Session = Depends(get_db)):
    """Get dashboard statistics."""
    total_printers = db.query(Printer).count()
    active_printers = db.query(Printer).filter(Printer.is_active == True).count()
    
    pending_jobs = db.query(Job).filter(Job.status == JobStatus.PENDING).count()
    scheduled_jobs = db.query(Job).filter(Job.status == JobStatus.SCHEDULED).count()
    printing_jobs = db.query(Job).filter(Job.status == JobStatus.PRINTING).count()
    completed_today = db.query(Job).filter(
        Job.status == JobStatus.COMPLETED,
        Job.actual_end >= datetime.now().replace(hour=0, minute=0, second=0)
    ).count()
    
    # Include MQTT-tracked jobs
    today_start = datetime.now().replace(hour=0, minute=0, second=0).isoformat()
    mqtt_printing = db.execute(text("SELECT COUNT(*) FROM print_jobs WHERE status = 'running'")).scalar() or 0
    mqtt_completed_today = db.execute(text("SELECT COUNT(*) FROM print_jobs WHERE status = 'completed' AND ended_at >= :today"), {"today": today_start}).scalar() or 0
    
    printing_jobs += mqtt_printing
    completed_today += mqtt_completed_today
    
    total_models = db.query(Model).count()
    
    # Check Spoolman connection
    spoolman_connected = False
    if settings.spoolman_url:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=3)
                spoolman_connected = resp.status_code == 200
        except:
            pass
    
    return {
        "printers": {
            "total": total_printers,
            "active": active_printers
        },
        "jobs": {
            "pending": pending_jobs,
            "scheduled": scheduled_jobs,
            "printing": printing_jobs,
            "completed_today": completed_today
        },
        "models": total_models,
        "spoolman_connected": spoolman_connected
    }

# ============== Spoolman Integration ==============
import httpx

SPOOLMAN_URL = "http://192.168.68.103:7912"

@app.get("/api/spoolman/spools", tags=["Spoolman"])
async def get_spoolman_spools():
    """Fetch all spools from Spoolman."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{SPOOLMAN_URL}/api/v1/spool", timeout=10.0)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {str(e)}")

@app.get("/api/spoolman/filaments", tags=["Spoolman"])
async def get_spoolman_filaments():
    """Fetch all filament types from Spoolman."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{SPOOLMAN_URL}/api/v1/filament", timeout=10.0)
            response.raise_for_status()
            return response.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to connect to Spoolman: {str(e)}")

# ============== Filament Library ==============
from models import FilamentLibrary

@app.get("/api/filaments", tags=["Filaments"])
def list_filaments(
    brand: Optional[str] = None,
    material: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get filaments from library. If Spoolman is connected, merge with Spoolman data."""
    query = db.query(FilamentLibrary)
    if brand:
        query = query.filter(FilamentLibrary.brand == brand)
    if material:
        query = query.filter(FilamentLibrary.material == material)
    
    library_filaments = query.all()
    
    # Format response
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

@app.post("/api/filaments", tags=["Filaments"])
def add_custom_filament(
    brand: str,
    name: str,
    material: str = "PLA",
    color_hex: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Add a custom filament to the library."""
    filament = FilamentLibrary(
        brand=brand,
        name=name,
        material=material,
        color_hex=color_hex,
        is_custom=True
    )
    db.add(filament)
    db.commit()
    return {"id": filament.id, "message": "Filament added"}

@app.get("/api/filaments/combined", tags=["Filaments"])
async def get_combined_filaments(db: Session = Depends(get_db)):
    """Get filaments from both Spoolman (if available) and local library."""
    result = []
    
    # Try Spoolman first
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
            pass  # Spoolman not available, continue with library
    
    # Add library filaments
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

# ============== Filament Library ==============
from models import FilamentLibrary

@app.get("/api/filaments", tags=["Filaments"])
def list_filaments(
    brand: Optional[str] = None,
    material: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Get filaments from library. If Spoolman is connected, merge with Spoolman data."""
    query = db.query(FilamentLibrary)
    if brand:
        query = query.filter(FilamentLibrary.brand == brand)
    if material:
        query = query.filter(FilamentLibrary.material == material)
    
    library_filaments = query.all()
    
    # Format response
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

@app.post("/api/filaments", tags=["Filaments"])
def add_custom_filament(
    brand: str,
    name: str,
    material: str = "PLA",
    color_hex: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Add a custom filament to the library."""
    filament = FilamentLibrary(
        brand=brand,
        name=name,
        material=material,
        color_hex=color_hex,
        is_custom=True
    )
    db.add(filament)
    db.commit()
    return {"id": filament.id, "message": "Filament added"}

@app.get("/api/filaments/combined", tags=["Filaments"])
async def get_combined_filaments(db: Session = Depends(get_db)):
    """Get filaments from both Spoolman (if available) and local library."""
    result = []
    
    # Try Spoolman first
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
            pass  # Spoolman not available, continue with library
    
    # Add library filaments
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


# ============== Config Schema ==============
class ConfigUpdate(PydanticBaseModel):
    """Validated config update request."""
    spoolman_url: Optional[str] = None
    blackout_start: Optional[str] = None
    blackout_end: Optional[str] = None
    
    @field_validator('spoolman_url')
    @classmethod
    def validate_url(cls, v):
        if v is None or v == '':
            return v
        # Basic URL validation
        url_pattern = re.compile(
            r'^https?://'  # http:// or https://
            r'[a-zA-Z0-9]+'  # domain start
            r'[a-zA-Z0-9.-]*'  # domain rest
            r'(:\d+)?'  # optional port
            r'(/.*)?$'  # optional path
        )
        if not url_pattern.match(v):
            raise ValueError('Invalid URL format. Must be http:// or https://')
        return v
    
    @field_validator('blackout_start', 'blackout_end')
    @classmethod
    def validate_time(cls, v):
        if v is None:
            return v
        # Validate HH:MM format
        time_pattern = re.compile(r'^([01]?[0-9]|2[0-3]):[0-5][0-9]$')
        if not time_pattern.match(v):
            raise ValueError('Invalid time format. Use HH:MM (e.g., 22:30)')
        return v


# Allowed config keys that can be written to .env
ALLOWED_CONFIG_KEYS = {'SPOOLMAN_URL', 'BLACKOUT_START', 'BLACKOUT_END'}


# ============== Config Endpoints ==============
@app.get("/api/config", tags=["Config"])
def get_config():
    """Get current configuration."""
    return {
        "spoolman_url": settings.spoolman_url,
        "blackout_start": settings.blackout_start,
        "blackout_end": settings.blackout_end,
    }

@app.put("/api/config", tags=["Config"])
def update_config(config: ConfigUpdate):
    """Update configuration. Writes to .env file."""
    # Use environment variable or default path
    env_path = os.environ.get('ENV_FILE_PATH', '/opt/printfarm-scheduler/backend/.env')
    
    # Read existing env, only keeping allowed keys
    env_vars = {}
    try:
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    key, val = line.strip().split('=', 1)
                    # Preserve existing keys (including ones we don't manage like API_KEY)
                    env_vars[key] = val
    except FileNotFoundError:
        pass
    
    # Update only the values that were provided and are allowed
    if config.spoolman_url is not None:
        env_vars['SPOOLMAN_URL'] = config.spoolman_url
    if config.blackout_start is not None:
        env_vars['BLACKOUT_START'] = config.blackout_start
    if config.blackout_end is not None:
        env_vars['BLACKOUT_END'] = config.blackout_end
    
    # Write back
    with open(env_path, 'w') as f:
        for key, val in env_vars.items():
            f.write(f"{key}={val}\n")
    
    return {"success": True, "message": "Config updated. Restart backend to apply changes."}

@app.get("/api/spoolman/test", tags=["Spoolman"])
async def test_spoolman_connection():
    """Test Spoolman connection."""
    if not settings.spoolman_url:
        return {"success": False, "message": "Spoolman URL not configured"}
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{settings.spoolman_url}/api/v1/health", timeout=5)
            if resp.status_code == 200:
                return {"success": True, "message": f"Connected to Spoolman at {settings.spoolman_url}"}
            else:
                return {"success": False, "message": f"Spoolman returned status {resp.status_code}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}

@app.get("/api/analytics", tags=["Analytics"])
def get_analytics(db: Session = Depends(get_db)):
    """Get analytics data for dashboard."""
    from sqlalchemy import func
    
    # Get all models with profitability data
    models = db.query(Model).all()
    
    # Top models by value per hour
    models_by_value = sorted(
        [m for m in models if m.cost_per_item and m.build_time_hours],
        key=lambda m: (m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours,
        reverse=True
    )[:10]
    
    top_by_hour = [{
        "id": m.id,
        "name": m.name,
        "value_per_hour": round((m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours, 2),
        "value_per_bed": round(m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1), 2),
        "build_time_hours": m.build_time_hours,
        "units_per_bed": m.units_per_bed or 1,
    } for m in models_by_value]
    
    # Worst performers
    models_by_value_asc = sorted(
        [m for m in models if m.cost_per_item and m.build_time_hours],
        key=lambda m: (m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours,
    )[:10]
    
    worst_performers = [{
        "id": m.id,
        "name": m.name,
        "value_per_hour": round((m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours, 2),
        "value_per_bed": round(m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1), 2),
        "build_time_hours": m.build_time_hours,
    } for m in models_by_value_asc]
    
    # Jobs stats
    all_jobs = db.query(Job).all()
    completed_jobs = [j for j in all_jobs if j.status == "completed"]
    pending_jobs = [j for j in all_jobs if j.status in ("pending", "scheduled")]
    
    # Revenue from completed jobs (estimate from model data)
    total_revenue = 0
    total_print_hours = 0
    for job in completed_jobs:
        if job.model_id:
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                total_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity
        if job.duration_hours:
            total_print_hours += job.duration_hours
    
    # Projected revenue from pending jobs
    projected_revenue = 0
    for job in pending_jobs:
        if job.model_id:
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                projected_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity
    
    # Printer utilization
    printers = db.query(Printer).filter(Printer.is_active == True).all()
    printer_stats = []
    for printer in printers:
        printer_jobs = [j for j in completed_jobs if j.printer_id == printer.id]
        hours = sum(j.duration_hours or 0 for j in printer_jobs)
        printer_stats.append({
            "id": printer.id,
            "name": printer.name,
            "completed_jobs": len(printer_jobs),
            "total_hours": round(hours, 1),
        })
    
    # Jobs over time (last 30 days)
    from datetime import datetime, timedelta
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)
    recent_jobs = db.query(Job).filter(Job.created_at >= thirty_days_ago).all()
    
    # Group by date
    jobs_by_date = {}
    for job in recent_jobs:
        date_str = job.created_at.strftime("%Y-%m-%d")
        if date_str not in jobs_by_date:
            jobs_by_date[date_str] = {"created": 0, "completed": 0}
        jobs_by_date[date_str]["created"] += 1
        if job.status == "completed":
            jobs_by_date[date_str]["completed"] += 1
    
    # Average $/hour across all models
    valid_models = [m for m in models if m.cost_per_item and m.build_time_hours]
    if valid_models:
        avg_value_per_hour = sum(
            (m.cost_per_item * (m.markup_percent or 300) / 100 * (m.units_per_bed or 1)) / m.build_time_hours
            for m in valid_models
        ) / len(valid_models)
    else:
        avg_value_per_hour = 0
    
    return {
        "top_by_hour": top_by_hour,
        "worst_performers": worst_performers,
        "summary": {
            "total_models": len(models),
            "total_jobs": len(all_jobs),
            "completed_jobs": len(completed_jobs),
            "pending_jobs": len(pending_jobs),
            "total_revenue": round(total_revenue, 2),
            "projected_revenue": round(projected_revenue, 2),
            "total_print_hours": round(total_print_hours, 1),
            "avg_value_per_hour": round(avg_value_per_hour, 2),
        },
        "printer_stats": printer_stats,
        "jobs_by_date": jobs_by_date,
    }


class MoveJobRequest(PydanticBaseModel):
    printer_id: int
    scheduled_start: datetime

@app.patch("/api/jobs/{job_id}/move", tags=["Jobs"])
def move_job(
    job_id: int,
    request: MoveJobRequest,
    db: Session = Depends(get_db)
):
    """Move a job to a different printer and/or time slot."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Verify printer exists
    printer = db.query(Printer).filter(Printer.id == request.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    # Calculate end time based on job duration
    duration_hours = job.effective_duration
    scheduled_end = request.scheduled_start + timedelta(hours=duration_hours)
    
    # Check for conflicts (other jobs on same printer overlapping this time)
    conflict = db.query(Job).filter(
        Job.id != job_id,
        Job.printer_id == request.printer_id,
        Job.status.in_([JobStatus.SCHEDULED, JobStatus.PRINTING]),
        Job.scheduled_start < scheduled_end,
        Job.scheduled_end > request.scheduled_start
    ).first()
    
    if conflict:
        raise HTTPException(
            status_code=400, 
            detail=f"Time slot conflicts with job \'{conflict.item_name}\'"
        )
    
    # Update the job
    job.printer_id = request.printer_id
    job.scheduled_start = request.scheduled_start
    job.scheduled_end = scheduled_end
    if job.status == JobStatus.PENDING:
        job.status = JobStatus.SCHEDULED
    
    db.commit()
    db.refresh(job)
    
    return {
        "success": True,
        "job_id": job.id,
        "printer_id": request.printer_id,
        "scheduled_start": request.scheduled_start.isoformat(),
        "scheduled_end": scheduled_end.isoformat()
    }



# ============== Bambu Lab Integration ==============

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


@app.post("/api/bambu/test-connection", tags=["Bambu"])
async def test_bambu_printer_connection(request: BambuConnectionTest):
    """Test connection to a Bambu Lab printer via local MQTT."""
    if not BAMBU_AVAILABLE:
        raise HTTPException(status_code=501, detail="Bambu integration not available. Install: pip install paho-mqtt")
    
    result = test_bambu_connection(
        ip_address=request.ip_address,
        serial_number=request.serial_number,
        access_code=request.access_code
    )
    return result


@app.post("/api/printers/{printer_id}/bambu/sync-ams", response_model=BambuSyncResult, tags=["Bambu"])
async def sync_bambu_ams(printer_id: int, db: Session = Depends(get_db)):
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
        spoolman_spools=spoolman_list
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
            FilamentSlot.slot_number == slot_info.slot_number
        ).first()
        
        if not db_slot:
            db_slot = FilamentSlot(
                printer_id=printer_id,
                slot_number=slot_info.slot_number,
                filament_type=FilamentType.PLA
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
        db_slot.loaded_at = datetime.utcnow()
        
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
        unmatched_slots=unmatched_slots
    )


@app.get("/api/bambu/filament-types", tags=["Bambu"])
async def list_bambu_filament_types():
    """List Bambu filament type codes and their mappings."""
    if not BAMBU_AVAILABLE:
        return {"error": "Bambu integration not available"}
    return {"bambu_to_normalized": BAMBU_FILAMENT_TYPE_MAP}


@app.patch("/api/printers/{printer_id}/slots/{slot_number}/manual-assign", tags=["Bambu"])
async def manual_slot_assignment(
    printer_id: int,
    slot_number: int,
    assignment: ManualSlotAssignment,
    db: Session = Depends(get_db)
):
    """Manually assign filament to a slot when auto-matching fails."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number
    ).first()
    
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    
    if assignment.filament_library_id:
        lib_entry = db.query(FilamentLibrary).filter(
            FilamentLibrary.id == assignment.filament_library_id
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
    
    slot.loaded_at = datetime.utcnow()
    db.commit()
    db.refresh(slot)
    
    return {
        "success": True,
        "slot_number": slot_number,
        "filament_type": slot.filament_type.value if slot.filament_type else None,
        "color": slot.color,
        "color_hex": slot.color_hex
    }


@app.get("/api/printers/{printer_id}/unmatched-slots", tags=["Bambu"])
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
        if not slot.spoolman_id and slot.color_hex:
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
        "slots": unmatched
    }


# ============== Spool Management ==============

from models import Spool, SpoolUsage, SpoolStatus

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


@app.get("/api/spools", tags=["Spools"])
def list_spools(
    status: Optional[str] = None,
    filament_id: Optional[int] = None,
    printer_id: Optional[int] = None,
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


@app.get("/api/spools/{spool_id}", tags=["Spools"])
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
                "notes": u.notes
            }
            for u in spool.usage_history
        ]
    }


@app.post("/api/spools", tags=["Spools"])
def create_spool(spool: SpoolCreate, db: Session = Depends(get_db)):
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
        status=SpoolStatus.ACTIVE
    )
    db.add(db_spool)
    db.commit()
    db.refresh(db_spool)
    
    log_audit(db, "create", "spool", db_spool.id, {"filament_id": spool.filament_id, "qr_code": db_spool.qr_code})
    return {
        "id": db_spool.id,
        "qr_code": db_spool.qr_code,
        "message": "Spool created"
    }


@app.patch("/api/spools/{spool_id}", tags=["Spools"])
def update_spool(spool_id: int, updates: SpoolUpdate, db: Session = Depends(get_db)):
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


@app.delete("/api/spools/{spool_id}", tags=["Spools"])
def delete_spool(spool_id: int, db: Session = Depends(get_db)):
    """Delete a spool (or archive it)."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    
    # Archive instead of delete
    spool.status = SpoolStatus.ARCHIVED
    db.commit()
    
    return {"success": True, "message": "Spool archived"}


@app.post("/api/spools/{spool_id}/load", tags=["Spools"])
def load_spool(spool_id: int, request: SpoolLoadRequest, db: Session = Depends(get_db)):
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
        Spool.location_slot == request.slot_number
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
        FilamentSlot.slot_number == request.slot_number
    ).first()
    if slot:
        slot.assigned_spool_id = spool_id
        slot.spool_confirmed = True
    
    db.commit()
    
    return {
        "success": True,
        "spool_id": spool_id,
        "printer": printer.name,
        "slot": request.slot_number
    }


@app.post("/api/spools/{spool_id}/unload", tags=["Spools"])
def unload_spool(
    spool_id: int,
    storage_location: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Unload a spool from printer to storage."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    
    # Clear slot assignment
    if spool.location_printer_id and spool.location_slot:
        slot = db.query(FilamentSlot).filter(
            FilamentSlot.printer_id == spool.location_printer_id,
            FilamentSlot.slot_number == spool.location_slot
        ).first()
        if slot and slot.assigned_spool_id == spool_id:
            slot.assigned_spool_id = None
            slot.spool_confirmed = False
    
    spool.location_printer_id = None
    spool.location_slot = None
    spool.storage_location = storage_location
    
    db.commit()
    
    return {"success": True, "message": "Spool unloaded"}


@app.post("/api/spools/{spool_id}/use", tags=["Spools"])
def use_spool(spool_id: int, request: SpoolUseRequest, db: Session = Depends(get_db)):
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
        spool_id=spool_id,
        job_id=request.job_id,
        weight_used_g=request.weight_used_g,
        notes=request.notes
    )
    db.add(usage)
    db.commit()
    
    log_audit(db, "use", "spool", spool_id, {"weight_used_g": request.weight_used_g, "remaining": spool.remaining_weight_g})
    return {
        "success": True,
        "remaining_weight_g": spool.remaining_weight_g,
        "percent_remaining": spool.percent_remaining,
        "status": spool.status.value
    }


@app.post("/api/spools/{spool_id}/weigh", tags=["Spools"])
def weigh_spool(spool_id: int, request: SpoolWeighRequest, db: Session = Depends(get_db)):
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
            spool_id=spool_id,
            weight_used_g=old_weight - new_weight,
            notes="Manual weigh adjustment"
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
        "percent_remaining": spool.percent_remaining
    }


@app.get("/api/spools/{spool_id}/qr", tags=["Spools"])
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
        "color_hex": spool.filament.color_hex if spool.filament else None
    }


@app.get("/api/spools/lookup/{qr_code}", tags=["Spools"])
def lookup_spool_by_qr(qr_code: str, db: Session = Depends(get_db)):
    """Look up a spool by QR code."""
    spool = db.query(Spool).filter(Spool.qr_code == qr_code).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    
    return get_spool(spool.id, db)


@app.post("/api/printers/{printer_id}/slots/{slot_number}/assign", tags=["Spools"])
def assign_spool_to_slot(
    printer_id: int,
    slot_number: int,
    spool_id: int,
    db: Session = Depends(get_db)
):
    """Assign a spool to a printer slot."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number
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


@app.post("/api/printers/{printer_id}/slots/{slot_number}/confirm", tags=["Spools"])
def confirm_slot_assignment(
    printer_id: int,
    slot_number: int,
    db: Session = Depends(get_db)
):
    """Confirm the spool assignment for a slot."""
    slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == printer_id,
        FilamentSlot.slot_number == slot_number
    ).first()
    if not slot:
        raise HTTPException(status_code=404, detail="Slot not found")
    
    if not slot.assigned_spool_id:
        raise HTTPException(status_code=400, detail="No spool assigned to confirm")
    
    slot.spool_confirmed = True
    db.commit()
    
    return {"success": True, "message": "Spool assignment confirmed"}


@app.get("/api/printers/{printer_id}/slots/needs-attention", tags=["Spools"])
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
                "spool_confirmed": slot.spool_confirmed
            })
    
    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "slots_needing_attention": len(issues),
        "slots": issues
    }


# ============== QR Label Generation ==============

import qrcode
from io import BytesIO
from fastapi.responses import StreamingResponse
from PIL import Image, ImageDraw, ImageFont


@app.get("/api/spools/{spool_id}/label", tags=["Spools"])
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
        headers={"Content-Disposition": f"inline; filename=spool_{spool_id}_label.png"}
    )


@app.get("/api/spools/labels/batch", tags=["Spools"])
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
        headers={"Content-Disposition": f"inline; filename=spool_labels_batch.png"}
    )


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


# ============== Audit Log ==============

@app.get("/api/audit-logs", tags=["Audit"])
def list_audit_logs(
    limit: int = 100,
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List audit log entries."""
    query = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(AuditLog.action == action)
    
    logs = query.limit(limit).all()
    
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "action": log.action,
            "entity_type": log.entity_type,
            "entity_id": log.entity_id,
            "details": log.details,
            "ip_address": log.ip_address
        }
        for log in logs
    ]

# ============== MQTT Print Jobs / Live Status ==============

@app.get("/api/print-jobs", tags=["Print Jobs"])
def get_print_jobs(
    printer_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 50,
    db: Session = Depends(get_db)
):
    """Get print job history from MQTT tracking."""
    from datetime import datetime as dt
    from sqlalchemy import text
    
    # Build query dynamically
    sql = """
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE 1=1
    """
    params = {}
    
    if printer_id is not None:
        sql += " AND pj.printer_id = :printer_id"
        params["printer_id"] = printer_id
    if status is not None:
        sql += " AND pj.status = :status"
        params["status"] = status
    
    sql += " ORDER BY pj.started_at DESC LIMIT :limit"
    params["limit"] = limit
    
    result = db.execute(text(sql), params).fetchall()
    
    jobs = []
    for row in result:
        job = dict(row._mapping)
        if job.get('ended_at') and job.get('started_at'):
            try:
                start = dt.fromisoformat(job['started_at'])
                end = dt.fromisoformat(job['ended_at'])
                job['duration_minutes'] = round((end - start).total_seconds() / 60, 1)
            except:
                job['duration_minutes'] = None
        else:
            job['duration_minutes'] = None
        jobs.append(job)
    
    return jobs

@app.get("/api/print-jobs/stats", tags=["Print Jobs"])
def get_print_job_stats(db: Session = Depends(get_db)):
    """Get aggregated print job statistics."""
    query = text("""
        SELECT 
            p.id as printer_id,
            p.name as printer_name,
            COUNT(*) as total_jobs,
            SUM(CASE WHEN pj.status = 'completed' THEN 1 ELSE 0 END) as completed_jobs,
            SUM(CASE WHEN pj.status = 'failed' THEN 1 ELSE 0 END) as failed_jobs,
            SUM(CASE WHEN pj.status = 'running' THEN 1 ELSE 0 END) as running_jobs,
            ROUND(SUM(
                CASE WHEN pj.ended_at IS NOT NULL 
                THEN (julianday(pj.ended_at) - julianday(pj.started_at)) * 24 
                ELSE 0 END
            ), 2) as total_hours
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        GROUP BY p.id
        ORDER BY total_hours DESC
    """)
    result = db.execute(query).fetchall()
    return [dict(row._mapping) for row in result]

@app.get("/api/printers/{printer_id}/live-status", tags=["Printers"])
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
    import time
    
    status_data = {}
    def on_status(status):
        nonlocal status_data
        status_data = status.raw_data.get('print', {})
    
    try:
        bp = BambuPrinter(
            ip=printer.api_host,
            serial=serial,
            access_code=access_code,
            on_status_update=on_status
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

@app.get("/api/printers/live-status", tags=["Printers"])
def get_all_printers_live_status(db: Session = Depends(get_db)):
    """Get real-time status from all Bambu printers."""
    printers = db.query(Printer).filter(
        Printer.api_host.isnot(None),
        Printer.api_key.isnot(None)
    ).all()
    
    results = []
    for printer in printers:
        status = get_printer_live_status(printer.id, db)
        results.append(status)
    
    return results
