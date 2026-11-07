"""SIEM stays inside the customer's trusted boundary; canonical rows are not redacted."""
from sqlalchemy import text


def lock_policy(db):
    # Keep this lock before job/config locks. Using the audit-chain lock here
    # would invert ordinary job writers' job -> audit ordering on PostgreSQL.
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed internal mutex key/value, no external input
    db.execute(text("INSERT INTO system_config(key,value) VALUES('controlled:siem_policy_lock','0') ON CONFLICT(key) DO NOTHING"))
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed internal mutex key, no external input
    db.execute(text("UPDATE system_config SET value=value WHERE key='controlled:siem_policy_lock'"))


def restrictions_exist(db):
    from core.controlled_cache import request_cache
    from core.controlled_parts import any_read_restrictions
    # Recheck after the policy lock; middleware may have cached the old state.
    cache = request_cache(db)
    if cache is not None:
        cache.pop('_controlled_any', None)
        cache.pop('_controlled_history', None)
    return any_read_restrictions(db)


def target(destination):
    """Bind trust to the receiver and transport, including credentials/CA changes."""
    import hashlib
    import json
    values = [destination[key] for key in ('endpoint', 'kind', 'tls', 'ca_bundle', 'secret')]
    return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode()).hexdigest()


def acknowledgement(db, destination):
    import json
    from core.system_config_values import config_text
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed config lookup with a bound destination key
    value = db.execute(text('SELECT value FROM system_config WHERE key=:key'),
                       {'key': 'controlled:siem_ack:' + destination['id']}).scalar()
    try:
        saved = json.loads(config_text(value))
        if isinstance(saved, dict) and saved.get('target') == target(destination):
            return {'user_id': saved['user_id'], 'acknowledged_at': saved['acknowledged_at']}
    except (ValueError, TypeError, KeyError):
        pass
    return None


def clear_acknowledgement(db, destination_id):
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed config deletion with a bound destination key
    db.execute(text('DELETE FROM system_config WHERE key=:key'),
               {'key': 'controlled:siem_ack:' + destination_id})


def store_acknowledgement(db, destination, actor):
    import json
    from datetime import datetime, timezone
    from core.system_config_values import write_config_text
    saved = {'user_id': actor['id'], 'acknowledged_at': datetime.now(timezone.utc).isoformat()}
    write_config_text(db, 'controlled:siem_ack:' + destination['id'],
                      json.dumps({**saved, 'target': target(destination)}))
    return saved


def require_acknowledged(db, destination):
    if acknowledgement(db, destination) is not None:
        return
    from fastapi import HTTPException
    from core.audit import audit_download
    code = 'controlled_part_siem_acknowledgement_required'
    # A refusal may persist its audit, never caller-staged policy/config writes.
    db.rollback()
    audit_download(db, 'controlled_part.refused', 'system', None,
                   {'reason': code, 'destination_id': destination['id']})
    raise HTTPException(403, {'code': code, 'destination_id': destination['id'],
                             'message': f"A superadmin must acknowledge SIEM destination {destination['name']}."})


def first_mark(db):
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed enabled-destination lookup with no external input
    for destination in db.execute(text('SELECT * FROM audit_siem_destinations WHERE enabled=1 ORDER BY id')).mappings():
        require_acknowledged(db, destination)
