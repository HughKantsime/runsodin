"""Settings validation, actual RBAC dependencies and atomic audit storage."""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from core.db import get_db
from core.dependencies import get_current_user
from modules.system import routes_pause_reminders as routes
from tests.test_contracts.test_temperature_alerts import farm  # noqa: F401


@pytest.fixture
def settings_client(farm):
    actor = {'id': 1, 'role': 'admin', 'group_id': None}
    app = FastAPI()
    app.include_router(routes.router, prefix='/api')
    def database():
        with farm.factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: actor
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, actor


def test_get_put_and_audit(settings_client, farm):
    client, actor = settings_client
    assert client.get('/api/config/pause-reminders').json() == {'enabled': True, 'first_hours': 2, 'repeat_hours': 12}
    response = client.put('/api/config/pause-reminders', json={'first_hours': 0.25, 'repeat_hours': 0})
    assert response.status_code == 200
    assert response.json() == {'enabled': True, 'first_hours': 0.25, 'repeat_hours': 0}
    with farm.factory() as db:
        details = db.execute(text("SELECT details FROM audit_logs WHERE action='pause_reminders.updated'")).scalar_one()
    assert json.loads(details)['changed'] == {'first_hours': 0.25, 'repeat_hours': 0}
    assert client.get('/api/config/pause-reminders').json() == response.json()


@pytest.mark.parametrize('body,message', [
    ({'first_hours': 0}, 'first_hours must be between 0.25 and 168.'),
    ({'first_hours': 169}, 'first_hours must be between 0.25 and 168.'),
    ({'first_hours': 10**400}, 'first_hours must be between 0.25 and 168.'),
    ({'first_hours': True}, 'first_hours must be between 0.25 and 168.'),
    ({'first_hours': '2'}, 'first_hours must be between 0.25 and 168.'),
    ({'first_hours': None}, 'first_hours must be between 0.25 and 168.'),
    ({'repeat_hours': 0.5}, 'repeat_hours must be 0 (no repeat) or between 1 and 168.'),
    ({'repeat_hours': -1}, 'repeat_hours must be 0 (no repeat) or between 1 and 168.'),
    ({'repeat_hours': 169}, 'repeat_hours must be 0 (no repeat) or between 1 and 168.'),
    ({'repeat_hours': False}, 'repeat_hours must be 0 (no repeat) or between 1 and 168.'),
    ({'enabled': 'false'}, 'enabled must be true or false.'),
    ({'enabled': False, 'other': 1}, 'Unknown setting: other.'),
])
def test_refuses_whole_invalid_update(settings_client, farm, body, message):
    client, _ = settings_client
    response = client.put('/api/config/pause-reminders', json=body)
    assert response.status_code == 422
    assert response.json()['detail'] == message
    assert client.get('/api/config/pause-reminders').json()['enabled'] is True
    with farm.factory() as db:
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='pause_reminders.updated'")).scalar() == 0


@pytest.mark.parametrize('role', ['viewer', 'operator'])
def test_non_admin_can_read_but_not_write(settings_client, role):
    client, actor = settings_client
    actor['role'] = role
    assert client.get('/api/config/pause-reminders').status_code == 200
    response = client.put('/api/config/pause-reminders', json={'enabled': False})
    assert response.status_code == 403
    assert response.json()['detail'] == 'Insufficient permissions'


def test_audit_failure_rolls_back_settings(settings_client, farm, monkeypatch):
    client, _ = settings_client
    original = routes.log_audit
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(routes, 'log_audit', fail)
    assert client.put('/api/config/pause-reminders', json={'enabled': False}).status_code == 500
    with farm.factory() as db:
        assert db.execute(text("SELECT count(*) FROM system_config WHERE key='pause_reminder_settings'")).scalar() == 0
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='pause_reminders.updated'")).scalar() == 0
