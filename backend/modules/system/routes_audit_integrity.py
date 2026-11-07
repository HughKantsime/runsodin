"""Enterprise audit verification, signed exports, and installation-wide SIEM settings."""
from datetime import date
from typing import Literal
from urllib.parse import urlsplit
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.audit import log_audit
from core.controlled_csrf import require_controlled_ack
from core.rbac import require_superadmin, require_interactive_session
from license_manager import feature_gate

router = APIRouter()


class Destination(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['webhook', 'syslog']
    endpoint: str = Field(max_length=2000)
    secret: str | None = Field(default=None, min_length=32, max_length=512)
    enabled: bool = False
    tls: bool = True
    ca_bundle: str | None = Field(default=None, max_length=65536)
    acknowledge_controlled: bool = False

    @model_validator(mode='after')
    def validate_endpoint(self):
        try:
            parsed = urlsplit(self.endpoint)
            port = parsed.port
            if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or any(ord(c) < 33 for c in self.endpoint)
                    or parsed.scheme != ('https' if self.kind == 'webhook' else 'syslog')
                    or (port is not None and not 1 <= port <= 65535)
                    or (self.kind == 'syslog' and parsed.path not in ('', '/'))):
                raise ValueError()
        except ValueError:
            raise ValueError('Use https://host/path or syslog://host:port without credentials or query strings') from None
        if self.ca_bundle:
            import ssl
            try:
                ssl.create_default_context(cadata=self.ca_bundle)
            except (ValueError, ssl.SSLError):
                raise ValueError('CA bundle must contain valid PEM certificates') from None
        return self


def require_save_ack(body: Destination, request: Request):
    if body.acknowledge_controlled:
        require_controlled_ack(request)


@router.get('/audit-logs/verify', dependencies=[Depends(feature_gate('audit_integrity'))])
def verify_audit(current_user: dict = Depends(require_superadmin(scope='read')), db: Session = Depends(get_db)):
    from core.audit_integrity import verify
    from core.audit_snapshot import snapshot
    with snapshot(db.get_bind()) as connection:
        result = verify(connection)
    log_audit(db, 'audit.verified', details=result)
    db.commit()
    result['siem_gap_observations'] = [dict(row) for row in db.execute(text(
        'SELECT * FROM audit_siem_gaps ORDER BY gap_id DESC LIMIT 100')).mappings()]
    return result


@router.get('/export/audit-logs/jsonl', dependencies=[Depends(feature_gate('audit_integrity')), Depends(feature_gate('audit_export'))])
def signed_export(date_from: date, date_to: date, bundle: bool = False, current_user: dict = Depends(require_superadmin(scope='read')),
                  db: Session = Depends(get_db)):
    from core.controlled_exports import acknowledge_export
    acknowledge_export(db, current_user, False, 'audit_jsonl')
    from modules.system.audit_signed_export import export
    return export(db, date_from, date_to, bundle)


@router.post('/export/audit-logs/jsonl/acknowledge', dependencies=[
    Depends(feature_gate('audit_integrity')), Depends(feature_gate('audit_export')),
    Depends(require_interactive_session()), Depends(require_controlled_ack)])
def acknowledged_signed_export(date_from: date, date_to: date, bundle: bool = False,
                               current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    from core.controlled_exports import acknowledge_export
    acknowledge_export(db, current_user, True, 'audit_jsonl')
    from modules.system.audit_signed_export import export
    return export(db, date_from, date_to, bundle)


@router.get('/settings/siem', dependencies=[Depends(feature_gate('siem_stream'))])
def destinations(current_user: dict = Depends(require_superadmin(scope='read')), db: Session = Depends(get_db)):
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant destination/status projection without external input; secrets are removed before returning
    rows = db.execute(text('''SELECT d.id,d.name,d.kind,d.endpoint,d.enabled,d.tls,d.ca_bundle,d.secret,d.last_error,d.last_success,
        CASE WHEN d.secret IS NULL THEN 0 ELSE 1 END AS has_secret,
        (SELECT count(*) FROM audit_siem_outbox o WHERE o.destination_id=d.id) AS pending
        FROM audit_siem_destinations d ORDER BY d.name,d.id''')).mappings().all()
    from core.controlled_siem import acknowledgement
    result = []
    for row in rows:
        item = dict(row)
        item['controlled_acknowledgement'] = acknowledgement(db, row)
        item.pop('secret')
        result.append(item)
    return result


@router.put('/settings/siem/{destination_id}', dependencies=[
    Depends(feature_gate('siem_stream')), Depends(require_interactive_session()), Depends(require_save_ack)])
def save_destination(destination_id: str, body: Destination,
                     current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    from core.controlled_siem import lock_policy
    lock_policy(db)
    # Serialize config/backlog changes with the append trigger's destination snapshot.
    from core.audit_integrity import lock_chain
    lock_chain(db.connection())
    if destination_id == 'new':
        destination_id = uuid.uuid4().hex
    elif len(destination_id) != 32 or any(c not in '0123456789abcdef' for c in destination_id):
        raise HTTPException(422, 'Invalid destination ID')
    prior = db.execute(text('SELECT * FROM audit_siem_destinations WHERE id=:id'), {'id': destination_id}).mappings().first()
    secret = prior['secret'] if prior and prior['kind'] == body.kind else None
    if body.secret:
        from core.crypto import encrypt
        secret = encrypt(body.secret)
    if body.kind == 'webhook' and not secret:
        raise HTTPException(422, 'HTTPS destinations require a signing secret of at least 32 characters')
    values = {'id': destination_id, 'name': body.name, 'kind': body.kind, 'endpoint': body.endpoint,
              'secret': secret if body.kind == 'webhook' else None, 'enabled': int(body.enabled), 'tls': int(body.tls), 'ca_bundle': body.ca_bundle}
    # Endpoint changes never redirect pending evidence to an unreviewed receiver.
    if prior and (prior['endpoint'], prior['kind'], prior['tls']) != (body.endpoint, body.kind, int(body.tls)):
        if db.execute(text('SELECT 1 FROM audit_siem_outbox WHERE destination_id=:id LIMIT 1'), values).first():
            raise HTTPException(409, 'Drain pending deliveries or delete this destination before changing its endpoint')
    from core.controlled_siem import restrictions_exist, require_acknowledged, clear_acknowledgement, store_acknowledgement, target
    if body.enabled and not body.acknowledge_controlled and restrictions_exist(db):
        require_acknowledged(db, values)
    from license_manager import get_license
    license = get_license()
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed singleton update with a bound verified-license deadline, after acknowledgement validation
    db.execute(text('UPDATE audit_chain_state SET siem_until=:until WHERE id=1'),
               {'until': license.expiry_deadline.timestamp() if license.expiry_deadline else 0})
    if prior and target(prior) != target(values):
        clear_acknowledgement(db, destination_id)
    db.execute(text('''INSERT INTO audit_siem_destinations(id,name,kind,endpoint,secret,enabled,tls,ca_bundle)
        VALUES(:id,:name,:kind,:endpoint,:secret,:enabled,:tls,:ca_bundle) ON CONFLICT(id) DO UPDATE SET
        name=excluded.name,kind=excluded.kind,endpoint=excluded.endpoint,secret=excluded.secret,
        enabled=excluded.enabled,tls=excluded.tls,ca_bundle=excluded.ca_bundle'''), values)
    if body.acknowledge_controlled:
        store_acknowledgement(db, values, current_user)
        log_audit(db, 'controlled_part.siem_acknowledged', 'system', None,
                  {'destination_id': destination_id, 'acknowledged': True})
    log_audit(db, 'siem.configured', details={'destination_id': destination_id, 'kind': body.kind, 'enabled': body.enabled})
    db.commit()
    return {'id': destination_id}


@router.post('/settings/siem/{destination_id}/acknowledge', dependencies=[
    Depends(feature_gate('siem_stream')), Depends(require_interactive_session()), Depends(require_controlled_ack)])
def acknowledge_destination(destination_id: str, current_user: dict = Depends(require_superadmin()),
                            db: Session = Depends(get_db)):
    from core.controlled_siem import lock_policy, store_acknowledgement
    lock_policy(db)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed destination lookup; destination id is bound
    destination = db.execute(text('SELECT * FROM audit_siem_destinations WHERE id=:id'),
                             {'id': destination_id}).mappings().first()
    if destination is None:
        raise HTTPException(404, 'Destination not found')
    result = store_acknowledgement(db, destination, current_user)
    log_audit(db, 'controlled_part.siem_acknowledged', 'system', None,
              {'destination_id': destination_id, 'acknowledged': True})
    db.commit()
    return result


@router.delete('/settings/siem/{destination_id}', dependencies=[Depends(feature_gate('siem_stream')), Depends(require_interactive_session())])
def delete_destination(destination_id: str, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    from core.controlled_siem import lock_policy, clear_acknowledgement
    lock_policy(db)
    from core.audit_integrity import lock_chain
    lock_chain(db.connection())
    db.execute(text('DELETE FROM audit_siem_outbox WHERE destination_id=:id'), {'id': destination_id})
    removed = db.execute(text('DELETE FROM audit_siem_destinations WHERE id=:id'), {'id': destination_id})
    if not removed.rowcount:
        raise HTTPException(404, 'Destination not found')
    clear_acknowledgement(db, destination_id)
    log_audit(db, 'siem.deleted', details={'destination_id': destination_id, 'pending_deliveries_discarded': True})
    db.commit()
    return {'status': 'ok'}


class RestoreBaseline(BaseModel):
    reason: str = Field(min_length=10, max_length=1000)

    @model_validator(mode='after')
    def nonblank_reason(self):
        self.reason = self.reason.strip()
        if len(self.reason) < 10:
            raise ValueError('Provide a restore acceptance reason of at least ten characters')
        return self


@router.post('/audit-logs/accept-restore-baseline',
             dependencies=[Depends(feature_gate('audit_integrity')), Depends(require_interactive_session())])
def accept_restore_baseline(body: RestoreBaseline, current_user: dict = Depends(require_superadmin()),
                            db: Session = Depends(get_db)):
    from modules.system.audit_restore_baseline import accept_baseline
    return accept_baseline(db, current_user, body.reason)


@router.post('/audit-logs/initialize-keyring',
             dependencies=[Depends(feature_gate('audit_integrity')), Depends(require_interactive_session())])
def initialize_audit_keyring(body: RestoreBaseline, current_user: dict = Depends(require_superadmin()),
                            db: Session = Depends(get_db)):
    from modules.system.audit_keyring_recovery import initialize_keyring
    return initialize_keyring(db, current_user, body.reason)
