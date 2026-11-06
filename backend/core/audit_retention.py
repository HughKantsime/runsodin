"""Tier-independent retention: authenticated deleted-range checkpoints, no rehash."""
import json
import uuid
from sqlalchemy import text
from core.audit_integrity import enabled, lock_chain, projection
from core.audit_verify import accepted
from core.audit_receipts import seal_deletion, deletion_key, authentic
from core.audit_keys import saved_head

BATCH_SIZE = 256


def retain(db, expired, cutoff):
    c = db.connection()
    expired = '('+expired+") AND action NOT IN ('audit.erasure','audit.retention'," \
              "'restore_completed','audit.restore_baseline_accepted','audit.keyring_initialized')"
    if not enabled(c):
        return db.execute(text('DELETE FROM audit_logs WHERE '+expired), {'cutoff': cutoff}).rowcount
    lock_chain(c)
    signing_available = deletion_key(c)
    operation = uuid.uuid4().hex
    range_count = 0
    # Receipts/checkpoint audit records remain evidence, never personal plaintext.
    # Keyset pages have fixed memory and bind counts, even for millions of rows.
    upper = c.execute(text('SELECT head_seq FROM audit_chain_state WHERE id=1')).scalar_one()
    last, count = 0, 0
    try:
        anchor = saved_head()
    except (ValueError, OSError):
        anchor = None
    if anchor and not authentic(anchor):
        anchor = None
    while True:
        rows = c.execute(text(f'SELECT {projection(c)} FROM audit_logs a WHERE '+expired+
            " AND chain_seq>:last AND chain_seq<=:upper"
            " AND NOT EXISTS (SELECT 1 FROM audit_siem_gaps g WHERE g.gap_id=a.id AND g.closed=0)"
            " ORDER BY chain_seq LIMIT :limit"),
            {'cutoff': cutoff, 'last': last, 'upper': upper, 'limit': BATCH_SIZE}).mappings().all()
        if not rows:
            c.execute(text("INSERT INTO audit_logs(action,entity_type,details) VALUES('audit.retention','audit_log',:d)"),
                      {'d': json.dumps(seal_deletion({'operation': operation, 'deleted': count, 'ranges': range_count,
                                        'unsealed': not signing_available,
                                        'reason': None if signing_available else 'audit_key_missing'}, c))})
            return count
        ranges = []
        for row in rows:
            valid = accepted(c, row)
            if ranges and row['chain_seq'] == ranges[-1]['last_seq']+1 and row['prev_hash'] == ranges[-1]['last_hash']:
                ranges[-1].update(last_seq=row['chain_seq'], last_hash=row['row_hash'],
                                  before_valid=ranges[-1]['before_valid'] and valid)
            else:
                ranges.append({'first_seq': row['chain_seq'], 'last_seq': row['chain_seq'],
                               'prev_hash': row['prev_hash'], 'last_hash': row['row_hash'],
                               'before_valid': valid, 'anchors': {}})
            if anchor and row['chain_seq'] == anchor['rows'] and row['row_hash'] == anchor['head']:
                ranges[-1]['anchors'][str(anchor['rows'])] = anchor['head']
        range_count += len(ranges)
        c.execute(text('INSERT INTO audit_retention_checkpoints(first_seq,evidence) VALUES(:seq,:evidence)'),
                  [{'seq': r['first_seq'], 'evidence': json.dumps(seal_deletion({**r, 'operation': operation}, c))} for r in ranges])
        for r in ranges:
            params = {'first': r['first_seq'], 'last': r['last_seq']}
            for table, column in (('audit_siem_outbox', 'audit_id'), ('audit_redactions', 'row_id'), ('audit_siem_gaps', 'gap_id')):
                c.execute(text(f'DELETE FROM {table} WHERE {column} IN '
                    '(SELECT id FROM audit_logs WHERE chain_seq BETWEEN :first AND :last)'), params)
            c.execute(text('DELETE FROM audit_logs WHERE chain_seq BETWEEN :first AND :last'), params)
        last = rows[-1]['chain_seq']
        count += len(rows)
