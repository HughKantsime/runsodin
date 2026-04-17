"""Shared utilities for printer route files.

These helpers are used by multiple printer route split files and by
external modules (e.g. system/routes.py imports _check_ssrf_blocklist).
"""

import fcntl
import logging
import os
import re
import threading
import time
from urllib.parse import quote as urlquote

import yaml
from fastapi import HTTPException
from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Optional

import core.crypto as crypto

log = logging.getLogger("odin.api")

GO2RTC_CONFIG = os.environ.get("GO2RTC_CONFIG", "/app/go2rtc/go2rtc.yaml")

# go2rtc restart protection — prevents restart storms when multiple
# cameras trigger sync simultaneously (e.g. control room page load).
# Uses a file lock so it works across processes (monitors + backend).
_go2rtc_thread_lock = threading.Lock()
_GO2RTC_MIN_RESTART_INTERVAL = 10.0  # seconds
_GO2RTC_LOCKFILE = os.environ.get("GO2RTC_LOCKFILE", "/tmp/go2rtc_sync.lock")

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
        except Exception as e:
            log.debug(f"Failed to decrypt camera URL (using raw): {e}")
        return url
    if printer.api_type == "bambu" and printer.api_key and printer.api_host:
        RTSP_MODELS = {'X1C', 'X1 Carbon', 'X1E', 'X1 Carbon Combo', 'H2D'}
        model = (printer.model or '').strip()
        if model not in RTSP_MODELS:
            return None
        try:
            parts = crypto.decrypt(printer.api_key).split("|")
            if len(parts) == 2:
                return f"rtsps://bblp:{urlquote(parts[1], safe='')}@{printer.api_host}:322/streaming/live/1"
        except Exception as e:
            log.debug(f"Failed to decrypt Bambu API key: {e}")
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


def _build_go2rtc_config(db: Session) -> dict:
    """Build go2rtc config dict from current printer state."""
    from modules.printers.models import Printer
    printers = db.query(Printer).filter(Printer.is_active.is_(True), Printer.camera_enabled.is_(True)).all()
    streams = {}
    for p in printers:
        url = get_camera_url(p)
        if url:
            # UniFi Protect: rtsps + port 7441 needs rtspx (no SRTP, skip cert check)
            if url.startswith("rtsps://") and ":7441" in url:
                url = "rtspx://" + url[8:]
                url = url.split("?enableSrtp")[0].split("&enableSrtp")[0]
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
    return {
        "api": {"listen": "127.0.0.1:1984"},
        "webrtc": webrtc_config,
        "streams": streams,
    }


def _go2rtc_restart_with_lock(config: dict, *, force: bool = False):
    """Write go2rtc config and restart if changed, with cross-process locking."""
    lockfd = None
    try:
        lockfd = open(_GO2RTC_LOCKFILE, "w")
        fcntl.flock(lockfd, fcntl.LOCK_EX)

        # Check if config actually changed
        try:
            with open(GO2RTC_CONFIG, "r") as f:
                existing = yaml.safe_load(f)
            if existing == config and not force:
                return  # nothing changed, skip restart
        except (FileNotFoundError, yaml.YAMLError):
            pass  # file missing or corrupt — write it

        # Cooldown: check mtime of lockfile as cross-process timestamp
        if not force:
            try:
                mtime = os.path.getmtime(GO2RTC_CONFIG)
                if (time.time() - mtime) < _GO2RTC_MIN_RESTART_INTERVAL:
                    with open(GO2RTC_CONFIG, "w") as f:
                        yaml.dump(config, f, default_flow_style=False)
                    log.debug("go2rtc config updated but restart skipped (cooldown)")
                    return
            except OSError:
                pass

        with open(GO2RTC_CONFIG, "w") as f:
            yaml.dump(config, f, default_flow_style=False)

        try:
            import subprocess
            subprocess.run(["supervisorctl", "restart", "go2rtc"], capture_output=True, timeout=5)
            log.info("go2rtc restarted (config changed)")
        except Exception as e:
            log.debug(f"Failed to restart go2rtc: {e}")
    finally:
        if lockfd:
            fcntl.flock(lockfd, fcntl.LOCK_UN)
            lockfd.close()


def sync_go2rtc_config(db: Session, *, force: bool = False):
    """Regenerate go2rtc config from printer camera URLs.

    Only restarts go2rtc if the config actually changed or if force=True.
    Protected by a cross-process file lock and cooldown to prevent
    restart storms when multiple cameras trigger sync simultaneously.
    """
    config = _build_go2rtc_config(db)
    with _go2rtc_thread_lock:
        _go2rtc_restart_with_lock(config, force=force)


def sync_go2rtc_config_standalone():
    """Regenerate go2rtc config (callable without a DB session)."""
    from core.db import SessionLocal
    db = SessionLocal()
    try:
        sync_go2rtc_config(db)
    finally:
        db.close()


def sync_go2rtc_config_raw():
    """Regenerate go2rtc config using raw SQL (safe to call from monitors)."""
    from core.db_utils import get_db
    streams = {}
    try:
        with get_db() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT id, api_type, api_host, api_key, camera_url, model "
                "FROM printers WHERE is_active = 1 AND camera_enabled = 1"
            ).fetchall()
            for row in rows:
                pid, api_type, api_host, api_key, camera_url, model = row
                url = None
                if camera_url:
                    try:
                        url = crypto.decrypt(camera_url)
                    except Exception:
                        url = camera_url
                elif api_type == 'bambu' and api_key and api_host:
                    RTSP_MODELS = {'X1C', 'X1 Carbon', 'X1E', 'X1 Carbon Combo', 'H2D'}
                    if (model or '').strip() in RTSP_MODELS:
                        try:
                            parts = crypto.decrypt(api_key).split('|')
                            if len(parts) == 2:
                                url = f"rtsps://bblp:{urlquote(parts[1], safe='')}@{api_host}:322/streaming/live/1"
                        except Exception as e:
                            log.debug(f"Failed to decrypt Bambu key for go2rtc: {e}")
                if url:
                    if url.startswith("rtsps://") and ":7441" in url:
                        url = "rtspx://" + url[8:]
                        url = url.split("?enableSrtp")[0].split("&enableSrtp")[0]
                    if url.startswith(("http://", "https://")):
                        streams[f"printer_{pid}"] = f"ffmpeg:{url}#video=h264"
                    else:
                        streams[f"printer_{pid}"] = url

            lan_ip = os.environ.get("ODIN_HOST_IP")
            if not lan_ip:
                r = cur.execute("SELECT value FROM system_config WHERE key = 'host_ip'").fetchone()
                if r:
                    lan_ip = r[0]
    except Exception as e:
        log.error(f"go2rtc raw sync DB error: {e}")
        return

    if not lan_ip:
        lan_ip = _get_lan_ip()
    webrtc_config = {"listen": "0.0.0.0:8555"}
    if lan_ip:
        webrtc_config["candidates"] = [f"{lan_ip}:8555"]
    config = {
        "api": {"listen": "127.0.0.1:1984"},
        "webrtc": webrtc_config,
        "streams": streams,
    }

    _go2rtc_restart_with_lock(config)


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
    from modules.printers.telemetry.feature_flag import is_v2_enabled

    if is_v2_enabled():
        return _bambu_command_v2(printer, action)
    return _bambu_command_legacy(printer, action)


def _bambu_command_v2(printer, action: str) -> bool:
    from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
    from modules.printers.telemetry.bambu.session import run_command
    try:
        creds = crypto.decrypt(printer.api_key)
        serial, access_code = creds.split("|", 1)
        config = BambuAdapterConfig(
            printer_id=f"cmd-{printer.id}",
            serial=serial,
            host=printer.api_host,
            access_code=access_code,
        )
        # V2's command adapter supports the same allowlisted methods.
        return run_command(config, action)
    except Exception as e:
        log.error(f"Bambu V2 {action} failed for printer {printer.id}: {e}")
        return False


def _bambu_command_legacy(printer, action: str) -> bool:
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
    from modules.printers.telemetry.feature_flag import is_v2_enabled

    if is_v2_enabled():
        from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
        from modules.printers.telemetry.bambu.session import run_command
        try:
            creds = crypto.decrypt(printer.api_key)
            serial, access_code = creds.split("|", 1)
            config = BambuAdapterConfig(
                printer_id=f"cmd-direct-{printer.id}",
                serial=serial,
                host=printer.api_host,
                access_code=access_code,
            )
            return run_command(config, method_name, *args, **kwargs)
        except Exception as e:
            log.error(f"Bambu V2 {method_name} failed for printer {printer.id}: {e}")
            return False

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
