"""Printer support levels and the per-printer opt-in for Experimental printer types.

Spec: .claude/specs/octoprint-adapter.md, Part 1.

* One table, ``SUPPORT_TABLE``: api_type -> (level, source the adapter was built from).
* An Experimental type is monitored always. Job sending and every print or motion
  control stay refused until a platform superadmin turns them on for that printer.
* The opt-in lives in ``system_config`` under ``CONFIG_KEY``:
  ``{printer_id: {"enabled": true, "by": user_id, "at": iso, "api_type": type}}``.
  ``api_type`` is stored so an opt-in never carries over to a different type.
* Everything here fails closed: an unreadable or malformed store means "not enabled".
"""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

SUPPORTED = 'supported'
EXPERIMENTAL = 'experimental'
UNSUPPORTED = 'unsupported'
LEVELS = (SUPPORTED, EXPERIMENTAL, UNSUPPORTED)

# api_type -> (level, what the adapter was built from).
# Tests mark a type experimental by monkeypatching this dict; there is no production flag.
SUPPORT_TABLE: dict[str, tuple[str, str]] = {
    'bambu': (SUPPORTED, 'Bambu Lab LAN-mode MQTT and FTPS; verified on real hardware'),
    'moonraker': (SUPPORTED, 'Moonraker REST API; verified on real hardware'),
    'prusalink': (SUPPORTED, 'PrusaLink REST API; verified on real hardware'),
    'elegoo': (SUPPORTED, 'Elegoo SDCP (port 3030); monitoring, pause, resume, speed and stop; job sending is Experimental (public SDCP documentation, not verified on real hardware) and need the per-printer opt-in'),
    'anycubic_lan': (SUPPORTED, 'Anycubic LAN mode; monitoring and ACE slots only'),
    'octoprint': (EXPERIMENTAL, "OctoPrint's public REST API documentation (docs.octoprint.org); built from the documentation, not verified on real hardware"),
    'ultimaker': (EXPERIMENTAL, "Ultimaker's public local REST API documentation; monitoring only for now (no job sending, no controls); built from the documentation, not verified on hardware"),
}

CONFIG_KEY = 'experimental_printer_actions'
EXPERIMENTAL_NOT_ENABLED = 'experimental_not_enabled'
EXPERIMENTAL_MESSAGE = (
    "This printer is Experimental: ODIN's support for its type is built from public documentation "
    "and has not been verified on real hardware. Job sending and printer controls stay off until an "
    "administrator turns them on for this printer. Monitoring still works."
)
EXPERIMENTAL_REFUSAL = f'{EXPERIMENTAL_NOT_ENABLED}: {EXPERIMENTAL_MESSAGE}'
UNSUPPORTED_NOT_ENABLED = 'printer_type_unsupported'
UNSUPPORTED_REFUSAL = (f"{UNSUPPORTED_NOT_ENABLED}: ODIN has no support for this printer type, so job sending "
                       "and printer controls are off. Pause and stop still work, and so does monitoring.")

# Experimental types that only monitor: job sending and every control are not built for them yet, so
# the per-printer opt-in means nothing and is never read for them. Pause and stop are refused too
# (there is no way to send them); the scheduler and dispatch refuse through dispatch_unsupported_reason.
MONITOR_ONLY = frozenset({'ultimaker'})
MONITOR_ONLY_NOT_AVAILABLE = 'printer_type_monitoring_only'
MONITOR_ONLY_DETAIL = "Job sending and printer controls are not available for this printer type yet. Monitoring works."
MONITOR_ONLY_REFUSAL = f'{MONITOR_ONLY_NOT_AVAILABLE}: {MONITOR_ONLY_DETAIL}'


def normalize(api_type) -> str:
    return str(api_type or '').strip().lower()


def support_level(api_type) -> Optional[str]:
    """None for a manual printer (no connection); 'unsupported' for a type not in the table."""
    key = normalize(api_type)
    if not key:
        return None
    entry = SUPPORT_TABLE.get(key)
    return entry[0] if entry else UNSUPPORTED


def support_source(api_type) -> Optional[str]:
    entry = SUPPORT_TABLE.get(normalize(api_type))
    return entry[1] if entry else None


def is_experimental(api_type) -> bool:
    return support_level(api_type) == EXPERIMENTAL


def is_monitor_only(api_type) -> bool:
    return normalize(api_type) in MONITOR_ONLY


# Capabilities that need the per-printer opt-in on a type whose OVERALL level stays Supported:
# monitoring and the controls that already shipped are unchanged and are never gated.
DISPATCH = 'dispatch'
GATED_CAPABILITIES: dict[str, frozenset] = {
    # Resume and speed shipped as Supported before job sending existed: they stay ungated.
    'elegoo': frozenset({DISPATCH}),
}
# Control route suffix or command name -> the capability it needs.
ACTION_CAPABILITY = {'resume': 'resume', 'resume_print': 'resume', 'speed': 'speed', 'set_print_speed': 'speed'}


def gated_capabilities(api_type) -> frozenset:
    return GATED_CAPABILITIES.get(normalize(api_type), frozenset())


def opt_in_applies(api_type) -> bool:
    """True when the per-printer opt-in means anything for this type."""
    return not is_monitor_only(api_type) and (support_level(api_type) == EXPERIMENTAL or bool(gated_capabilities(api_type)))


def _coerce(raw) -> dict:
    value = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    if not isinstance(value, dict):
        raise ValueError('malformed experimental-actions store')
    return value


def _read_store(db) -> dict:
    """``db`` is a Session or a Connection."""
    raw = db.execute(text('SELECT value FROM system_config WHERE key=:key'), {'key': CONFIG_KEY}).scalar_one_or_none()
    return {} if raw is None else _coerce(raw)


def experimental_enabled(db, printer_id, api_type) -> bool:
    """True only for a well-formed entry, enabled, recorded for this printer's current type AND
    name. The name binds the entry to this printer: a recycled id (bulk delete, raw SQL, cascade)
    for a different printer never inherits an opt-in."""
    if is_monitor_only(api_type):
        return False  # nothing to opt in to yet: a hand-written store entry enables nothing
    try:
        entry = _read_store(db).get(str(printer_id))
        if not (isinstance(entry, dict) and entry.get('enabled') is True and entry.get('api_type') == normalize(api_type)):
            return False
        name = db.execute(text('SELECT name FROM printers WHERE id=:id'), {'id': printer_id}).scalar_one_or_none()
    except Exception:
        return False
    return name is not None and entry.get('name') == name


def experimental_refusal(db, printer_id, api_type) -> Optional[str]:
    """Refusal text or None when allowed. Fails closed: a type missing from the table is refused
    outright (never opt-in-able); a manual printer (no type) is not a device and is untouched."""
    level = support_level(api_type)
    if level == UNSUPPORTED:
        return UNSUPPORTED_REFUSAL
    if level != EXPERIMENTAL:
        return None
    if is_monitor_only(api_type):
        return MONITOR_ONLY_REFUSAL
    return None if experimental_enabled(db, printer_id, api_type) else EXPERIMENTAL_REFUSAL


def experimental_refusal_for_printer(db, printer_id) -> Optional[str]:
    """Same, loading the type from the printer row. An unreadable row refuses."""
    try:
        api_type = db.execute(text('SELECT api_type FROM printers WHERE id=:id'), {'id': printer_id}).scalar_one_or_none()
    except Exception:
        return EXPERIMENTAL_REFUSAL
    return dispatch_refusal(db, printer_id, api_type)


def dispatch_refusal(db, printer_id, api_type) -> Optional[str]:
    """experimental_refusal, plus: a Supported type whose 'dispatch' capability is gated also needs
    the opt-in before any job is sent. Used at every job-sending boundary."""
    refusal = experimental_refusal(db, printer_id, api_type)
    if refusal is not None or DISPATCH not in gated_capabilities(api_type):
        return refusal
    return None if experimental_enabled(db, printer_id, api_type) else EXPERIMENTAL_REFUSAL


def control_refusal(db, printer_id, api_type, action) -> Optional[str]:
    """Refusal for a control route: Experimental and Unsupported types as experimental_refusal; a
    gated Supported type only for an action whose capability is gated (never pause or stop)."""
    refusal = experimental_refusal(db, printer_id, api_type)
    if refusal is not None or ACTION_CAPABILITY.get(action) not in gated_capabilities(api_type):
        return refusal
    return None if experimental_enabled(db, printer_id, api_type) else EXPERIMENTAL_REFUSAL


def command_refusal(printer, action=None) -> Optional[str]:
    """Refusal for a printer object (ORM row or namespace) about to receive a device command.

    A Supported type with gated capabilities is refused only for a gated ``action``; with no
    action it is refused unless opted in (the "is it opted in" question)."""
    api_type = getattr(printer, 'api_type', None)
    level = support_level(api_type)
    if level == UNSUPPORTED:
        return UNSUPPORTED_REFUSAL
    if level == EXPERIMENTAL and is_monitor_only(api_type):
        return MONITOR_ONLY_REFUSAL
    if level != EXPERIMENTAL:
        gated = gated_capabilities(api_type)
        if not gated or (action is not None and ACTION_CAPABILITY.get(action) not in gated):
            return None
    printer_id = getattr(printer, 'id', None)
    try:
        from sqlalchemy.orm import object_session
        session = object_session(printer)
        if session is not None:
            enabled = experimental_enabled(session, printer_id, api_type)
        else:
            from core.db import engine
            with engine.connect() as conn:
                enabled = experimental_enabled(conn, printer_id, api_type)
    except Exception:
        enabled = False
    return None if enabled else EXPERIMENTAL_REFUSAL


def enabled_for_printer(printer) -> bool:
    """Response field: only an Experimental type can be enabled, and only through the store."""
    return opt_in_applies(getattr(printer, 'api_type', None)) and command_refusal(printer) is None


def set_experimental_enabled(db, printer, enabled: bool, user_id) -> dict:
    """Stage the opt-in on ``db`` (a Session); the caller audits and commits.

    Returns {'changed': bool, 'enabled': bool}. Enabling an already-enabled printer
    of the same type changes nothing, so the original actor and time are kept.
    """
    from core.models import SystemConfig
    row = db.query(SystemConfig).filter(SystemConfig.key == CONFIG_KEY).with_for_update().one_or_none()
    try:
        store = dict(_coerce(row.value)) if row is not None else {}
    except ValueError:
        store = {}
    key = str(printer.id)
    api_type = normalize(printer.api_type)
    current = store.get(key)
    was_enabled = (isinstance(current, dict) and current.get('enabled') is True and current.get('api_type') == api_type
                   and current.get('name') == printer.name)
    if enabled:
        if was_enabled:
            return {'changed': False, 'enabled': True}
        store[key] = {'enabled': True, 'by': user_id, 'at': datetime.now(timezone.utc).isoformat(), 'api_type': api_type,
                      'name': printer.name}
    else:
        if key not in store:
            return {'changed': False, 'enabled': False}
        store.pop(key)
    if row is None:
        db.add(SystemConfig(key=CONFIG_KEY, value=store))
    else:
        row.value = store
    return {'changed': True, 'enabled': enabled}


def forget_printer(connection, printer_id) -> None:
    """Drop a deleted printer's entry so a recycled id never inherits an opt-in."""
    from sqlalchemy import select, update
    from core.models import SystemConfig
    table = SystemConfig.__table__
    # Serialize with opt-in changes before taking the shared JSON snapshot.
    raw = connection.execute(
        select(table.c.value).where(table.c.key == CONFIG_KEY).with_for_update()
    ).scalar_one_or_none()
    if raw is None:
        return
    try:
        store = _coerce(raw)
    except ValueError:
        store = {}
    store.pop(str(printer_id), None)
    connection.execute(update(table).where(table.c.key == CONFIG_KEY).values(value=store))
