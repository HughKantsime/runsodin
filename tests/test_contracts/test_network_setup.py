"""2.0.46: no implicit public ICE and no anonymous license probe."""
from pathlib import Path

import pytest
import yaml

from tests.test_contracts.test_cert_G_harness import run_probe

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('itar', ['0', '1'])
def test_generated_go2rtc_explicitly_disables_default_stun(itar, tmp_path, monkeypatch):
    from modules.printers import camera_relay, route_utils
    monkeypatch.setenv('ODIN_ITAR_MODE', itar)
    monkeypatch.setenv('ODIN_CAMERA_RELAY_SECRET_FILE', str(tmp_path / 'relay-secret'))
    camera_relay.rotate_relay_secret()
    config = route_utils.go2rtc_config_for([], '192.168.1.10')
    target = tmp_path / 'go2rtc.yaml'
    route_utils.write_go2rtc_config_before_start(config, _path=str(target))
    saved = yaml.safe_load(target.read_text())
    assert saved['webrtc']['ice_servers'] == []
    assert '192.168.1.10:8555' in saved['webrtc']['candidates']
    assert not any(scheme in target.read_text() for scheme in ('stun:', 'stuns:', 'turn:', 'turns:'))


@pytest.mark.parametrize('filename', ['docker/go2rtc.yaml', 'go2rtc/go2rtc.yaml.example'])
def test_shipped_go2rtc_templates_disable_default_stun(filename):
    assert yaml.safe_load((ROOT / filename).read_text())['webrtc']['ice_servers'] == []


@pytest.fixture(scope='module')
def probe(tmp_path_factory):
    return run_probe('license-probe-auth', '''
from modules.system import routes_health
from core.rbac_routes import ROLE_ROUTES
calls = []
async def probe():
    calls.append(True)
    return {'reachable': True, 'cached': False}
routes_health._probe_license_server = probe
install_license('enterprise')
for role in ('admin', 'operator', 'viewer'):
    seed_user(role, role)
OUT['status'] = {}
for prefix in ('/api', '/api/v1'):
    for role in ('anonymous', 'operator', 'viewer', 'admin'):
        c = client() if role == 'anonymous' else login(role)
        before = len(calls)
        r = c.get(prefix + '/system/license-server-status')
        OUT['status'][prefix + ':' + role] = [r.status_code, len(calls) - before]
OUT['health'] = {}
for path in ('/health', '/api/health', '/api/v1/health', '/system/health', '/api/system/health'):
    before = len(calls)
    r = client().get(path)
    OUT['health'][path] = [r.status_code, len(calls) - before]
OUT['registered_role'] = ROLE_ROUTES[('GET', '/api/system/license-server-status')]
''', tmp_path_factory.mktemp('license-probe-auth'))


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
@pytest.mark.parametrize('role,status,count', [('anonymous', 401, 0), ('operator', 403, 0), ('viewer', 403, 0), ('admin', 200, 1)])
def test_license_probe_admin_only(probe, prefix, role, status, count):
    assert probe['registered_role'] == 'admin'
    assert probe['status'][prefix + ':' + role] == [status, count]
    assert probe['_net_attempts'] == []


@pytest.mark.parametrize('path', ['/health', '/api/health', '/api/v1/health', '/system/health', '/api/system/health'])
def test_anonymous_health_does_not_probe_license(probe, path):
    status, count = probe['health'][path]
    assert count == 0
    if path in ('/health', '/api/health', '/api/v1/health'):
        assert status == 200


def test_setup_token_uploads_signed_offline_file_without_network(tmp_path):
    result = run_probe('setup-offline-upload', '''
c = client()
r = c.post('/api/setup/admin', json={'username':'first-admin','email':'admin@example.test','password':PASSWORD})
assert r.status_code == 200, (r.status_code, r.text)
c.headers['Authorization'] = 'Bearer ' + r.json()['access_token']
signed = sign_license(license_payload('pro'))
r = c.post('/api/license/upload', files={'file': ('offline.json', json.dumps(dict(zip(('payload', 'signature'), signed.split('.')))), 'application/json')})
OUT['upload'] = [r.status_code, body(r)]
OUT['installed'] = LM.load_license().tier
r = c.post('/api/license/upload', files={'file': ('invalid.json', '{"signed_license":"invalid"}', 'application/json')})
OUT['invalid_status'] = r.status_code
OUT['retained'] = LM.load_license().tier
''', tmp_path)
    assert result['upload'][0] == 200, result['upload']
    assert result['upload'][1]['tier'] == result['installed'] == 'pro'
    assert result['invalid_status'] == 400
    assert result['retained'] == 'pro'
    assert result['_net_attempts'] == []


def test_timeline_print_job_detail_preserves_org_scope(tmp_path):
    result = run_probe('print-job-detail', '''
install_license('enterprise')
sql("INSERT INTO groups(id,name,is_org) VALUES (1,'One',1),(2,'Two',1)")
seed_user('one', 'viewer', 1)
seed_user('two', 'viewer', 2)
sql("INSERT INTO printers(id,name,api_type,org_id,shared) VALUES (1,'Printer','bambu',1,0)")
sql("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,ended_at) VALUES (1,1,'Synthetic print','completed','2026-11-01 12:00:00','2026-11-01 13:00:00')")
a, b = login('one'), login('two')
OUT['anonymous'] = client().get('/api/print-jobs/1').status_code
r = a.get('/api/print-jobs/1')
OUT['own'] = [r.status_code, body(r)]
OUT['other'] = b.get('/api/print-jobs/1').status_code
OUT['missing'] = a.get('/api/print-jobs/99999').status_code
OUT['list'] = body(a.get('/api/print-jobs'))
OUT['stats'] = a.get('/api/print-jobs/stats').status_code
OUT['alias'] = a.get('/api/v1/print-jobs/1').status_code
''', tmp_path)
    assert result['anonymous'] == 401
    assert result['own'][0] == 200, result
    assert result['own'][1]['id'] == 1
    assert result['own'][1]['duration_minutes'] == 60
    assert result['own'][1] == result['list'][0]
    assert result['other'] == result['missing'] == 404
    assert result['stats'] == result['alias'] == 200
    assert result['_net_attempts'] == []
