"""Closed SQL vocabulary for audit erasure and retention maintenance."""
import re


ERASURE_PREDICATES = frozenset((
    'id=:id', 'id<=:id', 'id>:id', 'actor_user_id=:uid',
    "entity_type = 'user' AND entity_id = :uid",
    "action='auth.login' AND entity_type='user' AND entity_id=:uid",
    "id>:low AND id<=:high AND entity_type='user' AND "
    "action IN ('login_success','login_failed','auth.login_failed')",
))
ERASURE_UPDATES = frozenset((
    'UPDATE audit_logs SET ip_address=NULL WHERE id=:id',
    'UPDATE audit_logs SET ip_address=NULL WHERE id<=:id',
    'UPDATE audit_logs SET ip_address=NULL WHERE id>:id',
    'UPDATE audit_logs SET details=:d WHERE id=:id',
    'UPDATE audit_logs SET details=:d,ip_address=NULL WHERE id=:id',
    "UPDATE audit_logs SET details = :details WHERE entity_type = 'user' AND entity_id = :uid",
))


def erasure_predicate(predicate):
    if predicate in ERASURE_PREDICATES:
        return predicate
    # GDPR batches contain generated bind names only; never literal row IDs.
    if re.fullmatch(r'id IN \(:id[0-9]+(?:,:id[0-9]+)*\)', predicate):
        return predicate
    raise ValueError('Unsupported audit erasure predicate')


def retention_predicate(predicate):
    comparisons = ('timestamp<:cutoff', 'timestamp < :cutoff',
                   'julianday(timestamp) < julianday(:cutoff)')
    if predicate in comparisons:
        return predicate, {}
    # Existing maintenance callers can scope retention to a single row. Bind
    # that value rather than passing even validated numeric SQL through.
    match = re.fullmatch(r'id=([0-9]+) AND (timestamp<:cutoff)', predicate)
    if match:
        return 'id=:retention_id AND timestamp<:cutoff', {'retention_id': int(match[1])}
    raise ValueError('Unsupported audit retention predicate')
