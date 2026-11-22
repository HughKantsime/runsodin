"""Merge last-reported fixed and rack nozzles by their hardware identifiers."""
import json
from datetime import datetime, timezone
from core.base import slot_filament_value
from core.display_json import display_tray_state
from core.monitor_observations import fresh_report
from modules.printers.nozzle_capability import installed_nozzles, rack_rows_with_mounted_tool, rack_slot_id
from modules.printers.printer_models import bambu_model_name, bambu_nozzle_count, rack_model_identity, rack_nozzle_ids


_RACK_SLOT_IDS = {str(i) for i in range(247, 256)}
_RACK_TRAY_IDS = {str(i) for i in range(9)}


def _key(row):
    return row['nozzle_id'] if row.get('side') == 'rack' else row['extruder']


def _merge(old, new):
    return {**old, **{key: value for key, value in new.items() if value is not None}}


def _complete_rack_ids(mask, ids, mounted=None):
    if type(mask) is not int or mask <= 0 or not ids or max(ids) > 24:
        return set()
    if mounted is not None:
        # The mounted tool occupies bit 0 in place of its rack bit.
        expected = 1 + sum(1 << i for i in ids if i != mounted)
        return ids if len(ids) == 9 and mask == expected else set()
    # A delta can omit the H2C's fixed nozzles even when its mask includes them.
    return ids if sum((mask >> i) & 1 for i in ids) == mask.bit_count() else set()


def observe(monitor, packet, status, runtime):
    now = runtime.time.time()
    age = getattr(status, 'report_age_seconds', None)
    received_at = getattr(status, 'mqtt_received_at', None)
    if received_at is not None and age is None:
        age = now - received_at
    fresh = fresh_report(getattr(status, 'mqtt_retained', False), age)
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    model = bambu_model_name(getattr(monitor, '_reported_model', None))
    device = packet.get('device')
    nozzle = device.get('nozzle') if isinstance(device, dict) else None
    info = nozzle.get('info') if isinstance(nozzle, dict) else None
    mask = nozzle.get('exist') if isinstance(nozzle, dict) else None
    info, mounted = rack_rows_with_mounted_tool(info)
    rack_rows = set()
    if fresh and isinstance(info, list) and all(isinstance(row, dict) and 'id' in row for row in info):
        rack_rows = rack_nozzle_ids(info)
    ids = _complete_rack_ids(mask, rack_rows, mounted)
    # Without a known model, rack-only reports cannot establish the absence of a fixed nozzle.
    rack_only_mask = bool(rack_rows) and type(mask) is int and mask >= 0 and (bool(ids) or not (mask & 3))
    stored_models = getattr(monitor, '_stored_models', None)
    stored_rack = getattr(monitor, '_rack_rows_stored', False)
    if stored_models is None and (rack_only_mask or stored_rack):
        # Before the first heartbeat even get_version may not have supplied a model.
        try:
            with runtime.get_db() as conn:
                row = conn.execute('SELECT model,machine_type FROM printers WHERE id=?', (monitor.printer_id,)).fetchone()
            if row:
                monitor._stored_models = stored_models = tuple(row)
        except Exception:
            ids = set()  # The stored identity could not be verified for this report.
    if stored_models is None:
        stored_models = ('H3C',) if getattr(monitor, '_configured_h3c', False) else ()
    model_code = packet.get('printer_type') or monitor._state.get('printer_type')
    identity = rack_model_identity(model, model_code, getattr(status, 'printer_type', None), *stored_models)
    fixed_report = fresh and isinstance(info, list) and any(
        isinstance(r, dict) and type(r.get('id')) is int and r['id'] < 16 for r in info)
    if identity is False and ids and not getattr(monitor, '_reported_model', None):
        # get_version may correct an operator-entered model. Keep this fresh reading
        # for that explicit identification, without applying it to the fixed printer.
        monitor._unconfirmed_nozzle_report = ({'device': {'nozzle': nozzle}, 'vir_slot': packet.get('vir_slot'),
                                               'ams': packet.get('ams')},
                                              now - (age or 0))
    elif fixed_report:
        monitor._unconfirmed_nozzle_report = None
    if identity is False or fixed_report:
        # Withdraw a previous rack-only identification as soon as a fixed
        # extruder appears. An empty set is a pending correction until commit.
        if getattr(monitor, '_rack_rows_stored', False) or getattr(monitor, '_pending_rack_ids', None):
            monitor._pending_rack_ids = set()
        monitor._rack_rows_stored = False
    rack = identity if identity is not None else (
        getattr(monitor, '_rack_rows_stored', False) or ids or getattr(monitor, '_pending_rack_ids', None))
    if rack:
        pending = {key: row for key, row in pending.items() if row['side'] == 'rack'}
    elif getattr(monitor, '_pending_rack_filaments', None) or any(r['side'] == 'rack' for r in pending.values()):
        pending = {key: row for key, row in pending.items() if row['side'] != 'rack'}
        monitor._pending_rack_filaments = {}
    if fresh and rack:
        readings = getattr(monitor, '_pending_rack_filaments', {})
        stamp = datetime.fromtimestamp(now - (age or 0), timezone.utc).isoformat()
        for slot_id, fields in _rack_filament_fields(packet).items():
            latest, pending_fields = readings.get(slot_id, ('', {}))
            # A coalesced delta must not refresh the age of its omitted fields.
            if fields and stamp >= latest:
                pending_fields.update({key: (stamp, value) for key, value in fields.items()})
                readings[slot_id] = (stamp, pending_fields)
        monitor._pending_rack_filaments = readings
    dual = bambu_nozzle_count(model) == 2 or any(
        row.get('side') in ('left', 'right') for row in pending.values())
    for nozzle in installed_nozzles(packet, dual=dual, include_rack=bool(fresh and rack)):
        key = _key(nozzle)
        if nozzle['side'] == 'rack':
            pending[key] = _merge(pending.get(key, {}), nozzle)
        elif not rack and not (identity is None and rack_only_mask):
            pending[key] = nozzle
    monitor._pending_installed_nozzles = pending
    if ids and identity is None and not getattr(monitor, '_rack_rows_stored', False):
        monitor._pending_rack_ids = ids
    pending_ids = getattr(monitor, '_pending_rack_ids', None)
    if pending_ids is not None:
        try:
            with runtime.get_db() as conn:
                persist(conn, monitor, first_rack=pending_ids)
                conn.commit()
        except Exception:
            runtime.log.warning('[%s] Rack observation will retry on the next report', monitor.name)
        else:
            monitor._pending_rack_ids = None
            monitor._rack_rows_stored = bool(pending_ids)


def persist(conn, monitor, *, first_rack=None):
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    readings = getattr(monitor, '_pending_rack_filaments', {})
    if not pending and not readings and first_rack is None:
        return {}
    row = conn.execute('SELECT installed_nozzles,machine_type,model,slot_count FROM printers WHERE id=?', (monitor.printer_id,)).fetchone()
    if not row:
        return {}
    stored = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    identified = rack_model_identity(getattr(monitor, '_reported_model', None), monitor._state.get('printer_type'),
                                     row[1], row[2])
    if identified is False:
        first_rack = None
    stored_rack = bool(rack_nozzle_ids(stored)) and not any(
        item['side'] != 'rack' for item in pending.values())
    if first_rack and stored_rack:
        return readings
    rack = identified if identified is not None else (stored_rack if first_rack is None else bool(first_rack))
    dual = bambu_nozzle_count(row[1] or row[2]) == 2
    merged = {_key(item): item for item in stored or [] if (item.get('side') == 'rack') == rack}
    for key, new in pending.items():
        if (new['side'] == 'rack') != rack or (dual and new['side'] == 'single'):
            continue
        item = _merge(merged.get(key, {}), new)
        if not rack or item.get('position') is not None or (first_rack and key in first_rack):
            merged[key] = item
    if first_rack:
        # Count reported nozzles even when their addresses have no known display mapping.
        for identifier in sorted(first_rack):
            merged.setdefault(identifier, {'side': 'rack', 'nozzle_id': identifier})
    if identified and not first_rack:
        _persist_filaments(conn, monitor.printer_id, row[3], readings)
    if not rack or list(merged.values()) != (stored or []):
        conn.execute('UPDATE printers SET installed_nozzles=? WHERE id=?',
                     (json.dumps(list(merged.values())), monitor.printer_id))
    return readings if rack and not identified else {}


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


def _rack_filament_fields(packet):
    def fields(tray):
        return {field: tray[key] for key, field in (
            ('tray_type', 'type'), ('tray_info_idx', 'preset'), ('tray_color', 'color'))
            if isinstance(tray.get(key), str)}

    slots = packet.get('vir_slot')
    readings = {}
    if isinstance(slots, list):
        for tray in slots:
            if isinstance(tray, dict) and str(tray.get('id')) in _RACK_SLOT_IDS:
                readings.setdefault(int(tray['id']), {}).update(fields(tray))
    ams = packet.get('ams')
    units = ams.get('ams') if isinstance(ams, dict) else None
    if not isinstance(units, list):
        return readings
    for unit in units:
        if not isinstance(unit, dict) or str(unit.get('id')) != '32' or not isinstance(unit.get('tray'), list):
            continue
        for tray in unit['tray']:
            if not isinstance(tray, dict) or str(tray.get('id')) not in _RACK_TRAY_IDS:
                continue
            slot_id = rack_slot_id(int(tray['id']) + 1)
            reported = fields(tray)
            if reported:
                readings[slot_id] = reported
            elif type(tray.get('state')) is int and tray['state'] == 0:
                # An explicit unload clears the AMS reading, exposing any hand-fed fields.
                readings[slot_id] = {'type': '', 'preset': '', 'color': '', **readings.get(slot_id, {})}
    return readings
