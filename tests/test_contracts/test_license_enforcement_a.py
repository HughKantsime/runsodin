"""Slice A operations: capacity, feature refusal, and local recovery."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from core.errors import OdinError
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_classroom_poc import classroom_db, _configure, _roster

ADMIN = {'id': 1, 'username': 'admin', 'role': 'admin', 'group_id': None}


@pytest.mark.parametrize('entry', ['api', 'setup', 'service'])
def test_printer_creation_uses_same_capacity_and_rolls_back(classroom_db, tier_license, entry):
    from modules.printers import routes_crud, services
    from modules.printers.schemas import PrinterCreate
    from modules.system.routes_setup import setup_create_printer, SetupPrinterRequest
    tier_license('pro', max_printers=1)
    db = classroom_db
    if entry == 'api':
        create = lambda name: routes_crud.create_printer(PrinterCreate(name=name), ADMIN, db)
    elif entry == 'setup':
        create = lambda name: setup_create_printer(SetupPrinterRequest(name=name, api_type='bambu'), ADMIN, db)
    else:
        create = lambda name: services.create_printer_record(db, name=name, slot_count=1)
    create('first')
    db.commit()
    with pytest.raises(HTTPException) as refused:
        create('over-cap')
    assert refused.value.status_code == 403
    db.rollback()
    assert db.execute(text('SELECT COUNT(*) FROM printers')).scalar() == 1


@pytest.mark.parametrize('resource', ['printers', 'users'])
def test_concurrent_creators_cannot_both_take_last_slot(classroom_db, tier_license, resource):
    from modules.printers.services import create_printer_record
    from modules.organizations.user_creation import create_user_record
    tier_license('pro', max_printers=1, max_users=2)  # one user already seeded
    engine = classroom_db.get_bind()
    def create(number):
        with Session(engine) as db:
            try:
                if resource == 'printers':
                    create_printer_record(db, name=f'printer-{number}')
                else:
                    create_user_record(db, username=f'user-{number}', email=f'{number}@example.test', password_hash='', role='viewer')
                db.commit()
                return 201
            except HTTPException as exc:
                db.rollback()
                return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, (1, 2))) == [201, 403]


@pytest.mark.parametrize('entry', ['api', 'csv', 'oidc', 'classroom'])
def test_all_user_entrypoints_respect_cap(classroom_db, tier_license, monkeypatch, entry):
    from core.auth import UserCreate
    from modules.organizations import routes_users, routes_oidc, routes_classroom
    db = classroom_db
    tier_license('education', max_users=1)
    if entry == 'csv':
        result = asyncio.run(routes_users.import_users_csv(UploadFile(filename='users.csv', file=BytesIO(
            b'username,email,password,role\nstudent,student@example.test,SyntheticPassword123!,viewer\n')), ADMIN, db))
        assert result['created'] == 0 and result['errors']
    elif entry == 'oidc':
        with pytest.raises(routes_oidc.OIDCIdentityError, match='license_user_limit_reached'):
            routes_oidc._resolve_oidc_user(db, {'auto_create_users': True, 'default_role': 'viewer', 'default_group_id': 1},
                {'iss': 'https://issuer.example', 'sub': 'student', 'email': 'student@example.test'}, {})
        db.rollback()
    elif entry == 'api':
        with pytest.raises(HTTPException) as exc:
            asyncio.run(routes_users.create_user(UserCreate(username='student', email='student@example.test', password='SyntheticPassword123!', role='viewer'), ADMIN, db))
        assert exc.value.status_code == 403
    else:
        _configure(db)
        async def roster(*args):
            return _roster()
        monkeypatch.setattr(routes_classroom, 'get_roster', roster)
        principal = {**ADMIN, 'group_id': 1, 'actor_kind': 'user', 'is_superadmin': False}
        with pytest.raises(OdinError) as exc:
            asyncio.run(routes_classroom.import_classroom_course('course-123',
                routes_classroom.ClassroomImportInput(command_id=uuid4()), principal, db))
        assert exc.value.status == 403
    assert db.execute(text('SELECT COUNT(*) FROM users')).scalar() == 1


def test_classroom_partial_roster_rolls_back_at_cap(classroom_db, tier_license, monkeypatch):
    from modules.organizations import routes_classroom
    tier_license('education', max_users=2)  # admin + only one of the two members
    _configure(classroom_db)
    async def roster(*args):
        return _roster()
    monkeypatch.setattr(routes_classroom, 'get_roster', roster)
    with pytest.raises(OdinError) as exc:
        asyncio.run(routes_classroom.import_classroom_course('course-123',
            routes_classroom.ClassroomImportInput(command_id=uuid4()),
            {**ADMIN, 'group_id': 1, 'actor_kind': 'user', 'is_superadmin': False}, classroom_db))
    assert exc.value.status == 403
    assert classroom_db.execute(text('SELECT COUNT(*) FROM users')).scalar() == 1
    assert classroom_db.execute(text('SELECT COUNT(*) FROM education_commands')).scalar() == 0


def client_for(db, *routers):
    from core.db import get_db
    from core.dependencies import get_current_user
    app = FastAPI()
    for router in routers:
        app.include_router(router)
    def session():
        with Session(db.get_bind()) as request_db:
            yield request_db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    return TestClient(app)


@pytest.mark.parametrize('state', ['community', 'expired'])
def test_sso_refused_at_all_steps_but_local_admin_can_recover(classroom_db, tier_license, state):
    import license_manager as lm
    from core.auth import hash_password
    from modules.organizations import routes_auth, routes_oidc
    if state == 'community':
        tier_license(None)
    else:
        tier_license('pro')
        # Keep the signature genuine: reissue through fixture with a deadline
        # just ahead of its injectable clock, then advance that clock.
        clock = lm.license_clock()
        from datetime import timedelta
        expires = clock.wall() + timedelta(seconds=10)
        tier_license('pro', expires_at=expires.isoformat())
        clock.wall = lambda: expires + timedelta(seconds=1)
    classroom_db.execute(text('UPDATE users SET password_hash=:p,group_id=NULL WHERE id=1'), {'p': hash_password('SyntheticPassword123!')})
    classroom_db.commit()
    routes_auth.limiter._storage.reset()
    client = client_for(classroom_db, routes_auth.router, routes_oidc.router)
    for method, url in [('get','/auth/oidc/login'), ('post','/auth/oidc/exchange')]:
        response = getattr(client, method)(url, **({'json': {'code': 'owned'}} if method == 'post' else {}))
        assert response.status_code == 403, response.text
        assert response.headers['X-ODIN-License-Feature'] == 'sso'
    response = client.get('/auth/oidc/callback?state=owned&code=owned', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'] == '/login?error=sso_license_required'
    response = client.post('/auth/login', data={'username':'admin', 'password':'SyntheticPassword123!'})
    assert response.status_code == 200, response.text
    assert 'session=' in response.headers['set-cookie']


@pytest.mark.parametrize('path', ['/export/jobs', '/export/models', '/export/spools', '/export/filament-usage', '/spools/export', '/archives/log/export'])
def test_csv_exports_refuse_community_at_operation_boundary(classroom_db, tier_license, path):
    from modules.reporting.routes import exports
    from modules.inventory.routes import spool_ops
    from modules.archives.routes import archives_crud
    tier_license(None)
    response = client_for(classroom_db, exports.router, spool_ops.router, archives_crud.router).get(path)
    assert response.status_code == 403, response.text
    assert response.headers['X-ODIN-License-Feature'] == 'csv_export'


@pytest.mark.parametrize('method,path,body', [('get','/orgs',None), ('post','/orgs',{'name':'new'}),
    ('patch','/orgs/1',{'name':'new'}), ('delete','/orgs/1',None),
    ('post','/orgs/1/members',{'user_id':1}), ('get','/orgs/1/settings',None),
    ('put','/orgs/1/settings',{'timezone':'UTC'})])
def test_org_operations_match_user_groups_gate(classroom_db, tier_license, method, path, body):
    from modules.organizations import routes
    tier_license('pro')  # Pro lacks user_groups too
    response = client_for(classroom_db, routes.router).request(method, path, json=body)
    assert response.status_code == 403, response.text
    assert response.headers['X-ODIN-License-Feature'] == 'user_groups'


def test_report_patch_refuses_enable_or_edits_but_allows_pause(classroom_db, tier_license):
    from modules.reporting.routes import reports
    db = classroom_db
    tier_license(None)
    db.execute(text("INSERT INTO report_schedules (id,name,report_type,frequency,recipients,created_by,is_active) "
                    "VALUES (1,'retained','daily_summary','daily','[]',1,TRUE)"))
    db.commit()
    client = client_for(db, reports.router)
    for body in ({'is_active': True}, {'name': 'renamed'}, {'is_active': False, 'name':'renamed'}):
        response = client.patch('/report-schedules/1', json=body)
        assert response.status_code == 403, response.text
    assert client.patch('/report-schedules/1', json={'is_active':False}).status_code == 200
    assert db.execute(text('SELECT is_active FROM report_schedules WHERE id=1')).scalar() == 0


def test_calculator_and_pricing_config_are_community(classroom_db, tier_license):
    import license_manager as lm
    from modules.models_library.routes import pricing
    tier_license(None)
    assert lm.get_license().has_feature('cost_calculator')
    assert 'cost_calculator' not in lm.TIERS['pro']['features']
    client = client_for(classroom_db, pricing.router)
    assert client.get('/pricing-config').status_code == 200
    response = client.put('/pricing-config', json={'hourly_rate': 12})
    assert response.status_code == 200, response.text
    assert response.json()['hourly_rate'] == 12
