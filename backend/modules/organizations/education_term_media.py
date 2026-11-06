"""Bridge to the existing credential-bound, sealed timelapse retirement provider."""
from pathlib import Path
from fastapi import HTTPException
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.orm import Session
from core.interfaces.timelapse_media import require_timelapse_media_provider
from core.timelapse_request_budget import VerificationBudget
from core.timelapse_media_errors import NoCompletedReceiptYet, RetirementUnavailable

TIMELAPSE_ROOT = Path('/data/timelapses')


def media_retirement(request, db, principal):
    provider = require_timelapse_media_provider()
    kind = principal.get('_auth_kind')
    if kind == 'session_jwt':
        carrier, credential = 'cookie', request.cookies.get('session')
    elif kind == 'bearer_jwt':
        scheme, credential = get_authorization_scheme_param(request.headers.get('authorization'))
        if scheme.lower() != 'bearer':
            raise HTTPException(401, 'Not authenticated')
        carrier = 'bearer'
    else:
        raise HTTPException(403, 'Interactive session required')
    evidence = provider.capture_action_credential(request, db, credential, kind=kind,
        carrier=carrier, purpose='retire', selected=principal)
    engine = db.get_bind()

    def retire(media):
        nonce = media['ownership_operation_id']
        if not nonce:
            raise HTTPException(409, 'Timelapse ownership unconfirmed')
        try:
            try:
                provider.sealed_lifetime_receipt(engine, evidence, operation_id=nonce, original_id=media['id'])
                return
            except NoCompletedReceiptYet:
                pass
            with Session(engine) as admission:
                provider.authorize_retirement(admission, evidence, media['id'], nonce)
            budget = VerificationBudget()
            provider.prepare_request_lifetime_retirement(engine, TIMELAPSE_ROOT, media['id'], evidence,
                expected_nonce=nonce, budget=budget)
            provider.retire_sealed_lifetime(engine, TIMELAPSE_ROOT, media['id'], evidence,
                expected_nonce=nonce, budget=budget)
            provider.sealed_lifetime_receipt(engine, evidence, operation_id=nonce, original_id=media['id'])
        except RetirementUnavailable:
            raise HTTPException(409, 'Timelapse retirement could not be confirmed') from None
    return retire
