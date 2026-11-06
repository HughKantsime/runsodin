"""Tenant-scoped encrypted configuration and short-lived, single-use receipts."""
import hashlib
import base64
import hmac
import json
import os
import secrets
import time
import uuid
from sqlalchemy import text
from core.crypto import encrypt, get_fernet
from core.audit import log_audit
from modules.organizations.education_roster_providers import refuse, https_url, public_jwk


def pack(value):
    return encrypt(json.dumps(value, sort_keys=True, separators=(',', ':')))


def unpack(value):
    if not value:
        return {}
    try:
        return json.loads(get_fernet().decrypt(value.encode()))
    except Exception:
        refuse('roster_encryption_unavailable', 503)


def audit(db, action, org, *, resource=None, reason=None, user_id=None, subject_hash=None):
    log_audit(db, 'roster.' + action, 'education_roster', details={
        'org_id': org, 'resource_id': resource, **({'user_id': user_id} if user_id is not None else {}),
        **({'subject_hash': subject_hash} if subject_hash else {}), **({'reason': reason} if reason else {})})


def load(db, connection_id, org=None, *, enabled=True, lock=False):
    if lock:
        scope = ' AND org_id=:o' if org is not None else ''
        db.execute(text('UPDATE education_roster_connections SET revision=revision WHERE id=:id' + scope),
                   {'id': connection_id, 'o': org})
    row = db.execute(text('SELECT * FROM education_roster_connections WHERE id=:id'), {'id': connection_id}).mappings().first()
    if not row or (org is not None and row['org_id'] != org):
        refuse('roster_connection_not_found', 404)
    if enabled and not row['enabled']:
        refuse('roster_disabled', 409)
    return {**row, 'config': unpack(row['config_encrypted']), 'tokens': unpack(row['tokens_encrypted'])}


def status(row):
    config = unpack(row['config_encrypted'])
    public = {k: v for k, v in config.items() if k not in {'client_secret', 'private_key'}}
    return {'id': row['id'], 'provider': row['provider'], 'enabled': bool(row['enabled']),
            'revision': row['revision'], 'config': public, 'has_credentials': True,
            'connected': bool(row['tokens_encrypted'])}


def save(db, principal, body):
    from modules.organizations.education_classes_service import actor
    org, admin = actor(db, principal, write=True)
    if not admin:
        refuse('roster_admin_required', 403)
    db.execute(text('UPDATE education_roster_connections SET revision=revision WHERE org_id=:o AND provider=:p'),
               {'o': org, 'p': body.provider})
    old = db.execute(text('SELECT * FROM education_roster_connections WHERE org_id=:o AND provider=:p'),
                     {'o': org, 'p': body.provider}).mappings().first()
    config = {**(unpack(old['config_encrypted']) if old else {}), **body.config}
    config['provider'] = body.provider
    if old:
        prior = unpack(old['config_encrypted'])
        namespace_changed = any(prior.get(k) != config.get(k) for k in ('client_id', 'issuer', 'deployment_id'))
        if namespace_changed and db.execute(text('SELECT 1 FROM education_roster_courses WHERE connection_id=:c'), {'c': old['id']}).first():
            refuse('roster_identity_namespace_immutable')
    allowed = {'provider', 'client_id', 'public_origin', 'allowed_domains', 'client_secret'} if body.provider == 'google' else {
        'provider', 'client_id', 'public_origin', 'issuer', 'deployment_id', 'authorization_url',
        'token_url', 'jwks_url', 'api_origin', 'private_key', 'key_id', 'allowed_domains'}
    if set(config) - allowed or any(not config.get(key) for key in allowed):
        refuse('roster_configuration_incomplete', 422)
    if any(not isinstance(v, str) or len(v) > 16000 for k, v in config.items() if k != 'allowed_domains'):
        refuse('roster_configuration_invalid', 422)
    if not isinstance(config['allowed_domains'], list) or not config['allowed_domains'] or any(
        not isinstance(d, str) or '.' not in d or '@' in d or d != d.lower().strip() for d in config['allowed_domains']):
        refuse('roster_domains_invalid', 422)
    from urllib.parse import urlsplit
    for key in ('public_origin', 'authorization_url', 'token_url', 'jwks_url', 'api_origin', 'issuer'):
        if key in config:
            config[key] = https_url(config[key])
    for key in ('public_origin', 'api_origin'):
        if key in config and (urlsplit(config[key]).path or urlsplit(config[key]).query):
            refuse('roster_origin_required', 422)
    if body.provider == 'canvas':
        try:
            public_jwk(config)
        except (ValueError, TypeError):
            refuse('roster_rsa_key_required', 422)
    cid = old['id'] if old else str(uuid.uuid4())
    params = {'id': cid, 'o': org, 'p': body.provider, 'e': body.enabled,
              'c': pack(config), 'a': principal['id'], 'r': body.revision}
    if old:
        credential_keys = ('client_id', 'client_secret') if body.provider == 'google' else (
            'client_id', 'issuer', 'deployment_id', 'authorization_url', 'token_url', 'jwks_url', 'api_origin', 'private_key', 'key_id')
        params['t'] = None if any(prior.get(k) != config.get(k) for k in credential_keys) else old['tokens_encrypted']
        params['connected_by'] = None if params['t'] is None else old['connected_by']
        changed = db.execute(text('UPDATE education_roster_connections SET config_encrypted=:c,enabled=:e,'
            'revision=revision+1,tokens_encrypted=:t,connected_by=:connected_by '
            'WHERE id=:id AND revision=:r'), params)
        if changed.rowcount != 1:
            refuse('roster_configuration_changed')
        db.execute(text('DELETE FROM education_roster_receipts WHERE connection_id=:id'), {'id': cid})
    else:
        if body.revision != 0:
            refuse('roster_configuration_changed')
        db.execute(text('INSERT INTO education_roster_connections(id,org_id,provider,enabled,config_encrypted,created_by) '
                        'VALUES(:id,:o,:p,:e,:c,:a)'), params)
    audit(db, 'configured', org, resource=cid)
    db.commit()
    return status(load(db, cid, org, enabled=False))


def receipt(db, connection_id, kind, payload, actor_id=None, ttl=600):
    token = secrets.token_urlsafe(32)
    purge_expired(db)
    db.execute(text('INSERT INTO education_roster_receipts(id,connection_id,kind,payload_encrypted,actor_id,expires_at) '
        'VALUES(:id,:c,:k,:p,:a,:e)'), {'id': digest(token), 'c': connection_id, 'k': kind,
        'p': pack(payload), 'a': actor_id, 'e': int(time.time()) + ttl})
    return token


def purge_expired(db):
    return db.execute(text('DELETE FROM education_roster_receipts WHERE expires_at<:now'),
                      {'now': int(time.time())}).rowcount


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def subject_digest(connection_id, subject):
    # Domain-separated HMAC: a database copy alone cannot enumerate provider IDs.
    # Use the same installation secret that protects the roster configuration.
    if not get_fernet():
        refuse('roster_encryption_unavailable', 503)
    key = base64.urlsafe_b64decode(os.environ['ENCRYPTION_KEY'])
    message = json.dumps(['odin-roster-erasure-v1', connection_id, subject], separators=(',', ':')).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def consume(db, token, cid, kind, actor_id=None):
    row = db.execute(text('DELETE FROM education_roster_receipts WHERE id=:id AND connection_id=:c '
        'AND kind=:k AND expires_at>=:now RETURNING payload_encrypted,actor_id'),
        {'id': digest(token), 'c': cid, 'k': kind, 'now': int(time.time())}).first()
    # Anonymous OAuth/exchange callers prove browser binding at the HTTP boundary.
    # Administrative previews additionally require the exact initiating actor.
    if not row or (actor_id is not None and row.actor_id != actor_id):
        refuse('roster_receipt_invalid', 409)
    return unpack(row.payload_encrypted)
