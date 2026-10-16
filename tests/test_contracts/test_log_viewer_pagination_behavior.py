"""Owned temporary log files; real HTTP pagination, no process/provider access."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from modules.system import routes_admin

@pytest.fixture
def client(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic.log'
    path.write_text(''.join(f'[INFO] Synthetic line {i}\n' for i in range(1, 8)))
    monkeypatch.setattr(routes_admin, '_LOG_FILES', {'backend': str(path), 'supervisord': str(path)})
    app = FastAPI()
    app.include_router(routes_admin.router, prefix='/api')
    route = next(r for r in app.routes if r.path == '/api/admin/logs')
    auth = next(d.call for d in route.dependant.dependencies if d.name == 'user')
    app.dependency_overrides[auth] = lambda: {'role': 'admin', 'group_id': None}
    # The route is interactive-only; this fixture stands in for a login session.
    from core.dependencies import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {'role': 'admin', 'group_id': None}
    with TestClient(app) as http:
        yield http, path


def test_tail_default_and_older_pages_preserve_original_order_and_append_snapshot(client):
    http, path = client
    first = http.get('/api/admin/logs', params={'lines': 3}).json()
    assert first['lines'] == [f'[INFO] Synthetic line {i}' for i in (5, 6, 7)]
    with path.open('a') as file:
        file.write('[INFO] Synthetic appended line\n')
    second = http.get('/api/admin/logs', params={'lines': 3, 'cursor': first['next_cursor']}).json()
    assert second['lines'] == [f'[INFO] Synthetic line {i}' for i in (2, 3, 4)]
    last = http.get('/api/admin/logs', params={'lines': 3, 'cursor': second['next_cursor']}).json()
    assert last['lines'] == ['[INFO] Synthetic line 1']
    assert last['next_cursor'] is None
    original = http.get('/api/admin/logs', params={'lines': 3, 'cursor': first['page_cursor']}).json()
    assert original['lines'] == first['lines']


def test_filtered_pagination_only_counts_matching_lines(client):
    http, path = client
    path.write_text('[INFO] Alpha\n[ERROR] Synthetic 1\n[INFO] Noise\n[ERROR] Synthetic 2\n[ERROR] Synthetic 3\n')
    params = {'lines': 2, 'level': 'error', 'search': 'SYNTHETIC'}
    first = http.get('/api/admin/logs', params=params).json()
    assert first['lines'] == ['[ERROR] Synthetic 2', '[ERROR] Synthetic 3']
    last = http.get('/api/admin/logs', params={**params, 'cursor': first['next_cursor']}).json()
    assert last['lines'] == ['[ERROR] Synthetic 1']
    assert last['next_cursor'] is None


def test_supervisord_fixed_source_is_served_and_unknown_source_cannot_read_paths(client):
    http, _ = client
    assert http.get('/api/admin/logs', params={'source': 'supervisord', 'lines': 1}).json()['lines'] == ['[INFO] Synthetic line 7']
    assert http.get('/api/admin/logs', params={'source': '/etc/passwd'}).json()['lines'] == []


@pytest.mark.parametrize('params', [{'lines': 0}, {'lines': 5001}, {'cursor': 'x' * 129}])
def test_page_input_bounds(client, params):
    assert client[0].get('/api/admin/logs', params=params).status_code == 422


@pytest.mark.parametrize('cursor', ['invalid', '1:2:3:-1', '1:2:3:4', '1:2:3:0:5'])
def test_malformed_cursor_rejected(client, cursor):
    assert client[0].get('/api/admin/logs', params={'cursor': cursor}).status_code == 400


def test_rotation_and_truncation_require_refresh(client):
    http, path = client
    cursor = http.get('/api/admin/logs', params={'lines': 2}).json()['next_cursor']
    path.write_text('[INFO] Short\n')
    assert http.get('/api/admin/logs', params={'cursor': cursor}).status_code == 409
    path.unlink()
    path.write_text('replacement\n' * 100)
    assert http.get('/api/admin/logs', params={'cursor': cursor}).status_code == 409


def test_large_physical_line_is_bounded_and_still_one_pagination_entry(client):
    http, path = client
    path.write_text('x' * (routes_admin._MAX_LOG_LINE_BYTES * 4) + '\n[INFO] Next\n')
    page = http.get('/api/admin/logs', params={'lines': 1}).json()
    assert page['lines'] == ['[INFO] Next']
    older = http.get('/api/admin/logs', params={'lines': 1, 'cursor': page['next_cursor']}).json()
    assert len(older['lines']) == 1
    assert len(older['lines'][0]) <= routes_admin._MAX_LOG_LINE_BYTES + 20
    assert older['lines'][0].endswith('[line truncated]')
    assert older['next_cursor'] is None

@pytest.mark.parametrize('principal,status', [({'role':'viewer','group_id':None},403),({'role':'operator','group_id':None},403),({'role':'admin','group_id':3},403),(None,401),({'role':'admin','group_id':None},200)])
def test_real_superadmin_dependency_retained(client, principal, status):
    http, _ = client
    route = next(route for route in http.app.routes if route.path == '/api/admin/logs')
    dependency = next(dependency for dependency in route.dependant.dependencies if dependency.name == 'user')
    http.app.dependency_overrides.pop(dependency.call)
    http.app.dependency_overrides[dependency.dependencies[0].call] = lambda: principal
    assert http.get('/api/admin/logs', params={'lines':1}).status_code == status
