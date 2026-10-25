"""Smart plug routes — plug configuration, power control, energy monitoring."""

import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.db_compat import sql
from core.rbac import require_role, check_org_access, require_action
import core.crypto as crypto
import modules.printers.smart_plug as smart_plug
from modules.printers.models import Printer

log = logging.getLogger("odin.api")
router = APIRouter()


def _require_printer_org_access(printer_id: int, current_user: dict, db: Session) -> Printer:
    """Load a printer by id and enforce the caller's org scope.

    Raises HTTPException(404) if the printer doesn't exist OR if the caller
    is in a different org. 404 (not 403) by convention — don't leak whether
    a printer exists in another tenant.

    Added for R1 from the 2026-04-12 adversarial review: the smart-plug
    routes used to load printers by id with no tenancy check, letting any
    operator in any org reconfigure smart-plug integration or power-cycle
    another tenant's printer.
    """
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


def _require_visible_plug_printer(printer_id: int, current_user: dict, db: Session) -> Printer:
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer or (not check_org_access(current_user, printer.org_id) and not printer.shared):
        raise HTTPException(status_code=404, detail="Printer not found")
    return printer


@router.get("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def get_plug_config(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")), db: Session = Depends(get_db)):
    """Get smart plug configuration for a printer."""
    _require_printer_org_access(printer_id, current_user, db)
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
        "cooldown_minutes": result[5] if result[5] is not None else 5,
        "power_state": result[6],
        "energy_kwh": result[7] or 0,
        "configured": result[0] is not None,
    }


PLUG_TYPES = ("tasmota", "homeassistant", "mqtt")
PLUG_COOLDOWN_MAX_MINUTES = 1440


def _validate_plug_host(plug_type, host):
    """Config-time destination policy for HTTP plug hosts.

    Same policy as printer hosts (Moonraker/PrusaLink api_host):
    _check_ssrf_blocklist, plus the ITAR outbound check that the plug HTTP
    helper repeats at request time. MQTT plugs publish to a topic and have
    no HTTP destination.
    """
    if plug_type == "mqtt" or host is None:
        return
    if not isinstance(host, str) or not host.strip():
        raise HTTPException(422, "Plug host must be a non-empty string")
    import urllib.parse
    if plug_type == "homeassistant":
        try:
            parsed = urllib.parse.urlsplit(host.strip())
            parsed.port
        except ValueError:
            raise HTTPException(400, "Home Assistant URL is malformed")
        # scheme://host[:port] only: no userinfo, path, query or fragment.
        if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                or "@" in parsed.netloc or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise HTTPException(400, "Home Assistant URL must be http(s)://host[:port] with no path or query")
        hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        if parsed.port:
            hostname += f":{parsed.port}"
        url = host.strip()
    else:
        if "://" in host or "/" in host:
            raise HTTPException(400, "Tasmota host must be a host or host:port")
        hostname, url = host.strip(), f"http://{host.strip()}/"
    from modules.printers.route_utils import _check_ssrf_blocklist
    _check_ssrf_blocklist(hostname)
    from core.itar import enforce_request_destination, ItarOutboundBlocked
    try:
        enforce_request_destination(url)
    except ItarOutboundBlocked:
        raise HTTPException(400, "Plug host blocked by outbound policy")


@router.put("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def update_plug_config(printer_id: int, request: Request, current_user: dict = Depends(require_action("printers.edit", scope="write:printers")), db: Session = Depends(get_db)):
    """Update smart plug configuration (merge semantics).

    Only fields present in the body change; omitted fields keep their stored
    values. Clearing the configuration is DELETE, not a null type. The stored
    Home Assistant token is kept only while type and host are unchanged.
    """
    _require_printer_org_access(printer_id, current_user, db)
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(422, "Body must be a JSON object")

    current = db.execute(text(
        "SELECT plug_type, plug_host, plug_entity_id, plug_auth_token, plug_auto_on, plug_auto_off,"
        " plug_cooldown_minutes FROM printers WHERE id = :id"
    ), {"id": printer_id}).fetchone()
    cur_type, cur_host, cur_entity, cur_token, cur_on, cur_off, cur_cool = current

    if "type" in data and data["type"] not in PLUG_TYPES:
        if data["type"] in (None, ""):
            raise HTTPException(400, "Use DELETE /printers/{id}/plug to remove the plug configuration")
        raise HTTPException(400, "Invalid plug type. Use: tasmota, homeassistant, mqtt")
    for key in ("auto_on", "auto_off"):
        if key in data and type(data[key]) is not bool:
            raise HTTPException(422, f"{key} must be a boolean")
    if "cooldown_minutes" in data:
        value = data["cooldown_minutes"]
        if type(value) is not int or not 0 <= value <= PLUG_COOLDOWN_MAX_MINUTES:
            raise HTTPException(422, f"cooldown_minutes must be an integer from 0 to {PLUG_COOLDOWN_MAX_MINUTES}")
    for key in ("host", "entity_id", "auth_token"):
        if key in data and data[key] is not None and not isinstance(data[key], str):
            raise HTTPException(422, f"{key} must be a string")

    plug_type = data.get("type", cur_type)
    host = data["host"] if "host" in data else cur_host
    if "type" in data or "host" in data:
        _validate_plug_host(plug_type, host)
        if plug_type != "mqtt" and isinstance(host, str):
            # Stored normalized (case, IDNA, trailing dot) like api_host.
            from core.webhook_utils import normalize_device_host, normalize_device_url
            host = normalize_device_url(host) if plug_type == "homeassistant" else normalize_device_host(host)
    if "auth_token" in data:
        auth_token = crypto.encrypt(data["auth_token"]) if data["auth_token"] else None
    elif plug_type == cur_type and host == cur_host:
        auth_token = cur_token
    else:
        # Never send a stored token to a different plug destination.
        auth_token = None

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
        "plug_host": host,
        "plug_entity_id": data["entity_id"] if "entity_id" in data else cur_entity,
        "plug_auth_token": auth_token,
        "plug_auto_on": data["auto_on"] if "auto_on" in data else (cur_on if cur_on is not None else True),
        "plug_auto_off": data["auto_off"] if "auto_off" in data else (cur_off if cur_off is not None else True),
        "plug_cooldown_minutes": data["cooldown_minutes"] if "cooldown_minutes" in data else (cur_cool if cur_cool is not None else 5),
    })
    db.commit()

    return {"status": "ok", "message": "Smart plug configuration updated"}


@router.delete("/printers/{printer_id}/plug", tags=["Smart Plug"])
async def remove_plug_config(printer_id: int, current_user: dict = Depends(require_action("printers.edit", scope="write:printers")), db: Session = Depends(get_db)):
    """Remove smart plug configuration from a printer."""
    _require_printer_org_access(printer_id, current_user, db)
    db.execute(text("""
        UPDATE printers SET
            plug_type = NULL, plug_host = NULL, plug_entity_id = NULL,
            plug_auth_token = NULL, plug_auto_on = TRUE, plug_auto_off = TRUE,
            plug_cooldown_minutes = 5, plug_power_state = NULL
        WHERE id = :id
    """), {"id": printer_id})
    db.commit()
    return {"status": "ok"}


def _power_receipt(result, *, fenced=True):
    if isinstance(result, smart_plug.MqttPowerReceipt):
        if result.status == "accepted":
            return {"accepted": True, "power_state": None}
        if result.status == "unconfirmed":
            raise HTTPException(409, "Power command may have been sent; confirmation unavailable. Check before retrying.")
        raise HTTPException(400, "Power command was not accepted")
    if type(result) is not bool:
        if not fenced:
            raise HTTPException(400, "No smart plug configured or plug unreachable")
        raise HTTPException(409, "Power command was refused or its outcome is uncertain; reconcile before retrying.")
    return {"power_state": result}


@router.post("/printers/{printer_id}/plug/on", tags=["Smart Plug"])
def plug_power_on(printer_id: int, request: Request, current_user: dict = Depends(require_action("printers.plug", scope="write:printers")), db: Session = Depends(get_db)):
    """Turn on a printer's smart plug."""
    printer = _require_printer_org_access(printer_id, current_user, db)
    if printer.plug_type in ('homeassistant', 'mqtt'):
        # A frontend nonce is accepted, but these protocols cannot prove
        # command completion and retain legacy process-local behavior.
        return _power_receipt(smart_plug.power_on(printer_id), fenced=False)
    # Finish the request's read transaction before target-first admission.
    db.rollback()
    from modules.printers.power_authority import capture
    try:
        authority = capture(db.get_bind(), request, current_user, printer_id, 'manual_on')
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    result = smart_plug.power_on(printer_id, authority=authority)
    return _power_receipt(result)


@router.post("/printers/{printer_id}/plug/off", tags=["Smart Plug"])
def plug_power_off(printer_id: int, request: Request, current_user: dict = Depends(require_action("printers.plug", scope="write:printers")), db: Session = Depends(get_db)):
    """Turn off a printer's smart plug."""
    printer = _require_printer_org_access(printer_id, current_user, db)
    if printer.plug_type in ('homeassistant', 'mqtt'):
        # A frontend nonce is accepted, but these protocols cannot prove
        # command completion and retain legacy process-local behavior.
        return _power_receipt(smart_plug.power_off(printer_id), fenced=False)
    # Finish the request's read transaction before target-first admission.
    db.rollback()
    from modules.printers.power_authority import capture
    try:
        authority = capture(db.get_bind(), request, current_user, printer_id, 'manual_off')
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    result = smart_plug.power_off(printer_id, authority=authority)
    return _power_receipt(result)


@router.post("/printers/{printer_id}/plug/toggle", tags=["Smart Plug"])
def plug_power_toggle(printer_id: int, request: Request, current_user: dict = Depends(require_action("printers.plug", scope="write:printers")), db: Session = Depends(get_db)):
    """Toggle a printer's smart plug."""
    printer = _require_printer_org_access(printer_id, current_user, db)
    if printer.plug_type in ('homeassistant', 'mqtt'):
        # A frontend nonce is accepted, but these protocols cannot prove
        # command completion and retain legacy process-local behavior.
        return _power_receipt(smart_plug.power_toggle(printer_id), fenced=False)
    # Finish the request's read transaction before target-first admission.
    db.rollback()
    from modules.printers.power_authority import capture
    try:
        authority = capture(db.get_bind(), request, current_user, printer_id, 'manual_toggle')
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    result = smart_plug.power_toggle(printer_id, authority=authority)
    return _power_receipt(result)


@router.get("/printers/{printer_id}/plug/energy", tags=["Smart Plug"])
async def plug_energy(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")), db: Session = Depends(get_db)):
    """Get current energy data from smart plug."""
    _require_visible_plug_printer(printer_id, current_user, db)
    data = smart_plug.get_energy(printer_id)
    if data is None:
        raise HTTPException(400, "No energy data available")
    return data


@router.get("/printers/{printer_id}/plug/state", tags=["Smart Plug"])
async def plug_state(printer_id: int, current_user: dict = Depends(require_role("viewer", scope="read:printers")), db: Session = Depends(get_db)):
    """Query current power state from smart plug."""
    _require_visible_plug_printer(printer_id, current_user, db)
    state = smart_plug.get_power_state(printer_id)
    if state is None:
        raise HTTPException(400, "No smart plug configured or plug unreachable")
    return {"power_state": state}


@router.get("/settings/energy-rate", tags=["Smart Plug"])
async def get_energy_rate(current_user: dict = Depends(require_role("viewer", scope="read:printers")), db: Session = Depends(get_db)):
    """Get energy cost per kWh."""
    result = db.execute(text("SELECT value FROM system_config WHERE key = 'energy_cost_per_kwh'")).fetchone()
    try:
        rate = float(result[0]) if result else 0.12
    except (TypeError, ValueError):
        raise HTTPException(409, "Stored energy rate is invalid") from None
    if not math.isfinite(rate) or rate < 0:
        raise HTTPException(409, "Stored energy rate is invalid")
    return {"energy_cost_per_kwh": rate}


@router.put("/settings/energy-rate", tags=["Smart Plug"])
async def set_energy_rate(request: Request, current_user: dict = Depends(require_action("settings.edit", scope="admin")), db: Session = Depends(get_db)):
    """Set energy cost per kWh."""
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(422, "Expected an energy rate object")
    rate = data.get("energy_cost_per_kwh", 0.12)
    if type(rate) not in (int, float):
        raise HTTPException(422, "Energy rate must be a finite nonnegative number")
    try:
        valid_rate = math.isfinite(rate) and rate >= 0
    except OverflowError:
        valid_rate = False
    if not valid_rate:
        raise HTTPException(422, "Energy rate must be a finite nonnegative number")
    db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — see docs/SEMGREP_TRIAGE.md (params bound, f-string interpolates only allowlisted/internal symbols)
        f"{sql.upsert_prefix()} system_config (key, value) VALUES ('energy_cost_per_kwh', :rate)"
        f"{sql.on_conflict_suffix('key', ['value'])}"
    ), {"rate": str(rate)})
    db.commit()
    return {"energy_cost_per_kwh": rate}
