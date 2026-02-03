"""
PrintFarm Scheduler API

FastAPI application providing REST endpoints for managing
printers, jobs, and the scheduling engine.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager
import re
import os

from pydantic import BaseModel as PydanticBaseModel, field_validator, ConfigDict
from fastapi import FastAPI, Depends, HTTPException, Query, status, Header, Request, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
import httpx
import shutil
from fastapi.staticfiles import StaticFiles
from branding import Branding, get_or_create_branding, branding_to_dict, UPDATABLE_FIELDS

from auth import (
    Token, UserCreate, UserResponse,
    verify_password, hash_password, create_access_token, decode_token, has_permission
)
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
from models import (Spool, SpoolUsage, SpoolStatus, AuditLog, 
    Base, Printer, FilamentSlot, Model, Job, JobStatus,
    FilamentType, SchedulerRun, init_db, FilamentLibrary,
    MaintenanceTask, MaintenanceLog, SystemConfig
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

# Auth helpers
async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    if not token:
        return None
    token_data = decode_token(token)
    if not token_data:
        return None
    user = db.execute(text("SELECT * FROM users WHERE username = :username"),
                      {"username": token_data.username}).fetchone()
    if not user:
        return None
    return dict(user._mapping)

def require_role(required_role: str):
    async def role_checker(current_user: dict = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not has_permission(current_user["role"], required_role):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return current_user
    return role_checker


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

# Static files for branding assets (logos, favicons)
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# API Key authentication middleware
@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    """Check API key for all routes except health check."""
    # Skip auth for health endpoint and OPTIONS (CORS preflight)
    if request.url.path == "/health" or "/label" in request.url.path or request.url.path.startswith("/api/auth") or request.url.path.startswith("/api/branding") or request.url.path.startswith("/static/branding") or request.method == "OPTIONS":
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


@app.post("/api/models/{model_id}/schedule", tags=["Models"])
def schedule_from_model(
    model_id: int,
    printer_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Create a print job from a model."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    colors = []
    if model.color_requirements:
        req = model.color_requirements if isinstance(model.color_requirements, dict) else json_lib.loads(model.color_requirements)
        for slot_key in sorted(req.keys()):
            slot = req[slot_key]
            if isinstance(slot, dict) and slot.get("color"):
                colors.append(slot["color"])
    
    job_result = db.execute(text("""
        INSERT INTO jobs (
            item_name, model_id, duration_hours, colors_required,
            quantity, priority, status, printer_id, hold, is_locked
        ) VALUES (
            :item_name, :model_id, :duration_hours, :colors_required,
            1, 5, 'PENDING', :printer_id, 0, 0
        )
    """), {
        "item_name": model.name,
        "model_id": model.id,
        "duration_hours": model.build_time_hours or 0,
        "colors_required": ','.join(colors),
        "printer_id": printer_id
    })
    db.commit()
    
    return {
        "job_id": job_result.lastrowid,
        "model_id": model.id,
        "model_name": model.name,
        "status": "pending"
    }

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
    """Mark a job as completed and auto-deduct filament from loaded spools."""
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
    
    # ---- Auto-deduct filament from spools ----
    deductions = []
    slot_grams = {}  # {slot_number: grams_to_deduct}
    
    # Source 1: Model color_requirements (has per-slot gram amounts)
    if job.model_id:
        model = db.query(Model).filter(Model.id == job.model_id).first()
        if model and model.color_requirements:
            req = model.color_requirements if isinstance(model.color_requirements, dict) else json_lib.loads(model.color_requirements)
            for i, slot_key in enumerate(sorted(req.keys())):
                slot_data = req[slot_key]
                if isinstance(slot_data, dict) and slot_data.get("grams"):
                    slot_grams[i + 1] = float(slot_data["grams"])
    
    # Source 2: Linked print file filaments (fallback if model has no gram data)
    if not slot_grams:
        # Check if a print_file is linked to this job
        pf_row = db.execute(text(
            "SELECT filaments_json FROM print_files WHERE job_id = :jid LIMIT 1"
        ), {"jid": job.id}).first()
        if pf_row and pf_row[0]:
            try:
                pf_filaments = json_lib.loads(pf_row[0]) if isinstance(pf_row[0], str) else pf_row[0]
                for i, fil in enumerate(pf_filaments):
                    grams = fil.get("used_grams") or fil.get("weight_grams")
                    if grams:
                        slot_grams[i + 1] = float(grams)
            except (json_lib.JSONDecodeError, TypeError):
                pass
    
    # Apply deductions to spools loaded on this printer
    if slot_grams and job.printer_id:
        loaded_spools = db.query(Spool).filter(
            Spool.location_printer_id == job.printer_id,
            Spool.status == SpoolStatus.ACTIVE
        ).all()
        
        spool_by_slot = {s.location_slot: s for s in loaded_spools if s.location_slot}
        
        for slot_num, grams in slot_grams.items():
            spool = spool_by_slot.get(slot_num)
            if spool and grams > 0:
                old_weight = spool.remaining_weight_g
                spool.remaining_weight_g = max(0, spool.remaining_weight_g - grams)
                
                # Create usage record for audit trail
                usage = SpoolUsage(
                    spool_id=spool.id,
                    weight_used_g=grams,
                    job_id=job.id,
                    notes=f"Auto-deducted on job #{job.id} complete ({job.item_name})"
                )
                db.add(usage)
                
                deductions.append({
                    "spool_id": spool.id,
                    "slot": slot_num,
                    "deducted_g": round(grams, 1),
                    "remaining_g": round(spool.remaining_weight_g, 1)
                })
    
    if deductions:
        deduct_summary = "; ".join(
            f"Slot {d['slot']}: -{d['deducted_g']}g (spool #{d['spool_id']})" 
            for d in deductions
        )
        job.notes = f"{job.notes or ''}\nFilament deducted: {deduct_summary}".strip()
    
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

@app.post("/api/jobs/{job_id}/cancel", response_model=JobResponse, tags=["Jobs"])
def cancel_job(job_id: int, db: Session = Depends(get_db)):
    """Cancel a pending or scheduled job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in [JobStatus.PENDING, JobStatus.SCHEDULED]:
        raise HTTPException(status_code=400, detail="Can only cancel pending or scheduled jobs")
    job.status = JobStatus.CANCELLED
    job.is_locked = True
    db.commit()
    db.refresh(job)
    return job
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
import shutil
from fastapi.staticfiles import StaticFiles
from branding import Branding, get_or_create_branding, branding_to_dict, UPDATABLE_FIELDS

SPOOLMAN_URL = "http://localhost:7912"

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


@app.get("/api/filaments", tags=["Filaments"])
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


@app.post("/api/filaments", tags=["Filaments"])
def add_custom_filament(data: FilamentCreateRequest, db: Session = Depends(get_db)):
    """Add a custom filament to the library."""
    filament = FilamentLibrary(
        brand=data.brand,
        name=data.name,
        material=data.material,
        color_hex=data.color_hex,
        is_custom=True
    )
    db.add(filament)
    db.commit()
    return {"id": filament.id, "brand": filament.brand, "name": filament.name, "message": "Filament added"}


@app.get("/api/filaments/combined", tags=["Filaments"])
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


@app.get("/api/filaments/{filament_id}", tags=["Filaments"])
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


@app.patch("/api/filaments/{filament_id}", tags=["Filaments"])
def update_filament(filament_id: str, updates: FilamentUpdateRequest, db: Session = Depends(get_db)):
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
        "message": "Filament updated"
    }


@app.delete("/api/filaments/{filament_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Filaments"])
def delete_filament(filament_id: str, db: Session = Depends(get_db)):
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


# ============== 3MF Upload ==============
import tempfile
import json as json_lib
from threemf_parser import parse_3mf


def _normalize_model_name(name: str) -> str:
    """Strip printer model suffixes for variant matching."""
    import re
    # Patterns: " (X1C)", " (H2D)", "_P1S", " - A1", etc.
    patterns = [
        r'\s*[\(\[\-_]\s*(X1C?|X1E|H2D|P1[SP]|A1(\s*Mini)?|Kobra\s*S1)\s*[\)\]]?\s*$',
    ]
    result = name
    for p in patterns:
        result = re.sub(p, '', result, flags=re.IGNORECASE)
    return result.strip()

@app.post("/api/print-files/upload", tags=["Print Files"])
async def upload_3mf(
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """Upload and parse a .3mf file."""
    if not file.filename.endswith('.3mf'):
        raise HTTPException(status_code=400, detail="Only .3mf files are supported")
    
    # Save to temp file for parsing
    with tempfile.NamedTemporaryFile(delete=False, suffix='.3mf') as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name
    
    try:
        # Parse the file
        metadata = parse_3mf(tmp_path)
        if not metadata:
            raise HTTPException(status_code=400, detail="Failed to parse .3mf file")
        
        # Store in database
        result = db.execute(text("""
            INSERT INTO print_files (
                filename, project_name, print_time_seconds, total_weight_grams,
                layer_count, layer_height, nozzle_diameter, printer_model,
                supports_used, bed_type, filaments_json, thumbnail_b64
            ) VALUES (
                :filename, :project_name, :print_time_seconds, :total_weight_grams,
                :layer_count, :layer_height, :nozzle_diameter, :printer_model,
                :supports_used, :bed_type, :filaments_json, :thumbnail_b64
            )
        """), {
            "filename": file.filename,
            "project_name": metadata.project_name,
            "print_time_seconds": metadata.print_time_seconds,
            "total_weight_grams": metadata.total_weight_grams,
            "layer_count": metadata.layer_count,
            "layer_height": metadata.layer_height,
            "nozzle_diameter": metadata.nozzle_diameter,
            "printer_model": metadata.printer_model,
            "supports_used": metadata.supports_used,
            "bed_type": metadata.bed_type,
            "filaments_json": json_lib.dumps([{
                "slot": f.slot,
                "type": f.type,
                "color": f.color,
                "used_meters": f.used_meters,
                "used_grams": f.used_grams
            } for f in metadata.filaments]),
            "thumbnail_b64": metadata.thumbnail_b64
        })
        db.commit()
        
        file_id = result.lastrowid
        
        # Check for existing model with same name (multi-variant support)
        normalized_name = _normalize_model_name(metadata.project_name)
        existing_model = db.execute(text(
            "SELECT id FROM models WHERE name = :name OR name = :raw_name LIMIT 1"
        ), {"name": normalized_name, "raw_name": metadata.project_name}).fetchone()
        
        color_req = {}
        for f_item in metadata.filaments:
            color_req[f"slot{f_item.slot}"] = {
                "color": f_item.color,
                "grams": round(f_item.used_grams, 2) if f_item.used_grams else 0
            }
        
        fil_type = "PLA"
        if metadata.filaments:
            fil_type = metadata.filaments[0].type or "PLA"
        
        if existing_model:
            # Attach as variant to existing model
            model_id = existing_model[0]
            is_new_model = False
            db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                       {"mid": model_id, "fid": file_id})
            db.commit()
        else:
            # Create new model
            model_result = db.execute(text("""
                INSERT INTO models (
                    name, build_time_hours, default_filament_type,
                    color_requirements, thumbnail_b64, print_file_id, category
                ) VALUES (
                    :name, :build_time_hours, :filament_type,
                    :color_requirements, :thumbnail_b64, :print_file_id, :category
                )
            """), {
                "name": normalized_name,  # Use normalized name for model
                "build_time_hours": round(metadata.print_time_seconds / 3600.0, 2),
                "filament_type": fil_type,
                "color_requirements": json_lib.dumps(color_req),
                "thumbnail_b64": metadata.thumbnail_b64,
                "print_file_id": file_id,
                "category": "Uploaded"
            })
            db.commit()
            model_id = model_result.lastrowid
            is_new_model = True
            db.execute(text("UPDATE print_files SET model_id = :mid WHERE id = :fid"),
                       {"mid": model_id, "fid": file_id})
            db.commit()
        
        return {
            "id": file_id,
            "filename": file.filename,
            "project_name": metadata.project_name,
            "print_time_seconds": metadata.print_time_seconds,
            "print_time_formatted": metadata.print_time_formatted(),
            "total_weight_grams": metadata.total_weight_grams,
            "layer_count": metadata.layer_count,
            "filaments": [{
                "slot": f.slot,
                "type": f.type,
                "color": f.color,
                "used_grams": f.used_grams
            } for f in metadata.filaments],
            "thumbnail_b64": metadata.thumbnail_b64,
            "is_sliced": metadata.print_time_seconds > 0,
            "model_id": model_id,
            "is_new_model": is_new_model,
            "printer_model": metadata.printer_model
        }
    finally:
        # Clean up temp file
        import os
        os.unlink(tmp_path)


@app.get("/api/print-files", tags=["Print Files"])
def list_print_files(
    limit: int = Query(default=20, ge=1, le=100),
    include_scheduled: bool = False,
    db: Session = Depends(get_db)
):
    """List uploaded print files."""
    query = """
        SELECT pf.*, j.status as job_status, j.item_name as job_name
        FROM print_files pf
        LEFT JOIN jobs j ON j.id = pf.job_id
    """
    if not include_scheduled:
        query += " WHERE pf.job_id IS NULL"
    query += " ORDER BY pf.uploaded_at DESC LIMIT :limit"
    
    results = db.execute(text(query), {"limit": limit}).fetchall()
    
    files = []
    for row in results:
        r = dict(row._mapping)
        r['filaments'] = json_lib.loads(r['filaments_json']) if r['filaments_json'] else []
        del r['filaments_json']
        r['print_time_formatted'] = f"{r['print_time_seconds'] // 3600}h {(r['print_time_seconds'] % 3600) // 60}m" if r['print_time_seconds'] >= 3600 else f"{r['print_time_seconds'] // 60}m"
        files.append(r)
    
    return files


@app.get("/api/print-files/{file_id}", tags=["Print Files"])
def get_print_file(file_id: int, db: Session = Depends(get_db)):
    """Get details of a specific print file."""
    result = db.execute(text("SELECT * FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")
    
    r = dict(result._mapping)
    r['filaments'] = json_lib.loads(r['filaments_json']) if r['filaments_json'] else []
    del r['filaments_json']
    r['print_time_formatted'] = f"{r['print_time_seconds'] // 3600}h {(r['print_time_seconds'] % 3600) // 60}m" if r['print_time_seconds'] >= 3600 else f"{r['print_time_seconds'] // 60}m"
    
    return r


@app.delete("/api/print-files/{file_id}", tags=["Print Files"])
def delete_print_file(file_id: int, db: Session = Depends(get_db)):
    """Delete an uploaded print file."""
    result = db.execute(text("SELECT id FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")
    
    db.execute(text("DELETE FROM print_files WHERE id = :id"), {"id": file_id})
    db.commit()
    return {"deleted": True}


@app.post("/api/print-files/{file_id}/schedule", tags=["Print Files"])
def schedule_print_file(
    file_id: int,
    printer_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Create a job from an uploaded print file."""
    # Get the print file
    result = db.execute(text("SELECT * FROM print_files WHERE id = :id"), {"id": file_id}).fetchone()
    if not result:
        raise HTTPException(status_code=404, detail="Print file not found")
    
    pf = dict(result._mapping)
    if pf['job_id']:
        raise HTTPException(status_code=400, detail="File already scheduled")
    
    filaments = json_lib.loads(pf['filaments_json']) if pf['filaments_json'] else []
    colors = [f['color'] for f in filaments]
    
    # Create the job
    job_result = db.execute(text("""
        INSERT INTO jobs (
            item_name, duration_hours, colors_required, quantity, priority, status, printer_id, hold, is_locked
        ) VALUES (
            :item_name, :duration_hours, :colors_required, 1, 5, 'PENDING', :printer_id, 0, 0
        )
    """), {
        "item_name": pf['project_name'],
        "duration_hours": pf['print_time_seconds'] / 3600.0,
        "colors_required": ','.join(colors),
        "printer_id": printer_id
    })
    db.commit()
    
    job_id = job_result.lastrowid
    
    # Link the print file to the job
    db.execute(text("UPDATE print_files SET job_id = :job_id WHERE id = :id"), {
        "job_id": job_id,
        "id": file_id
    })
    db.commit()
    
    return {
        "job_id": job_id,
        "file_id": file_id,
        "project_name": pf['project_name'],
        "status": "pending"
    }


# ============== Auth Endpoints ==============
@app.post("/api/auth/login", response_model=Token, tags=["Auth"])
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.execute(text("SELECT * FROM users WHERE username = :username"), 
                      {"username": form_data.username}).fetchone()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    
    db.execute(text("UPDATE users SET last_login = :now WHERE id = :id"), 
               {"now": datetime.now(), "id": user.id})
    db.commit()
    
    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/api/auth/me", tags=["Auth"])
async def get_me(current_user: dict = Depends(get_current_user)):
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"username": current_user["username"], "email": current_user["email"], "role": current_user["role"]}

@app.get("/api/users", tags=["Users"])
async def list_users(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    users = db.execute(text("SELECT id, username, email, role, is_active, last_login, created_at FROM users")).fetchall()
    return [dict(u._mapping) for u in users]

@app.post("/api/users", tags=["Users"])
async def create_user(user: UserCreate, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    password_hash = hash_password(user.password)
    try:
        db.execute(text("""
            INSERT INTO users (username, email, password_hash, role) 
            VALUES (:username, :email, :password_hash, :role)
        """), {"username": user.username, "email": user.email, "password_hash": password_hash, "role": user.role})
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail="Username or email already exists")
    return {"status": "created"}

@app.patch("/api/users/{user_id}", tags=["Users"])
async def update_user(user_id: int, updates: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if 'password' in updates and updates['password']:
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)
    
    if updates:
        set_clause = ", ".join(f"{k} = :{k}" for k in updates.keys())
        updates['id'] = user_id
        db.execute(text(f"UPDATE users SET {set_clause} WHERE id = :id"), updates)
        db.commit()
    return {"status": "updated"}

@app.delete("/api/users/{user_id}", tags=["Users"])
async def delete_user(user_id: int, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    if current_user["id"] == user_id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    db.commit()
    return {"status": "deleted"}

import yaml

GO2RTC_CONFIG = "/opt/printfarm-scheduler/go2rtc/go2rtc.yaml"

def get_camera_url(printer):
    """Get RTSP URL for a printer - from camera_url field or auto-generated from Bambu credentials."""
    if printer.camera_url:
        return printer.camera_url
    if printer.api_key and printer.api_host:
        try:
            parts = crypto.decrypt(printer.api_key).split("|")
            if len(parts) == 2:
                return f"rtsps://bblp:{parts[1]}@{printer.api_host}:322/streaming/live/1"
        except Exception:
            pass
    return None

def sync_go2rtc_config(db: Session):
    """Regenerate go2rtc config from printer camera URLs."""
    printers = db.query(Printer).filter(Printer.is_active == True).all()
    streams = {}
    for p in printers:
        url = get_camera_url(p)
        if url:
            streams[f"printer_{p.id}"] = url
    config = {
        "api": {"listen": "127.0.0.1:1984"},
        "webrtc": {"listen": "127.0.0.1:8555"},
        "streams": streams
    }
    with open(GO2RTC_CONFIG, "w") as f:
        yaml.dump(config, f, default_flow_style=False)

# Camera endpoints
@app.get("/api/cameras", tags=["Cameras"])
def list_cameras(db: Session = Depends(get_db)):
    """List printers with active camera streams in go2rtc."""
    import httpx
    
    # Check which streams go2rtc actually has configured
    active_streams = set()
    try:
        resp = httpx.get("http://127.0.0.1:1984/api/streams", timeout=2.0)
        if resp.status_code == 200:
            streams = resp.json()
            for key in streams:
                # Stream names are "printer_{id}"
                if key.startswith("printer_"):
                    try:
                        active_streams.add(int(key.split("_")[1]))
                    except ValueError:
                        pass
    except Exception:
        pass
    
    printers = db.query(Printer).filter(Printer.is_active == True).all()
    cameras = []
    for p in printers:
        if p.id in active_streams:
            cameras.append({"id": p.id, "name": p.name, "has_camera": True, "display_order": p.display_order or 0})
    return sorted(cameras, key=lambda x: x.get("display_order", 0))

@app.get("/api/cameras/{printer_id}/stream", tags=["Cameras"])
def get_camera_stream(printer_id: int, db: Session = Depends(get_db)):
    """Get go2rtc stream info for a printer camera."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")
    
    stream_name = f"printer_{printer_id}"
    
    # Ensure go2rtc config is up to date
    sync_go2rtc_config(db)
    
    return {
        "printer_id": printer_id,
        "printer_name": printer.name,
        "stream_name": stream_name,
        "webrtc_url": f"/api/cameras/{printer_id}/webrtc"
    }

@app.post("/api/cameras/{printer_id}/webrtc", tags=["Cameras"])
async def camera_webrtc(printer_id: int, request: Request, db: Session = Depends(get_db)):
    """Proxy WebRTC signaling to go2rtc."""
    import httpx
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    camera_url = get_camera_url(printer)
    if not camera_url:
        raise HTTPException(status_code=404, detail="Camera not found")
    
    stream_name = f"printer_{printer_id}"
    body = await request.body()
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"http://127.0.0.1:1984/api/webrtc?src={stream_name}",
            content=body,
            headers={"Content-Type": request.headers.get("content-type", "application/sdp")}
        )
    
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/sdp"))


# ============== Branding (White-Label) ==============

@app.get("/api/branding", tags=["Branding"])
async def get_branding(db: Session = Depends(get_db)):
    """Get branding config. PUBLIC - no auth required."""
    return branding_to_dict(get_or_create_branding(db))


@app.put("/api/branding", tags=["Branding"])
async def update_branding(data: dict, db: Session = Depends(get_db)):
    """Update branding config. Admin only."""
    branding = get_or_create_branding(db)
    for key, value in data.items():
        if key in UPDATABLE_FIELDS:
            setattr(branding, key, value)
    branding.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(branding)
    return branding_to_dict(branding)


@app.post("/api/branding/logo", tags=["Branding"])
async def upload_logo(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload brand logo. Admin only."""
    allowed = {"image/png", "image/jpeg", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"logo.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.logo_url = f"/static/branding/{filename}"
    db.commit()
    return {"logo_url": branding.logo_url}


@app.post("/api/branding/favicon", tags=["Branding"])
async def upload_favicon(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Upload favicon. Admin only."""
    allowed = {"image/png", "image/x-icon", "image/svg+xml", "image/webp"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="File type not allowed")
    upload_dir = os.path.join(os.path.dirname(__file__), "static", "branding")
    os.makedirs(upload_dir, exist_ok=True)
    ext = file.filename.split(".")[-1] if "." in file.filename else "png"
    filename = f"favicon.{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as f:
        shutil.copyfileobj(file.file, f)
    branding = get_or_create_branding(db)
    branding.favicon_url = f"/static/branding/{filename}"
    db.commit()
    return {"favicon_url": branding.favicon_url}


@app.delete("/api/branding/logo", tags=["Branding"])
async def remove_logo(db: Session = Depends(get_db)):
    """Remove brand logo. Admin only."""
    branding = get_or_create_branding(db)
    if branding.logo_url:
        filepath = os.path.join(os.path.dirname(__file__), branding.logo_url.lstrip("/"))
        if os.path.exists(filepath):
            os.remove(filepath)
    branding.logo_url = None
    db.commit()
    return {"logo_url": None}


# ============== Database Backups ==============

@app.post("/api/backups", tags=["System"])
def create_backup(db: Session = Depends(get_db)):
    """Create a database backup using SQLite online backup API."""
    import sqlite3 as sqlite3_mod
    
    backup_dir = Path(__file__).parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    
    # Resolve DB file path from engine URL
    engine_url = str(db.get_bind().url)
    if "///" in engine_url:
        db_path = engine_url.split("///", 1)[1]
    else:
        db_path = "printfarm.db"
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
    
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"printfarm_backup_{timestamp}.db"
    backup_path = str(backup_dir / backup_name)
    
    # Use SQLite online backup API — safe while DB is in use
    src = sqlite3_mod.connect(db_path)
    dst = sqlite3_mod.connect(backup_path)
    src.backup(dst)
    dst.close()
    src.close()
    
    size = os.path.getsize(backup_path)
    
    log_audit(db, "backup_created", "system", details={"filename": backup_name, "size_bytes": size})
    
    return {
        "filename": backup_name,
        "size_bytes": size,
        "size_mb": round(size / 1048576, 2),
        "created_at": datetime.utcnow().isoformat()
    }


@app.get("/api/backups", tags=["System"])
def list_backups():
    """List all database backups."""
    backup_dir = Path(__file__).parent / "backups"
    if not backup_dir.exists():
        return []
    
    backups = []
    for f in sorted(backup_dir.glob("printfarm_backup_*.db"), reverse=True):
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / 1048576, 2),
            "created_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        })
    return backups


@app.get("/api/backups/{filename}", tags=["System"])
def download_backup(filename: str):
    """Download a database backup file."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    backup_dir = Path(__file__).parent / "backups"
    backup_path = backup_dir / filename
    
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    
    from starlette.responses import FileResponse
    return FileResponse(
        path=str(backup_path),
        filename=filename,
        media_type="application/octet-stream"
    )


@app.delete("/api/backups/{filename}", status_code=status.HTTP_204_NO_CONTENT, tags=["System"])
def delete_backup(filename: str, db: Session = Depends(get_db)):
    """Delete a database backup."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    backup_dir = Path(__file__).parent / "backups"
    backup_path = backup_dir / filename
    
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    
    backup_path.unlink()
    log_audit(db, "backup_deleted", "system", details={"filename": filename})



# ============== Maintenance Tracking (v0.11.0) ==============


class MaintenanceTaskCreate(PydanticBaseModel):
    name: str
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: float = 0
    estimated_downtime_min: int = 30


class MaintenanceTaskUpdate(PydanticBaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    printer_model_filter: Optional[str] = None
    interval_print_hours: Optional[float] = None
    interval_days: Optional[int] = None
    estimated_cost: Optional[float] = None
    estimated_downtime_min: Optional[int] = None
    is_active: Optional[bool] = None


class MaintenanceLogCreate(PydanticBaseModel):
    printer_id: int
    task_id: Optional[int] = None
    task_name: str
    performed_by: Optional[str] = None
    notes: Optional[str] = None
    cost: float = 0
    downtime_minutes: int = 0


@app.get("/api/maintenance/tasks", tags=["Maintenance"])
def list_maintenance_tasks(db: Session = Depends(get_db)):
    """List all maintenance task templates."""
    tasks = db.query(MaintenanceTask).order_by(MaintenanceTask.name).all()
    return [{
        "id": t.id,
        "name": t.name,
        "description": t.description,
        "printer_model_filter": t.printer_model_filter,
        "interval_print_hours": t.interval_print_hours,
        "interval_days": t.interval_days,
        "estimated_cost": t.estimated_cost,
        "estimated_downtime_min": t.estimated_downtime_min,
        "is_active": t.is_active,
    } for t in tasks]


@app.post("/api/maintenance/tasks", tags=["Maintenance"])
def create_maintenance_task(data: MaintenanceTaskCreate, db: Session = Depends(get_db)):
    """Create a new maintenance task template."""
    task = MaintenanceTask(
        name=data.name,
        description=data.description,
        printer_model_filter=data.printer_model_filter,
        interval_print_hours=data.interval_print_hours,
        interval_days=data.interval_days,
        estimated_cost=data.estimated_cost,
        estimated_downtime_min=data.estimated_downtime_min,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return {"id": task.id, "name": task.name, "message": "Task created"}


@app.patch("/api/maintenance/tasks/{task_id}", tags=["Maintenance"])
def update_maintenance_task(task_id: int, data: MaintenanceTaskUpdate, db: Session = Depends(get_db)):
    """Update a maintenance task template."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    return {"id": task.id, "message": "Task updated"}


@app.delete("/api/maintenance/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_task(task_id: int, db: Session = Depends(get_db)):
    """Delete a maintenance task template and its logs."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    db.delete(task)
    db.commit()


@app.get("/api/maintenance/logs", tags=["Maintenance"])
def list_maintenance_logs(
    printer_id: Optional[int] = None,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db)
):
    """List maintenance logs, optionally filtered by printer."""
    query = db.query(MaintenanceLog).order_by(MaintenanceLog.performed_at.desc())
    if printer_id:
        query = query.filter(MaintenanceLog.printer_id == printer_id)
    logs = query.limit(limit).all()
    return [{
        "id": l.id,
        "printer_id": l.printer_id,
        "task_id": l.task_id,
        "task_name": l.task_name,
        "performed_at": l.performed_at.isoformat() if l.performed_at else None,
        "performed_by": l.performed_by,
        "notes": l.notes,
        "cost": l.cost,
        "downtime_minutes": l.downtime_minutes,
        "print_hours_at_service": l.print_hours_at_service,
    } for l in logs]


@app.post("/api/maintenance/logs", tags=["Maintenance"])
def create_maintenance_log(data: MaintenanceLogCreate, db: Session = Depends(get_db)):
    """Log a maintenance action performed on a printer."""
    printer = db.query(Printer).filter(Printer.id == data.printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")

    # Compute current total print hours for this printer (from completed jobs)
    result = db.execute(text(
        "SELECT COALESCE(SUM(duration_hours), 0) FROM jobs "
        "WHERE printer_id = :pid AND status = 'completed'"
    ), {"pid": data.printer_id}).scalar()
    total_hours = float(result or 0)

    log = MaintenanceLog(
        printer_id=data.printer_id,
        task_id=data.task_id,
        task_name=data.task_name,
        performed_by=data.performed_by,
        notes=data.notes,
        cost=data.cost,
        downtime_minutes=data.downtime_minutes,
        print_hours_at_service=total_hours,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return {"id": log.id, "message": "Maintenance logged", "print_hours_at_service": total_hours}


@app.delete("/api/maintenance/logs/{log_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_log(log_id: int, db: Session = Depends(get_db)):
    """Delete a maintenance log entry."""
    log = db.query(MaintenanceLog).filter(MaintenanceLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    db.delete(log)
    db.commit()


@app.get("/api/maintenance/status", tags=["Maintenance"])
def get_maintenance_status(db: Session = Depends(get_db)):
    """Get maintenance status for all active printers. Returns per-printer task health."""
    printers = db.query(Printer).filter(Printer.is_active == True).order_by(Printer.name).all()
    tasks = db.query(MaintenanceTask).filter(MaintenanceTask.is_active == True).all()

    # Total print hours per printer (computed from completed jobs)
    hours_rows = db.execute(text(
        "SELECT printer_id, COALESCE(SUM(duration_hours), 0) as total_hours "
        "FROM jobs WHERE status = 'completed' AND printer_id IS NOT NULL "
        "GROUP BY printer_id"
    )).fetchall()
    hours_map = {row[0]: float(row[1]) for row in hours_rows}

    # Latest maintenance log per (printer_id, task_id)
    all_logs = db.query(MaintenanceLog).all()
    log_map = {}
    for log in all_logs:
        key = (log.printer_id, log.task_id)
        if key not in log_map or (log.performed_at and log_map[key].performed_at and log.performed_at > log_map[key].performed_at):
            log_map[key] = log

    now = datetime.utcnow()
    result = []

    for printer in printers:
        total_hours = hours_map.get(printer.id, 0)
        printer_tasks = []
        worst_status = "ok"

        for task in tasks:
            # Filter: does this task apply to this printer?
            if task.printer_model_filter:
                if task.printer_model_filter.lower() not in (printer.model or "").lower():
                    continue

            last_log = log_map.get((printer.id, task.id))

            if last_log and last_log.performed_at:
                hours_since = total_hours - (last_log.print_hours_at_service or 0)
                days_since = (now - last_log.performed_at).days
                last_serviced = last_log.performed_at.isoformat()
                last_by = last_log.performed_by
            else:
                hours_since = total_hours
                days_since = (now - printer.created_at).days if printer.created_at else 0
                last_serviced = None
                last_by = None

            # Determine status
            task_status = "ok"
            progress = 0.0

            if task.interval_print_hours and task.interval_print_hours > 0:
                pct = (hours_since / task.interval_print_hours) * 100
                progress = max(progress, pct)
                if hours_since >= task.interval_print_hours:
                    task_status = "overdue"
                elif hours_since >= task.interval_print_hours * 0.8:
                    task_status = "due_soon"

            if task.interval_days and task.interval_days > 0:
                pct = (days_since / task.interval_days) * 100
                progress = max(progress, pct)
                if days_since >= task.interval_days:
                    task_status = "overdue"
                elif days_since >= task.interval_days * 0.8:
                    if task_status != "overdue":
                        task_status = "due_soon"

            if task_status == "overdue":
                worst_status = "overdue"
            elif task_status == "due_soon" and worst_status == "ok":
                worst_status = "due_soon"

            printer_tasks.append({
                "task_id": task.id,
                "task_name": task.name,
                "description": task.description,
                "interval_print_hours": task.interval_print_hours,
                "interval_days": task.interval_days,
                "hours_since_service": round(hours_since, 1),
                "days_since_service": days_since,
                "last_serviced": last_serviced,
                "last_by": last_by,
                "status": task_status,
                "progress_percent": round(min(progress, 150), 1),
            })

        result.append({
            "printer_id": printer.id,
            "printer_name": printer.name,
            "printer_model": printer.model,
            "total_print_hours": round(total_hours, 1),
            "tasks": sorted(printer_tasks, key=lambda t: {"overdue": 0, "due_soon": 1, "ok": 2}.get(t["status"], 3)),
            "overall_status": worst_status,
        })

    # Sort: overdue printers first
    result.sort(key=lambda p: {"overdue": 0, "due_soon": 1, "ok": 2}.get(p["overall_status"], 3))
    return result


@app.post("/api/maintenance/seed-defaults", tags=["Maintenance"])
def seed_default_maintenance_tasks(db: Session = Depends(get_db)):
    """Seed default maintenance tasks for common Bambu Lab printer models."""
    defaults = [
        # Universal tasks (all printers)
        {"name": "General Cleaning", "description": "Clean build plate, wipe exterior, clear debris from print area",
         "printer_model_filter": None, "interval_print_hours": 50, "interval_days": 14,
         "estimated_cost": 0, "estimated_downtime_min": 15},
        {"name": "Nozzle Inspection", "description": "Check nozzle for wear, clogs, or damage — replace if needed",
         "printer_model_filter": None, "interval_print_hours": 500, "interval_days": None,
         "estimated_cost": 8, "estimated_downtime_min": 15},
        {"name": "Build Plate Check", "description": "Inspect build plate surface — clean, re-level, or replace if worn",
         "printer_model_filter": None, "interval_print_hours": 1000, "interval_days": 180,
         "estimated_cost": 30, "estimated_downtime_min": 10},
        {"name": "Belt Tension Check", "description": "Verify X/Y belt tension and adjust if loose",
         "printer_model_filter": None, "interval_print_hours": 500, "interval_days": None,
         "estimated_cost": 0, "estimated_downtime_min": 20},
        {"name": "Firmware Update Check", "description": "Check for and apply firmware updates",
         "printer_model_filter": None, "interval_print_hours": None, "interval_days": 30,
         "estimated_cost": 0, "estimated_downtime_min": 15},
        # X1C / X1E specific
        {"name": "Carbon Rod Lubrication", "description": "Lubricate carbon rods on X/Y axes (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 5, "estimated_downtime_min": 20},
        {"name": "HEPA Filter Replacement", "description": "Replace HEPA filter in enclosure (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 500, "interval_days": 90,
         "estimated_cost": 12, "estimated_downtime_min": 5},
        {"name": "Purge Wiper Replacement", "description": "Replace purge/wiper assembly (X1 series)",
         "printer_model_filter": "X1", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 6, "estimated_downtime_min": 10},
        # P1S specific
        {"name": "HEPA Filter Replacement", "description": "Replace HEPA filter in enclosure (P1S)",
         "printer_model_filter": "P1S", "interval_print_hours": 500, "interval_days": 90,
         "estimated_cost": 12, "estimated_downtime_min": 5},
        {"name": "Carbon Rod Lubrication", "description": "Lubricate carbon rods on X/Y axes (P1S)",
         "printer_model_filter": "P1S", "interval_print_hours": 200, "interval_days": None,
         "estimated_cost": 5, "estimated_downtime_min": 20},
        # A1 specific
        {"name": "Hotend Cleaning", "description": "Clean hotend assembly and check for leaks (A1 series)",
         "printer_model_filter": "A1", "interval_print_hours": 300, "interval_days": None,
         "estimated_cost": 0, "estimated_downtime_min": 20},
    ]

    created = 0
    skipped = 0
    for d in defaults:
        existing = db.query(MaintenanceTask).filter(
            MaintenanceTask.name == d["name"],
            MaintenanceTask.printer_model_filter == d["printer_model_filter"]
        ).first()
        if not existing:
            task = MaintenanceTask(**d)
            db.add(task)
            created += 1
        else:
            skipped += 1

    db.commit()
    return {"message": f"Seeded {created} tasks ({skipped} already existed)", "created": created, "skipped": skipped}




# ============== Model Variants (v0.11.0) ==============

@app.get("/api/models/{model_id}/variants", tags=["Models"])
def get_model_variants(model_id: int, db: Session = Depends(get_db)):
    """Get all print file variants for a model."""
    model = db.execute(text("SELECT id, name FROM models WHERE id = :id"), {"id": model_id}).fetchone()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    variants = db.execute(text("""
        SELECT id, filename, printer_model, print_time_seconds, total_weight_grams,
               nozzle_diameter, layer_height, uploaded_at
        FROM print_files WHERE model_id = :model_id ORDER BY uploaded_at DESC
    """), {"model_id": model_id}).fetchall()
    
    return {
        "model_id": model_id,
        "model_name": model[1],
        "variants": [{
            "id": v[0], "filename": v[1], "printer_model": v[2] or "Unknown",
            "print_time_seconds": v[3], "print_time_hours": round(v[3]/3600.0, 2) if v[3] else 0,
            "total_weight_grams": v[4], "nozzle_diameter": v[5], "layer_height": v[6], "uploaded_at": v[7]
        } for v in variants]
    }


@app.delete("/api/models/{model_id}/variants/{variant_id}", tags=["Models"])
def delete_model_variant(model_id: int, variant_id: int, db: Session = Depends(get_db)):
    """Delete a variant from a model."""
    v = db.execute(text("SELECT id FROM print_files WHERE id=:id AND model_id=:mid"),
                   {"id": variant_id, "mid": model_id}).fetchone()
    if not v:
        raise HTTPException(status_code=404, detail="Variant not found")
    
    db.execute(text("DELETE FROM print_files WHERE id = :id"), {"id": variant_id})
    db.commit()
    remaining = db.execute(text("SELECT COUNT(*) FROM print_files WHERE model_id=:mid"),
                           {"mid": model_id}).scalar()
    return {"message": "Variant deleted", "remaining": remaining}

# ============== RBAC Permissions (v0.11.0) ==============

RBAC_DEFAULT_PAGE_ACCESS = {
    "dashboard": [
        "admin",
        "operator",
        "viewer"
    ],
    "timeline": [
        "admin",
        "operator",
        "viewer"
    ],
    "jobs": [
        "admin",
        "operator",
        "viewer"
    ],
    "printers": [
        "admin",
        "operator",
        "viewer"
    ],
    "models": [
        "admin",
        "operator",
        "viewer"
    ],
    "spools": [
        "admin",
        "operator",
        "viewer"
    ],
    "cameras": [
        "admin",
        "operator",
        "viewer"
    ],
    "analytics": [
        "admin",
        "operator",
        "viewer"
    ],
    "calculator": [
        "admin",
        "operator",
        "viewer"
    ],
    "upload": [
        "admin",
        "operator"
    ],
    "maintenance": [
        "admin",
        "operator"
    ],
    "settings": [
        "admin"
    ],
    "admin": [
        "admin"
    ],
    "branding": [
        "admin"
    ]
}

RBAC_DEFAULT_ACTION_ACCESS = {
    "jobs.create": [
        "admin",
        "operator"
    ],
    "jobs.edit": [
        "admin",
        "operator"
    ],
    "jobs.cancel": [
        "admin",
        "operator"
    ],
    "jobs.delete": [
        "admin",
        "operator"
    ],
    "jobs.start": [
        "admin",
        "operator"
    ],
    "jobs.complete": [
        "admin",
        "operator"
    ],
    "printers.add": [
        "admin"
    ],
    "printers.edit": [
        "admin",
        "operator"
    ],
    "printers.delete": [
        "admin"
    ],
    "printers.slots": [
        "admin",
        "operator"
    ],
    "printers.reorder": [
        "admin",
        "operator"
    ],
    "models.create": [
        "admin",
        "operator"
    ],
    "models.edit": [
        "admin",
        "operator"
    ],
    "models.delete": [
        "admin"
    ],
    "spools.edit": [
        "admin",
        "operator"
    ],
    "spools.delete": [
        "admin"
    ],
    "timeline.move": [
        "admin",
        "operator"
    ],
    "upload.upload": [
        "admin",
        "operator"
    ],
    "upload.schedule": [
        "admin",
        "operator"
    ],
    "upload.delete": [
        "admin",
        "operator"
    ],
    "maintenance.log": [
        "admin",
        "operator"
    ],
    "maintenance.tasks": [
        "admin"
    ],
    "dashboard.actions": [
        "admin",
        "operator"
    ]
}


def _get_rbac(db: Session):
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row and row.value:
        data = row.value
        return {
            "page_access": data.get("page_access", RBAC_DEFAULT_PAGE_ACCESS),
            "action_access": data.get("action_access", RBAC_DEFAULT_ACTION_ACCESS),
        }
    return {
        "page_access": RBAC_DEFAULT_PAGE_ACCESS,
        "action_access": RBAC_DEFAULT_ACTION_ACCESS,
    }


@app.get("/api/permissions", tags=["RBAC"])
def get_permissions(db: Session = Depends(get_db)):
    """Get current RBAC permission map. Public (needed at login)."""
    return _get_rbac(db)


class RBACUpdateRequest(PydanticBaseModel):
    page_access: dict
    action_access: dict


@app.put("/api/permissions", tags=["RBAC"])
def update_permissions(data: RBACUpdateRequest, db: Session = Depends(get_db)):
    """Update RBAC permissions. Admin only."""
    valid_roles = {"admin", "operator", "viewer"}
    for key, roles in data.page_access.items():
        if not isinstance(roles, list):
            raise HTTPException(400, f"page_access.{key} must be a list")
        for r in roles:
            if r not in valid_roles:
                raise HTTPException(400, f"Invalid role '{r}' in page_access.{key}")
        if key in ("admin", "settings") and "admin" not in roles:
            raise HTTPException(400, f"Cannot remove admin from '{key}' page")

    for key, roles in data.action_access.items():
        if not isinstance(roles, list):
            raise HTTPException(400, f"action_access.{key} must be a list")
        for r in roles:
            if r not in valid_roles:
                raise HTTPException(400, f"Invalid role '{r}' in action_access.{key}")

    value = {"page_access": data.page_access, "action_access": data.action_access}
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row:
        row.value = value
    else:
        row = SystemConfig(key="rbac_permissions", value=value)
        db.add(row)
    db.commit()
    return {"message": "Permissions updated", **value}


@app.post("/api/permissions/reset", tags=["RBAC"])
def reset_permissions(db: Session = Depends(get_db)):
    """Reset permissions to defaults. Admin only."""
    row = db.query(SystemConfig).filter(SystemConfig.key == "rbac_permissions").first()
    if row:
        db.delete(row)
        db.commit()
    return {
        "message": "Reset to defaults",
        "page_access": RBAC_DEFAULT_PAGE_ACCESS,
        "action_access": RBAC_DEFAULT_ACTION_ACCESS,
    }
