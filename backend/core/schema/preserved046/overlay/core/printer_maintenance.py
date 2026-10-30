"""Printer-owned maintenance facts, shared by admission and completion readers."""
import json
from sqlalchemy import text


def maintenance_key(printer_id):
    return 'printer.maintenance.' + str(printer_id)


def read_maintenance(db, printer_id):
    value = db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                       {'key': maintenance_key(printer_id)}).scalar_one_or_none()
    if value is None:
        return {'state': 'ok', 'note': '', 'set_by': None, 'set_by_name': None, 'set_at': None}
    return json.loads(value) if isinstance(value, str) else value


def hold_reason(db, printer_id):
    facts = read_maintenance(db, printer_id)
    if facts['state'] == 'hold':
        return 'printer_maintenance_hold: ' + facts['note']
    return None


def delete_maintenance(db, printer_id):
    db.execute(text('DELETE FROM system_config WHERE key=:key'), {'key': maintenance_key(printer_id)})


def enrich_maintenance(db, printers):
    """Attach maintenance in one query to the already tenant-filtered response."""
    keys = [maintenance_key(p.id) for p in printers]
    if not keys:
        return printers
    from sqlalchemy import bindparam
    rows = db.execute(text('SELECT key,value FROM system_config WHERE key IN :keys')
                      .bindparams(bindparam('keys', expanding=True)), {'keys': keys})
    values = {key: json.loads(value) if isinstance(value, str) else value for key, value in rows}
    for printer in printers:
        printer.maintenance = values.get(maintenance_key(printer.id),
            {'state': 'ok', 'note': '', 'set_by': None, 'set_by_name': None, 'set_at': None})
    return printers
