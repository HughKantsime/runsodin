"""Merge last-reported fixed and rack nozzles by their hardware identifiers."""
import json
from datetime import datetime, timezone
from core.base import slot_filament_value
from core.print_attempts import qmark_runner
from core.tray_identity import advance, observe as observe_tray
from modules.printers.nozzle_capability import installed_nozzles
from modules.printers.telemetry.bambu.ams_identity import reported_color, stored_observation
from modules.printers.printer_models import bambu_model_name, bambu_nozzle_count


_RACK_SLOT_IDS = {str(i) for i in range(247, 256)}


def _key(row):
    return row['nozzle_id'] if row.get('side') == 'rack' else row['extruder']


def _merge(old, new):
    return {**old, **{key: value for key, value in new.items() if value is not None}}


def observe(monitor, packet):
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    model = bambu_model_name(getattr(monitor, '_reported_model', None))
    slots = packet.get('vir_slot')
    if model in (None, 'H3C') and isinstance(slots, list):
        readings = getattr(monitor, '_pending_rack_filaments', {})
        stamp = datetime.now(timezone.utc).isoformat()
        for tray in slots:
            if not isinstance(tray, dict) or str(tray.get('id')) not in _RACK_SLOT_IDS:
                continue
            if not isinstance(tray.get('tray_type'), str):
                continue  # Missing material is not an empty position.
            slot_id = int(tray['id'])
            # The hand-fed positions establish material, preset and colour only.
            fields = {key: tray[key] for key in ('tray_type', 'tray_info_idx', 'tray_color', 'tray_sub_brands')
                      if isinstance(tray.get(key), str)}
            fields['_exists'] = bool(fields['tray_type'])
            readings[slot_id] = (fields, stamp)
        monitor._pending_rack_filaments = readings
    dual = bambu_nozzle_count(model) == 2 or any(
        row.get('side') in ('left', 'right') for row in pending.values())
    for nozzle in installed_nozzles(packet, dual=dual, include_rack=model in (None, 'H3C')):
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
    rack = bambu_model_name(model) == 'H3C'
    dual = bambu_nozzle_count(model) == 2
    if model and not rack and not pending:
        return {}, {}
    merged = {_key(item): item for item in stored or [] if (item.get('side') == 'rack') == rack}
    for key, new in pending.items():
        if (new['side'] == 'rack') != rack or (dual and new['side'] == 'single'):
            continue
        item = _merge(merged.get(key, {}), new)
        if not rack or item.get('position') is not None:
            merged[key] = item
    if rack:
        readings = _persist_filaments(conn, monitor.printer_id, row[3], merged.values(), readings)
    if list(merged.values()) != (stored or []):
        conn.execute('UPDATE printers SET installed_nozzles=? WHERE id=?',
                     (json.dumps(list(merged.values())), monitor.printer_id))
    # A new printer can send its rack before get_version identifies the model.
    if not model:
        return {key: item for key, item in pending.items() if item['side'] == 'rack' and item.get('position')}, readings
    return {}, readings if rack else {}


def _persist_filaments(conn, printer_id, count, nozzles, readings):
    if count == 9 and not readings:
        return {}
    if count != 9:
        conn.execute('UPDATE printers SET slot_count=9 WHERE id=?', (printer_id,))
    rows = conn.execute('SELECT slot_number,id,tray_observation,assigned_spool_id FROM filament_slots WHERE printer_id=?',
                        (printer_id,)).fetchall()
    slots = {number: (slot_id, prior, spool_id) for number, slot_id, prior, spool_id in rows}
    # Older H3Cs may have been created with just one configurable slot.
    for position in range(1, 10):
        if position not in slots:
            slot_id = conn.execute("INSERT INTO filament_slots(printer_id,filament_type,slot_number) "
                                   "VALUES(?,?,?) RETURNING id", (printer_id, slot_filament_value('empty'), position)).fetchone()[0]
            slots[position] = (slot_id, None, None)

    run = qmark_runner(conn)
    unmatched = dict(readings)
    peer_ids = [row.get('virtual_slot_id') for row in nozzles]
    positions = [row['position'] for row in nozzles]
    for row in nozzles:
        peer, position = row.get('virtual_slot_id'), row['position']
        if peer not in readings or peer_ids.count(peer) != 1 or positions.count(position) != 1:
            continue
        tray, stamp = unmatched.pop(peer)
        slot_id, prior, spool_id = slots[position]
        previous = stored_observation(prior)
        if previous.get('rack_filament', {}).get('id') != peer:
            previous = {}
        # Only write the observation. A provisional nozzle pairing cannot move a spool.
        state, departed = advance(previous, tray)
        if spool_id is not None and tray['_exists'] and (departed or not previous):
            state['pending'] = True
            state['hold_id'] = previous.get('hold_id', state['id'])
        state['rack_filament'] = dict(id=peer, type=tray['tray_type'], preset=tray.get('tray_info_idx'),
                                      color=tray.get('tray_color'), observed_at=stamp)
        conn.execute('UPDATE filament_slots SET filament_type=?,color_hex=?,loaded_at=? WHERE id=?',
                     (slot_filament_value(tray['tray_type']), reported_color(tray) if tray['tray_type'] else None, stamp, slot_id))
        observe_tray(run, slot_id, state)
    return unmatched
