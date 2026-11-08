"""Browser-bound sign-in; only approved imported identities can become ODIN sessions."""
import hmac
import secrets
from functools import wraps
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.errors import OdinError
from core.db import get_db
from core.rate_limit import limiter
from license_manager import feature_gate
from modules.organizations import education_roster_store as store, education_roster_providers as provider
from modules.organizations.routes_education_roster import private
from modules.organizations.routes_oidc import OIDC_BINDING_COOKIE, oidc_state_binding, oidc_code_key

router = APIRouter(prefix='/auth/classroom', tags=['Classroom sign-in'], dependencies=[
    Depends(feature_gate('roster_sync')), Depends(feature_gate('classes')), Depends(feature_gate('education_workflows')), Depends(private)])
COOKIE = 'odin_roster_bind'


def audited_failure(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except (OdinError, HTTPException) as error:
            db, cid = kwargs['db'], kwargs['connection_id']
            db.rollback()
            org = db.execute(text('SELECT org_id FROM education_roster_connections WHERE id=:c'), {'c': cid}).scalar()
            reason = error.extra.get('reason', 'signin_refused') if isinstance(error, OdinError) else 'signin_refused'
            store.audit(db, 'signin_refused', org, resource=cid, reason=reason)
            db.commit()
            raise
    return wrapped


def begin(db, connection, request, *, actor_id=None, login_hint=None, message_hint=None):
    config = connection['config']
    callback = config['public_origin'] + '/api/auth/classroom/' + connection['id'] + (
        '/callback' if connection['provider'] == 'google' else '/launch')
    # Generate state first, then persist its hash and the browser-bound payload.
    state = secrets.token_urlsafe(32)
    url, payload = provider.authorization(config, callback, state, connect=actor_id is not None,
                                           login_hint=login_hint, message_hint=message_hint)
    payload.update(revision=connection['revision'], connect=actor_id is not None, actor_id=actor_id)
    # receipt() creates its own opaque token; bind authorization to that exact token.
    state = store.receipt(db, connection['id'], 'oauth', payload, actor_id=actor_id)
    from urllib.parse import urlsplit, urlencode
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    params['state'] = [state]
    url = parts._replace(query=urlencode(params, doseq=True)).geturl()
    if actor_id is not None:
        store.audit(db, 'connect_started', connection['org_id'], resource=connection['id'])
    db.commit()
    response = JSONResponse({'authorization_url': url}) if actor_id else RedirectResponse(url, status_code=302)
    response.headers['Cache-Control'] = 'private, no-store'
    response.set_cookie(COOKIE, oidc_state_binding(state), httponly=True, secure=True,
                        samesite='lax' if connection['provider'] == 'google' else 'none', path='/api', max_age=600)
    return response


async def form_fields(request):
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            provider.refuse('roster_launch_too_large', 413)
    try:
        return parse_qs(body.decode(), keep_blank_values=True)
    except (ValueError, UnicodeError):
        provider.refuse('roster_launch_invalid', 422)


@router.post('/{connection_id}/login')
@limiter.limit("30/minute")
async def login_post(connection_id: str, request: Request, db: Session = Depends(get_db)):
    fields = await form_fields(request)
    known = ('iss', 'login_hint', 'target_link_uri', 'lti_message_hint')
    if any(len(fields.get(key, [])) > 1 for key in known):
        provider.refuse('roster_lti_initiation_invalid', 403)
    return initiate(connection_id, request, db=db, **{k: fields.get(k, [None])[0] for k in known})


@router.get('/{connection_id}/login')
@limiter.limit("30/minute")
def login(connection_id: str, request: Request, iss: str | None = None, login_hint: str | None = None,
          target_link_uri: str | None = None, lti_message_hint: str | None = None, db: Session = Depends(get_db)):
    return initiate(connection_id, request, iss, login_hint, target_link_uri, lti_message_hint, db)


def initiate(connection_id, request, iss=None, login_hint=None, target_link_uri=None, lti_message_hint=None, db=None):
    connection = store.load(db, connection_id)
    if connection['provider'] == 'canvas':
        target = connection['config']['public_origin'] + '/api/auth/classroom/' + connection_id + '/launch'
        if iss != connection['config']['issuer'] or not login_hint or len(login_hint) > 1000 or target_link_uri != target:
            provider.refuse('roster_lti_initiation_invalid', 403)
    return begin(db, connection, request, login_hint=login_hint, message_hint=lti_message_hint)


@router.get('/{connection_id}/jwks')
def jwks(connection_id: str, db: Session = Depends(get_db)):
    connection = store.load(db, connection_id)
    if connection['provider'] != 'canvas':
        provider.refuse('roster_connection_not_found', 404)
    return {'keys': [provider.public_jwk(connection['config'])]}


def pending(db, cid, request, state):
    if not state or len(state) > 64 or not hmac.compare_digest(request.cookies.get(COOKIE, ''), oidc_state_binding(state)):
        provider.refuse('roster_browser_binding_invalid', 403)
    connection = store.load(db, cid)
    payload = store.consume(db, state, cid, 'oauth')
    db.commit()  # One-time even on provider failure.
    if payload['revision'] != connection['revision']:
        provider.refuse('roster_configuration_changed')
    return connection, payload


def imported_user(db, connection, subject):
    user = db.execute(text('SELECT u.* FROM users u JOIN education_roster_identities i ON i.user_id=u.id AND i.org_id=u.group_id '
        'WHERE i.connection_id=:c AND i.subject=:s AND u.is_active IS TRUE'),
        {'c': connection['id'], 's': subject}).mappings().first()
    if not user or user['role'] != 'viewer':
        provider.refuse('roster_import_required', 403)
    grant = db.execute(text('SELECT 1 FROM education_cost_center_grants g JOIN education_classes c '
        'ON c.cost_center_id=g.cost_center_id AND c.org_id=g.org_id JOIN education_roster_courses r ON r.class_id=c.id '
        'JOIN education_cost_centers e ON e.id=c.cost_center_id '
        "WHERE r.connection_id=:c AND g.user_id=:u AND g.state='active' AND c.state='active' AND e.state='active'"),
        {'c': connection['id'], 'u': user['id']}).first()
    if not grant:
        provider.refuse('roster_membership_required', 403)
    return user


def finish(db, request, connection, subject):
    user = imported_user(db, connection, subject)
    from core.license_lock import authorize_user
    authorize_user(db, user['id'], issuance=True)
    binding = request.cookies[COOKIE]
    code = store.receipt(db, connection['id'], 'exchange', {
        'subject': subject, 'binding': binding, 'revision': connection['revision']}, actor_id=user['id'], ttl=120)
    store.audit(db, 'identity_verified', connection['org_id'], resource=connection['id'], user_id=user['id'])
    db.commit()
    response = RedirectResponse('/#roster_code=' + code + '&roster_connection=' + connection['id'], status_code=302)
    response.headers['Cache-Control'] = 'private, no-store'
    return response


@router.get('/{connection_id}/callback')
@limiter.limit("30/minute")
@audited_failure
async def callback(connection_id: str, request: Request, state: str = '', code: str = '', db: Session = Depends(get_db)):
    connection, payload = pending(db, connection_id, request, state)
    if connection['provider'] != 'google' or not code or len(code) > 4096:
        provider.refuse('roster_oauth_invalid', 403)
    subject, tokens = await provider.google_callback(connection['config'], payload, code)
    connection = store.load(db, connection_id, lock=True)
    if connection['revision'] != payload['revision']:
        provider.refuse('roster_configuration_changed')
    if payload['connect']:
        # Recheck that the initiating admin still has authority after the external round trip.
        user = db.execute(text("SELECT 1 FROM users WHERE id=:a AND group_id=:o AND role='admin' AND is_active IS TRUE"),
            {'a': payload['actor_id'], 'o': connection['org_id']}).first()
        if not user:
            provider.refuse('roster_admin_required', 403)
        from core.license_lock import authorize_user
        authorize_user(db, payload['actor_id'], issuance=True)
        db.execute(text('UPDATE education_roster_connections SET tokens_encrypted=:t,connected_by=:a WHERE id=:id'),
            {'id': connection_id, 't': store.pack({'refresh_token': tokens['refresh_token']}), 'a': payload['actor_id']})
        store.audit(db, 'connected', connection['org_id'], resource=connection_id, user_id=payload['actor_id'])
        db.commit()
        return RedirectResponse('/settings?tab=integrations', status_code=302)
    return finish(db, request, connection, subject)


@router.post('/{connection_id}/launch')
@limiter.limit("30/minute")
@audited_failure
async def launch(connection_id: str, request: Request, db: Session = Depends(get_db)):
    fields = await form_fields(request)
    if any(len(fields.get(key, [])) != 1 or not fields[key][0] for key in ('state', 'id_token')):
        provider.refuse('roster_launch_invalid', 422)
    connection, payload = pending(db, connection_id, request, fields['state'][0])
    if connection['provider'] != 'canvas':
        provider.refuse('roster_launch_invalid', 403)
    claims, context = await provider.canvas_launch(connection['config'], payload, fields['id_token'][0])
    connection = store.load(db, connection_id, lock=True)
    if connection['revision'] != payload['revision']:
        provider.refuse('roster_configuration_changed')
    contexts = connection['tokens'].get('contexts', {})
    if len(contexts) >= 100 and context['id'] not in contexts:
        provider.refuse('roster_context_limit', 422)
    contexts[context['id']] = context
    db.execute(text('UPDATE education_roster_connections SET tokens_encrypted=:t WHERE id=:id'),
        {'t': store.pack({'contexts': contexts}), 'id': connection_id})
    store.audit(db, 'canvas_context', connection['org_id'], resource=connection_id)
    db.commit()
    # A valid first launch makes its course available for admin preview; it never auto-provisions.
    try:
        return finish(db, request, connection, claims['sub'])
    except OdinError as error:
        if error.extra.get('reason') != 'roster_import_required':
            raise
        db.rollback()
        store.audit(db, 'signin_refused', connection['org_id'], resource=connection_id, reason='roster_import_required')
        db.commit()
        return RedirectResponse('/?error=roster_import_required', status_code=302)


@router.post('/{connection_id}/exchange')
@limiter.limit("30/minute")
@audited_failure
async def exchange(connection_id: str, body: dict, request: Request, db: Session = Depends(get_db)):
    code = body.get('code')
    if not isinstance(code, str) or not 32 <= len(code) <= 64:
        provider.refuse('roster_receipt_invalid', 403)
    connection = store.load(db, connection_id, lock=True)
    payload = store.consume(db, code, connection_id, 'exchange')
    db.commit()
    if (not hmac.compare_digest(payload['binding'], request.cookies.get(COOKIE, ''))
        or payload['revision'] != connection['revision']):
        provider.refuse('roster_browser_binding_invalid', 403)
    user = imported_user(db, connection, payload['subject'])
    from core.license_lock import authorize_user
    authorize_user(db, user['id'], issuance=True)
    # Delegate session/MFA issuance to the established OIDC exchange, with a new
    # internal one-time code. The external receipt carries no pre-issued session.
    from core.auth import create_access_token
    from core.crypto import encrypt
    from modules.organizations.routes_oidc import _oidc_exchange_code
    internal = secrets.token_urlsafe(48)
    binding = payload['binding']
    token = create_access_token(data={'sub': user['username'], 'role': user['role']})
    db.execute(text('INSERT INTO oidc_auth_codes(code,access_token,expires_at) VALUES(:c,:t,:e)'),
        {'c': oidc_code_key(internal, binding), 't': encrypt(token),
         'e': (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()})
    db.commit()
    request.cookies[OIDC_BINDING_COOKIE] = binding
    response = await _oidc_exchange_code({'code': internal}, request, db, audit_method='classroom_' + connection['provider'])
    response.delete_cookie(COOKIE, path='/api')
    response.headers['Cache-Control'] = 'private, no-store'
    return response
