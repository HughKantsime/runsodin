"""
O.D.I.N. — Orchestrated Dispatch & Inventory Network API

FastAPI application providing REST endpoints for managing
printers, jobs, and the scheduling engine.
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from contextlib import asynccontextmanager
import re
import os
import json
import logging

log = logging.getLogger("odin.api")
logger = log  # alias used in some places

from pydantic import BaseModel as PydanticBaseModel, field_validator, ConfigDict
from fastapi import FastAPI, Depends, HTTPException, Query, status, Header, Request, Response, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker, joinedload
import httpx
import shutil
import asyncio
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
    Alert, AlertPreference, AlertType, AlertSeverity, PushSubscription,
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
    HealthCheck,
    AlertResponse, AlertSummary, AlertPreferenceResponse,
    AlertPreferencesUpdate, SmtpConfigBase, SmtpConfigResponse,
    PushSubscriptionCreate, AlertTypeEnum, AlertSeverityEnum
)
from scheduler import Scheduler, SchedulerConfig, run_scheduler
from config import settings
import crypto
from license_manager import get_license, require_feature, check_printer_limit, check_user_limit, save_license_file


# Database setup
engine = create_engine(settings.database_url, echo=settings.debug)
# Enable WAL mode for concurrent read support (5-10 readers + writers)
with engine.connect() as conn:
    conn.execute(text("PRAGMA journal_mode=WAL"))
    conn.execute(text("PRAGMA busy_timeout=5000"))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
# Compute is_online from last_seen timestamp
def compute_printer_online(printer_dict):
    """Add is_online field based on last_seen within 90 seconds."""
    from datetime import datetime, timedelta
    if printer_dict.get('last_seen'):
        try:
            last = datetime.fromisoformat(str(printer_dict['last_seen']))
            printer_dict['is_online'] = (datetime.utcnow() - last).total_seconds() < 90
        except:
            printer_dict['is_online'] = False
    else:
        printer_dict['is_online'] = False
    return printer_dict



# Read version from VERSION file
import pathlib as _pathlib
_version_file = _pathlib.Path(__file__).parent.parent / "VERSION"
if _version_file.exists():
    __version__ = _version_file.read_text().strip()
else:
    __version__ = "1.0.29"


def get_db():
    """Dependency for database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Auth helpers
async def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
):
    # Try 1: JWT Bearer token (primary auth)
    if token:
        token_data = decode_token(token)
        if token_data:
            user = db.execute(text("SELECT * FROM users WHERE username = :username"),
                              {"username": token_data.username}).fetchone()
            if user:
                return dict(user._mapping)

    # Try 2: X-API-Key header (fallback for raw fetch calls missing Bearer)
    api_key = request.headers.get("X-API-Key")
    if api_key and api_key != "undefined":
        configured_key = os.getenv("API_KEY", "")
        if configured_key and api_key == configured_key:
            admin = db.execute(
                text("SELECT * FROM users WHERE role = 'admin' AND is_active = 1 ORDER BY id LIMIT 1")
            ).fetchone()
            if admin:
                return dict(admin._mapping)

    return None

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
    """Initialize database on startup, start WebSocket broadcaster."""
    Base.metadata.create_all(bind=engine)
    # Start WebSocket event broadcaster
    broadcast_task = asyncio.create_task(ws_broadcaster())
    yield
    broadcast_task.cancel()


# Create FastAPI app
app = FastAPI(
    title="O.D.I.N.",
    description="Orchestrated Dispatch & Inventory Network — Self-hosted 3D print farm management",
    version=__version__,
    lifespan=lifespan
, docs_url="/api/docs", redoc_url="/api/redoc")

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for branding assets (logos, favicons)
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# API Key authentication middleware
# === Security: Rate limiting + Account lockout ===
from collections import defaultdict
import time as _time

_login_attempts = defaultdict(list)  # ip -> [timestamps]
_account_lockouts = {}  # username -> lockout_until_timestamp
_LOGIN_RATE_LIMIT = 10  # max attempts per window
_LOGIN_RATE_WINDOW = 300  # 5 minute window
_LOCKOUT_THRESHOLD = 5  # failed attempts before lockout
_LOCKOUT_DURATION = 900  # 15 minute lockout


def _validate_password(password: str) -> tuple:
    """Validate password complexity. Returns (is_valid, message)."""
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter"
    if not any(c.islower() for c in password):
        return False, "Password must contain at least one lowercase letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    return True, "OK"

def _check_rate_limit(ip: str) -> bool:
    """Returns True if rate limited"""
    now = _time.time()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _LOGIN_RATE_WINDOW]
    return len(_login_attempts[ip]) >= _LOGIN_RATE_LIMIT

def _record_login_attempt(ip: str, username: str, success: bool, db=None):
    """Record attempt for rate limiting and audit"""
    now = _time.time()
    _login_attempts[ip].append(now)
    
    if not success:
        # Check for lockout
        recent_failures = [t for t in _login_attempts[ip] if now - t < _LOGIN_RATE_WINDOW]
        if len(recent_failures) >= _LOCKOUT_THRESHOLD:
            _account_lockouts[username] = now + _LOCKOUT_DURATION
    
    # Log to audit trail if db available
    if db:
        try:
            log = AuditLog(
                action="login_success" if success else "login_failed",
                entity_type="user",
                details=f"{'Login' if success else 'Failed login'}: {username} from {ip}",
                ip_address=ip,
            )
            db.add(log)
            db.commit()
        except Exception:
            pass

def _is_locked_out(username: str) -> bool:
    """Returns True if account is locked"""
    if username in _account_lockouts:
        if _time.time() < _account_lockouts[username]:
            return True
        del _account_lockouts[username]
    return False


@app.middleware("http")
async def authenticate_request(request: Request, call_next):
    """Check API key for all routes except health check."""
    # Skip auth for health endpoint and OPTIONS (CORS preflight)
    if request.url.path in ("/health", "/metrics", "/ws") or (request.url.path.endswith("/label") or request.url.path.endswith("/labels/batch")) or request.url.path.startswith("/api/auth") or request.url.path.startswith("/api/setup") or request.url.path == "/api/license" or (request.url.path == "/api/branding" and request.method == "GET") or request.url.path.startswith("/static/branding") or request.method == "OPTIONS":
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
        version=__version__,
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
):
    """Create a new printer."""
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
            filament_type=slot_data.filament_type if slot_data else None,
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
                        filament_type=FilamentType.EMPTY
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
def delete_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
    
    # If slot is set to empty, clear all filament data
    if slot.filament_type and slot.filament_type.value == "empty":
        slot.color = None
        slot.color_hex = None
        slot.spoolman_spool_id = None
        slot.assigned_spool_id = None
        slot.spool_confirmed = False
    
    slot.loaded_at = datetime.utcnow()
    db.commit()
    db.refresh(slot)
    return slot


@app.post("/api/printers/{printer_id}/sync-ams", tags=["Printers"])
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



@app.post("/api/printers/{printer_id}/lights", tags=["Printers"])
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
        import time
        
        bambu = BambuPrinter(
            ip=printer.api_host,
            serial=serial,
            access_code=access_code
        )
        
        if not bambu.connect():
            raise HTTPException(status_code=503, detail="Failed to connect to printer")
        
        time.sleep(3)
        
        payload = {
            'system': {
                'sequence_id': '0',
                'command': 'ledctrl',
                'led_node': 'chamber_light',
                'led_mode': 'on' if turn_on else 'off'
            }
        }
        success = bambu._publish(payload)
        
        time.sleep(1)
        bambu.disconnect()
        
        if not success:
            raise HTTPException(status_code=503, detail="Failed to send light command")
        
        # Update DB immediately + set cooldown so monitor doesn't overwrite
        from datetime import datetime
        printer.lights_on = turn_on
        printer.lights_toggled_at = datetime.utcnow()
        db.commit()
        db.refresh(printer)
        
        return {"lights_on": turn_on, "message": f"Lights {'on' if turn_on else 'off'}"}
        
    except ImportError:
        raise HTTPException(status_code=500, detail="bambu_adapter not installed")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Printer connection error: {str(e)}")

@app.post("/api/printers/test-connection", tags=["Printers"])
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






# ============== License Management ==============

@app.get("/api/license", tags=["License"])
def get_license_info():
    """Get current license status. No auth required so frontend can check tier."""
    license_info = get_license()
    return license_info.to_dict()


@app.post("/api/license/upload", tags=["License"])
async def upload_license(
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("admin")),
):
    """Upload a license file. Admin only."""
    content = await file.read()
    license_text = content.decode("utf-8").strip()

    # Handle both formats:
    # 1. JSON format from generate_license.py: {"format":"odin-license-v1","payload":"...","signature":"..."}
    # 2. Dot-separated format: base64_payload.base64_signature
    import json as _json
    try:
        license_json = _json.loads(license_text)
        if "payload" in license_json and "signature" in license_json:
            # Convert JSON format to dot-separated for storage
            license_text = license_json["payload"] + "." + license_json["signature"]
    except (ValueError, KeyError):
        pass  # Not JSON, try dot-separated format

    parts = license_text.split(".")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid license file format")

    # Save the file
    path = save_license_file(license_text)

    # Reload and validate
    license_info = get_license()
    if license_info.error:
        # Remove invalid file
        import os
        os.remove(path)
        raise HTTPException(status_code=400, detail=license_info.error)

    return {
        "status": "activated",
        "tier": license_info.tier,
        "licensee": license_info.licensee,
        "expires_at": license_info.expires_at,
    }


@app.delete("/api/license", tags=["License"])
def remove_license(
    current_user: dict = Depends(require_role("admin")),
):
    """Remove the license file (revert to Community tier). Admin only."""
    import os
    from license_manager import LICENSE_DIR, LICENSE_FILENAME, _cached_license
    license_path = os.path.join(LICENSE_DIR, LICENSE_FILENAME)
    if os.path.exists(license_path):
        os.remove(license_path)
    # Clear cache
    import license_manager
    license_manager._cached_license = None
    license_manager._cached_mtime = 0
    return {"status": "removed", "tier": "community"}


# ============== Setup / Onboarding Wizard ==============

class SetupAdminRequest(PydanticBaseModel):
    username: str
    email: str
    password: str
    role: str = "admin"

class SetupPrinterRequest(PydanticBaseModel):
    name: str
    model: Optional[str] = None
    api_type: Optional[str] = None
    api_host: Optional[str] = None
    api_key: Optional[str] = None
    slot_count: int = 4
    is_active: bool = True

class SetupTestPrinterRequest(PydanticBaseModel):
    api_type: str
    api_host: str
    serial: Optional[str] = None
    access_code: Optional[str] = None


def _setup_users_exist(db: Session) -> bool:
    """Check if any users exist in the database."""
    result = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    return result > 0


def _setup_is_complete(db: Session) -> bool:
    """Check if setup has been marked as complete."""
    row = db.execute(text(
        "SELECT value FROM system_config WHERE key = 'setup_complete'"
    )).fetchone()
    if row:
        return row[0] == "true"
    return False


@app.get("/api/setup/status", tags=["Setup"])
def setup_status(db: Session = Depends(get_db)):
    """Check if initial setup is needed. No auth required."""
    has_users = _setup_users_exist(db)
    is_complete = _setup_is_complete(db)
    return {
        "needs_setup": not has_users and not is_complete,
        "has_users": has_users,
        "is_complete": is_complete,
    }


@app.post("/api/setup/admin", tags=["Setup"])
def setup_create_admin(request: SetupAdminRequest, db: Session = Depends(get_db)):
    """Create the first admin user during setup. Refuses if any user exists."""
    if _setup_users_exist(db):
        raise HTTPException(status_code=403, detail="Setup already completed — users exist")

    pw_valid, pw_msg = _validate_password(request.password)
    if not pw_valid:
        raise HTTPException(status_code=400, detail=pw_msg)
    password_hash_val = hash_password(request.password)
    try:
        db.execute(text("""
            INSERT INTO users (username, email, password_hash, role)
            VALUES (:username, :email, :password_hash, :role)
        """), {
            "username": request.username,
            "email": request.email,
            "password_hash": password_hash_val,
            "role": "admin"
        })
        db.commit()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to create user: {str(e)}")

    # Return a JWT token so the wizard can make authenticated calls
    access_token = create_access_token(
        data={"sub": request.username, "role": "admin"}
    )
    return {"access_token": access_token, "token_type": "bearer"}


@app.post("/api/setup/test-printer", tags=["Setup"])
def setup_test_printer(request: SetupTestPrinterRequest, db: Session = Depends(get_db)):
    """Test printer connection during setup. Wraps existing test logic."""
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    if request.api_type.lower() == "bambu":
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
                return {"success": False, "error": "Failed to connect. Check IP, serial, and access code."}

            time.sleep(2)
            bambu_status = bambu.get_status()
            bambu.disconnect()

            return {
                "success": True,
                "state": bambu_status.state.value,
                "bed_temp": bambu_status.bed_temp,
                "nozzle_temp": bambu_status.nozzle_temp,
                "ams_slots": len(bambu_status.ams_slots)
            }
        except ImportError:
            raise HTTPException(status_code=500, detail="bambu_adapter not installed")
        except Exception as e:
            return {"success": False, "error": str(e)}

    elif request.api_type.lower() == "moonraker":
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
            return {"success": False, "error": f"Moonraker returned {r.status_code}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    return {"success": False, "error": f"Unknown printer type: {request.api_type}"}


@app.post("/api/setup/printer", tags=["Setup"])
def setup_create_printer(request: SetupPrinterRequest, db: Session = Depends(get_db)):
    """Create a printer during setup. Requires JWT from admin creation step."""
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    # Encrypt api_key if provided
    encrypted_api_key = None
    if request.api_key:
        encrypted_api_key = crypto.encrypt(request.api_key)

    db_printer = Printer(
        name=request.name,
        model=request.model,
        slot_count=request.slot_count,
        is_active=request.is_active,
        api_type=request.api_type,
        api_host=request.api_host,
        api_key=encrypted_api_key,
    )
    db.add(db_printer)
    db.flush()

    # Create empty filament slots
    for i in range(1, request.slot_count + 1):
        slot = FilamentSlot(
            printer_id=db_printer.id,
            slot_number=i,
            filament_type=FilamentType.EMPTY,
        )
        db.add(slot)

    db.commit()
    db.refresh(db_printer)
    return {"id": db_printer.id, "name": db_printer.name, "status": "created"}


@app.post("/api/setup/complete", tags=["Setup"])
def setup_mark_complete(db: Session = Depends(get_db)):
    """Mark setup as complete. Prevents wizard from showing again."""
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    existing = db.execute(text(
        "SELECT id FROM system_config WHERE key = 'setup_complete'"
    )).fetchone()

    if existing:
        db.execute(text(
            "UPDATE system_config SET value = 'true' WHERE key = 'setup_complete'"
        ))
    else:
        # Insert using the SystemConfig model pattern
        config = SystemConfig(key="setup_complete", value="true")
        db.add(config)

    db.commit()
    return {"status": "complete"}

@app.get("/api/setup/network", tags=["Setup"])
def setup_network_info(request: Request):
    """Return auto-detected host IP for network configuration."""
    # Best detection: use the Host header from the browser request
    # When user hits http://192.168.70.200:8000, Host = "192.168.70.200:8000"
    detected_ip = ""
    host_header = request.headers.get("host", "")
    host_part = host_header.split(":")[0] if host_header else ""
    # Only use if it looks like a real LAN IP (not localhost, not Docker internal)
    import re as _re
    if host_part and _re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_part):
        if not host_part.startswith("127.") and not host_part.startswith("172."):
            detected_ip = host_part
    # Fallback to socket detection (works on bare metal, useless in Docker)
    if not detected_ip:
        detected_ip = _get_lan_ip() or ""
    return {"detected_ip": detected_ip, "configured_ip": os.environ.get("ODIN_HOST_IP", "")}

@app.post("/api/setup/network", tags=["Setup"])
async def setup_save_network(request: Request, db: Session = Depends(get_db)):
    """Save host IP for WebRTC camera streaming."""
    data = await request.json()
    host_ip = data.get("host_ip", "").strip()
    if not host_ip:
        raise HTTPException(status_code=400, detail="host_ip is required")
    import re
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_ip):
        raise HTTPException(status_code=400, detail="Invalid IP address format")
    existing = db.execute(text("SELECT 1 FROM system_config WHERE key = 'host_ip'")).fetchone()
    if existing:
        db.execute(text("UPDATE system_config SET value = :v WHERE key = 'host_ip'"), {"v": host_ip})
    else:
        db.execute(text("INSERT INTO system_config (key, value) VALUES ('host_ip', :v)"), {"v": host_ip})
    db.commit()
    sync_go2rtc_config(db)
    return {"success": True, "host_ip": host_ip}


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


@app.get("/api/models-with-pricing", tags=["Models"])
def list_models_with_pricing(
    category: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all print models with calculated cost and suggested price."""
    query = db.query(Model)
    if category:
        query = query.filter(Model.category == category)
    models = query.order_by(Model.name).all()
    
    # Get pricing config once
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG
    
    # Get variant counts using raw SQL (print_files has no SQLAlchemy model)
    variant_counts = {}
    for model in models:
        result = db.execute(text("SELECT COUNT(*) FROM print_files WHERE model_id = :mid"), {"mid": model.id}).scalar()
        variant_counts[model.id] = result or 1
    
    result = []
    for model in models:
        # Calculate cost for this model
        filament_grams = model.total_filament_grams or 0
        print_hours = model.build_time_hours or 1.0
        
        # Try to get per-material cost
        material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
        filament_entry = db.query(FilamentLibrary).filter(
            FilamentLibrary.material == material_type,
            FilamentLibrary.cost_per_gram.isnot(None)
        ).first()
        
        if filament_entry and filament_entry.cost_per_gram:
            cost_per_gram = filament_entry.cost_per_gram
        else:
            cost_per_gram = config["spool_cost"] / config["spool_weight"]
        
        # Calculate costs
        material_cost = filament_grams * cost_per_gram
        labor_hours = (config["post_processing_min"] + config["packing_min"] + config["support_min"]) / 60
        labor_cost = labor_hours * config["hourly_rate"]
        electricity_cost = (config["printer_wattage"] / 1000) * print_hours * config["electricity_rate"]
        depreciation_cost = (config["printer_cost"] / config["printer_lifespan"]) * print_hours
        packaging_cost = config["packaging_cost"]
        base_cost = material_cost + labor_cost + electricity_cost + depreciation_cost + packaging_cost + config["other_costs"]
        failure_cost = base_cost * (config["failure_rate"] / 100)
        overhead_cost = config["monthly_rent"] / config["parts_per_month"] if config["parts_per_month"] > 0 else 0
        subtotal = base_cost + failure_cost + overhead_cost
        
        margin = model.markup_percent if model.markup_percent else config["default_margin"]
        suggested_price = subtotal * (1 + margin / 100)
        
        # Build response
        model_dict = {
            "id": model.id,
            "name": model.name,
            "build_time_hours": model.build_time_hours,
            "default_filament_type": model.default_filament_type.value if model.default_filament_type else None,
            "color_requirements": model.color_requirements,
            "category": model.category,
            "thumbnail_url": model.thumbnail_url,
            "thumbnail_b64": model.thumbnail_b64,
            "notes": model.notes,
            "cost_per_item": model.cost_per_item,
            "units_per_bed": model.units_per_bed,
            "markup_percent": model.markup_percent,
            "created_at": model.created_at.isoformat() if model.created_at else None,
            "updated_at": model.updated_at.isoformat() if model.updated_at else None,
            "required_colors": model.required_colors,
            "total_filament_grams": model.total_filament_grams,
            "variant_count": variant_counts.get(model.id, 1),
            # New pricing fields
            "estimated_cost": round(subtotal, 2),
            "suggested_price": round(suggested_price, 2),
            "margin_percent": margin,
            "is_favorite": model.is_favorite or False
        }
        result.append(model_dict)
    
    return result


@app.post("/api/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED, tags=["Models"])
def create_model(model: ModelCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def update_model(model_id: int, updates: ModelUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def delete_model(model_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
    
    # Calculate cost
    estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=model.id)
    
    job_result = db.execute(text("""
        INSERT INTO jobs (
            item_name, model_id, duration_hours, colors_required,
            quantity, priority, status, printer_id, hold, is_locked,
            estimated_cost, suggested_price
        ) VALUES (
            :item_name, :model_id, :duration_hours, :colors_required,
            1, 5, 'PENDING', :printer_id, 0, 0,
            :estimated_cost, :suggested_price
        )
    """), {
        "item_name": model.name,
        "model_id": model.id,
        "duration_hours": model.build_time_hours or 0,
        "colors_required": ','.join(colors),
        "printer_id": printer_id,
        "estimated_cost": estimated_cost,
        "suggested_price": suggested_price
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
def create_job(job: JobCreate, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    """Create a new print job. If approval is required and user is a viewer, job is created as 'submitted'."""
    # Calculate cost if model is linked
    estimated_cost, suggested_price, _ = (None, None, None)
    if job.model_id:
        estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)
    
    # Check if approval workflow is enabled
    approval_required = False
    approval_config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if approval_config and approval_config.value in (True, "true", "True", "1"):
        approval_required = True
    
    # Determine initial status
    initial_status = JobStatus.PENDING
    submitted_by = None
    if approval_required and current_user and current_user.get("role") == "viewer":
        initial_status = "submitted"
        submitted_by = current_user.get("id")
    
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
        status=initial_status,
        estimated_cost=estimated_cost,
        suggested_price=suggested_price,
        submitted_by=submitted_by,
        due_date=job.due_date,
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)

    # If submitted for approval, notify approvers
    if initial_status == "submitted":
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_SUBMITTED,
                severity=AlertSeverity.INFO,
                title=f"Job awaiting approval: {job.item_name or 'Untitled'}",
                message=f"{current_user.get('display_name') or current_user.get('username', 'A user')} submitted a print job",
                job_id=db_job.id
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_submitted alert: {e}")
    
    return db_job


@app.post("/api/jobs/bulk", response_model=List[JobResponse], status_code=status.HTTP_201_CREATED, tags=["Jobs"])
def create_jobs_bulk(jobs: List[JobCreate], current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create multiple jobs at once."""
    db_jobs = []
    for job in jobs:
        # Calculate cost if model is linked
        estimated_cost, suggested_price, _ = (None, None, None)
        if job.model_id:
            estimated_cost, suggested_price, _ = calculate_job_cost(db, model_id=job.model_id)
        
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
            status=JobStatus.PENDING,
            estimated_cost=estimated_cost,
            suggested_price=suggested_price
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
def update_job(job_id: int, updates: JobUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def delete_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a job."""
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    db.delete(job)
    db.commit()



@app.post("/api/jobs/{job_id}/repeat", tags=["Jobs"])
async def repeat_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Clone a job for printing again. Creates a new pending job with same settings."""
    original = db.query(Job).filter(Job.id == job_id).first()
    if not original:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Create new job with same settings
    new_job = Job(
        model_id=original.model_id,
        item_name=original.item_name,
        quantity=original.quantity,
        status="pending",
        priority=original.priority,
        printer_id=original.printer_id,  # Same printer preference
        duration_hours=original.duration_hours,
        colors_required=original.colors_required,
        filament_type=original.filament_type,
        notes=f"Repeat of job #{job_id}" + (f" - {original.notes}" if original.notes else ""),
        estimated_cost=original.estimated_cost,
        suggested_price=original.suggested_price,
        quantity_on_bed=original.quantity_on_bed,
    )
    
    db.add(new_job)
    db.commit()
    db.refresh(new_job)
    
    return {
        "success": True,
        "message": f"Job cloned successfully",
        "original_job_id": job_id,
        "new_job_id": new_job.id
    }


@app.post("/api/jobs/{job_id}/start", response_model=JobResponse, tags=["Jobs"])
def start_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def complete_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def fail_job(job_id: int, notes: Optional[str] = None, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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

@app.post("/api/jobs/{job_id}/cancel", response_model=JobResponse, tags=["Jobs"])
def cancel_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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


@app.post("/api/jobs/{job_id}/reset", response_model=JobResponse, tags=["Jobs"])
def reset_job(job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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


# ============== Job Approval Workflow (v0.18.0) ==============

class _RejectJobRequest(PydanticBaseModel):
    """Inline schema for reject endpoint."""
    reason: str

@app.post("/api/jobs/{job_id}/approve", tags=["Jobs"])
def approve_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    require_feature("job_approval")
    """Approve a submitted job. Moves it to pending status for scheduling."""
    if not current_user or current_user.get("role") not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Only operators and admins can approve jobs")
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")
    
    job.status = JobStatus.PENDING
    job.approved_by = current_user["id"]
    job.approved_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    
    # Notify the student who submitted
    if job.submitted_by:
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_APPROVED,
                severity=AlertSeverity.INFO,
                title=f"Job approved: {job.item_name or 'Untitled'}",
                message=f"Approved by {current_user.get('display_name') or current_user.get('username', 'an approver')}",
                job_id=job.id
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_approved alert: {e}")
    
    return {"status": "approved", "job_id": job.id}


@app.post("/api/jobs/{job_id}/reject", tags=["Jobs"])
def reject_job(job_id: int, body: _RejectJobRequest, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    require_feature("job_approval")
    """Reject a submitted job with a required reason."""
    if not current_user or current_user.get("role") not in ("operator", "admin"):
        raise HTTPException(status_code=403, detail="Only operators and admins can reject jobs")
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status != "submitted":
        raise HTTPException(status_code=400, detail="Job is not in submitted status")
    
    if not body.reason or not body.reason.strip():
        raise HTTPException(status_code=400, detail="Rejection reason is required")
    
    job.status = "rejected"
    job.approved_by = current_user["id"]
    job.rejected_reason = body.reason.strip()
    db.commit()
    db.refresh(job)
    
    # Notify the student who submitted
    if job.submitted_by:
        try:
            from alert_dispatcher import dispatch_alert
            dispatch_alert(
                db=db,
                alert_type=AlertType.JOB_REJECTED,
                severity=AlertSeverity.WARNING,
                title=f"Job rejected: {job.item_name or 'Untitled'}",
                message=f"Reason: {body.reason.strip()}",
                job_id=job.id
            )
        except Exception as e:
            logger.warning(f"Failed to dispatch job_rejected alert: {e}")
    
    return {"status": "rejected", "job_id": job.id, "reason": body.reason.strip()}


@app.post("/api/jobs/{job_id}/resubmit", tags=["Jobs"])
def resubmit_job(job_id: int, db: Session = Depends(get_db), current_user: dict = Depends(get_current_user)):
    require_feature("job_approval")
    """Resubmit a rejected job for approval again."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    if job.status != "rejected":
        raise HTTPException(status_code=400, detail="Only rejected jobs can be resubmitted")
    
    if job.submitted_by != current_user["id"]:
        raise HTTPException(status_code=403, detail="Only the original submitter can resubmit")
    
    job.status = "submitted"
    job.rejected_reason = None
    job.approved_by = None
    job.approved_at = None
    db.commit()
    db.refresh(job)
    
    # Re-notify approvers
    try:
        from alert_dispatcher import dispatch_alert
        dispatch_alert(
            db=db,
            alert_type=AlertType.JOB_SUBMITTED,
            severity=AlertSeverity.INFO,
            title=f"Job resubmitted: {job.item_name or 'Untitled'}",
            message=f"{current_user.get('display_name') or current_user.get('username', 'A user')} resubmitted a previously rejected job",
            job_id=job.id
        )
    except Exception as e:
        logger.warning(f"Failed to dispatch job_submitted alert: {e}")
    
    return {"status": "resubmitted", "job_id": job.id}


@app.get("/api/config/require-job-approval", tags=["Config"])
def get_approval_setting(db: Session = Depends(get_db)):
    """Get the current job approval requirement setting."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    enabled = False
    if config and config.value in (True, "true", "True", "1"):
        enabled = True
    return {"require_job_approval": enabled}


@app.put("/api/config/require-job-approval", tags=["Config"])
def set_approval_setting(body: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Toggle the job approval requirement. Admin only. Requires Education tier."""
    require_feature("job_approval")
    enabled = body.get("enabled", False)
    config = db.query(SystemConfig).filter(SystemConfig.key == "require_job_approval").first()
    if config:
        config.value = "true" if enabled else "false"
    else:
        config = SystemConfig(key="require_job_approval", value="true" if enabled else "false")
        db.add(config)
    db.commit()
    return {"require_job_approval": enabled}


# ============== Scheduler ==============

@app.post("/api/scheduler/run", response_model=ScheduleResult, tags=["Scheduler"])
def run_scheduler_endpoint(
    config: Optional[SchedulerConfigSchema] = None,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
    
    # --- Printer utilization stats for Utilization page ---
    printer_stats = []
    all_printers = db.query(Printer).filter(Printer.is_active == True).all()
    for p in all_printers:
        # Count completed and failed jobs for this printer
        completed_jobs = db.query(Job).filter(
            Job.printer_id == p.id,
            Job.status == JobStatus.COMPLETED
        ).count()
        # Also count MQTT-tracked completed jobs
        mqtt_completed = db.execute(
            text("SELECT COUNT(*) FROM print_jobs WHERE printer_id = :pid AND status = 'completed'"),
            {"pid": p.id}
        ).scalar() or 0
        completed_jobs += mqtt_completed

        failed_jobs = db.query(Job).filter(
            Job.printer_id == p.id,
            Job.status == JobStatus.FAILED
        ).count()
        mqtt_failed = db.execute(
            text("SELECT COUNT(*) FROM print_jobs WHERE printer_id = :pid AND status = 'failed'"),
            {"pid": p.id}
        ).scalar() or 0
        failed_jobs += mqtt_failed

        total_hours = round(p.total_print_hours or 0, 1)
        total_jobs = completed_jobs + failed_jobs
        success_rate = round((completed_jobs / total_jobs * 100), 1) if total_jobs > 0 else 100.0
        avg_job_hours = round(total_hours / completed_jobs, 1) if completed_jobs > 0 else 0

        # Utilization: hours printed / hours available (assume 24h/day over last 30 days = 720h)
        utilization_pct = round(min(total_hours / 720 * 100, 100), 1) if total_hours > 0 else 0

        printer_stats.append({
            "id": p.id,
            "name": p.name,
            "completed_jobs": completed_jobs,
            "failed_jobs": failed_jobs,
            "total_hours": total_hours,
            "utilization_pct": utilization_pct,
            "success_rate": success_rate,
            "avg_job_hours": avg_job_hours,
        })

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
        "spoolman_connected": spoolman_connected,
        "printer_stats": printer_stats
    }

# ============== Spoolman Integration ==============
import httpx
import shutil
from fastapi.staticfiles import StaticFiles
from branding import Branding, get_or_create_branding, branding_to_dict, UPDATABLE_FIELDS

SPOOLMAN_URL = "http://localhost:7912"


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
def add_custom_filament(data: FilamentCreateRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
        "message": "Filament updated"
    }


@app.delete("/api/filaments/{filament_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Filaments"])
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
def update_config(config: ConfigUpdate, current_user: dict = Depends(require_role("admin"))):
    """Update configuration. Writes to .env file."""
    # Use environment variable or default path
    env_path = os.environ.get('ENV_FILE_PATH', '/data/.env')
    
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



@app.post("/api/jobs/{job_id}/link-print", tags=["Jobs"])
def link_job_to_print(job_id: int, print_job_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Link a scheduled job to an MQTT-detected print."""
    from sqlalchemy import text
    
    # Check job exists
    job = db.execute(text("SELECT id, printer_id FROM jobs WHERE id = :id"), {"id": job_id}).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    # Check print_job exists
    print_job = db.execute(text("SELECT id, printer_id, status FROM print_jobs WHERE id = :id"), {"id": print_job_id}).fetchone()
    if not print_job:
        raise HTTPException(status_code=404, detail="Print job not found")
    
    # Verify same printer
    if job.printer_id != print_job.printer_id:
        raise HTTPException(status_code=400, detail="Job and print are on different printers")
    
    # Link them
    db.execute(text("UPDATE print_jobs SET scheduled_job_id = :job_id WHERE id = :id"), 
               {"job_id": job_id, "id": print_job_id})
    
    # Update job status based on print status
    new_status = None
    if print_job.status == 'completed':
        new_status = 'completed'
    elif print_job.status == 'failed':
        new_status = 'failed'
    elif print_job.status in ('printing', 'running'):
        new_status = 'printing'
    
    if new_status:
        db.execute(text("UPDATE jobs SET status = :status WHERE id = :id"),
                   {"status": new_status, "id": job_id})
    
    db.commit()
    return {"message": "Linked", "job_id": job_id, "print_job_id": print_job_id}


@app.get("/api/print-jobs/unlinked", tags=["Print Jobs"])
def get_unlinked_print_jobs(printer_id: int = None, db: Session = Depends(get_db)):
    """Get recent print jobs not linked to scheduled jobs."""
    from sqlalchemy import text
    
    sql = """
        SELECT pj.*, p.name as printer_name
        FROM print_jobs pj
        JOIN printers p ON p.id = pj.printer_id
        WHERE pj.scheduled_job_id IS NULL
    """
    params = {}
    
    if printer_id:
        sql += " AND pj.printer_id = :printer_id"
        params["printer_id"] = printer_id
    
    sql += " ORDER BY pj.started_at DESC LIMIT 20"
    
    result = db.execute(text(sql), params).fetchall()
    return [dict(row._mapping) for row in result]


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
    
    # Revenue and costs from completed jobs (use job.suggested_price/estimated_cost when available)
    total_revenue = 0
    total_cost = 0
    total_print_hours = 0
    jobs_with_cost_data = 0
    
    for job in completed_jobs:
        # Use job's stored cost data if available
        if job.suggested_price:
            total_revenue += job.suggested_price * job.quantity
            jobs_with_cost_data += 1
        elif job.model_id:
            # Fallback to model data for older jobs
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                total_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity
        
        if job.estimated_cost:
            total_cost += job.estimated_cost * job.quantity
        
        if job.duration_hours:
            total_print_hours += job.duration_hours
    
    # Calculate margin
    total_margin = total_revenue - total_cost if total_cost > 0 else 0
    margin_percent = (total_margin / total_revenue * 100) if total_revenue > 0 else 0
    
    # Projected revenue from pending jobs
    projected_revenue = 0
    projected_cost = 0
    for job in pending_jobs:
        if job.suggested_price:
            projected_revenue += job.suggested_price * job.quantity
        elif job.model_id:
            model = db.query(Model).filter(Model.id == job.model_id).first()
            if model and model.cost_per_item:
                projected_revenue += model.cost_per_item * (model.markup_percent or 300) / 100 * job.quantity
        
        if job.estimated_cost:
            projected_cost += job.estimated_cost * job.quantity
    
    # Printer utilization
    printers = db.query(Printer).filter(Printer.is_active == True).all()
    printer_stats = []
    # Calculate time window for utilization (since first completed job or 30 days)
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    for printer in printers:
        printer_jobs = [j for j in completed_jobs if j.printer_id == printer.id]
        hours = sum(j.duration_hours or 0 for j in printer_jobs)
        # Utilization = print hours / available hours (since printer's first job, max 30 days)
        if printer_jobs:
            earliest = min(j.created_at for j in printer_jobs if j.created_at)
            available_hours = min((now - earliest).total_seconds() / 3600, 30 * 24)
            utilization_pct = round((hours / available_hours * 100), 1) if available_hours > 0 else 0
        else:
            available_hours = 0
            utilization_pct = 0
        # Average job duration
        avg_hours = round(hours / len(printer_jobs), 1) if printer_jobs else 0
        # Success rate
        total_printer_jobs = [j for j in db.query(Job).filter(Job.printer_id == printer.id).all()]
        failed = len([j for j in total_printer_jobs if j.status == 'failed'])
        total_attempted = len([j for j in total_printer_jobs if j.status in ('complete', 'failed')])
        success_rate = round(((total_attempted - failed) / total_attempted * 100), 1) if total_attempted > 0 else 100
        printer_stats.append({
            "id": printer.id,
            "name": printer.name,
            "completed_jobs": len(printer_jobs),
            "total_hours": round(hours, 1),
            "utilization_pct": utilization_pct,
            "avg_job_hours": avg_hours,
            "success_rate": success_rate,
            "failed_jobs": failed,
            "has_plug": bool(getattr(printer, 'plug_type', None)),
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
            "total_cost": round(total_cost, 2),
            "total_margin": round(total_margin, 2),
            "margin_percent": round(margin_percent, 1),
            "projected_revenue": round(projected_revenue, 2),
            "projected_cost": round(projected_cost, 2),
            "total_print_hours": round(total_print_hours, 1),
            "avg_value_per_hour": round(avg_value_per_hour, 2),
            "jobs_with_cost_data": jobs_with_cost_data,
        },
        "printer_stats": printer_stats,
        "jobs_by_date": jobs_by_date,
    }


# ============== Education Usage Report ==============

@app.get("/api/education/usage-report", tags=["Education"])
def get_education_usage_report(
    days: int = Query(default=30, ge=7, le=90),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_role("operator")),
):
    """Education usage report — per-user job metrics and summary stats."""
    require_feature("usage_reports")

    cutoff = datetime.utcnow() - timedelta(days=days)

    # Get all users
    users_rows = db.execute(
        text("SELECT id, username, email, role, is_active, last_login FROM users")
    ).fetchall()

    # Get jobs in window, eager-load model for filament data
    jobs_in_range = (
        db.query(Job)
        .options(joinedload(Job.model))
        .filter(Job.created_at >= cutoff)
        .all()
    )

    # Build per-user stats
    user_stats = []
    fleet_hours = 0
    fleet_jobs = 0
    fleet_approved = 0
    fleet_rejected = 0
    active_ids = set()

    for row in users_rows:
        u = dict(row._mapping)
        uid = u["id"]
        user_jobs = [j for j in jobs_in_range if j.submitted_by == uid]
        if not user_jobs:
            continue

        active_ids.add(uid)
        n_submitted = len(user_jobs)
        n_approved = sum(1 for j in user_jobs if j.approved_by is not None and j.rejected_reason is None)
        n_rejected = sum(1 for j in user_jobs if j.rejected_reason is not None)
        n_completed = sum(1 for j in user_jobs if j.status == JobStatus.COMPLETED)
        n_failed = sum(1 for j in user_jobs if j.status == JobStatus.FAILED)

        hours = sum(
            (j.duration_hours or (j.model.build_time_hours if j.model else 0) or 0) * j.quantity
            for j in user_jobs if j.status == JobStatus.COMPLETED
        )
        grams = sum(
            (j.model.total_filament_grams if j.model else 0) * j.quantity
            for j in user_jobs if j.status == JobStatus.COMPLETED
        )

        last_act = max((j.created_at for j in user_jobs), default=None)

        user_stats.append({
            "user_id": uid,
            "username": u["username"],
            "email": u["email"],
            "role": u["role"],
            "total_jobs_submitted": n_submitted,
            "total_jobs_approved": n_approved,
            "total_jobs_rejected": n_rejected,
            "total_jobs_completed": n_completed,
            "total_jobs_failed": n_failed,
            "total_print_hours": round(hours, 1),
            "total_filament_grams": round(grams, 1),
            "approval_rate": round(n_approved / n_submitted * 100, 1) if n_submitted else 0,
            "success_rate": round(n_completed / (n_completed + n_failed) * 100, 1) if (n_completed + n_failed) else 0,
            "last_activity": last_act.isoformat() if last_act else None,
        })

        fleet_hours += hours
        fleet_jobs += n_submitted
        fleet_approved += n_approved
        fleet_rejected += n_rejected

    user_stats.sort(key=lambda x: x["total_jobs_submitted"], reverse=True)

    # Daily submissions for chart
    daily = {}
    for j in jobs_in_range:
        d = j.created_at.strftime("%Y-%m-%d")
        daily[d] = daily.get(d, 0) + 1

    return {
        "summary": {
            "total_users_active": len(active_ids),
            "total_print_hours": round(fleet_hours, 1),
            "total_jobs": fleet_jobs,
            "approval_rate": round(fleet_approved / fleet_jobs * 100, 1) if fleet_jobs else 0,
            "rejection_rate": round(fleet_rejected / fleet_jobs * 100, 1) if fleet_jobs else 0,
        },
        "users": user_stats,
        "daily_submissions": daily,
        "days": days,
    }


# ============== CSV Export ==============

from fastapi.responses import StreamingResponse
import csv
import io

@app.get("/api/export/jobs", tags=["Export"])
def export_jobs_csv(
    status: Optional[str] = None,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Export jobs as CSV."""
    query = db.query(Job)
    if status:
        query = query.filter(Job.status == status)
    jobs = query.order_by(Job.created_at.desc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "ID", "Item Name", "Model ID", "Quantity", "Status", "Priority",
        "Printer ID", "Duration (hrs)", "Estimated Cost", "Suggested Price",
        "Scheduled Start", "Actual Start", "Actual End", "Created At"
    ])
    
    # Data
    for job in jobs:
        writer.writerow([
            job.id,
            job.item_name,
            job.model_id,
            job.quantity,
            job.status.value if job.status else "",
            job.priority,
            job.printer_id,
            job.duration_hours,
            job.estimated_cost,
            job.suggested_price,
            job.scheduled_start.isoformat() if job.scheduled_start else "",
            job.actual_start.isoformat() if job.actual_start else "",
            job.actual_end.isoformat() if job.actual_end else "",
            job.created_at.isoformat() if job.created_at else ""
        ])
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs_export.csv"}
    )


@app.get("/api/export/spools", tags=["Export"])
def export_spools_csv(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Export spools as CSV."""
    spools = db.query(Spool).order_by(Spool.id).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "ID", "Filament ID", "QR Code", "RFID Tag", "Color Hex",
        "Initial Weight (g)", "Remaining Weight (g)", "Status",
        "Printer ID", "Slot", "Storage Location", "Vendor", "Price", "Created At"
    ])
    
    # Data
    for spool in spools:
        writer.writerow([
            spool.id,
            spool.filament_id,
            spool.qr_code,
            spool.rfid_tag,
            spool.color_hex,
            spool.initial_weight_g,
            spool.remaining_weight_g,
            spool.status.value if spool.status else "",
            spool.location_printer_id,
            spool.location_slot,
            spool.storage_location,
            spool.vendor,
            spool.price,
            spool.created_at.isoformat() if spool.created_at else ""
        ])
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=spools_export.csv"}
    )


@app.get("/api/export/filament-usage", tags=["Export"])
def export_filament_usage_csv(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Export filament usage history as CSV."""
    usage_records = db.query(SpoolUsage).order_by(SpoolUsage.used_at.desc()).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "ID", "Spool ID", "Job ID", "Weight Used (g)", "Used At", "Notes"
    ])
    
    # Data
    for usage in usage_records:
        writer.writerow([
            usage.id,
            usage.spool_id,
            usage.job_id,
            usage.weight_used_g,
            usage.used_at.isoformat() if usage.used_at else "",
            usage.notes
        ])
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=filament_usage_export.csv"}
    )


@app.get("/api/export/models", tags=["Export"])
def export_models_csv(current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Export models as CSV."""
    models = db.query(Model).order_by(Model.name).all()
    
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow([
        "ID", "Name", "Category", "Filament Type", "Build Time (hrs)",
        "Total Filament (g)", "Cost Per Item", "Markup %", "Units Per Bed", "Created At"
    ])
    
    # Data
    for model in models:
        writer.writerow([
            model.id,
            model.name,
            model.category,
            model.default_filament_type.value if model.default_filament_type else "",
            model.build_time_hours,
            model.total_filament_grams,
            model.cost_per_item,
            model.markup_percent,
            model.units_per_bed,
            model.created_at.isoformat() if model.created_at else ""
        ])
    
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=models_export.csv"}
    )


@app.get("/api/export/audit-logs", tags=["Export"])
def export_audit_logs_csv(
    entity_type: Optional[str] = None,
    action: Optional[str] = None,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Export audit logs as CSV."""
    query = db.query(AuditLog).order_by(AuditLog.timestamp.desc())
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if action:
        query = query.filter(AuditLog.action == action)
    logs = query.limit(5000).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Timestamp", "Action", "Entity Type", "Entity ID", "Details", "IP Address"])
    for log in logs:
        writer.writerow([
            log.id,
            log.timestamp.isoformat() if log.timestamp else "",
            log.action,
            log.entity_type or "",
            log.entity_id or "",
            json.dumps(log.details) if isinstance(log.details, dict) else (log.details or ""),
            log.ip_address or ""
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_logs_{datetime.utcnow().strftime('%Y%m%d')}.csv"}
    )


# ============== CSV Export ==============

from fastapi.responses import StreamingResponse
import csv
import io


class MoveJobRequest(PydanticBaseModel):
    printer_id: int
    scheduled_start: datetime

@app.patch("/api/jobs/{job_id}/move", tags=["Jobs"])
def move_job(
    job_id: int,
    request: MoveJobRequest,
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
async def test_bambu_printer_connection(request: BambuConnectionTest, current_user: dict = Depends(require_role("operator"))):
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
                filament_type=FilamentType.EMPTY
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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


@app.delete("/api/spools/{spool_id}", tags=["Spools"])
def delete_spool(spool_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a spool (or archive it)."""
    spool = db.query(Spool).filter(Spool.id == spool_id).first()
    if not spool:
        raise HTTPException(status_code=404, detail="Spool not found")
    
    # Archive instead of delete
    spool.status = SpoolStatus.ARCHIVED
    db.commit()
    
    return {"success": True, "message": "Spool archived"}


@app.post("/api/spools/{spool_id}/load", tags=["Spools"])
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
, current_user: dict = Depends(require_role("admin"))):
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
from threemf_parser import parse_3mf, extract_objects_from_plate, extract_mesh_from_3mf
import smart_plug


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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
        
        # Extract objects for quantity counting
        import zipfile
        with zipfile.ZipFile(tmp_path, 'r') as zf:
            plate_objects = extract_objects_from_plate(zf)
        
        # Extract 3D mesh for viewer
        mesh_data = extract_mesh_from_3mf(tmp_path)
        mesh_json = json_lib.dumps(mesh_data) if mesh_data else None
        
        # Store in database
        result = db.execute(text("""
            INSERT INTO print_files (
                filename, project_name, print_time_seconds, total_weight_grams,
                layer_count, layer_height, nozzle_diameter, printer_model,
                supports_used, bed_type, filaments_json, thumbnail_b64, mesh_data
            ) VALUES (
                :filename, :project_name, :print_time_seconds, :total_weight_grams,
                :layer_count, :layer_height, :nozzle_diameter, :printer_model,
                :supports_used, :bed_type, :filaments_json, :thumbnail_b64, :mesh_json
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
            "thumbnail_b64": metadata.thumbnail_b64,
            "mesh_json": mesh_json
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
            "printer_model": metadata.printer_model,
            "objects": plate_objects,
            "has_mesh": mesh_data is not None
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
def delete_print_file(file_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
    current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)
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
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # Security checks
    client_ip = request.client.host if hasattr(request, 'client') and request.client else "unknown"
    if _check_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again in 5 minutes.")
    if _is_locked_out(form_data.username):
        raise HTTPException(status_code=423, detail="Account temporarily locked due to repeated failed attempts. Try again in 15 minutes.")
    
    user = db.execute(text("SELECT * FROM users WHERE username = :username"), 
                      {"username": form_data.username}).fetchone()
    if not user or not verify_password(form_data.password, user.password_hash):
        _record_login_attempt(client_ip, form_data.username, False, db)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account disabled")
    
    _record_login_attempt(client_ip, form_data.username, True, db)
    
    db.execute(text("UPDATE users SET last_login = :now WHERE id = :id"), 
               {"now": datetime.now(), "id": user.id})
    db.commit()
    
    access_token = create_access_token(data={"sub": user.username, "role": user.role})
    return {"access_token": access_token, "token_type": "bearer"}

# =============================================================================
# OIDC / SSO Authentication
# =============================================================================

@app.get("/api/auth/oidc/config", tags=["Auth"])
async def get_oidc_public_config(db: Session = Depends(get_db)):
    """Get public OIDC config for login page (is SSO enabled, display name)."""
    row = db.execute(text("SELECT is_enabled, display_name FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"enabled": False}
    return {
        "enabled": bool(row[0]),
        "display_name": row[1] or "Single Sign-On",
    }


@app.get("/api/auth/oidc/login", tags=["Auth"])
async def oidc_login(request: Request, db: Session = Depends(get_db)):
    """Initiate OIDC login flow. Redirects to identity provider."""
    from oidc_handler import create_handler_from_config
    
    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled = 1 LIMIT 1")).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="OIDC not configured")
    
    config = dict(row._mapping)
    
    # Build redirect URI from request
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/auth/oidc/callback"
    
    handler = create_handler_from_config(config, redirect_uri)

    url, state = await handler.get_authorization_url()

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/auth/oidc/callback", tags=["Auth"])
async def oidc_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None,
    db: Session = Depends(get_db)
):
    """Handle OIDC callback from identity provider."""
    from oidc_handler import create_handler_from_config
    from fastapi.responses import RedirectResponse
    
    # Handle errors from provider
    if error:
        log.error(f"OIDC error: {error} - {error_description}")
        return RedirectResponse(url=f"/?error={error}", status_code=302)
    
    if not code or not state:
        return RedirectResponse(url="/?error=missing_params", status_code=302)
    
    # Get OIDC config
    row = db.execute(text("SELECT * FROM oidc_config WHERE is_enabled = 1 LIMIT 1")).fetchone()
    if not row:
        return RedirectResponse(url="/?error=oidc_not_configured", status_code=302)
    
    config = dict(row._mapping)
    
    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/auth/oidc/callback"
    
    handler = create_handler_from_config(config, redirect_uri)
    
    # Validate state
    if not handler.validate_state(state):
        return RedirectResponse(url="/?error=invalid_state", status_code=302)
    
    try:
        # Exchange code for tokens
        tokens = await handler.exchange_code(code)

        # Parse ID token to get user info
        id_token_claims = handler.parse_id_token(tokens["id_token"])

        # Also get user info for more details
        user_info = await handler.get_user_info(tokens["access_token"])
        
        # Extract user details
        oidc_subject = id_token_claims.get("sub") or id_token_claims.get("oid")
        email = user_info.get("mail") or user_info.get("userPrincipalName") or id_token_claims.get("email")
        display_name = user_info.get("displayName") or id_token_claims.get("name") or email
        
        if not oidc_subject or not email:
            log.error(f"Missing required claims: sub={oidc_subject}, email={email}")
            return RedirectResponse(url="/?error=missing_claims", status_code=302)
        
        # Find or create user
        oidc_provider = config.get("display_name", "oidc").lower().replace(" ", "_")
        existing = db.execute(
            text("SELECT * FROM users WHERE oidc_subject = :sub AND oidc_provider = :provider"),
            {"sub": oidc_subject, "provider": oidc_provider}
        ).fetchone()
        
        if existing:
            # Update last login
            user_id = existing[0]
            db.execute(
                text("UPDATE users SET last_login = :now, email = :email WHERE id = :id"),
                {"now": datetime.utcnow().isoformat(), "email": email, "id": user_id}
            )
            db.commit()
            user_role = existing._mapping.get("role", "operator")
        elif config.get("auto_create_users", True):
            # Create new user
            username = email.split("@")[0]  # Use email prefix as username
            default_role = config.get("default_role", "operator")
            
            # Ensure unique username
            base_username = username
            counter = 1
            while db.execute(text("SELECT id FROM users WHERE username = :u"), {"u": username}).fetchone():
                username = f"{base_username}{counter}"
                counter += 1
            
            db.execute(
                text("""
                    INSERT INTO users (username, email, password_hash, role, oidc_subject, oidc_provider, last_login)
                    VALUES (:username, :email, '', :role, :sub, :provider, :now)
                """),
                {
                    "username": username,
                    "email": email,
                    "role": default_role,
                    "sub": oidc_subject,
                    "provider": oidc_provider,
                    "now": datetime.utcnow().isoformat(),
                }
            )
            db.commit()
            
            user_id = db.execute(text("SELECT last_insert_rowid()")).fetchone()[0]
            user_role = default_role
            log.info(f"Created OIDC user: {username} ({email})")
        else:
            log.warning(f"OIDC user not found and auto-create disabled: {email}")
            return RedirectResponse(url="/?error=user_not_found", status_code=302)
        
        # Generate JWT — use the same secret/function as normal login
        access_token = create_access_token(
            data={
                "sub": existing._mapping.get("username") if existing else username,
                "role": user_role,
            }
        )
        
        # Redirect to frontend with token
        # Frontend will store this and complete login
        return RedirectResponse(
            url=f"/?token={access_token}",
            status_code=302
        )
        
    except Exception as e:
        log.error(f"OIDC callback error: {e}", exc_info=True)
        return RedirectResponse(url=f"/?error=auth_failed", status_code=302)


@app.get("/api/admin/oidc", tags=["Admin"])
async def get_oidc_config(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Get full OIDC configuration (admin only)."""
    row = db.execute(text("SELECT * FROM oidc_config LIMIT 1")).fetchone()
    if not row:
        return {"configured": False}
    
    config = dict(row._mapping)
    # Don't return the encrypted secret
    if "client_secret_encrypted" in config:
        config["has_client_secret"] = bool(config["client_secret_encrypted"])
        del config["client_secret_encrypted"]
    
    return config


@app.put("/api/admin/oidc", tags=["Admin"])
async def update_oidc_config(
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update OIDC configuration (admin only)."""
    data = await request.json()
    
    # Encrypt client secret if provided
    client_secret = data.get("client_secret")
    if client_secret:
        from crypto import encrypt
        data["client_secret_encrypted"] = encrypt(client_secret)
        del data["client_secret"]
    
    # Build update query
    allowed_fields = [
        "display_name", "client_id", "client_secret_encrypted", "tenant_id",
        "discovery_url", "scopes", "auto_create_users", "default_role", "is_enabled"
    ]
    
    updates = []
    params = {}
    for field in allowed_fields:
        if field in data:
            updates.append(f"{field} = :{field}")
            params[field] = data[field]
    
    if updates:
        updates.append("updated_at = datetime('now')")
        query = f"UPDATE oidc_config SET {', '.join(updates)} WHERE id = 1"
        db.execute(text(query), params)
        db.commit()
    
    return {"success": True}



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
    # Check license user limit
    current_count = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    check_user_limit(current_count)

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
        pw_valid, pw_msg = _validate_password(updates['password'])
        if not pw_valid:
            raise HTTPException(status_code=400, detail=pw_msg)
        updates['password_hash'] = hash_password(updates.pop('password'))
    else:
        updates.pop('password', None)
    
    # SB-6: Whitelist allowed columns to prevent SQL injection via column names
    ALLOWED_USER_FIELDS = {"username", "email", "role", "is_active", "password_hash"}
    updates = {k: v for k, v in updates.items() if k in ALLOWED_USER_FIELDS}
    
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

GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/app/go2rtc/go2rtc.yaml")

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
    import re
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
    printers = db.query(Printer).filter(Printer.is_active == True, Printer.camera_enabled == True).all()
    streams = {}
    for p in printers:
        url = get_camera_url(p)
        if url:
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
        print(f"[go2rtc] WebRTC ICE candidate: {lan_ip}:8555")
    config = {
        "api": {"listen": "0.0.0.0:1984"},
        "webrtc": webrtc_config,
        "streams": streams
    }
    with open(GO2RTC_CONFIG, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    # Restart go2rtc to pick up config changes
    try:
        import subprocess
        subprocess.run(["supervisorctl", "restart", "go2rtc"], capture_output=True, timeout=5)
    except Exception:
        pass

# Camera endpoints


# =============================================================================
# Printer Control (Emergency Stop, Pause, Resume)
# =============================================================================


def sync_go2rtc_config_standalone():
    """Regenerate go2rtc config (callable without a DB session)."""
    db = SessionLocal()
    try:
        sync_go2rtc_config(db)
    finally:
        db.close()

# Sync go2rtc on startup
try:
    sync_go2rtc_config_standalone()
except Exception:
    pass

@app.post("/api/printers/{printer_id}/stop", tags=["Printers"])
async def stop_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Emergency stop - cancel current print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    success = False
    
    if printer.api_type == "moonraker":
        from moonraker_adapter import MoonrakerPrinter
        adapter = MoonrakerPrinter(printer.api_host)
        success = adapter.cancel_print()
    else:
        # Bambu printers
        from bambu_adapter import BambuPrinter
        from crypto import decrypt
        try:
            creds = decrypt(printer.api_key)
            serial, access_code = creds.split("|", 1)
            adapter = BambuPrinter(printer.api_host, serial, access_code)
            if adapter.connect():
                success = adapter.stop_print()
                adapter.disconnect()
        except Exception as e:
            log.error(f"Stop failed for printer {printer_id}: {e}")
    
    if success:
        # Update printer state
        db.execute(text("UPDATE printers SET gcode_state = 'IDLE', print_stage = 'Idle' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print stopped"}
    else:
        raise HTTPException(status_code=500, detail="Failed to stop print")


@app.post("/api/printers/{printer_id}/pause", tags=["Printers"])
async def pause_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Pause current print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    success = False
    
    if printer.api_type == "moonraker":
        from moonraker_adapter import MoonrakerPrinter
        adapter = MoonrakerPrinter(printer.api_host)
        success = adapter.pause_print()
    else:
        from bambu_adapter import BambuPrinter
        from crypto import decrypt
        try:
            creds = decrypt(printer.api_key)
            serial, access_code = creds.split("|", 1)
            adapter = BambuPrinter(printer.api_host, serial, access_code)
            if adapter.connect():
                success = adapter.pause_print()
                adapter.disconnect()
        except Exception as e:
            log.error(f"Pause failed for printer {printer_id}: {e}")
    
    if success:
        db.execute(text("UPDATE printers SET gcode_state = 'PAUSED' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print paused"}
    else:
        raise HTTPException(status_code=500, detail="Failed to pause print")


@app.post("/api/printers/{printer_id}/resume", tags=["Printers"])
async def resume_printer(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Resume paused print."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    success = False
    
    if printer.api_type == "moonraker":
        from moonraker_adapter import MoonrakerPrinter
        adapter = MoonrakerPrinter(printer.api_host)
        success = adapter.resume_print()
    else:
        from bambu_adapter import BambuPrinter
        from crypto import decrypt
        try:
            creds = decrypt(printer.api_key)
            serial, access_code = creds.split("|", 1)
            adapter = BambuPrinter(printer.api_host, serial, access_code)
            if adapter.connect():
                success = adapter.resume_print()
                adapter.disconnect()
        except Exception as e:
            log.error(f"Resume failed for printer {printer_id}: {e}")
    
    if success:
        db.execute(text("UPDATE printers SET gcode_state = 'RUNNING' WHERE id = :id"), {"id": printer_id})
        db.commit()
        return {"success": True, "message": "Print resumed"}
    else:
        raise HTTPException(status_code=500, detail="Failed to resume print")



# =============================================================================
# Webhooks (Discord/Slack)
# =============================================================================

@app.get("/api/webhooks", tags=["Webhooks"])
async def list_webhooks(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """List all webhooks."""
    rows = db.execute(text("SELECT * FROM webhooks ORDER BY name")).fetchall()
    return [dict(r._mapping) for r in rows]


@app.post("/api/webhooks", tags=["Webhooks"])
async def create_webhook(
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Create a new webhook."""
    data = await request.json()
    
    name = data.get("name", "Webhook")
    url = data.get("url")
    webhook_type = data.get("webhook_type", "discord")
    alert_types = data.get("alert_types")  # JSON array or comma-separated
    
    if not url:
        raise HTTPException(status_code=400, detail="URL is required")
    
    # Store alert_types as JSON
    if isinstance(alert_types, list):
        alert_types = json.dumps(alert_types)
    
    db.execute(text("""
        INSERT INTO webhooks (name, url, webhook_type, alert_types)
        VALUES (:name, :url, :type, :alerts)
    """), {"name": name, "url": url, "type": webhook_type, "alerts": alert_types})
    db.commit()
    
    return {"success": True, "message": "Webhook created"}


@app.patch("/api/webhooks/{webhook_id}", tags=["Webhooks"])
async def update_webhook(
    webhook_id: int,
    request: Request,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update a webhook."""
    data = await request.json()
    
    updates = []
    params = {"id": webhook_id}
    
    for field in ["name", "url", "webhook_type", "is_enabled", "alert_types"]:
        if field in data:
            value = data[field]
            if field == "alert_types" and isinstance(value, list):
                value = json.dumps(value)
            updates.append(f"{field} = :{field}")
            params[field] = value
    
    if updates:
        updates.append("updated_at = datetime('now')")
        query = f"UPDATE webhooks SET {', '.join(updates)} WHERE id = :id"
        db.execute(text(query), params)
        db.commit()
    
    return {"success": True}


@app.delete("/api/webhooks/{webhook_id}", tags=["Webhooks"])
async def delete_webhook(
    webhook_id: int,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Delete a webhook."""
    db.execute(text("DELETE FROM webhooks WHERE id = :id"), {"id": webhook_id})
    db.commit()
    return {"success": True}


@app.post("/api/webhooks/{webhook_id}/test", tags=["Webhooks"])
async def test_webhook(
    webhook_id: int,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Send a test message to webhook."""
    row = db.execute(text("SELECT * FROM webhooks WHERE id = :id"), {"id": webhook_id}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Webhook not found")
    
    webhook = dict(row._mapping)
    
    try:
        import httpx
        
        wtype = webhook["webhook_type"]
        
        if wtype == "discord":
            payload = {
                "embeds": [{
                    "title": "🖨️ O.D.I.N. Test",
                    "description": "Webhook connection successful!",
                    "color": 0xd97706,
                    "footer": {"text": "O.D.I.N."}
                }]
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)
        
        elif wtype == "slack":
            payload = {
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": "🖨️ O.D.I.N. Test"}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": "Webhook connection successful!"}}
                ]
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)
        
        elif wtype == "ntfy":
            # ntfy: URL is the topic endpoint (e.g., https://ntfy.sh/my-printfarm)
            resp = httpx.post(
                webhook["url"],
                content="Webhook connection successful!",
                headers={
                    "Title": "O.D.I.N. Test",
                    "Priority": "default",
                    "Tags": "white_check_mark,printer",
                },
                timeout=10
            )
        
        elif wtype == "telegram":
            # Telegram: URL format is https://api.telegram.org/bot<TOKEN>/sendMessage
            # User stores just the bot token + chat_id in the URL as:
            #   bot_token|chat_id  (we parse and construct the API call)
            # OR they can store the full URL with chat_id as a query param
            url = webhook["url"]
            if "|" in url:
                # Format: bot_token|chat_id
                bot_token, chat_id = url.split("|", 1)
                api_url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
            else:
                # Assume full URL, extract chat_id from stored data
                # Fallback: treat URL as bot token, chat_id from name field
                api_url = f"https://api.telegram.org/bot{url.strip()}/sendMessage"
                chat_id = webhook.get("name", "").split("|")[-1] if "|" in webhook.get("name", "") else ""
            
            resp = httpx.post(
                api_url,
                json={
                    "chat_id": chat_id.strip(),
                    "text": "🖨️ *O.D.I.N. Test*\nWebhook connection successful!",
                    "parse_mode": "Markdown"
                },
                timeout=10
            )
        
        else:
            # Generic webhook — POST JSON
            payload = {
                "event": "test",
                "source": "odin",
                "message": "Webhook connection successful!"
            }
            resp = httpx.post(webhook["url"], json=payload, timeout=10)
        
        if resp.status_code in (200, 204):
            return {"success": True, "message": "Test message sent"}
        else:
            return {"success": False, "message": f"Failed: HTTP {resp.status_code} - {resp.text[:200]}"}
    
    except Exception as e:
        return {"success": False, "message": str(e)}



# ============================================================
# Prometheus Metrics (v0.18.0)
# ============================================================

@app.get("/metrics", tags=["Monitoring"])
async def prometheus_metrics(db: Session = Depends(get_db)):
    """Prometheus-compatible metrics endpoint. No auth required."""
    lines = []
    
    # --- Fleet metrics ---
    printers_all = db.execute(text("SELECT * FROM printers WHERE is_active = 1")).fetchall()
    total_printers = len(printers_all)
    online_count = 0
    printing_count = 0
    idle_count = 0
    error_count = 0
    
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    
    for p in printers_all:
        pm = dict(p._mapping)
        last_seen = pm.get("last_seen")
        is_online = False
        if last_seen:
            try:
                ls = datetime.fromisoformat(str(last_seen).replace("Z", ""))
                is_online = (now - ls).total_seconds() < 90
            except Exception:
                pass
        
        if is_online:
            online_count += 1
            gcode_state = pm.get("gcode_state", "")
            if gcode_state in ("RUNNING", "PREPARE"):
                printing_count += 1
            elif gcode_state in ("FAILED", "UNKNOWN"):
                error_count += 1
            else:
                idle_count += 1
    
    lines.append("# HELP odin_printers_total Total registered printers")
    lines.append("# TYPE odin_printers_total gauge")
    lines.append(f"odin_printers_total {total_printers}")
    
    lines.append("# HELP odin_printers_online Online printers (seen in last 90s)")
    lines.append("# TYPE odin_printers_online gauge")
    lines.append(f"odin_printers_online {online_count}")
    
    lines.append("# HELP odin_printers_printing Currently printing")
    lines.append("# TYPE odin_printers_printing gauge")
    lines.append(f"odin_printers_printing {printing_count}")
    
    lines.append("# HELP odin_printers_idle Online but idle")
    lines.append("# TYPE odin_printers_idle gauge")
    lines.append(f"odin_printers_idle {idle_count}")
    
    lines.append("# HELP odin_printers_error Online with errors")
    lines.append("# TYPE odin_printers_error gauge")
    lines.append(f"odin_printers_error {error_count}")
    
    # --- Per-printer telemetry ---
    lines.append("# HELP odin_printer_nozzle_temp_celsius Current nozzle temperature")
    lines.append("# TYPE odin_printer_nozzle_temp_celsius gauge")
    lines.append("# HELP odin_printer_bed_temp_celsius Current bed temperature")
    lines.append("# TYPE odin_printer_bed_temp_celsius gauge")
    lines.append("# HELP odin_printer_progress Print progress 0-100")
    lines.append("# TYPE odin_printer_progress gauge")
    lines.append("# HELP odin_printer_print_hours_total Lifetime print hours")
    lines.append("# TYPE odin_printer_print_hours_total counter")
    lines.append("# HELP odin_printer_print_count_total Lifetime print count")
    lines.append("# TYPE odin_printer_print_count_total counter")
    
    for p in printers_all:
        pm = dict(p._mapping)
        name = pm.get("nickname") or pm.get("name", f"printer_{pm['id']}")
        pid = pm["id"]
        labels = f'printer="{name}",printer_id="{pid}"'
        
        nozzle = pm.get("nozzle_temp")
        bed = pm.get("bed_temp")
        progress = pm.get("print_progress")
        total_hours = pm.get("total_print_hours", 0) or 0
        total_prints = pm.get("total_print_count", 0) or 0
        
        if nozzle is not None:
            lines.append(f"odin_printer_nozzle_temp_celsius{{{labels}}} {nozzle}")
        if bed is not None:
            lines.append(f"odin_printer_bed_temp_celsius{{{labels}}} {bed}")
        if progress is not None:
            lines.append(f"odin_printer_progress{{{labels}}} {progress}")
        lines.append(f"odin_printer_print_hours_total{{{labels}}} {total_hours}")
        lines.append(f"odin_printer_print_count_total{{{labels}}} {total_prints}")
    
    # --- Job metrics ---
    job_counts = db.execute(text("""
        SELECT status, COUNT(*) as cnt FROM jobs GROUP BY status
    """)).fetchall()
    
    lines.append("# HELP odin_jobs_by_status Number of jobs by status")
    lines.append("# TYPE odin_jobs_by_status gauge")
    for row in job_counts:
        r = dict(row._mapping)
        lines.append(f'odin_jobs_by_status{{status="{r["status"]}"}} {r["cnt"]}')
    
    # Queue depth (pending + scheduled)
    queue = db.execute(text("""
        SELECT COUNT(*) as cnt FROM jobs WHERE status IN ('pending', 'scheduled', 'submitted')
    """)).fetchone()
    lines.append("# HELP odin_queue_depth Jobs waiting to print")
    lines.append("# TYPE odin_queue_depth gauge")
    lines.append(f"odin_queue_depth {dict(queue._mapping)['cnt']}")
    
    # --- Spool metrics ---
    spool_data = db.execute(text("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN remaining_weight_g < 100 THEN 1 ELSE 0 END) as low
        FROM spools WHERE remaining_weight_g IS NOT NULL
    """)).fetchone()
    sd = dict(spool_data._mapping)
    
    lines.append("# HELP odin_spools_total Total tracked spools")
    lines.append("# TYPE odin_spools_total gauge")
    lines.append(f"odin_spools_total {sd['total'] or 0}")
    
    lines.append("# HELP odin_spools_low Spools under 100g remaining")
    lines.append("# TYPE odin_spools_low gauge")
    lines.append(f"odin_spools_low {sd['low'] or 0}")
    
    # --- Order metrics ---
    order_data = db.execute(text("""
        SELECT status, COUNT(*) as cnt FROM orders GROUP BY status
    """)).fetchall()
    
    lines.append("# HELP odin_orders_by_status Orders by status")
    lines.append("# TYPE odin_orders_by_status gauge")
    for row in order_data:
        r = dict(row._mapping)
        lines.append(f'odin_orders_by_status{{status="{r["status"]}"}} {r["cnt"]}')
    
    # --- Alert metrics ---
    unread = db.execute(text("SELECT COUNT(*) as cnt FROM alerts WHERE is_read = 0")).fetchone()
    lines.append("# HELP odin_alerts_unread Unread alerts")
    lines.append("# TYPE odin_alerts_unread gauge")
    lines.append(f"odin_alerts_unread {dict(unread._mapping)['cnt']}")
    
    from starlette.responses import Response
    return Response(
        content="\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4; charset=utf-8"
    )






# ============== HMS Code Lookup ==============

@app.get("/api/hms-codes/{code}", tags=["Monitoring"])
async def lookup_hms(code: str):
    """Look up human-readable description for a Bambu HMS error code."""
    try:
        from hms_codes import lookup_hms_code, get_code_count
        return {
            "code": code,
            "message": lookup_hms_code(code),
            "total_codes": get_code_count()
        }
    except Exception as e:
        raise HTTPException(500, str(e))



# ============== Job Queue Reorder ==============

class JobReorderRequest(PydanticBaseModel):
    job_ids: list[int]  # Ordered list of job IDs in desired queue position

@app.patch("/api/jobs/reorder", tags=["Jobs"])
async def reorder_jobs(req: JobReorderRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """
    Reorder job queue. Accepts ordered list of job IDs.
    Sets queue_position on each job based on array index.
    Only reorders pending/scheduled jobs.
    """
    for position, job_id in enumerate(req.job_ids):
        db.execute(
            text("UPDATE jobs SET queue_position = :pos WHERE id = :id AND status IN ('pending', 'scheduled')"),
            {"pos": position, "id": job_id}
        )
    db.commit()
    return {"reordered": len(req.job_ids)}




# ============== Quiet Hours Configuration ==============

@app.get("/api/config/quiet-hours")
async def get_quiet_hours_config(db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Get quiet hours settings."""
    keys = ["quiet_hours_enabled", "quiet_hours_start", "quiet_hours_end", "quiet_hours_digest"]
    config = {}
    defaults = {"enabled": False, "start": "22:00", "end": "07:00", "digest": True}

    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("quiet_hours_", "")
        if row:
            val = row[0]
            if short_key in ("enabled", "digest"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            else:
                config[short_key] = val
        else:
            config[short_key] = defaults.get(short_key, "")
    return config


@app.put("/api/config/quiet-hours")
async def update_quiet_hours_config(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update quiet hours settings. Admin only."""

    body = await request.json()

    for short_key, value in body.items():
        db_key = f"quiet_hours_{short_key}"
        str_val = str(value).lower() if isinstance(value, bool) else str(value)

        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"),
                       {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"),
                       {"k": db_key, "v": str_val})

    db.commit()

    # Invalidate cache
    try:
        from quiet_hours import invalidate_cache
        invalidate_cache()
    except Exception:
        pass

    return {"status": "ok"}

# ============== MQTT Republish Configuration ==============
try:
    import mqtt_republish
except ImportError:
    mqtt_republish = None

@app.get("/api/config/mqtt-republish")
async def get_mqtt_republish_config(db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Get MQTT republish settings."""
    keys = [
        "mqtt_republish_enabled", "mqtt_republish_host", "mqtt_republish_port",
        "mqtt_republish_username", "mqtt_republish_password",
        "mqtt_republish_topic_prefix", "mqtt_republish_use_tls",
    ]
    config = {}
    for key in keys:
        row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": key}).fetchone()
        short_key = key.replace("mqtt_republish_", "")
        if row:
            val = row[0]
            if short_key in ("enabled", "use_tls"):
                config[short_key] = val.lower() in ("true", "1", "yes")
            elif short_key == "port":
                config[short_key] = int(val) if val else 1883
            elif short_key == "password":
                config[short_key] = "••••••••" if val else ""
            else:
                config[short_key] = val
        else:
            defaults = {"enabled": False, "host": "", "port": 1883, "username": "",
                        "password": "", "topic_prefix": "odin", "use_tls": False}
            config[short_key] = defaults.get(short_key, "")
    return config


@app.put("/api/config/mqtt-republish")
async def update_mqtt_republish_config(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Update MQTT republish settings. Admin only."""

    body = await request.json()

    for short_key, value in body.items():
        db_key = f"mqtt_republish_{short_key}"
        # Don't overwrite password if it's the masked value
        if short_key == "password" and value == "••••••••":
            continue

        str_val = str(value).lower() if isinstance(value, bool) else str(value)

        existing = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": db_key}).fetchone()
        if existing:
            db.execute(text("UPDATE system_config SET value = :v WHERE key = :k"),
                       {"v": str_val, "k": db_key})
        else:
            db.execute(text("INSERT INTO system_config (key, value) VALUES (:k, :v)"),
                       {"k": db_key, "v": str_val})

    db.commit()

    # Invalidate the republish module's cached config
    if mqtt_republish:
        mqtt_republish.invalidate_cache()

    return {"status": "ok"}


@app.post("/api/config/mqtt-republish/test")
async def test_mqtt_republish(request: Request, current_user: dict = Depends(require_role("admin"))):
    """Test connection to external MQTT broker."""
    # Role check handled by require_role("admin") dependency

    if not mqtt_republish:
        raise HTTPException(status_code=503, detail="MQTT republish module not available")

    body = await request.json()
    result = mqtt_republish.test_connection(
        host=body.get("host", ""),
        port=int(body.get("port", 1883)),
        username=body.get("username", ""),
        password=body.get("password", ""),
        use_tls=body.get("use_tls", False),
        topic_prefix=body.get("topic_prefix", "odin"),
    )
    return result


# ============== WebSocket Real-Time Updates ==============

class ConnectionManager:
    """Manages active WebSocket connections."""
    
    def __init__(self):
        self.active: list[WebSocket] = []
    
    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)
    
    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)
    
    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

ws_manager = ConnectionManager()

async def ws_broadcaster():
    """Background task: read events from hub file, broadcast to WebSocket clients."""
    from ws_hub import read_events_since
    import time
    
    last_ts = time.time()
    
    while True:
        await asyncio.sleep(1)  # Check every 1 second
        
        if not ws_manager.active:
            # No clients connected, just advance timestamp
            last_ts = time.time()
            continue
        
        events, last_ts = read_events_since(last_ts)
        
        for evt in events:
            await ws_manager.broadcast(evt)


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """
    WebSocket endpoint for real-time printer telemetry and job updates.
    
    Pushes events:
    - printer_telemetry: {printer_id, bed_temp, nozzle_temp, state, progress, ...}
    - job_update: {printer_id, job_name, status, progress, layer, ...}  
    - alert_new: {count} (new unread alert count)
    """
    await ws_manager.connect(ws)
    try:
        while True:
            # Keep connection alive, handle client messages (ping/pong)
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30)
                # Client can send "ping" to keep alive
                if data == "ping":
                    await ws.send_text("pong")
            except asyncio.TimeoutError:
                # Send server-side ping to keep connection alive
                try:
                    await ws.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_manager.disconnect(ws)

# ============================================================
# Webhook Alert Dispatch (v0.18.0 — ntfy + telegram support)
# ============================================================

def _dispatch_to_webhooks(db, alert_type_value: str, title: str, message: str, severity: str):
    """Send alert to all matching enabled webhooks."""
    import httpx
    import threading
    
    rows = db.execute(text("SELECT * FROM webhooks WHERE is_enabled = 1")).fetchall()
    
    for row in rows:
        wh = dict(row._mapping)
        
        # Check if this webhook subscribes to this alert type
        alert_types = wh.get("alert_types")
        if alert_types:
            try:
                types_list = json.loads(alert_types) if isinstance(alert_types, str) else alert_types
                if alert_type_value not in types_list and "all" not in types_list:
                    continue
            except (json.JSONDecodeError, TypeError):
                pass
        
        wtype = wh["webhook_type"]
        url = wh["url"]
        
        severity_colors = {"critical": 0xef4444, "warning": 0xf59e0b, "info": 0x3b82f6}
        severity_emoji = {"critical": "🔴", "warning": "🟡", "info": "🔵"}
        emoji = severity_emoji.get(severity, "🔵")
        color = severity_colors.get(severity, 0x3b82f6)
        
        def _send(wtype=wtype, url=url):
            try:
                if wtype == "discord":
                    httpx.post(url, json={
                        "embeds": [{
                            "title": f"{emoji} {title}",
                            "description": message or "",
                            "color": color,
                            "footer": {"text": "O.D.I.N."}
                        }]
                    }, timeout=10)
                
                elif wtype == "slack":
                    httpx.post(url, json={
                        "blocks": [
                            {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} {title}"}},
                            {"type": "section", "text": {"type": "mrkdwn", "text": message or ""}}
                        ]
                    }, timeout=10)
                
                elif wtype == "ntfy":
                    priority_map = {"critical": "urgent", "warning": "high", "info": "default"}
                    httpx.post(url, content=message or title, headers={
                        "Title": title,
                        "Priority": priority_map.get(severity, "default"),
                        "Tags": "printer",
                    }, timeout=10)
                
                elif wtype == "telegram":
                    if "|" in url:
                        bot_token, chat_id = url.split("|", 1)
                        api_url = f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage"
                    else:
                        api_url = f"https://api.telegram.org/bot{url.strip()}/sendMessage"
                        chat_id = ""
                    
                    if chat_id:
                        httpx.post(api_url, json={
                            "chat_id": chat_id.strip(),
                            "text": f"{emoji} *{title}*\n{message or ''}",
                            "parse_mode": "Markdown"
                        }, timeout=10)
                
                else:
                    httpx.post(url, json={
                        "event": alert_type_value,
                        "title": title,
                        "message": message or "",
                        "severity": severity
                    }, timeout=10)
            
            except Exception as e:
                log.error(f"Webhook dispatch failed ({wtype}): {e}")
        
        thread = threading.Thread(target=_send, daemon=True)
        thread.start()



# ============================================================
# Failure Logging (v0.18.0)
# ============================================================

FAILURE_REASONS = [
    {"value": "spaghetti", "label": "Spaghetti / Detached"},
    {"value": "adhesion", "label": "Bed Adhesion Failure"},
    {"value": "clog", "label": "Nozzle Clog"},
    {"value": "layer_shift", "label": "Layer Shift"},
    {"value": "stringing", "label": "Excessive Stringing"},
    {"value": "warping", "label": "Warping / Curling"},
    {"value": "filament_runout", "label": "Filament Runout"},
    {"value": "filament_tangle", "label": "Filament Tangle"},
    {"value": "power_loss", "label": "Power Loss"},
    {"value": "firmware_error", "label": "Firmware / HMS Error"},
    {"value": "user_cancelled", "label": "User Cancelled"},
    {"value": "other", "label": "Other"},
]


@app.get("/api/failure-reasons", tags=["Jobs"])
async def get_failure_reasons():
    """List available failure reason categories."""
    return FAILURE_REASONS


@app.patch("/api/jobs/{job_id}/failure", tags=["Jobs"])
async def update_job_failure(
    job_id: int,
    request: Request,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Add or update failure reason and notes on a failed job."""
    data = await request.json()
    
    job = db.execute(text("SELECT id, status FROM jobs WHERE id = :id"), {"id": job_id}).fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    job_dict = dict(job._mapping)
    if job_dict["status"] != "failed":
        raise HTTPException(status_code=400, detail="Can only add failure info to failed jobs")
    
    fail_reason = data.get("fail_reason")
    fail_notes = data.get("fail_notes")
    
    updates = []
    params = {"id": job_id}
    
    if fail_reason is not None:
        updates.append("fail_reason = :reason")
        params["reason"] = fail_reason
    
    if fail_notes is not None:
        updates.append("fail_notes = :notes")
        params["notes"] = fail_notes
    
    if updates:
        updates.append("updated_at = datetime('now')")
        db.execute(text(f"UPDATE jobs SET {', '.join(updates)} WHERE id = :id"), params)
        db.commit()
    
    return {"success": True, "message": "Failure info updated"}

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
    
    printers = db.query(Printer).filter(Printer.is_active == True, Printer.camera_enabled == True).all()
    cameras = []
    for p in printers:
        if p.id in active_streams:
            cameras.append({"id": p.id, "name": p.name, "has_camera": True, "display_order": p.display_order or 0, "camera_enabled": bool(p.camera_enabled)})
    return sorted(cameras, key=lambda x: x.get("display_order", 0))



@app.patch("/api/cameras/{printer_id}/toggle", tags=["Cameras"])
def toggle_camera(printer_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Toggle camera on/off for a printer."""
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    
    # Toggle the camera_enabled flag
    new_state = not (printer.camera_enabled if printer.camera_enabled is not None else True)
    db.execute(text("UPDATE printers SET camera_enabled = :enabled WHERE id = :id"),
               {"enabled": new_state, "id": printer_id})
    db.commit()
    
    return {"id": printer_id, "camera_enabled": new_state}

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
async def update_branding(data: dict, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
async def upload_logo(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
async def upload_favicon(file: UploadFile = File(...), current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
async def remove_logo(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
def create_backup(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Create a database backup using SQLite online backup API."""
    import sqlite3 as sqlite3_mod
    
    backup_dir = Path(__file__).parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    
    # Resolve DB file path from engine URL
    engine_url = str(db.get_bind().url)
    if "///" in engine_url:
        db_path = engine_url.split("///", 1)[1]
    else:
        db_path = "odin.db"
    if not os.path.isabs(db_path):
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), db_path)
    
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    backup_name = f"odin_backup_{timestamp}.db"
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
def list_backups(current_user: dict = Depends(require_role("admin"))):
    """List all database backups."""
    backup_dir = Path(__file__).parent / "backups"
    if not backup_dir.exists():
        return []
    
    backups = []
    for f in sorted(backup_dir.glob("odin_backup_*.db"), reverse=True):
        stat = f.stat()
        backups.append({
            "filename": f.name,
            "size_bytes": stat.st_size,
            "size_mb": round(stat.st_size / 1048576, 2),
            "created_at": datetime.utcfromtimestamp(stat.st_mtime).isoformat(),
        })
    return backups


@app.get("/api/backups/{filename}", tags=["System"])
def download_backup(filename: str, current_user: dict = Depends(require_role("admin"))):
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
def delete_backup(filename: str, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Delete a database backup."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    
    backup_dir = Path(__file__).parent / "backups"
    backup_path = backup_dir / filename
    
    if not backup_path.exists():
        raise HTTPException(status_code=404, detail="Backup not found")
    
    backup_path.unlink()
    log_audit(db, "backup_deleted", "system", details={"filename": filename})











# ============== Language / i18n ==============

@app.get("/api/settings/language", tags=["Settings"])
async def get_language(db: Session = Depends(get_db)):
    """Get current interface language."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'language'")).fetchone()
    return {"language": result[0] if result else "en"}


@app.put("/api/settings/language", tags=["Settings"])
async def set_language(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set interface language."""
    data = await request.json()
    lang = data.get("language", "en")
    supported = ["en", "de", "ja", "es"]
    if lang not in supported:
        raise HTTPException(400, f"Unsupported language. Choose from: {', '.join(supported)}")
    db.execute(text(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('language', :lang)"
    ), {"lang": lang})
    db.commit()
    return {"language": lang}


# ============== AMS Environmental Monitoring ==============

@app.get("/api/printers/{printer_id}/ams/environment", tags=["AMS"])
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
        "hours_ago": f"-{hours} hours"
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
            "time": row[3]
        })
    
    return {
        "printer_id": printer_id,
        "hours": hours,
        "units": units
    }


@app.get("/api/printers/{printer_id}/ams/current", tags=["AMS"])
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
            "recorded_at": row[3]
        })
    
    return {"printer_id": printer_id, "units": units}


# ============== Smart Plug Control ==============

@app.get("/api/printers/{printer_id}/plug", tags=["Smart Plug"])
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


@app.put("/api/printers/{printer_id}/plug", tags=["Smart Plug"])
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


@app.delete("/api/printers/{printer_id}/plug", tags=["Smart Plug"])
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


@app.post("/api/printers/{printer_id}/plug/on", tags=["Smart Plug"])
async def plug_power_on(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Turn on a printer's smart plug."""
    result = smart_plug.power_on(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@app.post("/api/printers/{printer_id}/plug/off", tags=["Smart Plug"])
async def plug_power_off(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Turn off a printer's smart plug."""
    result = smart_plug.power_off(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@app.post("/api/printers/{printer_id}/plug/toggle", tags=["Smart Plug"])
async def plug_power_toggle(printer_id: int, current_user: dict = Depends(require_role("operator"))):
    """Toggle a printer's smart plug."""
    result = smart_plug.power_toggle(printer_id)
    if result is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": result}


@app.get("/api/printers/{printer_id}/plug/energy", tags=["Smart Plug"])
async def plug_energy(printer_id: int):
    """Get current energy data from smart plug."""
    data = smart_plug.get_energy(printer_id)
    if data is None:
        raise HTTPException(400, "No energy data available")
    return data


@app.get("/api/printers/{printer_id}/plug/state", tags=["Smart Plug"])
async def plug_state(printer_id: int):
    """Query current power state from smart plug."""
    state = smart_plug.get_power_state(printer_id)
    if state is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": state}


@app.get("/api/settings/energy-rate", tags=["Smart Plug"])
async def get_energy_rate(db: Session = Depends(get_db)):
    """Get energy cost per kWh."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")).fetchone()
    return {"energy_cost_per_kwh": float(result[0]) if result else 0.12}


@app.put("/api/settings/energy-rate", tags=["Smart Plug"])
async def set_energy_rate(request: Request, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
    """Set energy cost per kWh."""
    data = await request.json()
    rate = data.get("energy_cost_per_kwh", 0.12)
    db.execute(text(
        "INSERT OR REPLACE INTO system_config (key, value) VALUES ('energy_cost_per_kwh', :rate)"
    ), {"rate": str(rate)})
    db.commit()
    return {"energy_cost_per_kwh": rate}


# ============== 3D Model Viewer ==============

@app.get("/api/print-files/{file_id}/mesh", tags=["3D Viewer"])
async def get_print_file_mesh(file_id: int, db: Session = Depends(get_db)):
    """Get mesh geometry data for 3D viewer from a print file."""
    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": file_id}).fetchone()
    
    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available for this file")
    
    import json as json_stdlib
    return json_stdlib.loads(result[0])


@app.get("/api/models/{model_id}/mesh", tags=["3D Viewer"])
async def get_model_mesh(model_id: int, db: Session = Depends(get_db)):
    """Get mesh geometry for a model (via its linked print_file)."""
    # Find print_file_id from model
    model = db.execute(text(
        "SELECT print_file_id FROM models WHERE id = :id"
    ), {"id": model_id}).fetchone()
    
    if not model or not model[0]:
        raise HTTPException(status_code=404, detail="Model has no linked print file")
    
    result = db.execute(text(
        "SELECT mesh_data FROM print_files WHERE id = :id"
    ), {"id": model[0]}).fetchone()
    
    if not result or not result[0]:
        raise HTTPException(status_code=404, detail="No mesh data available")
    
    import json as json_stdlib
    return json_stdlib.loads(result[0])

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
def create_maintenance_task(data: MaintenanceTaskCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def update_maintenance_task(task_id: int, data: MaintenanceTaskUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a maintenance task template."""
    task = db.query(MaintenanceTask).filter(MaintenanceTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(task, field, value)
    db.commit()
    return {"id": task.id, "message": "Task updated"}


@app.delete("/api/maintenance/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Maintenance"])
def delete_maintenance_task(task_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def create_maintenance_log(data: MaintenanceLogCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
def delete_maintenance_log(log_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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

    # Total print hours per printer (from care counters, updated by monitors)
    # Note: Previously calculated from jobs table, now using real-time counter
    hours_map = {p.id: float(p.total_print_hours or 0) for p in printers}

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
def seed_default_maintenance_tasks(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
def delete_model_variant(model_id: int, variant_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
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
    ],
    "education_reports": [
        "admin",
        "operator"
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
def update_permissions(data: RBACUpdateRequest, current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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
def reset_permissions(current_user: dict = Depends(require_role("admin")), db: Session = Depends(get_db)):
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


# ============== Pricing Config ==============

DEFAULT_PRICING_CONFIG = {
    "spool_cost": 25.0,
    "spool_weight": 1000.0,
    "hourly_rate": 15.0,
    "electricity_rate": 0.12,
    "printer_wattage": 100,
    "printer_cost": 300.0,
    "printer_lifespan": 5000,
    "packaging_cost": 0.45,
    "failure_rate": 7.0,
    "monthly_rent": 0.0,
    "parts_per_month": 100,
    "post_processing_min": 5,
    "packing_min": 5,
    "support_min": 5,
    "default_margin": 50.0,
    "other_costs": 0.0
}


def calculate_job_cost(db: Session, model_id: int = None, filament_grams: float = 0, print_hours: float = 1.0, material_type: str = "PLA"):
    """Calculate estimated cost and suggested price for a job.
    
    Returns tuple: (estimated_cost, suggested_price, margin_percent)
    """
    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG
    
    # Get model for defaults if provided
    model = None
    if model_id:
        model = db.query(Model).filter(Model.id == model_id).first()
        if model:
            filament_grams = filament_grams or model.total_filament_grams or 0
            print_hours = print_hours or model.build_time_hours or 1.0
            material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
    
    # Try to get per-material cost
    filament_entry = db.query(FilamentLibrary).filter(
        FilamentLibrary.material == material_type,
        FilamentLibrary.cost_per_gram.isnot(None)
    ).first()
    
    if filament_entry and filament_entry.cost_per_gram:
        cost_per_gram = filament_entry.cost_per_gram
    else:
        cost_per_gram = config["spool_cost"] / config["spool_weight"]
    
    # Calculate costs
    material_cost = filament_grams * cost_per_gram
    labor_hours = (config["post_processing_min"] + config["packing_min"] + config["support_min"]) / 60
    labor_cost = labor_hours * config["hourly_rate"]
    electricity_cost = (config["printer_wattage"] / 1000) * print_hours * config["electricity_rate"]
    depreciation_cost = (config["printer_cost"] / config["printer_lifespan"]) * print_hours
    packaging_cost = config["packaging_cost"]
    base_cost = material_cost + labor_cost + electricity_cost + depreciation_cost + packaging_cost + config["other_costs"]
    failure_cost = base_cost * (config["failure_rate"] / 100)
    overhead_cost = config["monthly_rent"] / config["parts_per_month"] if config["parts_per_month"] > 0 else 0
    
    subtotal = base_cost + failure_cost + overhead_cost
    
    margin = model.markup_percent if model and model.markup_percent else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)
    
    return (round(subtotal, 2), round(suggested_price, 2), margin)


@app.get("/api/pricing-config")
def get_pricing_config(db: Session = Depends(get_db)):
    """Get system pricing configuration."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    if not config:
        # Return defaults if not configured
        return DEFAULT_PRICING_CONFIG
    return config.value


@app.put("/api/pricing-config")
def update_pricing_config(
    config_data: dict,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update system pricing configuration."""
    
    # Merge with defaults to ensure all fields exist
    merged_config = {**DEFAULT_PRICING_CONFIG, **config_data}
    
    config = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    if config:
        config.value = merged_config
    else:
        config = SystemConfig(key="pricing_config", value=merged_config)
        db.add(config)
    
    db.commit()
    db.refresh(config)
    
    return config.value


@app.get("/api/models/{model_id}/cost")
def calculate_model_cost(
    model_id: int,
    db: Session = Depends(get_db)
):
    """Calculate cost breakdown for a model using system pricing config."""
    model = db.query(Model).filter(Model.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    # Get pricing config
    config_row = db.query(SystemConfig).filter(SystemConfig.key == "pricing_config").first()
    config = config_row.value if config_row else DEFAULT_PRICING_CONFIG
    
    # Calculate costs
    filament_grams = model.total_filament_grams or 0
    print_hours = model.build_time_hours or 1.0
    
    # Try to get per-material cost from FilamentLibrary
    material_type = model.default_filament_type.value if model.default_filament_type else "PLA"
    filament_entry = db.query(FilamentLibrary).filter(
        FilamentLibrary.material == material_type,
        FilamentLibrary.cost_per_gram.isnot(None)
    ).first()
    
    if filament_entry and filament_entry.cost_per_gram:
        cost_per_gram = filament_entry.cost_per_gram
        cost_source = f"per-material ({material_type})"
    else:
        cost_per_gram = config["spool_cost"] / config["spool_weight"]
        cost_source = "global default"
    
    material_cost = filament_grams * cost_per_gram
    
    labor_hours = (config["post_processing_min"] + config["packing_min"] + config["support_min"]) / 60
    labor_cost = labor_hours * config["hourly_rate"]
    
    electricity_cost = (config["printer_wattage"] / 1000) * print_hours * config["electricity_rate"]
    
    depreciation_cost = (config["printer_cost"] / config["printer_lifespan"]) * print_hours
    
    packaging_cost = config["packaging_cost"]
    
    base_cost = material_cost + labor_cost + electricity_cost + depreciation_cost + packaging_cost + config["other_costs"]
    
    failure_cost = base_cost * (config["failure_rate"] / 100)
    
    overhead_cost = config["monthly_rent"] / config["parts_per_month"] if config["parts_per_month"] > 0 else 0
    
    subtotal = base_cost + failure_cost + overhead_cost
    
    margin = model.markup_percent if model.markup_percent else config["default_margin"]
    suggested_price = subtotal * (1 + margin / 100)
    
    return {
        "model_id": model_id,
        "model_name": model.name,
        "filament_grams": filament_grams,
        "print_hours": print_hours,
        "material_type": material_type,
        "cost_per_gram": round(cost_per_gram, 4),
        "cost_source": cost_source,
        "costs": {
            "material": round(material_cost, 2),
            "labor": round(labor_cost, 2),
            "electricity": round(electricity_cost, 2),
            "depreciation": round(depreciation_cost, 2),
            "packaging": round(packaging_cost, 2),
            "failure": round(failure_cost, 2),
            "overhead": round(overhead_cost, 2),
            "other": round(config["other_costs"], 2)
        },
        "subtotal": round(subtotal, 2),
        "margin_percent": margin,
        "suggested_price": round(suggested_price, 2)
    }



# ============== Products & Orders (v0.14.0) ==============
# Imports for this section
from models import Product, ProductComponent, Order, OrderItem, OrderStatus
from schemas import (
    ProductResponse, ProductCreate, ProductUpdate,
    ProductComponentResponse, ProductComponentCreate,
    OrderResponse, OrderCreate, OrderUpdate, OrderSummary,
    OrderItemResponse, OrderItemCreate, OrderItemUpdate, OrderShipRequest
)


# -------------- Products --------------

@app.get("/api/products", response_model=List[ProductResponse], tags=["Products"])
def list_products(db: Session = Depends(get_db)):
    """List all products."""
    products = db.query(Product).all()
    result = []
    for p in products:
        resp = ProductResponse.model_validate(p)
        resp.component_count = len(p.components)
        # Calculate estimated COGS from components
        cogs = 0
        for comp in p.components:
            if comp.model and comp.model.cost_per_item:
                cogs += comp.model.cost_per_item * comp.quantity_needed
        resp.estimated_cogs = round(cogs, 2) if cogs > 0 else None
        result.append(resp)
    return result


@app.post("/api/products", response_model=ProductResponse, tags=["Products"])
def create_product(data: ProductCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new product with optional BOM components."""
    product = Product(
        name=data.name,
        sku=data.sku,
        price=data.price,
        description=data.description
    )
    db.add(product)
    db.flush()  # Get the ID before adding components
    
    # Add components if provided
    if data.components:
        for comp_data in data.components:
            comp = ProductComponent(
                product_id=product.id,
                model_id=comp_data.model_id,
                quantity_needed=comp_data.quantity_needed,
                notes=comp_data.notes
            )
            db.add(comp)
    
    db.commit()
    db.refresh(product)
    return product


@app.get("/api/products/{product_id}", response_model=ProductResponse, tags=["Products"])
def get_product(product_id: int, db: Session = Depends(get_db)):
    """Get a product with its BOM components."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    resp = ProductResponse.model_validate(product)
    resp.component_count = len(product.components)
    
    # Enrich components with model names
    enriched_components = []
    cogs = 0
    for comp in product.components:
        comp_resp = ProductComponentResponse.model_validate(comp)
        if comp.model:
            comp_resp.model_name = comp.model.name
            if comp.model.cost_per_item:
                cogs += comp.model.cost_per_item * comp.quantity_needed
        enriched_components.append(comp_resp)
    
    resp.components = enriched_components
    resp.estimated_cogs = round(cogs, 2) if cogs > 0 else None
    return resp


@app.patch("/api/products/{product_id}", response_model=ProductResponse, tags=["Products"])
def update_product(product_id: int, data: ProductUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update a product."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(product, key, value)
    
    db.commit()
    db.refresh(product)
    return product


@app.delete("/api/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Products"])
def delete_product(product_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete a product."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    db.delete(product)
    db.commit()


# -------------- Product Components (BOM) --------------

@app.post("/api/products/{product_id}/components", response_model=ProductComponentResponse, tags=["Products"])
def add_product_component(product_id: int, data: ProductComponentCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Add a component to a product's BOM."""
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    model = db.query(Model).filter(Model.id == data.model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="Model not found")
    
    comp = ProductComponent(
        product_id=product_id,
        model_id=data.model_id,
        quantity_needed=data.quantity_needed,
        notes=data.notes
    )
    db.add(comp)
    db.commit()
    db.refresh(comp)
    
    resp = ProductComponentResponse.model_validate(comp)
    resp.model_name = model.name
    return resp


@app.delete("/api/products/{product_id}/components/{component_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Products"])
def remove_product_component(product_id: int, component_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Remove a component from a product's BOM."""
    comp = db.query(ProductComponent).filter(
        ProductComponent.id == component_id,
        ProductComponent.product_id == product_id
    ).first()
    if not comp:
        raise HTTPException(status_code=404, detail="Component not found")
    
    db.delete(comp)
    db.commit()


# -------------- Orders --------------

@app.get("/api/orders", response_model=List[OrderSummary], tags=["Orders"])
def list_orders(
    status_filter: Optional[str] = None,
    platform: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """List all orders with optional filters."""
    query = db.query(Order)
    
    if status_filter:
        query = query.filter(Order.status == status_filter)
    if platform:
        query = query.filter(Order.platform == platform)
    
    orders = query.order_by(Order.created_at.desc()).all()
    
    result = []
    for o in orders:
        summary = OrderSummary(
            id=o.id,
            order_number=o.order_number,
            platform=o.platform,
            customer_name=o.customer_name,
            status=o.status,
            revenue=o.revenue,
            order_date=o.order_date,
            item_count=len(o.items),
            fulfilled=all(item.fulfilled_quantity >= item.quantity for item in o.items) if o.items else False
        )
        result.append(summary)
    return result


@app.post("/api/orders", response_model=OrderResponse, tags=["Orders"])
def create_order(data: OrderCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Create a new order with optional line items."""
    order = Order(
        order_number=data.order_number,
        platform=data.platform,
        customer_name=data.customer_name,
        customer_email=data.customer_email,
        order_date=data.order_date,
        notes=data.notes,
        revenue=data.revenue,
        platform_fees=data.platform_fees,
        payment_fees=data.payment_fees,
        shipping_charged=data.shipping_charged,
        shipping_cost=data.shipping_cost,
        labor_minutes=data.labor_minutes or 0
    )
    db.add(order)
    db.flush()
    
    # Add line items if provided
    if data.items:
        for item_data in data.items:
            # Verify product exists
            product = db.query(Product).filter(Product.id == item_data.product_id).first()
            if not product:
                raise HTTPException(status_code=404, detail=f"Product {item_data.product_id} not found")
            
            item = OrderItem(
                order_id=order.id,
                product_id=item_data.product_id,
                quantity=item_data.quantity,
                unit_price=item_data.unit_price if item_data.unit_price else product.price
            )
            db.add(item)
    
    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


@app.get("/api/orders/{order_id}", response_model=OrderResponse, tags=["Orders"])
def get_order(order_id: int, db: Session = Depends(get_db)):
    """Get an order with items and P&L calculation."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    return _enrich_order_response(order, db)


@app.patch("/api/orders/{order_id}", response_model=OrderResponse, tags=["Orders"])
def update_order(order_id: int, data: OrderUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(order, key, value)
    
    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


@app.delete("/api/orders/{order_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Orders"])
def delete_order(order_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Delete an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    db.delete(order)
    db.commit()


# -------------- Order Items --------------

@app.post("/api/orders/{order_id}/items", response_model=OrderItemResponse, tags=["Orders"])
def add_order_item(order_id: int, data: OrderItemCreate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Add a line item to an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    product = db.query(Product).filter(Product.id == data.product_id).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    
    item = OrderItem(
        order_id=order_id,
        product_id=data.product_id,
        quantity=data.quantity,
        unit_price=data.unit_price if data.unit_price else product.price
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    
    resp = OrderItemResponse.model_validate(item)
    resp.product_name = product.name
    resp.product_sku = product.sku
    resp.subtotal = (item.unit_price or 0) * item.quantity
    resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
    return resp


@app.patch("/api/orders/{order_id}/items/{item_id}", response_model=OrderItemResponse, tags=["Orders"])
def update_order_item(order_id: int, item_id: int, data: OrderItemUpdate, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Update an order line item."""
    item = db.query(OrderItem).filter(
        OrderItem.id == item_id,
        OrderItem.order_id == order_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Order item not found")
    
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(item, key, value)
    
    db.commit()
    db.refresh(item)
    
    product = db.query(Product).filter(Product.id == item.product_id).first()
    resp = OrderItemResponse.model_validate(item)
    if product:
        resp.product_name = product.name
        resp.product_sku = product.sku
    resp.subtotal = (item.unit_price or 0) * item.quantity
    resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
    return resp


@app.delete("/api/orders/{order_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Orders"])
def remove_order_item(order_id: int, item_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Remove a line item from an order."""
    item = db.query(OrderItem).filter(
        OrderItem.id == item_id,
        OrderItem.order_id == order_id
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Order item not found")
    
    db.delete(item)
    db.commit()


# -------------- Order Actions --------------

@app.post("/api/orders/{order_id}/schedule", tags=["Orders"])
def schedule_order(order_id: int, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Generate jobs for an order based on BOM."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    jobs_created = []
    
    for item in order.items:
        product = item.product
        if not product or not product.components:
            continue
        
        # For each component in the BOM
        for comp in product.components:
            model = comp.model
            if not model:
                continue
            
            # Calculate how many jobs needed
            pieces_needed = item.quantity * comp.quantity_needed
            pieces_per_job = model.quantity_per_bed or 1
            jobs_needed = -(-pieces_needed // pieces_per_job)  # Ceiling division
            
            # Create jobs
            for i in range(jobs_needed):
                qty_this_job = min(pieces_per_job, pieces_needed - (i * pieces_per_job))
                
                job = Job(
                    model_id=model.id,
                    item_name=f"{model.name} (Order #{order.order_number or order.id})",
                    quantity=1,
                    order_item_id=item.id,
                    quantity_on_bed=qty_this_job,
                    status=JobStatus.PENDING,
                    duration_hours=model.build_time_hours,
                    filament_type=model.default_filament_type
                )
                db.add(job)
                jobs_created.append({
                    "model": model.name,
                    "quantity_on_bed": qty_this_job
                })
    
    # Update order status
    if jobs_created:
        order.status = OrderStatus.IN_PROGRESS
    
    db.commit()
    
    return {
        "success": True,
        "order_id": order_id,
        "jobs_created": len(jobs_created),
        "details": jobs_created
    }


@app.get("/api/orders/{order_id}/invoice.pdf", tags=["Orders"])
def get_order_invoice(
    order_id: int,
    current_user: dict = Depends(require_role("operator")),
    db: Session = Depends(get_db)
):
    """Generate a branded PDF invoice for an order."""
    order = db.query(Order).options(joinedload(Order.items)).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    try:
        enriched = _enrich_order_response(order, db)
        branding = branding_to_dict(get_or_create_branding(db))

        from invoice_generator import InvoiceGenerator
        gen = InvoiceGenerator(branding, enriched.model_dump())
        pdf_bytes = gen.generate()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[invoice] PDF generation failed for order {order_id}: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    from fastapi.responses import Response
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=invoice_{order.order_number or order.id}.pdf",
            "Content-Length": str(len(pdf_bytes)),
        }
    )


@app.patch("/api/orders/{order_id}/ship", response_model=OrderResponse, tags=["Orders"])
def ship_order(order_id: int, data: OrderShipRequest, current_user: dict = Depends(require_role("operator")), db: Session = Depends(get_db)):
    """Mark an order as shipped."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    
    order.status = OrderStatus.SHIPPED
    order.tracking_number = data.tracking_number
    order.shipped_date = data.shipped_date or datetime.utcnow()
    
    db.commit()
    db.refresh(order)
    return _enrich_order_response(order, db)


# -------------- Helper Functions --------------

def _enrich_order_response(order: Order, db: Session) -> OrderResponse:
    """Build a full OrderResponse with calculated fields."""
    resp = OrderResponse.model_validate(order)
    
    # Enrich items
    enriched_items = []
    total_items = 0
    fulfilled_items = 0
    
    for item in order.items:
        item_resp = OrderItemResponse.model_validate(item)
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if product:
            item_resp.product_name = product.name
            item_resp.product_sku = product.sku
        item_resp.subtotal = (item.unit_price or 0) * item.quantity
        item_resp.is_fulfilled = item.fulfilled_quantity >= item.quantity
        enriched_items.append(item_resp)
        
        total_items += item.quantity
        fulfilled_items += min(item.fulfilled_quantity, item.quantity)
    
    resp.items = enriched_items
    resp.total_items = total_items
    resp.fulfilled_items = fulfilled_items
    
    # Count jobs
    jobs = db.query(Job).join(OrderItem).filter(OrderItem.order_id == order.id).all()
    resp.jobs_total = len(jobs)
    resp.jobs_complete = len([j for j in jobs if j.status == JobStatus.COMPLETED])
    
    # Calculate costs from jobs
    estimated_cost = sum(j.estimated_cost or 0 for j in jobs)
    actual_cost = sum(j.estimated_cost or 0 for j in jobs if j.status == JobStatus.COMPLETED)
    
    # Add fees and shipping
    total_fees = (order.platform_fees or 0) + (order.payment_fees or 0) + (order.shipping_cost or 0)
    
    resp.estimated_cost = round(estimated_cost + total_fees, 2) if estimated_cost else None
    resp.actual_cost = round(actual_cost + total_fees, 2) if actual_cost else None
    
    # Calculate profit
    if order.revenue and resp.actual_cost:
        resp.profit = round(order.revenue - resp.actual_cost, 2)
        resp.margin_percent = round((resp.profit / order.revenue) * 100, 1) if order.revenue > 0 else None
    
    return resp


# ======================
# Global Search
# ======================

@app.get("/api/search", tags=["Search"])
def global_search(q: str = "", db: Session = Depends(get_db)):
    """Search across models, jobs, spools, and printers."""
    if not q or len(q) < 2:
        return {"models": [], "jobs": [], "spools": [], "printers": []}
    
    query = f"%{q.lower()}%"
    
    # Search models
    models = db.query(Model).filter(
        (Model.name.ilike(query)) | (Model.notes.ilike(query))
    ).limit(5).all()
    
    # Search jobs
    jobs = db.query(Job).filter(
        (Job.item_name.ilike(query)) | (Job.notes.ilike(query))
    ).order_by(Job.created_at.desc()).limit(5).all()
    
    # Search spools by QR code, vendor, notes, or filament info
    spools = db.query(Spool).outerjoin(FilamentLibrary, Spool.filament_id == FilamentLibrary.id).filter(
        (Spool.qr_code.ilike(query)) |
        (Spool.vendor.ilike(query)) | 
        (Spool.notes.ilike(query)) |
        (FilamentLibrary.brand.ilike(query)) |
        (FilamentLibrary.name.ilike(query)) |
        (FilamentLibrary.material.ilike(query))
    ).limit(5).all()
    
    # Search printers
    printers = db.query(Printer).filter(
        Printer.name.ilike(query)
    ).limit(5).all()
    
    return {
        "models": [{"id": m.id, "name": m.name, "type": "model"} for m in models],
        "jobs": [{"id": j.id, "name": j.item_name, "status": j.status.value if j.status else None, "type": "job"} for j in jobs],
        "spools": [{"id": s.id, "name": f"{s.filament.brand} {s.filament.name}" if s.filament else (s.vendor or f"Spool #{s.id}"), "qr_code": s.qr_code, "type": "spool"} for s in spools],
        "printers": [{"id": p.id, "name": p.name, "type": "printer"} for p in printers],
    }


# ============== QR Scan-to-Assign ==============

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


@app.post("/api/spools/scan-assign", response_model=ScanAssignResponse, tags=["Spools"])
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
    if data.slot < 1 or data.slot > (printer.slot_count or 4):
        return ScanAssignResponse(
            success=False,
            message=f"Invalid slot {data.slot} for {printer.name} (has {printer.slot_count or 4} slots)"
        )
    
    # Check if slot already has a spool assigned
    existing_slot = db.query(FilamentSlot).filter(
        FilamentSlot.printer_id == data.printer_id,
        FilamentSlot.slot_number == data.slot
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
        FilamentSlot.printer_id != data.printer_id
    ).update({FilamentSlot.assigned_spool_id: None})
    
    db.commit()
    
    spool_name = f"{spool.filament.brand} {spool.filament.name}" if spool.filament else spool.qr_code
    
    return ScanAssignResponse(
        success=True,
        message=f"Assigned {spool_name} to {printer.name} slot {data.slot}",
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
        "remaining_weight": spool.remaining_weight_g,
        "initial_weight": spool.initial_weight_g,
        "location_printer_id": spool.location_printer_id,
        "location_slot": spool.location_slot,
    }



# ============== Alerts & Notifications Endpoints (v0.17.0) ==============

@app.get("/api/alerts", response_model=List[AlertResponse], tags=["Alerts"])
async def list_alerts(
    severity: Optional[str] = None,
    alert_type: Optional[str] = None,
    is_read: Optional[bool] = None,
    limit: int = 25,
    offset: int = 0,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """List alerts for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    query = db.query(Alert).filter(Alert.user_id == current_user["id"])
    
    if severity:
        query = query.filter(Alert.severity == severity)
    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)
    if is_read is not None:
        query = query.filter(Alert.is_read == is_read)
    
    alerts = query.order_by(Alert.created_at.desc()).offset(offset).limit(limit).all()
    
    results = []
    for alert in alerts:
        data = AlertResponse.model_validate(alert)
        if alert.printer:
            data.printer_name = alert.printer.nickname or alert.printer.name
        if alert.job:
            data.job_name = alert.job.item_name
        if alert.spool and alert.spool.filament:
            data.spool_name = f"{alert.spool.filament.brand} {alert.spool.filament.name}"
        results.append(data)
    
    return results


@app.get("/api/alerts/unread-count", tags=["Alerts"])
async def get_unread_count(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get unread alert count for bell badge."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    count = db.query(Alert).filter(
        Alert.user_id == current_user["id"],
        Alert.is_read == False
    ).count()
    return {"unread_count": count}


@app.get("/api/alerts/summary", response_model=AlertSummary, tags=["Alerts"])
async def get_alert_summary(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get aggregated alert counts for dashboard widget."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    uid = current_user["id"]
    base = db.query(Alert).filter(
        Alert.user_id == uid,
        Alert.is_dismissed == False,
        Alert.is_read == False
    )
    
    failed = base.filter(Alert.alert_type == AlertType.PRINT_FAILED).count()
    spool = base.filter(Alert.alert_type == AlertType.SPOOL_LOW).count()
    maint = base.filter(Alert.alert_type == AlertType.MAINTENANCE_OVERDUE).count()
    
    return AlertSummary(
        print_failed=failed,
        spool_low=spool,
        maintenance_overdue=maint,
        total=failed + spool + maint
    )


@app.patch("/api/alerts/{alert_id}/read", tags=["Alerts"])
async def mark_alert_read(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark a single alert as read."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"]
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_read = True
    db.commit()
    return {"status": "ok"}


@app.post("/api/alerts/mark-all-read", tags=["Alerts"])
async def mark_all_read(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark all alerts as read for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.query(Alert).filter(
        Alert.user_id == current_user["id"],
        Alert.is_read == False
    ).update({"is_read": True})
    db.commit()
    return {"status": "ok"}


@app.patch("/api/alerts/{alert_id}/dismiss", tags=["Alerts"])
async def dismiss_alert(
    alert_id: int,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Dismiss an alert (hide from dashboard widget)."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == current_user["id"]
    ).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.is_dismissed = True
    alert.is_read = True
    db.commit()
    return {"status": "ok"}


# ============== Alert Preferences ==============

@app.get("/api/alert-preferences", response_model=List[AlertPreferenceResponse], tags=["Alerts"])
async def get_alert_preferences(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get current user's alert preferences."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    prefs = db.query(AlertPreference).filter(
        AlertPreference.user_id == current_user["id"]
    ).all()
    
    if not prefs:
        from alert_dispatcher import seed_alert_preferences
        seed_alert_preferences(db, current_user["id"])
        prefs = db.query(AlertPreference).filter(
            AlertPreference.user_id == current_user["id"]
        ).all()
    
    return prefs


@app.put("/api/alert-preferences", tags=["Alerts"])
async def update_alert_preferences(
    data: AlertPreferencesUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Bulk update alert preferences for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    uid = current_user["id"]
    for pref_data in data.preferences:
        existing = db.query(AlertPreference).filter(
            AlertPreference.user_id == uid,
            AlertPreference.alert_type == pref_data.alert_type
        ).first()
        
        if existing:
            existing.in_app = pref_data.in_app
            existing.browser_push = pref_data.browser_push
            existing.email = pref_data.email
            existing.threshold_value = pref_data.threshold_value
        else:
            db.add(AlertPreference(
                user_id=uid,
                alert_type=pref_data.alert_type,
                in_app=pref_data.in_app,
                browser_push=pref_data.browser_push,
                email=pref_data.email,
                threshold_value=pref_data.threshold_value
            ))
    
    db.commit()
    return {"status": "ok", "message": "Preferences updated"}


# ============== SMTP Config (Admin Only) ==============

@app.get("/api/smtp-config", tags=["Alerts"])
async def get_smtp_config(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Get SMTP configuration (admin only, password masked)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    if not config:
        return SmtpConfigResponse()
    smtp = config.value
    return SmtpConfigResponse(
        enabled=smtp.get("enabled", False),
        host=smtp.get("host", ""),
        port=smtp.get("port", 587),
        username=smtp.get("username", ""),
        password_set=bool(smtp.get("password")),
        from_address=smtp.get("from_address", ""),
        use_tls=smtp.get("use_tls", True)
    )


@app.put("/api/smtp-config", tags=["Alerts"])
async def update_smtp_config(
    data: SmtpConfigBase,
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Update SMTP configuration (admin only)."""
    config = db.query(SystemConfig).filter(SystemConfig.key == "smtp_config").first()
    smtp_data = data.dict()
    
    if not smtp_data.get("password") and config and config.value.get("password"):
        smtp_data["password"] = config.value["password"]
    
    if config:
        config.value = smtp_data
    else:
        db.add(SystemConfig(key="smtp_config", value=smtp_data))
    
    db.commit()
    return {"status": "ok", "message": "SMTP configuration updated"}


@app.post("/api/alerts/test-email", tags=["Alerts"])
async def send_test_email(
    current_user: dict = Depends(require_role("admin")),
    db: Session = Depends(get_db)
):
    """Send a test email to the current user (admin only)."""
    from alert_dispatcher import _get_smtp_config, _deliver_email
    
    smtp = _get_smtp_config(db)
    if not smtp:
        raise HTTPException(status_code=400, detail="SMTP not configured or not enabled")
    if not current_user.get("email"):
        raise HTTPException(status_code=400, detail="Your account has no email address")
    
    _deliver_email(
        db, current_user["id"],
        "Test Alert \u2014 O.D.I.N.",
        "This is a test notification. If you received this, SMTP is configured correctly.",
        "info"
    )
    return {"status": "ok", "message": f"Test email queued to {current_user['email']}"}


# ============== Browser Push Subscription ==============

@app.get("/api/push/vapid-key", tags=["Alerts"])
async def get_vapid_key(db: Session = Depends(get_db)):
    """Get VAPID public key for browser push subscription."""
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'vapid_keys'")).fetchone()
    if not row:
        return {"public_key": None, "enabled": False}
    try:
        import json
        keys = json.loads(row[0])
        return {"public_key": keys.get("public_key"), "enabled": True}
    except:
        return {"public_key": None, "enabled": False}


@app.post("/api/push/subscribe", tags=["Alerts"])
async def subscribe_push(
    data: PushSubscriptionCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Store a browser push subscription for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    existing = db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"],
        PushSubscription.endpoint == data.endpoint
    ).first()
    
    if existing:
        existing.p256dh_key = data.p256dh_key
        existing.auth_key = data.auth_key
    else:
        db.add(PushSubscription(
            user_id=current_user["id"],
            endpoint=data.endpoint,
            p256dh_key=data.p256dh_key,
            auth_key=data.auth_key
        ))
    
    db.commit()
    return {"status": "ok", "message": "Push subscription registered"}


@app.delete("/api/push/subscribe", tags=["Alerts"])
async def unsubscribe_push(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove all push subscriptions for the current user."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    db.query(PushSubscription).filter(
        PushSubscription.user_id == current_user["id"]
    ).delete()
    db.commit()
    return {"status": "ok", "message": "Push subscriptions removed"}


# ============== Production Frontend Serving ==============
# Serve built React app when frontend/dist exists (Docker/production mode)
import os as _os
_frontend_dist = _os.path.join(_os.path.dirname(__file__), "..", "frontend", "dist")
if _os.path.isdir(_frontend_dist):
    from fastapi.staticfiles import StaticFiles as _StaticFiles
    from fastapi.responses import FileResponse as _FileResponse

    # Serve static assets (JS, CSS, images)
    app.mount("/assets", _StaticFiles(directory=_os.path.join(_frontend_dist, "assets")), name="frontend-assets")

    # Catch-all: serve index.html for any non-API route (client-side routing)
    @app.get("/{path:path}", include_in_schema=False)
    async def _serve_frontend(path: str):
        # Don't intercept API routes or existing static mounts
        if path.startswith("api/") or path.startswith("static/") or path.startswith("health"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        # Try to serve the exact file first (favicon.ico, sw.js, etc.)
        exact_path = _os.path.realpath(_os.path.join(_frontend_dist, path))
        if not exact_path.startswith(_os.path.realpath(_frontend_dist)):
            from fastapi import HTTPException as _HTTPExc
            raise _HTTPExc(status_code=404)
        if path and _os.path.isfile(exact_path):
            return _FileResponse(exact_path)
        # Otherwise return index.html for client-side routing
        return _FileResponse(_os.path.join(_frontend_dist, "index.html"))
