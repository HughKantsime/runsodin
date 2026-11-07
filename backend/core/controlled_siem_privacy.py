"""Personal standing-trust records; erasure preserves the approved receiver hash."""
import json
from sqlalchemy import text
from core.system_config_values import config_text, write_config_text


def personal_acknowledgements(db, user_id):
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed exact config prefix and ordering without external input; user identity is compared after JSON decoding
    rows = db.execute(text("SELECT key,value FROM system_config WHERE substr(key,1,20)='controlled:siem_ack:' ORDER BY key"))
    for key, value in rows:
        try:
            saved = json.loads(config_text(value))
        except (ValueError, TypeError):
            continue
        if isinstance(saved, dict) and saved.get('user_id') == user_id:
            yield key, saved


def export_acknowledgements(db, user_id):
    return [{'destination_id': key.removeprefix('controlled:siem_ack:'),
             'user_id': saved['user_id'], 'acknowledged_at': saved.get('acknowledged_at')}
            for key, saved in personal_acknowledgements(db, user_id)]


def erase_acknowledgements(db, user_id):
    from core.controlled_siem import lock_policy
    # Use the same lock order as destination saves, before other erasure writes.
    lock_policy(db)
    for key, saved in list(personal_acknowledgements(db, user_id)):
        write_config_text(db, key, json.dumps({**saved, 'user_id': None}))
