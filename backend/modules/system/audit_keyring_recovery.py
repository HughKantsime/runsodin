"""Explicit, audited recovery when the original audit signing keys are lost."""
from fastapi import HTTPException
from sqlalchemy import text
from core.audit import log_audit
from core.audit_integrity import lock_chain
from core.audit_keys import atomic_json, directory, file_lock, missing_key, new_keyring
from core.base import AlertSeverity, AlertType
from modules.notifications.models import Alert


def initialize_keyring(db, principal, reason):
    with file_lock('.audit-daily.lock'):
        connection = db.connection()
        lock_chain(connection)
        with file_lock('.audit-key.lock'):
            if not missing_key(connection):
                raise HTTPException(409, 'Initialization requires audit_key_missing; restore the original keys when available')
            ring = new_keyring()
            details = {'actor_user_id': principal['id'], 'reason': reason,
                       'key_id': ring['active'], 'original_keys_unavailable': True,
                       'earlier_evidence': 'unverified'}
            log_audit(db, 'audit.keyring_initialized', 'audit_log', details=details)
            for user_id in db.execute(text("SELECT id FROM users WHERE role='admin' AND group_id IS NULL AND is_active IS TRUE")).scalars():
                db.add(Alert(user_id=user_id, alert_type=AlertType.AUDIT_INTEGRITY,
                             severity=AlertSeverity.CRITICAL, title='Audit keyring initialized after key loss',
                             message='Replacement audit keys were authorized. Earlier evidence remains unverified; investigate the key loss.',
                             metadata_json=details))
            # Persist the chained authorization and alerts BEFORE publishing keys.
            # A failed commit cannot silently enable signing. A failed file write
            # leaves the installation missing keys and permits an audited retry.
            db.commit()
            atomic_json(directory() / '.audit-hmac-keys.json', ring)
            # Keep the old head/status/receipts: new keys cannot authenticate them.
            return {'status': 'initialized', 'key_id': ring['active'], 'earlier_evidence': 'unverified'}
