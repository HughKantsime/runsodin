"""System setup routes — onboarding wizard (admin creation, printer add, network config, complete)."""

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from core.rbac import require_role
from core.auth_helpers import _validate_password
from core.base import FilamentType
from modules.printers.models import Printer, FilamentSlot
from core.models import SystemConfig
from core.auth import hash_password, create_access_token
import core.crypto as crypto

log = logging.getLogger("odin.api")
router = APIRouter()


# ============== v1.8.8: first-user-wins setup gate ==============
#
# Industry-standard onboarding. WordPress / Ghost / Immich / Jellyfin /
# Portainer all use the same invariant: setup routes are open until an
# admin claims the install, then they close.
#
# Why this replaces the v1.8.6 setup-token gate:
#   * The token gate was correct but required copying a 43-char string
#     from `docker logs odin` into a web form. Industry peers don't do
#     that; enterprise secrets-manager friction is wrong for the SMB
#     self-hosted class ODIN lives in.
#   * The "internet-exposed ODIN is a LAN scanner" threat the token
#     protected against is now mitigated at install time: the install
#     script probes public reachability and refuses without an explicit
#     --force-public override.
#   * If a customer ignores that warning AND an attacker races them to
#     first-admin, the attacker "wins" a fresh install with no data.
#     The legitimate operator notices immediately on the next page load
#     ("account already exists") and can wipe /data and redo the install.
#     Same loss profile as every other self-hosted tool in this class.


def _validate_setup_phase(db: Session) -> None:
    """First-user-wins gate for /setup/* routes.

    Allows the call only when no admin has claimed the install yet AND
    setup hasn't been explicitly marked complete. Once either is true,
    setup routes close and raise 403.

    Deliberately does NOT consider the network origin of the call:
    operational hygiene (don't expose the unclaimed-admin state to the
    internet) is enforced at install time, not at every request.
    """
    if _setup_is_complete(db):
        raise HTTPException(
            status_code=403,
            detail="Setup already completed.",
        )
    if _setup_users_exist(db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Setup already claimed — an admin account exists. "
                "Log in to continue."
            ),
        )


# ============== Pydantic models ==============

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


# ============== Setup state helpers ==============

def _setup_users_exist(db: Session) -> bool:
    result = db.execute(text("SELECT COUNT(*) FROM users")).scalar()
    return result > 0


def _setup_is_complete(db: Session) -> bool:
    row = db.execute(text("SELECT value FROM system_config WHERE key = 'setup_complete'")).fetchone()
    if row:
        return row[0] == "true"
    return False


def _setup_is_locked(db: Session) -> bool:
    return _setup_users_exist(db) or _setup_is_complete(db)


def _get_lan_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


# ============== Setup endpoints ==============

@router.get("/setup/status", tags=["Setup"])
def setup_status(db: Session = Depends(get_db)):
    """Check if initial setup is needed. No auth required.

    v1.8.8: no side effects. Previous revision minted a setup token on
    first call; the token gate is gone, so this is a pure read now.
    """
    has_users = _setup_users_exist(db)
    is_complete = _setup_is_complete(db)
    return {"needs_setup": not has_users and not is_complete, "has_users": has_users, "is_complete": is_complete}


@router.post("/setup/admin", tags=["Setup"])
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
            "username": request.username, "email": request.email,
            "password_hash": password_hash_val, "role": "admin"
        })
        db.commit()
    except Exception as e:
        log.error(f"Failed to create admin user during setup: {e}")
        raise HTTPException(status_code=400, detail="Failed to create user. The username may already be taken.")

    access_token = create_access_token(data={"sub": request.username, "role": "admin"})
    return {"access_token": access_token, "token_type": "bearer"}


def _test_bambu_legacy(request) -> dict:
    try:
        from modules.printers.adapters.bambu import BambuPrinter
        import time
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
        log.warning("Setup Bambu test-connection failed: %s", e)
        return {"success": False, "error": "Connection failed. Check IP, serial, and access code."}


def _test_bambu_v2(request) -> dict:
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.session import read_status_once
    from modules.printers.telemetry.bambu.status_view import ams_slots_from_section

    config = BambuAdapterConfig(
        printer_id="setup-test",
        serial=request.serial,
        host=request.api_host,
        access_code=request.access_code,
    )
    try:
        result = read_status_once(config, timeout=8.0)
    except Exception as e:
        log.warning("Setup Bambu V2 test-connection failed: %s", e)
        return {"success": False, "error": "Connection failed. Check IP, serial, and access code."}

    if not result.success:
        return {"success": False, "error": result.error or "Failed to connect."}

    view = result.view
    section = result.section
    ams_slots = ams_slots_from_section(section) if section else []
    # Model detection: V2 exposes printer_type via info events; may be empty here.
    try:
        from modules.printers.printer_models import normalize_model_name
        detected_model = normalize_model_name("bambu", view.printer_type)
    except Exception:
        detected_model = None
    return {
        "success": True,
        "state": view.state,                     # legacy-shaped enum string
        "bed_temp": view.bed_temp,
        "nozzle_temp": view.nozzle_temp,
        "ams_slots": len(ams_slots),
        "model": detected_model,
    }


@router.post("/setup/test-printer", tags=["Setup"])
def setup_test_printer(
    request: SetupTestPrinterRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """Test printer connection during setup.

    Access control (v1.8.8 onboarding refactor):

    Gated on first-user-wins via _validate_setup_phase(db). The endpoint
    accepts calls ONLY while no admin has claimed the install and setup
    hasn't been marked complete. Once either is true the endpoint
    refuses with 403 and the caller has to authenticate normally.

    The "freshly-installed ODIN on the internet is a LAN scanner" threat
    that motivated the v1.8.6 setup token is now addressed at install
    time: install.sh probes public reachability and refuses without
    --force-public. See DECISION-012 in conductor/decision-log.md.
    """
    _validate_setup_phase(db)

    from modules.printers.route_utils import _check_ssrf_blocklist
    _check_ssrf_blocklist(request.api_host)

    if request.api_type.lower() == "bambu":
        if not request.serial or not request.access_code:
            raise HTTPException(status_code=400, detail="Serial and access_code required for Bambu printers")

        from modules.printers.telemetry.feature_flag import is_v2_enabled
        if is_v2_enabled():
            return _test_bambu_v2(request)
        return _test_bambu_legacy(request)

    elif request.api_type.lower() == "moonraker":
        import httpx as httpx_client
        try:
            r = httpx_client.get(f"http://{request.api_host}/printer/info", timeout=5)  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host -- verified safe — admin-gated by require_role and SSRF-checked by _check_ssrf_blocklist (or setup-locked)
            if r.status_code == 200:
                info = r.json().get("result", {})
                detected_model = None
                try:
                    cfg_r = httpx_client.get(f"http://{request.api_host}/server/config", timeout=3)  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host -- verified safe — admin-gated by require_role and SSRF-checked by _check_ssrf_blocklist (or setup-locked)
                    if cfg_r.status_code == 200:
                        kinematics = (cfg_r.json().get("result", {}).get("config", {}).get("printer", {}).get("kinematics", "") or "")
                        if kinematics.lower() == "corexy":
                            detected_model = "Voron"
                except Exception as e:
                    log.debug(f"Failed to detect Moonraker kinematics: {e}")
                if detected_model is None:
                    try:
                        hostname = (info.get("hostname") or "").lower()
                        if "voron" in hostname: detected_model = "Voron"
                        elif "trident" in hostname: detected_model = "Voron Trident"
                        elif "switchwire" in hostname: detected_model = "Voron Switchwire"
                        elif "v0" in hostname: detected_model = "Voron V0"
                    except Exception as e:
                        log.debug(f"Failed to detect model from hostname: {e}")
                return {"success": True, "state": info.get("state", "unknown"), "bed_temp": 0, "nozzle_temp": 0, "ams_slots": 0, "model": detected_model}
            return {"success": False, "error": f"Moonraker returned {r.status_code}"}
        except Exception as e:
            log.warning("Setup Moonraker test-connection failed: %s", e)
            return {"success": False, "error": "Connection failed. Check printer IP and Moonraker configuration."}

    elif request.api_type.lower() == "prusalink":
        import httpx as httpx_client
        try:
            r = httpx_client.get(f"http://{request.api_host}/api/version", timeout=5)  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host -- verified safe — admin-gated by require_role and SSRF-checked by _check_ssrf_blocklist (or setup-locked)
            if r.status_code == 200:
                info = r.json()
                detected_model = None
                try:
                    from modules.printers.printer_models import normalize_model_name
                    printer_field = info.get("printer", None)
                    if isinstance(printer_field, dict): raw_type = printer_field.get("type", "") or ""
                    elif isinstance(printer_field, str): raw_type = printer_field
                    else: raw_type = ""
                    detected_model = normalize_model_name("prusalink", raw_type)
                except Exception:
                    detected_model = None
                return {"success": True, "state": "connected", "bed_temp": 0, "nozzle_temp": 0, "ams_slots": 0, "model": detected_model}
            return {"success": False, "error": f"PrusaLink returned HTTP {r.status_code}"}
        except Exception as e:
            log.warning("Setup PrusaLink test-connection failed: %s", e)
            return {"success": False, "error": "Connection failed. Check printer IP and PrusaLink configuration."}

    elif request.api_type.lower() == "elegoo":
        import socket
        import json as _json
        import httpx as httpx_client
        reachable = False
        try:
            httpx_client.get(f"http://{request.api_host}:3030", timeout=5)  # nosemgrep: python.django.security.injection.tainted-url-host.tainted-url-host -- verified safe — admin-gated by require_role and SSRF-checked by _check_ssrf_blocklist (or setup-locked)
            reachable = True
        except Exception as e:
            log.debug(f"Failed to reach Elegoo printer: {e}")
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

    return {"success": False, "error": f"Unknown printer type: {request.api_type}"}


@router.post("/setup/printer", tags=["Setup"])
def setup_create_printer(
    request: SetupPrinterRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a printer during setup. Requires JWT from admin creation step."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    encrypted_api_key = None
    if request.api_key:
        encrypted_api_key = crypto.encrypt(request.api_key)

    db_printer = Printer(
        name=request.name, model=request.model, slot_count=request.slot_count,
        is_active=request.is_active, api_type=request.api_type,
        api_host=request.api_host, api_key=encrypted_api_key,
    )
    db.add(db_printer)
    db.flush()

    for i in range(1, request.slot_count + 1):
        slot = FilamentSlot(printer_id=db_printer.id, slot_number=i, filament_type=FilamentType.EMPTY)
        db.add(slot)

    db.commit()
    db.refresh(db_printer)
    return {"id": db_printer.id, "name": db_printer.name, "status": "created"}


@router.post("/setup/complete", tags=["Setup"])
def setup_mark_complete(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    """Mark setup as complete. Prevents wizard from showing again.

    R4: deletes the setup token file once setup is locked. The token's
    only purpose was to authorize the printer-probe endpoint during the
    initial wizard, and that endpoint is now closed by _setup_is_locked().
    """
    if not current_user:
        raise HTTPException(status_code=401, detail="Authentication required")
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    existing = db.execute(text("SELECT key FROM system_config WHERE key = 'setup_complete'")).fetchone()
    if existing:
        db.execute(text("UPDATE system_config SET value = 'true' WHERE key = 'setup_complete'"))
    else:
        config = SystemConfig(key="setup_complete", value="true")
        db.add(config)
    db.commit()
    return {"status": "complete"}


@router.get("/setup/network", tags=["Setup"])
async def setup_network_info(
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return auto-detected host IP for network configuration."""
    if _setup_is_complete(db) and not current_user:
        return JSONResponse(status_code=401, content={"detail": "Authentication required"})
    detected_ip = ""
    host_header = request.headers.get("host", "")
    host_part = host_header.split(":")[0] if host_header else ""
    import re as _re
    if host_part and _re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_part):
        if not host_part.startswith("127.") and not host_part.startswith("172."):
            detected_ip = host_part
    if not detected_ip:
        detected_ip = _get_lan_ip() or ""
    return {"detected_ip": detected_ip, "configured_ip": os.environ.get("ODIN_HOST_IP", "")}


@router.post("/setup/network", tags=["Setup"])
async def setup_save_network(request: Request, db: Session = Depends(get_db), current_user: dict = Depends(require_role("admin"))):
    """Save host IP for WebRTC camera streaming. Admin only."""
    if _setup_is_complete(db):
        raise HTTPException(status_code=403, detail="Setup already completed")
    data = await request.json()
    host_ip = data.get("host_ip", "").strip()
    if not host_ip:
        raise HTTPException(status_code=400, detail="host_ip is required")
    if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", host_ip):
        raise HTTPException(status_code=400, detail="Invalid IP address format")
    existing = db.execute(text("SELECT 1 FROM system_config WHERE key = 'host_ip'")).fetchone()
    if existing:
        db.execute(text("UPDATE system_config SET value = :v WHERE key = 'host_ip'"), {"v": host_ip})
    else:
        db.execute(text("INSERT INTO system_config (key, value) VALUES ('host_ip', :v)"), {"v": host_ip})
    db.commit()
    from modules.printers.route_utils import sync_go2rtc_config
    sync_go2rtc_config(db)
    return {"success": True, "host_ip": host_ip}
