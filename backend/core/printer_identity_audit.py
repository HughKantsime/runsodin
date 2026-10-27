"""Atomic, change-only audit for non-secret printer identity observations."""
import json

from sqlalchemy import text
from core.dispatch_observation_executor import TransactionView


def observe_printer_identity(connection, printer_id, field, value, *, only_unset=False):
    """Keep the write and audit in the caller's transaction, including on failure.

    The write lock serializes observation against legacy backfill and user edits.
    The savepoint prevents a caller that catches errors from committing a change
    whose audit insert failed. Only these non-secret fields may enter details.
    """
    if field not in ('nozzle_diameter', 'model', 'machine_type'):
        raise ValueError('Not an observed printer identity field')
    db = TransactionView(connection)
    db.execute(text('UPDATE printers SET id=id WHERE id=:id'), {'id': printer_id})
    db.execute(text('SAVEPOINT printer_identity_observation'))
    try:
        row = db.execute(text(f'SELECT {field} FROM printers WHERE id=:id'),
                         {'id': printer_id}).fetchone()
        changed = row is not None and row[0] != value
        if changed and only_unset and row[0] not in (None, '', 'Unknown'):
            changed = False
        if changed:
            db.execute(text(f'UPDATE printers SET {field}=:value WHERE id=:id'),
                       {'value': value, 'id': printer_id})
            db.execute(text('''INSERT INTO audit_logs(action,entity_type,entity_id,details)
                VALUES ('printer.observed_config_changed','printer',:id,:details)'''),
                {'id': printer_id, 'details': json.dumps({'field': field, 'old': row[0], 'new': value})})
    except Exception:
        db.execute(text('ROLLBACK TO SAVEPOINT printer_identity_observation'))
        db.execute(text('RELEASE SAVEPOINT printer_identity_observation'))
        raise
    db.execute(text('RELEASE SAVEPOINT printer_identity_observation'))
    return changed
