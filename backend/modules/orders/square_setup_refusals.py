"""Record authenticated Square setup refusals without retaining request or response text."""
from datetime import datetime, timedelta, timezone

from fastapi import Depends
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select, text
from starlette.exceptions import HTTPException

from core.audit import log_audit
from core.db import get_db
from core.dependencies import get_current_user
from core.models import AuditLog


# Only these codes reach the audit log. Error messages may contain request values.
_REASONS = {
    'Insufficient token scope': 'scope_required',
    'This token needs Square setup': 'scope_required',
    'Insufficient permissions': 'permission_denied',
    'Square setup permission is no longer': 'grant_unavailable',
    'Square setup permission is unavailable in Education': 'education_mode',
    "Feature 'products'": 'license_required',
    'Record who approved': 'approval_note_required',
    'Remove anything that looks': 'approval_note_sensitive',
    'Square connection changed': 'revision_changed',
    'Connect Square first': 'connection_required',
    'This product is already mapped': 'already_mapped',
    'This product already has a stock baseline': 'already_baselined',
    'Map the product before': 'mapping_required',
    'Square shows ': 'count_mismatch',
    'Square has no stock count': 'count_unknown',
    'Square is not tracking': 'untracked',
    'Reconcile unknown additions': 'unknown_additions',
    'The baseline time': 'baseline_time',
    'Enter a baseline time': 'baseline_time',
    'Choose an active Square location': 'inactive_location',
    'This Square variation and location already map': 'variation_in_use',
    'This connection has reached': 'mapping_limit',
}


def reason_code(error):
    from modules.orders.square_client import ITAR_REFUSAL
    if isinstance(error, RequestValidationError):
        return 'invalid_request'
    if error.detail == ITAR_REFUSAL:
        return 'itar_mode'
    for prefix, code in _REASONS.items():
        if isinstance(error.detail, str) and error.detail.startswith(prefix):
            return code
    return {400: 'invalid_request', 403: 'access_denied', 404: 'product_unavailable',
            409: 'setup_conflict', 422: 'invalid_request', 429: 'rate_limit',
            502: 'square_read_failed', 503: 'service_unavailable'}.get(error.status_code, 'request_refused')


def record_refusal(db, token_id, product_id, reason):
    # Discard any partial mapping/baseline transaction before committing an audit.
    db.rollback()
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text("SELECT pg_advisory_xact_lock(1936814693, :token)"), {"token": token_id})
    else:
        db.execute(text("BEGIN IMMEDIATE"))
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = select(AuditLog.id).where(
        AuditLog.action == 'square.setup_refused', AuditLog.entity_id == product_id,
        AuditLog.timestamp > now - timedelta(minutes=1),
        AuditLog.details['token_id'].as_integer() == token_id,
        AuditLog.details['reason'].as_string() == reason).limit(1)
    if db.execute(recent).first() is None:
        row = log_audit(db, 'square.setup_refused', 'product', product_id,
                        {'token_id': token_id, 'product_id': product_id, 'reason': reason})
        row.timestamp = now
    db.commit()


def audit_setup_attempt(product_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    try:
        yield
    except (HTTPException, RequestValidationError) as error:
        if user and user.get('_auth_kind') == 'user_api_token' and type(user.get('_token_id')) is int:
            record_refusal(db, user['_token_id'], product_id, reason_code(error))
        raise
