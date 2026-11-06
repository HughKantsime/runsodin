"""Audited acceptance of a restored history; it never repairs row corruption."""
import time
from fastapi import HTTPException
from core.audit import log_audit
from core.audit_keys import file_lock, keyring
from core.audit_integrity import verify, lock_chain
from core.audit_heads import advance_head, continuity


def accept_baseline(db, principal, reason):
    with file_lock('.audit-daily.lock'):
        connection = db.connection()
        lock_chain(connection)
        result = verify(connection)
        if result.get('status') == 'audit_key_missing':
            raise HTTPException(409, 'audit_key_missing: recover the original keyring first')
        if any(not continuity(b) for b in result['breaks']):
            raise HTTPException(409, 'Restore baseline cannot accept row or checkpoint corruption')
        marker = result.get('pending_restore')
        if not marker:
            raise HTTPException(409, 'An unaccepted restore_completed marker newer than the signed anchor is required')
        keyring(create=True, connection=connection)
        details = {'actor_user_id': principal['id'], 'reason': reason,
                   'head': result['head'], 'rows': result['rows'], 'breaks': result['breaks'],
                   'restore_record_id': marker['record_id'], 'restore_row_hash': marker['row_hash']}
        record = log_audit(db, 'audit.restore_baseline_accepted', 'audit_log', details=details)
        db.flush()
        # Include the acceptance record in the new anchor. Commit before external
        # persistence: a crash can leave acceptance pending, never falsely clean.
        updated = verify(connection)
        from sqlalchemy import text
        from core.audit_receipts import seal
        import json
        updated['checked_at'] = time.time()
        clean = {**updated, 'ok': True, 'breaks': [], 'first_break': None, 'reason': None,
                 'incremental': False, 'full_checked_at': updated['checked_at']}
        record_id = record.id
        db.commit()
        # The external history contains only the audit reference, never free-text
        # reasons or actor identities that GDPR erasure must be able to scrub.
        advance_head(updated, accepted={'record_id': record_id, 'restore_row_hash': marker['row_hash']})
        # Publish healthy status only after the external baseline is durable.
        db.execute(text('INSERT INTO audit_integrity_status(id,checked_at,result) VALUES(1,:now,:result) '
                        'ON CONFLICT(id) DO UPDATE SET checked_at=excluded.checked_at,result=excluded.result'),
                   {'now': clean['checked_at'], 'result': json.dumps(seal(clean))})
        db.commit()
        return {'status': 'accepted', 'head': updated['head'], 'rows': updated['rows']}
