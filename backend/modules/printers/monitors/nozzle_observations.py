"""Merge last-reported installed nozzles, preserving per-extruder observation time."""
import json
from modules.printers.nozzle_capability import installed_nozzles
from modules.printers.printer_models import bambu_nozzle_count


def observe(monitor, packet):
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    dual = bambu_nozzle_count(getattr(monitor, '_reported_model', None)) == 2 or any(
        row.get('side') in ('left', 'right') for row in pending.values())
    for nozzle in installed_nozzles(packet, dual=dual):
        pending[nozzle['extruder']] = nozzle
    monitor._pending_installed_nozzles = pending


def persist(conn, monitor):
    pending = getattr(monitor, '_pending_installed_nozzles', {})
    if not pending:
        return
    row = conn.execute('SELECT installed_nozzles,machine_type,model FROM printers WHERE id=?', (monitor.printer_id,)).fetchone()
    stored = json.loads(row[0]) if row and isinstance(row[0], str) else (row[0] if row else None)
    merged = {item['extruder']: item for item in stored or []}
    dual = bool(row) and bambu_nozzle_count(row[1] or row[2]) == 2
    for extruder, new in pending.items():
        if dual and new.get('side') == 'single':
            continue
        old = merged.get(extruder, {})
        merged[extruder] = {**old, **{key: value for key, value in new.items() if value is not None}}
    conn.execute('UPDATE printers SET installed_nozzles=? WHERE id=?',
                 (json.dumps(list(merged.values())), monitor.printer_id))
