"""Target-only checks and batched authenticated GDPR receipts in the same transaction."""
from contextlib import contextmanager
import json
import uuid
import pickle
import tempfile
from sqlalchemy import text
from core.audit_integrity import MUTABLE, enabled, lock_chain, projection, row, finish_insert_seal
from core.audit_receipts import redaction, seal_deletion, deletion_key
from core.audit_verify import accepted
from core.audit_sql import ERASURE_UPDATES, erasure_predicate


@contextmanager
def record_erasure(db, predicate, parameters):
    predicate = erasure_predicate(predicate)
    connection = db.connection()
    if not enabled(connection):
        yield
        return
    lock_chain(connection)
    deletion_key(connection)
    # Spool pre-erasure evidence to an unlinked private file, never a giant ID list.
    # The caller's UPDATE remains atomic with every bounded receipt batch.
    with tempfile.TemporaryFile(mode='w+b') as spool:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- predicate is checked by erasure_predicate; projection uses fixed FIELDS, a. alias and dialect expressions; values are bound
        cursor = connection.execute(text(f'SELECT {projection(connection)} FROM audit_logs a WHERE {predicate}')
            .execution_options(stream_results=True, max_row_buffer=256), parameters).mappings()
        while batch := cursor.fetchmany(256):
            before = [dict(r) for r in batch]
            # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle -- writes only database scalar rows and validity booleans to this process-owned unlinked TemporaryFile; no external pickle bytes
            pickle.dump((before, {r['id']: accepted(connection, r) for r in before}), spool)
        cursor.close()
        yield
        db.flush()
        spool.seek(0)
        while True:
            try:
                # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle -- reads only this process's pickle.dump output from the same unlinked TemporaryFile; no uploaded or named file is opened
                before, validity = pickle.load(spool)
            except EOFError:
                break
            _record_batch(connection, before, validity)


def _record_batch(connection, before, validity):
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- projection uses fixed FIELDS, a. alias and dialect hash expressions; ids use an expanding bind
    after = {r['id']: r for r in connection.execute(text(f'SELECT {projection(connection)} FROM audit_logs a WHERE id IN :ids')
             .bindparams(__import__('sqlalchemy').bindparam('ids', expanding=True)), {'ids': list(validity)}).mappings()}
    changed = [(r, after[r['id']]) for r in before if r['content_hash'] != after[r['id']]['content_hash']]
    if not changed:
        return
    operation = uuid.uuid4().hex
    receipts = []
    for original, current in changed:
        if current['stable_hash'] != original['stable_hash'] or current['row_hash'] != original['row_hash']:
            raise RuntimeError('Erasure changed immutable audit fields')
        if not redaction(original, current):
            raise RuntimeError('Erasure must only redact actor/IP or known detail forms')
    ids = [original['id'] for original, _ in changed]
    connection.execute(text('UPDATE audit_logs SET hash_salt=NULL WHERE id IN :ids')
                       .bindparams(__import__('sqlalchemy').bindparam('ids', expanding=True)), {'ids': ids})
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- projection uses fixed FIELDS, a. alias and dialect hash expressions; ids use an expanding bind
    after = {r['id']: r for r in connection.execute(text(f'SELECT {projection(connection)} FROM audit_logs a WHERE id IN :ids')
             .bindparams(__import__('sqlalchemy').bindparam('ids', expanding=True)), {'ids': ids}).mappings()}
    # The record itself binds all signed transitions to this erasure operation.
    record_id = connection.execute(text("INSERT INTO audit_logs(action,entity_type,details) "
        "VALUES('audit.erasure','audit_log',:details) RETURNING id"),
        {'details': json.dumps({'operation': operation, 'rows': len(changed),
                              'integrity_breaks': [i for i in ids if not validity[i]]})}).scalar_one()
    record_before = row(connection, record_id)
    for original, current in changed:
        receipt = seal_deletion({'row_id': original['id'], 'record_id': record_id, 'operation': operation,
                        'before_valid': validity[original['id']], 'pre_erasure_row_hash': original['row_hash'],
                        'after_content_hash': after[original['id']]['content_hash'],
                        'stable_hash': original['stable_hash'],
                        'changed_fields': sorted(name for name in MUTABLE if current[name] != original[name])}, connection)
        receipts.append({'id': original['id'], 'record': record_id, 'evidence': json.dumps(receipt, sort_keys=True)})
    connection.execute(text('INSERT INTO audit_redactions(row_id,record_id,evidence) VALUES(:id,:record,:evidence) '
        'ON CONFLICT(row_id) DO UPDATE SET record_id=excluded.record_id,evidence=excluded.evidence'), receipts)
    # Finalize our own just-appended record before any other append, using the
    # same transactional seal as ORM actor stamping. Export carries the receipts.
    details = {'operation': operation, 'rows': len(changed),
               'integrity_breaks': [i for i in ids if not validity[i]],
               'receipts': [json.loads(r['evidence']) for r in receipts],
               'unsealed': any(json.loads(r['evidence']).get('unsealed') for r in receipts)}
    connection.execute(text('UPDATE audit_logs SET details=:d WHERE id=:id'),
                       {'d': json.dumps(details, sort_keys=True), 'id': record_id})
    from types import SimpleNamespace
    finish_insert_seal(None, connection, SimpleNamespace(id=record_id, _odin_insert_seal=record_before))


def rewrite(db, statement, parameters):
    """Reject SQL outside the closed, source-owned erasure statement registry."""
    if statement not in ERASURE_UPDATES:
        raise ValueError('Unsupported audit erasure update')
    predicate = statement.split(' WHERE ', 1)[1]
    with record_erasure(db, predicate, parameters):
        return db.execute(text(statement), parameters)


def erase_actor_history(db, user_id):
    from core.audit_actor import erase_actor_history as erase
    with record_erasure(db, 'actor_user_id=:uid', {'uid': user_id}):
        erase(db, user_id)
