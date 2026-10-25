"""Installation identity uses the authenticated administrator endpoint."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


@pytest.mark.parametrize('carrier', ['jwt','api'])
@pytest.mark.parametrize('role', ['admin','viewer'])
def test_installation_identity_requires_live_admin_role(token_client, review_db, monkeypatch, carrier, role):
    from core.auth import create_access_token
    from modules.system import routes_health
    client, issue = token_client
    client.app.include_router(routes_health.router)
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'), {'role':role})
    review_db.commit()
    calls=[]
    monkeypatch.setattr(routes_health, 'get_installation_id', lambda:calls.append('owned') or 'owned-synthetic-installation')
    headers = issue(['admin'] if role == 'admin' else ['read']) if carrier == 'api' else {'Authorization':'Bearer '+create_access_token(data={'sub':'admin'})}
    from fastapi import Depends
    from core.dependencies import get_current_user
    @client.app.get('/owned-identity-proof')
    def identity(principal=Depends(get_current_user)):
        return {'role':principal['role']}
    verified = client.get('/owned-identity-proof', headers=headers)
    assert verified.status_code == 200
    assert verified.json() == {'role':role}
    result = client.get('/license/installation-id', headers=headers)
    assert result.status_code == (200 if role == 'admin' else 403)
    if role == 'admin':
        # device_fingerprint is null until this installation has a device key (it is never created here).
        assert result.json()['installation_id'] == 'owned-synthetic-installation'
        assert set(result.json()) == {'installation_id', 'device_fingerprint', 'license_error'}
        assert calls == ['owned']
    else:
        assert calls == []
        assert 'owned-synthetic-installation' not in result.text
