"""Printer support levels and the per-printer opt-in for Experimental printer types.

Spec: .claude/specs/octoprint-adapter.md, Part 1.

* One table, ``SUPPORT_TABLE``: api_type -> (level, source the adapter was built from).
* An Experimental type is monitored always. Job sending and every print or motion
  control stay refused until a platform superadmin turns them on for that printer.
* The opt-in lives in ``system_config`` under ``CONFIG_KEY``:
  ``{printer_id: {"enabled": true, "by": user_id, "at": iso, "api_type": type}}``.
  ``api_type`` is stored so an opt-in never carries over to a different type.
* Command checks fail closed on an unreadable opt-in store.
"""
import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from modules.printers.printer_models import identified_h3c, is_h3c_printer, rack_printer_refusal

SUPPORTED = 'supported'
EXPERIMENTAL = 'experimental'
UNSUPPORTED = 'unsupported'
LEVELS = (SUPPORTED, EXPERIMENTAL, UNSUPPORTED)

# api_type -> (level, what the adapter was built from).
# Tests mark a type experimental by monkeypatching this dict; there is no production flag.
SUPPORT_TABLE: dict[str, tuple[str, str]] = {
    'bambu': (SUPPORTED, 'Bambu Lab LAN-mode MQTT and FTPS. Skipping objects was verified on a P2S. Other supported controls were verified on real hardware.'),
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
SEND_JOBS_MESSAGE = (
    "Sending jobs is Experimental on Elegoo printers and is off for this printer. "
    "A platform administrator can turn it on in this printer's Experimental features. "
    "Monitoring, pause, resume, speed and stop work without it."
)

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


CONTROL_PROOF = {
    ("kobra_s1", "pause"): "unproven",
    ("kobra_s1", "resume"): "proven",
    ("kobra_s1", "stop"): "unproven",
}


def require_control_capable_model(printer, *, safety=False, db=None):
    reason = rack_printer_refusal(printer, safety=safety, db=db)
    if reason:
        from fastapi import HTTPException
        code, _, message = reason.partition(': ')
        raise HTTPException(409, detail={'code': code, 'message': message})


def control_proof(printer, action):
    from modules.printers.dispatch_support import is_kobra_s1
    if rack_printer_refusal(printer):
        return "not_working"
    if identified_h3c(printer) and action not in ("pause", "resume", "stop"):
        return "unproven"
    kind = "kobra_s1" if is_kobra_s1(printer) else None
    return CONTROL_PROOF.get((kind, action), "proven")


def normalize(api_type) -> str:
    return str(api_type or '').strip().lower()


def support_level(api_type) -> Optional[str]:
    """None for a manual printer (no connection); 'unsupported' for a type not in the table."""
    key = normalize(api_type)
    if not key:
        return None
    entry = SUPPORT_TABLE.get(key)
    return entry[0] if entry else UNSUPPORTED


def support_source(api_type, *, h3c=False) -> Optional[str]:
    if h3c:
        return ('Job sending, resume and stop were verified on a real Bambu Lab H3C. '
                'Multi-tool jobs were verified with up to five tools.')
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
ACTION_CAPABILITY = {'skip-objects': 'skip_objects', 'skip_objects': 'skip_objects', 'resume': 'resume', 'resume_print': 'resume', 'speed': 'speed', 'set_print_speed': 'speed'}


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


def _entry_enabled(store, printer_id, api_type, name):
    entry = store.get(str(printer_id))
    return (isinstance(entry, dict) and entry.get('enabled') is True and entry.get('api_type') == normalize(api_type)
            and name is not None and entry.get('name') == name)


def enrich_response_opt_ins(db, printers):
    """Read the opt-in flags from one snapshot for this response."""
    try:
        store = _read_store(db) if any(opt_in_applies(p.api_type) for p in printers) else {}
    except Exception:
        store = {}  # Fail closed on an unreadable store.
    for p in printers:
        p.experimental_actions_enabled = opt_in_applies(p.api_type) and _entry_enabled(store, p.id, p.api_type, p.name)


def experimental_enabled(db, printer_id, api_type) -> bool:
    """True only for a well-formed entry, enabled, recorded for this printer's current type AND
    name. The name binds the entry to this printer: a recycled id (bulk delete, raw SQL, cascade)
    for a different printer never inherits an opt-in."""
    if not opt_in_applies(api_type):
        return False
    try:
        store = _read_store(db)
        if str(printer_id) not in store:
            return False
        name = db.execute(text('SELECT name FROM printers WHERE id=:id'), {'id': printer_id}).scalar_one_or_none()
    except Exception:
        return False
    return _entry_enabled(store, printer_id, api_type, name)


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


def experimental_refusal_for_printer(db, printer_id, *, expected_h3c=None) -> Optional[str]:
    """Same, loading the type from the printer row. An unreadable row refuses."""
    try:
        row = db.execute(text('SELECT id,api_type,model,machine_type,installed_nozzles FROM printers WHERE id=:id'),
                         {'id': printer_id}).mappings().one()
        if is_h3c_printer(row):
            reason = rack_printer_refusal(row, db=db)
            if reason:
                return reason
        if expected_h3c is not None and identified_h3c(row) != expected_h3c:
            return 'printer_model_changed: The printer model changed before the job could start.'
        api_type = row['api_type']
    except Exception:
        return EXPERIMENTAL_REFUSAL
    return dispatch_refusal(db, printer_id, api_type)


def dispatch_refusal(db, printer_id, api_type) -> Optional[str]:
    """experimental_refusal, plus: a Supported type whose 'dispatch' capability is gated also needs
    the opt-in before any job is sent. Used at every job-sending boundary."""
    refusal = experimental_refusal(db, printer_id, api_type)
    if refusal is not None or DISPATCH not in gated_capabilities(api_type):
        return refusal
    message = experimental_capability_message(api_type, DISPATCH)
    return None if experimental_enabled(db, printer_id, api_type) else f'{EXPERIMENTAL_NOT_ENABLED}: {message}'


def control_refusal(db, printer_id, api_type, action) -> Optional[str]:
    """Refusal for a control route: Experimental and Unsupported types as experimental_refusal; a
    gated Supported type only for an action whose capability is gated (never pause or stop)."""
    refusal = experimental_refusal(db, printer_id, api_type)
    if refusal is not None or ACTION_CAPABILITY.get(action) not in gated_capabilities(api_type):
        return refusal
    message = experimental_capability_message(api_type, ACTION_CAPABILITY.get(action))
    return None if experimental_enabled(db, printer_id, api_type) else f'{EXPERIMENTAL_NOT_ENABLED}: {message}'


def experimental_capability_message(api_type, capability):
    if support_level(api_type) == SUPPORTED and capability in gated_capabilities(api_type):
        messages = {('elegoo', DISPATCH): SEND_JOBS_MESSAGE}
        return messages.get((normalize(api_type), capability), EXPERIMENTAL_MESSAGE)
    return EXPERIMENTAL_MESSAGE


def command_refusal(printer, action=None) -> Optional[str]:
    """Refusal for a printer object (ORM row or namespace) about to receive a device command.

    A Supported type with gated capabilities is refused only for a gated ``action``; with no
    action it is refused unless opted in (the "is it opted in" question)."""
    reason = rack_printer_refusal(printer)
    if reason:
        return reason
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
    enabled = printer_opt_in(printer)
    capability = DISPATCH if action is None and DISPATCH in gated_capabilities(api_type) else ACTION_CAPABILITY.get(action)
    message = experimental_capability_message(api_type, capability)
    return None if enabled else f'{EXPERIMENTAL_NOT_ENABLED}: {message}'


def printer_opt_in(printer):
    api_type = getattr(printer, "api_type", None)
    printer_id = getattr(printer, 'id', None)
    try:
        from sqlalchemy.orm import object_session
        session = object_session(printer)
        if session is not None:
            return experimental_enabled(session, printer_id, api_type)
        from core.db import engine
        with engine.connect() as conn:
            return experimental_enabled(conn, printer_id, api_type)
    except Exception:
        return False


def enabled_for_printer(printer) -> bool:
    """Response field: Experimental types or capabilities can be enabled only through the store."""
    return opt_in_applies(printer.api_type) and command_refusal(printer) is None


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
    was_enabled = _entry_enabled(store, printer.id, api_type, printer.name)
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
