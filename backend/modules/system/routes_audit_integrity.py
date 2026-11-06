"""Enterprise audit verification, signed exports, and installation-wide SIEM settings."""
from datetime import date
from typing import Literal
from urllib.parse import urlsplit
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.audit import log_audit
from core.rbac import require_superadmin, require_interactive_session
from license_manager import feature_gate

router = APIRouter()


class Destination(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['webhook', 'syslog']
    endpoint: str = Field(max_length=2000)
    secret: str | None = Field(default=None, min_length=32, max_length=512)
    enabled: bool = False
    tls: bool = True
    ca_bundle: str | None = Field(default=None, max_length=65536)

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
    from modules.system.audit_signed_export import export
    return export(db, date_from, date_to, bundle)


@router.get('/settings/siem', dependencies=[Depends(feature_gate('siem_stream'))])
def destinations(current_user: dict = Depends(require_superadmin(scope='read')), db: Session = Depends(get_db)):
    rows = db.execute(text('''SELECT d.id,d.name,d.kind,d.endpoint,d.enabled,d.tls,d.ca_bundle,d.last_error,d.last_success,
        CASE WHEN d.secret IS NULL THEN 0 ELSE 1 END AS has_secret,
        (SELECT count(*) FROM audit_siem_outbox o WHERE o.destination_id=d.id) AS pending
        FROM audit_siem_destinations d ORDER BY d.name,d.id''')).mappings().all()
    return [dict(row) for row in rows]


@router.put('/settings/siem/{destination_id}', dependencies=[Depends(feature_gate('siem_stream')), Depends(require_interactive_session())])
def save_destination(destination_id: str, body: Destination,
                     current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    # Serialize config/backlog changes with the append trigger's destination snapshot.
    from core.audit_integrity import lock_chain
    lock_chain(db.connection())
    from license_manager import get_license
    license = get_license()
    db.execute(text('UPDATE audit_chain_state SET siem_until=:until WHERE id=1'),
               {'until': license.expiry_deadline.timestamp() if license.expiry_deadline else 0})
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
    db.execute(text('''INSERT INTO audit_siem_destinations(id,name,kind,endpoint,secret,enabled,tls,ca_bundle)
        VALUES(:id,:name,:kind,:endpoint,:secret,:enabled,:tls,:ca_bundle) ON CONFLICT(id) DO UPDATE SET
        name=excluded.name,kind=excluded.kind,endpoint=excluded.endpoint,secret=excluded.secret,
        enabled=excluded.enabled,tls=excluded.tls,ca_bundle=excluded.ca_bundle'''), values)
    log_audit(db, 'siem.configured', details={'destination_id': destination_id, 'kind': body.kind, 'enabled': body.enabled})
    db.commit()
    return {'id': destination_id}


@router.delete('/settings/siem/{destination_id}', dependencies=[Depends(feature_gate('siem_stream')), Depends(require_interactive_session())])
def delete_destination(destination_id: str, current_user: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    from core.audit_integrity import lock_chain
    lock_chain(db.connection())
    db.execute(text('DELETE FROM audit_siem_outbox WHERE destination_id=:id'), {'id': destination_id})
    removed = db.execute(text('DELETE FROM audit_siem_destinations WHERE id=:id'), {'id': destination_id})
    if not removed.rowcount:
        raise HTTPException(404, 'Destination not found')
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
