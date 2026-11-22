"""Read-only board facts. Existing system_config bridges monitor/API processes.

This bounded, expiring cache contains only observed display values, never raw
packets or dispatch authority. No schema changes or printer reads are needed.
"""
import json
import math
from datetime import datetime, timezone
from datetime import timedelta
from sqlalchemy import bindparam, text
from core.rbac import check_org_access, is_scoped_token, token_scope_granted

MAX_AGE_SECONDS = 300


def cache_key(printer_id):
    return f'printer.display_facts.{printer_id}'


def plate_clear_key(printer_id):
    return f'plate_clear:{printer_id}'


def stored_plate_clear_times(db, printer_ids, actor):
    """Printer-owned confirmations; job predicates do not apply to unlinked prints."""
    if not printer_ids:
        return {}
    printers = db.execute(text('SELECT id,created_at FROM printers WHERE id IN :ids')
                          .bindparams(bindparam('ids', expanding=True)), {'ids': printer_ids}).all()
    rows = db.execute(text('SELECT key,value FROM system_config WHERE key IN :keys')
                      .bindparams(bindparam('keys', expanding=True)),
                      {'keys': [plate_clear_key(p) for p in printer_ids]})
    stored = dict(rows.all())
    from modules.printers.plate_clear import as_utc
    result = {}
    for printer_id, created_at in printers:
        try:
            entry = stored.get(plate_clear_key(printer_id), {})
            entry = json.loads(entry) if isinstance(entry, str) else entry
            if str(created_at) != entry.get('printer_created_at') or 'org_id' not in entry:
                continue
            # An unowned printer's confirmation is visible to everyone who can see the
            # printer, including in multi-organization installs. Only a time is exposed.
            if check_org_access(actor, entry['org_id']) and as_utc(entry.get('at')) is not None:
                result[printer_id] = entry['at']
        except (ValueError, TypeError, AttributeError):
            continue
    return result


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
            if getattr(monitor, '_identified_h3c', False) and slot is not None and slot > 9:
                slot = None
            put('feeding_slot', slot)
        if 'ams' in ams:
            from modules.printers.telemetry.bambu.ams_evidence import parse_raw_ams_facts
            try:
                trays = parse_raw_ams_facts({'ams': ams})
                put('ht_trays', {str(t.slot_number): t.material for t in trays if t.slot_number >= 17})
                put('slot_extruders', {str(t.slot_number): t.extruder for t in trays if t.extruder is not None})
            except (ValueError, TypeError, KeyError):
                put('ht_trays', {})
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


def reported_ht_slots(db, printer_id):
    """Last reported HT slots and exact material labels for planning. Starting still needs a fresh owned read."""
    row = db.execute(text("SELECT p.created_at,c.value FROM printers p JOIN system_config c ON c.key=:key "
                          "WHERE p.id=:id AND p.api_type='bambu'"),
                     {'key': cache_key(printer_id), 'id': printer_id}).first()
    if not row:
        return {}
    try:
        entry = json.loads(row.value) if isinstance(row.value, str) else row.value
        slots = entry['facts']['ht_trays']['value']
        if str(row.created_at) == entry['printer_created_at'] and isinstance(slots, dict):
            from core.material_identity import canonical_reported_material
            return {n: canonical_reported_material(slots[str(n)]) for n in range(17, 25) if str(n) in slots}
    except (ValueError, TypeError, KeyError):
        pass
    return {}


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


def plate_clear_times(db, printer_ids, actor, job_clauses=(), job_params=None):
    """Visible job and printer-owned confirmations; no actor/job details leak.

    ``job_clauses`` are further source-owned predicates on the job (alias ``j``) with their
    bound ``job_params``: the Home Assistant status feed passes its own job visibility rule.
    """
    if not printer_ids or not may_read(actor, 'jobs'):
        return {}
    extra = ''.join(' AND (' + clause + ')' for clause in job_clauses)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed clauses plus the caller's source-owned job predicates; every value is bound
    rows = db.execute(text("SELECT e.printer_id,e.occurred_at,j.charged_to_org_id FROM job_timeline_events e "
        "JOIN jobs j ON j.id=e.job_id WHERE e.kind='bed_clear_confirmed' AND e.printer_id IN :ids "
        "AND NOT EXISTS (SELECT 1 FROM education_submissions s WHERE s.job_id=j.id AND s.archived_at IS NOT NULL)" + extra +
        " ORDER BY e.occurred_at DESC,e.id DESC").bindparams(bindparam('ids', expanding=True)),
        {**(job_params or {}), 'ids': printer_ids})
    # The newest visible confirmation per printer, by parsed instant: stored times mix naive
    # UTC and ISO forms with offsets, so the SQL order above is only a hint.
    from modules.printers.plate_clear import as_utc
    result, newest = {}, {}
    for printer_id, at, org_id in rows:
        if not check_org_access(actor, org_id):
            continue
        moment = as_utc(at)
        if printer_id not in result or (moment is not None and (newest[printer_id] is None or moment > newest[printer_id])):
            result[printer_id], newest[printer_id] = at, moment
    for printer_id, at in stored_plate_clear_times(db, printer_ids, actor).items():
        moment = as_utc(at)
        if newest.get(printer_id) is None or moment > newest[printer_id]:
            result[printer_id], newest[printer_id] = at, moment
    if not result:
        return result
    # A confirmation stands only until the next print starts: a plate cleared several prints
    # ago says nothing about the bed now.
    # Compared as parsed instants: stored times mix naive UTC and ISO forms, which do not
    # sort correctly as text. Include monitor-tracked prints without a scheduled job.
    # Offset timestamps can have a different date prefix from their UTC instant.
    # The one-day margin before the oldest confirmation keeps this text prefilter
    # inclusive; the parsed comparison below decides which starts invalidate it.
    confirmed = {printer_id: as_utc(at) for printer_id, at in result.items()}
    known = [at for at in confirmed.values() if at is not None]
    if not known:
        return result
    day = (min(known) - timedelta(days=1)).strftime('%Y-%m-%d')
    started = db.execute(text("SELECT printer_id, actual_start FROM jobs WHERE printer_id IN :ids "
        "AND actual_start IS NOT NULL AND actual_start >= :day "
        "UNION ALL SELECT printer_id, started_at FROM print_jobs WHERE printer_id IN :ids "
        "AND started_at IS NOT NULL AND started_at >= :day").bindparams(bindparam('ids', expanding=True)),
        {'ids': list(result), 'day': day})
    for printer_id, raw in started:
        begun, at = as_utc(raw), confirmed.get(printer_id)
        if printer_id in result and at is not None and begun is not None and begun > at:
            del result[printer_id]
    return result


def enrich_printer_facts(db, printers, actor):
    facts = read_facts(db, printers)
    cleared = plate_clear_times(db, [p.id for p in printers], actor)
    from modules.printers.plate_clear import plate_clear_states
    plate_states = plate_clear_states(db, printers, actor)
    for printer in printers:
        printer.plate_clear = plate_states[printer.id]
        observed = facts.get(printer.id, {})
        printer.feeding_slot = observed.get('feeding_slot')
        printer.chamber_temp = observed.get('chamber_temp')
        printer.plate_clear_confirmed_at = cleared.get(printer.id)
    return printers
