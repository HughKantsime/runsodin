"""D5 adapter against real credential fencing, journals and filesystem retirement."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.requests import Request
from core.auth import create_access_token
from core.audit_actor import RequestAuditScope
from tests.test_contracts.test_timelapse_lifetime_membership import (
    prepared_source, encoder, frame_writer, review_db, owned_runtime,
)


def test_real_media_retirement_and_idempotent_receipt(prepared_source, monkeypatch):
    from modules.organizations import education_term_media as adapter
    engine, root, tid, principal, nonce = prepared_source
    monkeypatch.setattr(adapter, 'TIMELAPSE_ROOT', root)
    principal = {**principal, '_auth_kind': 'bearer_jwt'}
    with Session(engine) as db:
        username = db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
        token = create_access_token({'sub': username, 'role': 'admin'})
        request = Request({'type': 'http', 'headers': [(b'authorization', ('Bearer ' + token).encode())],
                           'query_string': b'', 'odin.private.audit_actor_scope': RequestAuditScope(None)})
        retire = adapter.media_retirement(request, db, principal)
        db.rollback()
        media = {'id': tid, 'ownership_operation_id': nonce}
        retire(media)
        retire(media)
        assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'), {'id': tid}).scalar() == 0
        assert db.execute(text("SELECT state FROM timelapse_retirements WHERE operation_id=:op"), {'op': nonce}).scalar() == 'complete'
    assert not list(root.glob('frames_*')) and not list(root.glob('encode_*.mp4'))


def test_no_media_credential_fallback(prepared_source, monkeypatch):
    from fastapi import HTTPException
    from modules.organizations.education_term_media import media_retirement
    engine, _, _, principal, _ = prepared_source
    with Session(engine) as db, pytest.raises(HTTPException) as error:
        media_retirement(Request({'type': 'http', 'headers': [], 'query_string': b''}), db,
                         {**principal, '_auth_kind': 'user_api_token'})
    assert error.value.status_code == 403
