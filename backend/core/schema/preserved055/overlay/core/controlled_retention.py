"""Internal, narrowly scoped authority for expired controlled timelapse retirement."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from core.controlled_parts import policies

_PROOF = object()


class RetentionPrincipal(dict):
    def __init__(self, identifier, nonce, *, proof):
        if proof is not _PROOF:
            raise ValueError('Internal retention authority required')
        super().__init__(id=0, role='admin', group_id=None)
        self.identifier = identifier
        self.nonce = nonce


def expired(db, identifier, *, now=None):
    row = db.execute(text('SELECT print_job_id,completed_at,created_at,status,ownership_operation_id '
                          'FROM timelapses WHERE id=:id'), {'id': identifier}).first()
    if row is None or row.status not in ('ready', 'failed', 'deleting'):
        return False
    rules = policies(db, 'job', row.print_job_id)
    captured_file = db.execute(text('SELECT print_file_id FROM timelapse_ownership_anchors WHERE operation_id=:nonce'), {'nonce': row.ownership_operation_id}).scalar()
    rules += policies(db, 'file', captured_file)
    if not rules or not (row.completed_at or row.created_at):
        return False
    from core.controlled_policy_clock import media_deadline
    deadlines = [media_deadline(r, row.completed_at or row.created_at) for r in rules]
    return min(deadlines) <= (now or datetime.now(timezone.utc))


def retention_principal(db, identifier, nonce):
    if not expired(db, identifier) or not preview_acknowledged(db, identifier):
        raise ValueError('Controlled timelapse is not due for retirement')
    saved = db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'), {'id': identifier}).scalar()
    if saved != nonce:
        raise ValueError('Controlled timelapse identity changed')
    return RetentionPrincipal(identifier, nonce, proof=_PROOF)


def check_retention(db, principal, identifier=None):
    if type(principal) is not RetentionPrincipal:
        return False
    if identifier is not None and identifier != principal.identifier:
        raise ValueError('Retention authority names another timelapse')
    retention_principal(db, principal.identifier, principal.nonce)
    return True


def preview_acknowledged(db, identifier):
    row = db.execute(text('SELECT print_job_id,ownership_operation_id FROM timelapses WHERE id=:id'), {'id': identifier}).first()
    if not row:
        return False
    file_id = db.execute(text('SELECT print_file_id FROM timelapse_ownership_anchors WHERE operation_id=:nonce'), {'nonce': row.ownership_operation_id}).scalar()
    rules = policies(db, 'job', row.print_job_id) + policies(db, 'file', file_id)
    return bool(rules) and all(r['purge_previewed_at'] for r in rules)


def pending_notice(db):
    """Persistent admin notice until every due policy has been acknowledged."""
    from core.controlled_parts import any_marks, tombstone_job_ids
    if not any_marks(db) and not tombstone_job_ids(db):
        return []
    from core.controlled_retention_query import pending_count
    due = pending_count(db)
    if not due:
        return []
    return [{'kind': 'controlled_retention_pending', 'severity': 'warning', 'dismissible': False,
             'title': 'Controlled media retention needs acknowledgement',
             'detail': f'{due} due timelapse(s) await retention preview and acknowledgement.'}]
