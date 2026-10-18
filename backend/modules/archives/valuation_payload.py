"""Bounded database transfer after caller-owned locking and ownership proof.

This private helper provides no authorization and must never back a route alone.
"""
from sqlalchemy import text
from modules.archives.valuation_projection import MAX_SNAPSHOT_BYTES


def load_bounded_snapshot(db, *, attempt_id, birth_nonce, identity_uid):
    if type(attempt_id) is not int or attempt_id <= 0 or not isinstance(birth_nonce, str) or not isinstance(identity_uid, str):
        raise ValueError('Invalid valuation attempt binding')
    size = ("octet_length(convert_to(spool_snapshot,'UTF8'))" if db.get_bind().dialect.name == 'postgresql'
            else 'length(CAST(spool_snapshot AS BLOB))')
    # The same statement binds the previously proved lifetime and rejects an
    # oversized value before the driver transfers the payload into Python.
    row = db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- only a constant dialect size expression is interpolated; values are bound
        f'''SELECT spool_snapshot FROM print_jobs
        WHERE id=:id AND economic_birth_nonce=:birth AND economic_uid=:uid
        AND (spool_snapshot IS NULL OR {size} <= :budget)'''),
        {'id': attempt_id, 'birth': birth_nonce, 'uid': identity_uid, 'budget': MAX_SNAPSHOT_BYTES}).first()
    if row is None:
        raise ValueError('Valuation snapshot unavailable or changed')
    return row[0]
