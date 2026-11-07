"""Versioned audit hashing, snapshot verification, and final actor sealing.

No database is opened at import. Canonical values are database text, prefixed by
UTF-8 byte length (NULL is -1:). Details retain their exact stored JSON text.
"""
import hashlib
import json
import sqlite3
import time
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine

FIELDS = ('id', 'timestamp', 'actor_user_id', 'actor_username', 'actor_auth_kind',
          'action', 'entity_type', 'entity_id', 'details', 'ip_address', 'prev_hash', 'hash_salt')
MUTABLE = frozenset(('actor_user_id', 'actor_username', 'actor_auth_kind', 'details', 'ip_address'))
GENESIS = hashlib.sha256(b'odin-audit-chain-v1:genesis').hexdigest()


def timestamp_expression(pg, prefix=''):
    value = prefix+'timestamp'
    if pg:
        return f"to_char({value},'YYYY-MM-DD HH24:MI:SS.US')"
    return f"CASE WHEN length({value})=19 THEN {value}||'.000000' ELSE substr(replace({value},'T',' ')||'000000',1,26) END"


def canonical_projection(connection, prefix='a.'):
    columns = (*FIELDS, 'row_hash', 'chain_seq')
    return ', '.join((timestamp_expression(connection.dialect.name == 'postgresql', prefix)+' AS timestamp'
                      if f == 'timestamp' else f'CAST({prefix}details AS TEXT) AS details'
                      if f == 'details' else prefix+f) for f in columns)


def content_hash(row, fields=FIELDS):
    data = 'odin-audit-v1:'
    for field in fields:
        value = row[field]
        data += '-1:' if value is None else str(len(str(value).encode()))+':'+str(value)
    return hashlib.sha256(data.encode()).hexdigest()


def hash_expression(pg, prefix='', fields=FIELDS):
    parts = ["'odin-audit-v1:'"]
    for field in fields:
        value = (timestamp_expression(pg, prefix) if field == 'timestamp'
                 else f'CAST({prefix}{field} AS TEXT)')
        length = f"octet_length({value})" if pg else f"length(CAST({value} AS BLOB))"
        parts.append(f"CASE WHEN {prefix}{field} IS NULL THEN '-1:' ELSE CAST({length} AS TEXT)||':'||{value} END")
    data = '||'.join(parts)
    return f"encode(sha256(convert_to({data},'UTF8')),'hex')" if pg else f'odin_audit_sha256({data})'


def _siem_licensed():
    # Maintenance and license API calls refresh this process-local signed state.
    # SQLite UDFs must never read files or abort a business transaction.
    try:
        import license_manager
        license = license_manager._cached_license
        deadline = getattr(license, 'expiry_deadline', None)
        if deadline is not None and time.time() >= deadline.timestamp():
            return 0
        return int(license is not None and license.has_feature('siem_stream'))
    except Exception:
        return 0


def install_sqlite(connection, *_):
    if isinstance(connection, sqlite3.Connection):
        connection.create_function('odin_siem_licensed', 0, _siem_licensed)
        connection.create_function('odin_audit_sha256', 1,
                                   lambda value: hashlib.sha256(value.encode('utf-8')).hexdigest(),
                                   deterministic=True)


# Covers SQLAlchemy test/maintenance engines as well as the production factory.
if not event.contains(Engine, 'connect', install_sqlite):
    event.listen(Engine, 'connect', install_sqlite)


def enabled(connection):
    return inspect(connection).has_table('audit_chain_state')


def projection(connection):
    pg = connection.dialect.name == 'postgresql'
    stable = tuple(f for f in FIELDS if f not in MUTABLE and f != 'hash_salt')
    return f'{canonical_projection(connection)}, {hash_expression(pg, "a.")} AS content_hash, {hash_expression(pg, "a.", stable)} AS stable_hash'


def row(connection, row_id):
    return connection.execute(text(f'SELECT {projection(connection)} FROM audit_logs a WHERE id=:id'),
                              {'id': row_id}).mappings().one()


def lock_chain(connection):
    # SQLite obtains its database writer lock; PG serializes on this singleton.
    connection.execute(text('UPDATE audit_chain_state SET head_seq=head_seq WHERE id=1'))


def capture_insert_seal(mapper, connection, entry):
    entry._odin_insert_seal = row(connection, entry.id) if enabled(connection) else None


def finish_insert_seal(mapper, connection, entry):
    """Seal actor stamping before another ORM INSERT; preserve the original hook."""
    before = getattr(entry, '_odin_insert_seal', None)
    if before is None:
        return
    after = row(connection, entry.id)
    if before['row_hash'] != before['content_hash']:
        raise RuntimeError('Audit insert seal differs')
    if after['content_hash'] != before['content_hash']:
        changed = connection.execute(text('UPDATE audit_chain_state SET head_hash=:new '
            'WHERE id=1 AND head_seq=:seq AND head_hash=:old'),
            {'new': after['content_hash'], 'seq': before['chain_seq'], 'old': before['row_hash']})
        if changed.rowcount != 1:
            raise RuntimeError('Audit actor sealing lost append ownership')
        connection.execute(text('UPDATE audit_logs SET row_hash=:hash WHERE id=:id'),
                           {'hash': after['content_hash'], 'id': entry.id})


def _details(value):
    return json.loads(value) if isinstance(value, str) else value


def verify(connection, start=None):
    """Walk the caller's snapshot; never acquire a writer lock.

    Production callers use audit_snapshot.snapshot. Transactional tests and
    redaction callers may inspect their own pending writes explicitly.
    """
    from core.audit_verify import walk
    return walk(connection, start)
