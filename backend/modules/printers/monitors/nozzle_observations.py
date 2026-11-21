"""Merge last-reported fixed and rack nozzles by their hardware identifiers."""
import json
from datetime import datetime, timezone
from core.base import slot_filament_value
from core.display_json import display_tray_state
from core.monitor_observations import fresh_report
from modules.printers.nozzle_capability import installed_nozzles, rack_slot_id
from modules.printers.printer_models import bambu_model_name, bambu_nozzle_count


_RACK_SLOT_IDS = {str(i) for i in range(247, 256)}


def _key(row):
    return row['nozzle_id'] if row.get('side') == 'rack' else row['extruder']


def _merge(old, new):
    return {**old, **{key: value for key, value in new.items() if value is not None}}


def observe(monitor, packet, status, now):
    age = getattr(status, 'report_age_seconds', None)
    received_at = getattr(status, 'mqtt_received_at', None)
    if received_at is not None and age is None:
        age = now - received_at
    fresh = fresh_report(getattr(status, 'mqtt_retained', False), age)
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    model = bambu_model_name(getattr(monitor, '_reported_model', None))
    slots = packet.get('vir_slot')
    if fresh and isinstance(slots, list):
        readings = getattr(monitor, '_pending_rack_filaments', {})
        stamp = datetime.fromtimestamp(now - (age or 0), timezone.utc).isoformat()
        for tray in slots:
            if not isinstance(tray, dict) or str(tray.get('id')) not in _RACK_SLOT_IDS:
                continue
            slot_id = int(tray['id'])
            fields = {field: tray[key] for key, field in (
                ('tray_type', 'type'), ('tray_info_idx', 'preset'), ('tray_color', 'color'))
                if isinstance(tray.get(key), str)}
            latest, pending_fields = readings.get(slot_id, ('', {}))
            # A coalesced delta must not refresh the age of its omitted fields.
            if fields and stamp >= latest:
                pending_fields.update({key: (stamp, value) for key, value in fields.items()})
                readings[slot_id] = (stamp, pending_fields)
        monitor._pending_rack_filaments = readings
    dual = bambu_nozzle_count(model) == 2 or any(
        row.get('side') in ('left', 'right') for row in pending.values())
    for nozzle in installed_nozzles(packet, dual=dual, include_rack=fresh):
        key = _key(nozzle)
        pending[key] = _merge(pending.get(key, {}), nozzle)
    monitor._pending_installed_nozzles = pending


def persist(conn, monitor):
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    readings = getattr(monitor, '_pending_rack_filaments', {})
    if not pending and not readings:
        return {}, {}
    row = conn.execute('SELECT installed_nozzles,machine_type,model,slot_count FROM printers WHERE id=?', (monitor.printer_id,)).fetchone()
    if not row:
        return {}, {}
    stored = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    model = getattr(monitor, '_reported_model', None) or row[1] or row[2]
    rack = any(bambu_model_name(value) == 'H3C' for value in (model, row[1], row[2]))
    dual = bambu_nozzle_count(model) == 2
    merged = {_key(item): item for item in stored or [] if (item.get('side') == 'rack') == rack}
    for key, new in pending.items():
        if (new['side'] == 'rack') != rack or (dual and new['side'] == 'single'):
            continue
        item = _merge(merged.get(key, {}), new)
        if not rack or item.get('position') is not None:
            merged[key] = item
    if rack:
        _persist_filaments(conn, monitor.printer_id, row[3], readings)
    if list(merged.values()) != (stored or []):
        conn.execute('UPDATE printers SET installed_nozzles=? WHERE id=?',
                     (json.dumps(list(merged.values())), monitor.printer_id))
    # A new printer can send its rack before get_version identifies the model.
    if not rack:
        candidates = {key: item for key, item in pending.items() if item['side'] == 'rack' and item.get('position')}
        if candidates or not getattr(monitor, '_reported_model', None):
            return candidates, readings
    return {}, {}


def _persist_filaments(conn, printer_id, count, readings):
    if count == 9 and not readings:
        return
    if count != 9:
        conn.execute('UPDATE printers SET slot_count=9 WHERE id=?', (printer_id,))
    rows = conn.execute('SELECT slot_number,id,tray_observation FROM filament_slots WHERE printer_id=?',
                        (printer_id,)).fetchall()
    slots = {number: (slot_id, prior) for number, slot_id, prior in rows}
    for position in range(1, 10):
        if position not in slots:
            slot_id = conn.execute("INSERT INTO filament_slots(printer_id,filament_type,slot_number) "
                                   "VALUES(?,?,?) RETURNING id", (printer_id, slot_filament_value('empty'), position)).fetchone()[0]
            slots[position] = (slot_id, None)
        raw_id = rack_slot_id(position)
        reading = readings.get(raw_id)
        if reading:
            slot_id, prior = slots[position]
            stamp, fields = reading
            previous = display_tray_state(prior).get('rack_filament')
            if not isinstance(previous, dict) or previous.get('id') != raw_id:
                previous = {}
            saved_at = previous.get('observed_at') or ''
            if stamp >= saved_at:
                changes = {key: value for key, (at, value) in fields.items() if at >= saved_at}
                state = {'rack_filament': {**previous, **changes, 'id': raw_id, 'observed_at': stamp}}
                conn.execute('UPDATE filament_slots SET tray_observation=? WHERE id=?', (json.dumps(state), slot_id))
