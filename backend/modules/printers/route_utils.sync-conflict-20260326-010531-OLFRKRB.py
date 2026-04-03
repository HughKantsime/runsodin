"""Shared utilities for printer route files.

These helpers are used by multiple printer route split files and by
external modules (e.g. system/routes.py imports _check_ssrf_blocklist).
"""

import logging
import os
import re

import yaml
from fastapi import HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Optional

import core.crypto as crypto

log = logging.getLogger("odin.api")

GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/app/go2rtc/go2rtc.yaml")

# ====================================================================
# Inline Pydantic models shared across route files
# ====================================================================

class TestConnectionRequest(PydanticBaseModel):
    """Request body for testing printer connection."""
    api_type: str
    api_host: str
    serial: Optional[str] = None
    access_code: Optional[str] = None


# ====================================================================
# SSRF / URL Validation
# ====================================================================

def _check_ssrf_blocklist(host: str) -> None:
    """Block SSRF attempts targeting localhost, metadata endpoints, or internal-only hosts."""
    import ipaddress
    blocked_prefixes = ("localhost", "127.", "169.254.", "0.", "::1")
    h = (host or "").strip().lower().split(":")[0]  # strip port
    if any(h.startswith(p) for p in blocked_prefixes):
        raise HTTPException(status_code=400, detail="Invalid printer host")
    try:
        addr = ipaddress.ip_address(h)
        if addr.is_loopback or addr.is_link_local:
            raise HTTPException(status_code=400, detail="Invalid printer host")
    except ValueError:
        pass  # hostname — let OS resolve it


_CAMERA_SHELL_METACHAR_RE = re.compile(r'[;&|$`\\]')


def _validate_camera_url(url: str) -> str:
    """Validate and sanitize a camera URL before writing to go2rtc config.

    - Only rtsp:// or rtsps:// schemes allowed
    - Strips shell metacharacters (; & | $ ` \\)
    - Rejects localhost/loopback targets (SSRF prevention)

    Returns the sanitized URL or raises HTTPException 400.
    """
    if not url:
        return url

    import urllib.parse
    lower = url.strip().lower()
    if not (lower.startswith("rtsp://") or lower.startswith("rtsps://")):
        raise HTTPException(status_code=400, detail="Camera URL must use rtsp:// or rtsps:// scheme")

    sanitized = _CAMERA_SHELL_METACHAR_RE.sub('', url.strip())

    try:
        parsed = urllib.parse.urlparse(sanitized)
        host = parsed.hostname or ""
        _check_ssrf_blocklist(host)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Camera URL points to a blocked host")

    return sanitized


# ====================================================================
# Camera / go2rtc helpers
# ====================================================================

def get_camera_url(printer):
    """Get camera URL for a printer - from DB field or auto-generated from credentials."""
    if printer.camera_url:
        url = printer.camera_url
        try:
            url = crypto.decrypt(url)
        except Exception:
            pass
        return url
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
    from modules.printers.models import Printer
    printers = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True)).all()
    streams = {}
    for p in printers:
        url = get_camera_url(p)
        if url:
            if url.startswith(("http://", "https://")):
                streams[f"printer_{p.id}"] = f"ffmpeg:{url}#video=h264"
            else:
                streams[f"printer_{p.id}"] = url
    db.commit()
    webrtc_config = {"listen": "0.0.0.0:8555"}
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
        "api": {"listen": "127.0.0.1:1984"},
        "webrtc": webrtc_config,
        "streams": streams,
    }
    with open(GO2RTC_CONFIG, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    try:
        import subprocess
        subprocess.run(["supervisorctl", "restart", "go2rtc"], capture_output=True, timeout=5)
    except Exception:
        pass


def sync_go2rtc_config_standalone():
    """Regenerate go2rtc config (callable without a DB session)."""
    from core.db import SessionLocal
    db = SessionLocal()
    try:
        sync_go2rtc_config(db)
    finally:
        db.close()


# ====================================================================
# Printer command helpers
# ====================================================================

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
    """Send a command to a Bambu printer via a short-lived MQTT connection."""
    from modules.printers.adapters.bambu import BambuPrinter
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
            _time.sleep(0.3)
            adapter.disconnect()
            return success
    except Exception as e:
        log.error(f"Bambu {action} failed for printer {printer.id}: {e}")
    return False


def _prusalink_command(printer, action: str) -> bool:
    """Send a command to a PrusaLink printer."""
    from modules.printers.adapters.prusalink import PrusaLinkPrinter
    try:
        decrypted = crypto.decrypt(printer.api_key) if printer.api_key else ""
        if "|" in decrypted:
            username, password = decrypted.split("|", 1)
            adapter = PrusaLinkPrinter(printer.api_host, username=username, password=password)
        else:
            adapter = PrusaLinkPrinter(printer.api_host, api_key=decrypted)
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
    from modules.printers.adapters.elegoo import ElegooPrinter
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
        from modules.printers.adapters.moonraker import MoonrakerPrinter
        adapter = MoonrakerPrinter(printer.api_host)
        return _call_adapter_method(adapter, action)
    elif printer.api_type == "prusalink":
        return _prusalink_command(printer, action)
    elif printer.api_type == "elegoo":
        return _elegoo_command(printer, action)
    else:
        return _bambu_command(printer, action)


def _bambu_command_direct(printer, method_name: str, *args, **kwargs) -> bool:
    """Call a BambuPrinter method directly (for commands not in the generic allowlist)."""
    from modules.printers.adapters.bambu import BambuPrinter
    import time as _time
    try:
        creds = crypto.decrypt(printer.api_key)
        serial, access_code = creds.split("|", 1)
        adapter = BambuPrinter(
            printer.api_host, serial, access_code,
            client_id=f"odin_cmd_{printer.id}_{int(_time.time())}"
        )
        if adapter.connect():
            method = getattr(adapter, method_name, None)
            if method is None:
                return False
            success = method(*args, **kwargs)
            _time.sleep(0.3)
            adapter.disconnect()
            return success
    except Exception as e:
        log.error(f"Bambu {method_name} failed for printer {printer.id}: {e}")
    return False
