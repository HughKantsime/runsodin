"""Retained recipe identities and conservative, serialized legacy evidence."""
from datetime import datetime, timezone
import re
import math
from sqlalchemy import text
from fastapi import HTTPException

REFUSAL = ('Printer configuration history cannot be verified for this legacy receipt. '
           'Qualify a new recipe revision from a new dispatch.')


def utc(value):
    value = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def unchanged_since(db, printer_id, created_at):
    """Equal timestamps refuse too: audit timestamps may only have second precision.

    All printer CRUD edits are printer.updated; older generic edit actions and
    delete/recreate cycles also refuse. Identity-changing telemetry is audited;
    ordinary temperature/AMS sync does not edit the stable identity.
    Missing or malformed times never establish historical equality.
    """
    rows = db.execute(text('''SELECT timestamp FROM audit_logs
        WHERE entity_type='printer' AND entity_id=:printer
        AND (action LIKE 'printer.%' OR action IN ('update','delete','create'))'''),
        {'printer': printer_id}).scalars()
    try:
        created = utc(created_at)
        if created > datetime.now(timezone.utc):
            return False
        return all(utc(stamp) < created for stamp in rows)
    except (TypeError, ValueError, OverflowError):
        return False


def legacy_nozzle_matches(db, job_id, printer_id):
    """Use retained sliced metadata to detect pre-audit nozzle changes.

    Unknown metadata is deliberately allowed, but cannot prove historical
    equality. This is additional evidence, never a replacement for the audit.
    Called while the printer is locked by recipe_configuration_key.
    """
    row = db.execute(text('''SELECT f.nozzle_diameter, p.nozzle_diameter
        FROM jobs j LEFT JOIN print_files f ON f.id=j.print_file_id
        JOIN printers p ON p.id=:printer WHERE j.id=:job'''),
        {'printer': printer_id, 'job': job_id}).first()
    if row is None:
        return False
    def known(value):
        try:
            number = float(value)
            return number if math.isfinite(number) and number > 0 else None
        except (TypeError, ValueError, OverflowError):
            return None
    sliced, current = map(known, row)
    return sliced is None or current is None or sliced == current


def receipt_key(db, claim_token):
    from core.interfaces.printer_binding import require_printer_binding_provider
    row = db.execute(text('''SELECT printer_id,job_id,stable_configuration_key,stable_key_source,created_at
        FROM dispatch_attempt_receipts WHERE claim_token=:token'''), {'token':claim_token}).mappings().one_or_none()
    if row is None:
        raise HTTPException(409, REFUSAL)
    key = row['stable_configuration_key']
    if key is not None:
        if re.fullmatch('[0-9a-f]{64}', key) and row['stable_key_source'] in ('dispatch','backfilled_no_printer_audit'):
            return key
        raise HTTPException(409, REFUSAL)
    if row['stable_key_source'] is not None:
        raise HTTPException(409, REFUSAL)
    # Locks the printer through the audit check, current credential read and
    # receipt update. Printer edits also acquire this row's write lock.
    key = require_printer_binding_provider().recipe_configuration_key(
        db, row['printer_id'], since=row['created_at'])
    if key is None or not legacy_nozzle_matches(db, row['job_id'], row['printer_id']):
        raise HTTPException(409, REFUSAL)
    result = db.execute(text('''UPDATE dispatch_attempt_receipts
        SET stable_configuration_key=:key,stable_key_source='backfilled_no_printer_audit'
        WHERE claim_token=:token AND stable_configuration_key IS NULL AND stable_key_source IS NULL'''),
        {'key':key,'token':claim_token})
    if result.rowcount != 1:
        # Never infer a value if another writer won; let a fresh request retry.
        raise HTTPException(409, REFUSAL)
    return key
