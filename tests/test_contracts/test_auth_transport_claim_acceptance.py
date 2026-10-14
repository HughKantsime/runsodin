"""Actual special-purpose rejection and session-cookie transport assertions."""
import pytest
from core.auth import create_access_token
from tests.test_contracts.test_authenticated_principal_projection_behavior import principal_client,token_client,review_db
from tests.test_contracts.test_session_issuance_behavior import oidc_client,seed_code

@pytest.mark.parametrize('purpose',['ws','mfa_pending','mfa_setup_required'])
def test_special_purpose_bearer_cannot_read_rest_identity(principal_client,purpose):
    client,_=principal_client
    token=create_access_token({'sub':'admin','role':'admin',purpose:True})
    assert client.get('/auth/me',headers={'Authorization':'Bearer '+token}).status_code==401


def test_oidc_exchange_cookie_flags_and_cookie_rest_access(oidc_client,review_db,monkeypatch):
    from core.config import settings
    from modules.organizations import routes_auth
    monkeypatch.setattr(settings,'cookie_secure',True)
    monkeypatch.setattr(settings,'cookie_samesite','strict')
    oidc_client.app.include_router(routes_auth.router)
    response=oidc_client.post('/auth/oidc/exchange',json={'code':seed_code(review_db)})
    assert response.status_code==200
    flags=response.headers['set-cookie'].lower().split(';')[1:]
    assert ' secure' in flags and ' httponly' in flags and ' samesite=strict' in flags
    # HTTPS request exercises secure-cookie transmission via the real cookie jar.
    assert oidc_client.get('https://testserver/auth/me').status_code==200
