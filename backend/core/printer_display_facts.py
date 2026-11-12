"""Read-only board facts. Existing system_config bridges monitor/API processes.

This bounded, expiring cache contains only observed display values, never raw
packets or dispatch authority. No schema changes or printer reads are needed.
"""
import json
import math
from datetime import datetime, timezone
from sqlalchemy import bindparam, text
from core.rbac import check_org_access, is_scoped_token, token_scope_granted

MAX_AGE_SECONDS = 300


def cache_key(printer_id):
    return f'printer.display_facts.{printer_id}'


def may_read(actor, resource):
    return not is_scoped_token(actor) or any(token_scope_granted(actor.get('_token_scopes', []), scope)
        for scope in ('admin', 'agent:read', 'agent:write', f'read:{resource}'))


def observe(monitor, packet, *, retained=False):
    """Merge individual observations, so partial reports cannot refresh old facts."""
    if retained:
        return
    facts = getattr(monitor, '_display_facts', {})
    now = datetime.now(timezone.utc).timestamp()
    def put(key, value):
        facts[key] = {'value': value, 'at': now}
    ams = packet.get('ams')
    if isinstance(ams, dict):
        if 'tray_now' in ams:
            tray = ams['tray_now']
            if isinstance(tray, str) and tray.isdecimal():
                tray = int(tray) if len(tray) < 5 else None
            # 254/255 are external/no AMS; HT uses hardware ids 128..135.
            slot = (tray + 1 if 0 <= tray < 16 else tray - 128 + 17 if 128 <= tray <= 135 else None) if type(tray) is int else None
            put('feeding_slot', slot)
        if 'ams' in ams:
            from modules.printers.telemetry.bambu.ams_evidence import parse_raw_ams_facts
            try:
                put('slot_extruders', {str(t.slot_number): t.extruder for t in parse_raw_ams_facts({'ams': ams}) if t.extruder is not None})
            except (ValueError, TypeError, KeyError):
                put('slot_extruders', {})
    chamber = packet.get('chamber_temper')
    device = packet.get('device')
    ctc = device.get('ctc') if isinstance(device, dict) else None
    info = ctc.get('info') if isinstance(ctc, dict) else None
    if isinstance(info, dict) and 'temp' in info:
        packed = info['temp']
        # H2D ctc.info.temp packs current in low 16 bits, target in high 16.
        chamber = packed & 0xffff if type(packed) is int and 0 <= packed <= 0xffffffff else None
    if type(chamber) in (int, float) and 0 <= chamber <= 150 and math.isfinite(chamber):
        put('chamber_temp', chamber)
    monitor._display_facts = facts


def persist(conn, monitor):
    facts = getattr(monitor, '_display_facts', None)
    if not facts:
        return
    row = conn.execute('SELECT created_at FROM printers WHERE id=?', (monitor.printer_id,)).fetchone()
    if row:
        value = json.dumps({'printer_created_at': str(row[0]), 'facts': facts})
        conn.execute('INSERT INTO system_config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     (cache_key(monitor.printer_id), value))


def read_facts(db, printers):
    if not printers:
        return {}
    rows = db.execute(text('SELECT key,value FROM system_config WHERE key IN :keys')
                      .bindparams(bindparam('keys', expanding=True)), {'keys': [cache_key(p.id) for p in printers]})
    stored = {key: value for key, value in rows}
    now = datetime.now(timezone.utc).timestamp()
    result = {}
    for printer in printers:
        try:
            entry = stored.get(cache_key(printer.id), {})
            entry = json.loads(entry) if isinstance(entry, str) else entry
            # Protect against deleted/recreated printer ids and stale/offline caches.
            if printer.api_type != 'bambu' or str(printer.created_at) != entry.get('printer_created_at') or (printer.gcode_state or '').upper() == 'OFFLINE':
                continue
            result[printer.id] = {key: fact['value'] for key, fact in entry.get('facts', {}).items()
                                  if 0 <= now - fact['at'] <= MAX_AGE_SECONDS}
        except (ValueError, TypeError, AttributeError, KeyError):
            continue
    return result


def plate_clear_times(db, printer_ids, actor):
    """Only confirmations on jobs visible to this caller; no actor/job details leak."""
    if not printer_ids or not may_read(actor, 'jobs'):
        return {}
    rows = db.execute(text("SELECT e.printer_id,e.occurred_at,j.charged_to_org_id FROM job_timeline_events e "
        "JOIN jobs j ON j.id=e.job_id WHERE e.kind='bed_clear_confirmed' AND e.printer_id IN :ids "
        "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=j.id AND s.archived_at IS NOT NULL) "
        "ORDER BY e.occurred_at DESC,e.id DESC").bindparams(bindparam('ids', expanding=True)), {'ids': printer_ids})
    result = {}
    for printer_id, at, org_id in rows:
        if check_org_access(actor, org_id):
            result.setdefault(printer_id, at)
    if not result:
        return result
    # A confirmation stands only until the next print starts: a plate cleared several prints
    # ago says nothing about the bed now.
    from modules.printers.plate_clear import as_utc
    started = db.execute(text("SELECT printer_id, MAX(actual_start) FROM jobs WHERE printer_id IN :ids "
        "AND actual_start IS NOT NULL GROUP BY printer_id").bindparams(bindparam('ids', expanding=True)), {'ids': list(result)})
    for printer_id, latest in started:
        confirmed, begun = as_utc(result.get(printer_id)), as_utc(latest)
        if confirmed is not None and begun is not None and begun > confirmed:
            del result[printer_id]
    return result


def enrich_printer_facts(db, printers, actor):
    facts = read_facts(db, printers)
    cleared = plate_clear_times(db, [p.id for p in printers], actor)
    for printer in printers:
        observed = facts.get(printer.id, {})
        printer.feeding_slot = observed.get('feeding_slot')
        printer.chamber_temp = observed.get('chamber_temp')
        printer.plate_clear_confirmed_at = cleared.get(printer.id)
    return printers
