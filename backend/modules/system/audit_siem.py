"""Durable at-least-once SIEM delivery. Network I/O never owns an audit writer lock."""
import json
import time
import uuid
from datetime import datetime, timezone
from sqlalchemy import text
from core.audit_integrity import canonical_projection
from modules.system.audit_siem_transport import send, Transport


def payload(row):
    return json.dumps(dict(row), sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')


def deliver_once(engine, *, now=None, sender=send, destination_id=None):
    from license_manager import get_license
    if not get_license().has_feature('siem_stream'):
        return False
    now = time.time() if now is None else now
    token = uuid.uuid4().hex
    with engine.begin() as connection:
        candidate = connection.execute(text('''SELECT o.destination_id,o.audit_id FROM audit_siem_outbox o
            JOIN audit_siem_destinations d ON d.id=o.destination_id
            WHERE d.enabled=1 AND d.next_attempt<=:now AND d.lease_until<=:now
            AND (:destination IS NULL OR d.id=:destination) AND o.next_attempt<=:now AND o.lease_until<=:now
            ORDER BY o.next_attempt,o.audit_id LIMIT 1'''), {'now': now, 'destination': destination_id}).mappings().first()
        if not candidate:
            return False
        key = dict(candidate)
        if connection.execute(text('UPDATE audit_siem_destinations SET lease_until=:until,lease_token=:token '
            'WHERE id=:destination_id AND lease_until<=:now AND next_attempt<=:now'),
            {**key, 'until': now+60, 'token': token, 'now': now}).rowcount != 1:
            return False
        claimed = connection.execute(text('''UPDATE audit_siem_outbox SET lease_until=:until,lease_token=:token
            WHERE destination_id=:destination_id AND audit_id=:audit_id AND lease_until<=:now'''),
            {**key, 'until': now+60, 'token': token, 'now': now})
        if claimed.rowcount != 1:
            return False
        destination = dict(connection.execute(text('SELECT * FROM audit_siem_destinations WHERE id=:id'),
                                              {'id': key['destination_id'], 'token': token}).mappings().one())
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- canonical_projection uses fixed FIELDS, a. alias and dialect timestamp expressions; audit id is bound
        row = connection.execute(text(f'SELECT {canonical_projection(connection)} FROM audit_logs a WHERE id=:id'), {'id': key['audit_id']}).mappings().first()
        body = payload(row) if row else None
    error = None
    try:
        if body is None:
            raise ValueError('Audit event unavailable')
        sender(destination, body, str(key['audit_id']))
    except Exception as exc:
        # Driver messages can contain URLs, credentials and payloads; retain only class.
        error = type(exc).__name__
    with engine.begin() as connection:
        params = {**key, 'token': token}
        owned = connection.execute(text('''SELECT attempts FROM audit_siem_outbox
            WHERE destination_id=:destination_id AND audit_id=:audit_id AND lease_token=:token'''), params).first()
        if not owned:
            connection.execute(text('UPDATE audit_siem_destinations SET lease_until=0,lease_token=NULL WHERE id=:destination_id AND lease_token=:token'), params)
            return True
        if error:
            attempts = destination['failures']+1
            connection.execute(text('''UPDATE audit_siem_outbox SET attempts=:attempts,next_attempt=:next,
                lease_until=0,lease_token=NULL WHERE destination_id=:destination_id AND audit_id=:audit_id AND lease_token=:token'''),
                {**params, 'attempts': attempts, 'next': now+min(3600, 5*2**min(attempts, 10))})
            connection.execute(text('UPDATE audit_siem_destinations SET last_error=:error,failures=:failures,next_attempt=:next,lease_until=0,lease_token=NULL WHERE id=:id AND lease_token=:token'),
                               {'error': error, 'failures': attempts, 'next': now+min(3600, 5*2**min(attempts, 10)), 'id': key['destination_id'], 'token': token})
        else:
            connection.execute(text('DELETE FROM audit_siem_outbox WHERE destination_id=:destination_id AND audit_id=:audit_id AND lease_token=:token'), params)
            connection.execute(text('UPDATE audit_siem_destinations SET last_error=NULL,last_success=:now,failures=0,next_attempt=0,lease_until=0,lease_token=NULL WHERE id=:id AND lease_token=:token'),
                {'now': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'id': key['destination_id'], 'token': token})
    return True


def deliver_batch(engine, destination_id, limit=50):
    """One worker per receiver; a failed receiver backs off its entire queue."""
    transport = Transport()
    try:
        for _ in range(limit):
            if not deliver_once(engine, destination_id=destination_id, sender=transport):
                break
    finally:
        transport.close()


def licensed_destinations(engine):
    from license_manager import get_license
    license = get_license()
    licensed = license.has_feature('siem_stream')
    deadline = license.expiry_deadline.timestamp() if licensed and license.expiry_deadline else 0
    with engine.begin() as connection:
        # Shared by every writer process on both providers; no trigger file I/O.
        connection.execute(text('UPDATE audit_chain_state SET siem_until=:until WHERE id=1 AND siem_until!=:until'),
                           {'until': deadline})
        return list(connection.execute(text('SELECT id FROM audit_siem_destinations WHERE enabled=1')).scalars()) if licensed else []
