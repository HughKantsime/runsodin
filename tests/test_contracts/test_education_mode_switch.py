"""Education mode requires a real signed-in platform administrator."""
import json

import pytest
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_session_management_behavior import tracked_session

DETAIL = 'Education mode can only be changed by a signed-in administrator.'


@pytest.fixture
def education_client(token_client, review_db):
    from core.audit_actor import register_request_session, clear_session
    from core.db import get_db
    from core.middleware.audit_actor import AuditActorMiddleware
    from modules.system.routes_settings import router
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.commit()
    client, issue = token_client
    client.app.add_middleware(AuditActorMiddleware)

    def database(request: Request):
        with Session(review_db.get_bind()) as db:
            register_request_session(db, request)
            try:
                yield db
            finally:
                clear_session(db)

    client.app.dependency_overrides[get_db] = database
    client.app.include_router(router, prefix='/api')
    client.app.include_router(router, prefix='/api/v1')
    return client, issue


def set_mode(db, value):
    db.execute(text("DELETE FROM system_config WHERE key='education_mode'"))
    if value is not None:
        db.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode',:value)"),
                   {'value': 'true' if value else 'false'})
    db.commit()


@pytest.mark.parametrize('credential', ['write', 'admin', 'read', 'agent:write', 'legacy'])
@pytest.mark.parametrize('before', [False, True])
def test_education_mode_switch_refuses_tokens_and_keys(education_client, review_db, monkeypatch, credential, before):
    client, issue = education_client
    set_mode(review_db, before)
    if credential == 'legacy':
        monkeypatch.setenv('API_KEY', 'synthetic-education-global-key')
        headers = {'X-API-Key': 'synthetic-education-global-key'}
    else:
        headers = issue([credential])
    audits = review_db.execute(text('SELECT * FROM audit_logs')).all()
    for prefix in ('/api', '/api/v1'):
        response = client.put(prefix + '/settings/education-mode', headers=headers, json={'enabled': not before})
        assert response.status_code == 403
        assert response.json()['detail'] == DETAIL
        assert client.get(prefix + '/settings/education-mode').json() == {'enabled': before}
    assert review_db.execute(text('SELECT * FROM audit_logs')).all() == audits


@pytest.mark.parametrize('before,after', [(None, True), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize('carrier', ['bearer', 'cookie'])
def test_education_mode_switch_by_session_audits_before_and_after(education_client, review_db, before, after, carrier):
    client, _ = education_client
    set_mode(review_db, before)
    headers, _ = tracked_session(review_db)
    if carrier == 'cookie':
        client.cookies.set('session', headers['Authorization'].removeprefix('Bearer '))
        headers = {}
    response = client.put('/api/settings/education-mode', headers=headers, json={'enabled': after})
    assert response.status_code == 200, response.text
    assert response.json() == {'enabled': after}
    assert client.get('/api/settings/education-mode').json() == {'enabled': after}
    rows = review_db.execute(text("SELECT actor_user_id,details FROM audit_logs WHERE action='education_mode_toggled'")).all()
    assert len(rows) == 1
    assert rows[0][0] == 3
    assert json.loads(rows[0][1]) == {'before': bool(before), 'after': after}


@pytest.mark.parametrize('role,group', [('admin', 1), ('operator', None), ('viewer', None)])
def test_education_mode_switch_requires_platform_superadmin(education_client, review_db, role, group):
    client, _ = education_client
    review_db.execute(text('UPDATE users SET role=:role,group_id=:group WHERE id=3'), {'role': role, 'group': group})
    review_db.commit()
    headers, _ = tracked_session(review_db)
    set_mode(review_db, True)
    response = client.put('/api/settings/education-mode', headers=headers, json={'enabled': False})
    assert response.status_code == 403
    assert client.get('/api/settings/education-mode').json() == {'enabled': True}
    assert not review_db.execute(text("SELECT id FROM audit_logs WHERE action='education_mode_toggled'")).all()
