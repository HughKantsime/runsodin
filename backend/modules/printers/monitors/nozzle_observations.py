"""Merge last-reported fixed and rack nozzles by their hardware identifiers."""
import json
from modules.printers.nozzle_capability import installed_nozzles
from modules.printers.printer_models import bambu_model_name, bambu_nozzle_count


def _key(row):
    return row['nozzle_id'] if row.get('side') == 'rack' else row['extruder']


def _merge(old, new):
    return {**old, **{key: value for key, value in new.items() if value is not None}}


def observe(monitor, packet):
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    model = bambu_model_name(getattr(monitor, '_reported_model', None))
    dual = bambu_nozzle_count(model) == 2 or any(
        row.get('side') in ('left', 'right') for row in pending.values())
    for nozzle in installed_nozzles(packet, dual=dual, include_rack=model in (None, 'H3C')):
        key = _key(nozzle)
        pending[key] = _merge(pending.get(key, {}), nozzle)
    monitor._pending_installed_nozzles = pending


def persist(conn, monitor):
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    if not pending:
        return
    row = conn.execute('SELECT installed_nozzles,machine_type,model FROM printers WHERE id=?', (monitor.printer_id,)).fetchone()
    stored = json.loads(row[0]) if row and isinstance(row[0], str) else (row[0] if row else None)
    model = getattr(monitor, '_reported_model', None) or (row[1] or row[2] if row else None)
    rack = bambu_model_name(model) == 'H3C'
    dual = bambu_nozzle_count(model) == 2
    merged = {_key(item): item for item in stored or [] if (item.get('side') == 'rack') == rack}
    for key, new in pending.items():
        if (new['side'] == 'rack') != rack or (dual and new['side'] == 'single'):
            continue
        item = _merge(merged.get(key, {}), new)
        if not rack or item.get('position') is not None:
            merged[key] = item
    if list(merged.values()) != (stored or []):
        conn.execute('UPDATE printers SET installed_nozzles=? WHERE id=?',
                     (json.dumps(list(merged.values())), monitor.printer_id))
    # A new printer can send its rack before get_version identifies the model.
    if not model:
        return {key: item for key, item in pending.items() if item['side'] == 'rack' and item.get('position')}
