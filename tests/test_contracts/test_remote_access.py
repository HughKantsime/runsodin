"""Owned remote-access acceptance: no tailnet, credentials or production traffic."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from core.config import Settings
from core.remote_access import TrustedProxySchemeMiddleware, cors_origins, trusted_hosts
from tests.test_contracts.test_cert_G_harness import run_probe

ROOT = Path(__file__).resolve().parents[2]
URL = 'https://odin.example.ts.net'


def test_public_origin_host_and_cors(monkeypatch):
    from core import config
    from core.app import _setup_middleware
    settings = Settings(_env_file=None, trusted_hosts='localhost', cors_origins='http://localhost:3000', odin_public_url=URL+'/')
    assert trusted_hosts(settings) == ['localhost', 'odin.example.ts.net']
    assert cors_origins(settings) == ['http://localhost:3000', URL]
    monkeypatch.setattr(config, 'settings', settings)
    app = FastAPI()
    _setup_middleware(app)
    @app.get('/probe')
    def probe():
        return {'ok': True}
    c = TestClient(app, base_url=URL)
    assert c.get('/probe').status_code == 200
    assert c.get('/probe', headers={'Host': 'evil.example'}).status_code == 400
    r = c.options('/probe', headers={'Origin': URL, 'Access-Control-Request-Method': 'GET'})
    assert r.status_code == 200 and r.headers['access-control-allow-origin'] == URL
    assert c.options('/probe', headers={'Origin': 'https://evil.example', 'Access-Control-Request-Method': 'GET'}).status_code == 400


@pytest.mark.parametrize('url', ['http://odin.example.ts.net','https://u:p@odin.example.ts.net','https://odin.example.ts.net/path','https://odin.example.ts.net?key=x','https://odin.example.ts.net/#x','https://odin.example.ts.net:bad'])
def test_invalid_public_origins_rejected(url):
    with pytest.raises(ValueError):
        Settings(_env_file=None, odin_public_url=url)


@pytest.mark.parametrize('ip', ['192.168.1.1','100.64.0.1:8555','odin.example.ts.net','127.0.0.1'])
def test_candidate_requires_tailnet_literal(ip):
    with pytest.raises(ValueError):
        Settings(_env_file=None, odin_tailnet_ip=ip)


def test_candidate_addition_and_unconfigured_compatibility(monkeypatch):
    from core.config import settings
    from modules.printers import route_utils, go2rtc_access
    monkeypatch.setattr(route_utils, 'go2rtc_rtsp_credentials', lambda: ('fixture', 'fixture'))
    monkeypatch.setattr(go2rtc_access, 'go2rtc_api_credentials', lambda: ('fixture', 'fixture'))
    monkeypatch.setattr(settings, 'odin_tailnet_ip', '')
    old = route_utils.go2rtc_config_for([], '192.168.1.10')['webrtc']
    assert old == {'listen': '0.0.0.0:8555', 'candidates': ['192.168.1.10:8555']}
    monkeypatch.setattr(settings, 'odin_tailnet_ip', '100.101.102.103')
    new = route_utils.go2rtc_config_for([], '192.168.1.10')['webrtc']
    assert new == {**old, 'candidates': ['192.168.1.10:8555', '100.101.102.103:8555']}


def test_websocket_proxy_scheme_and_spoof_rejection():
    seen = []
    async def downstream(scope, receive, send):
        seen.append((scope['scheme'], scope['client']))
    middleware = TrustedProxySchemeMiddleware(downstream, "127.0.0.1,::1")
    for peer, expected in [('127.0.0.1', 'wss'), ('192.0.2.8', 'ws')]:
        scope = {'type': 'websocket', 'scheme': 'ws', 'client': (peer, 9999), 'headers': [(b'x-forwarded-proto', b'https'), (b'x-forwarded-for', b'100.101.102.103')]}
        asyncio.run(middleware(scope, None, None))
        assert seen[-1] == (expected, ('100.101.102.103', 0) if peer == '127.0.0.1' else (peer, 9999))


PROBE = r'''
from core.config import settings
from modules.organizations.routes_oidc import _redirect_uri
from core.application_url import configured_base_url
from starlette.requests import Request
seed_user('remote-admin','admin')
seed_user('remote-viewer','viewer')
seed_user('remote-operator','operator')
settings.odin_public_url = 'https://odin.example.ts.net'
settings.cookie_secure = False
for peer, forwarded, secure in [('127.0.0.1','https',True),('::1','https',True),('192.0.2.1','https',False),('192.0.2.1','',False),('127.0.0.1','https,http',False)]:
    c = TestClient(app, client=(peer,1234), base_url='http://odin.example.ts.net')
    r = c.post('/api/auth/login',data={'username':'remote-admin','password':PASSWORD},headers={'X-Forwarded-Proto':forwarded})
    assert r.status_code == 200, r.text
    assert ('secure' in [x.strip().lower() for x in r.headers['set-cookie'].split(';')]) == secure
    if not secure:
        assert c.get('/api/auth/me').status_code == 200
settings.cookie_secure = True
c = TestClient(app, client=('192.0.2.9',1234))
r = c.post('/api/auth/login',data={'username':'remote-admin','password':PASSWORD})
assert '; Secure' in r.headers['set-cookie']
settings.cookie_secure = False
admin = login('remote-admin')
for prefix in ['/api','/api/v1']:
    assert client().get(prefix+'/settings/remote-access').status_code == 401
    for name in ['remote-viewer','remote-operator']:
        assert login(name).get(prefix+'/settings/remote-access').status_code == 403
    r = admin.get(prefix+'/settings/remote-access')
    assert r.status_code == 200, r.text
    OUT['wire'] = r.json()
assert set(OUT['wire']) == {'configured','url','tailnet','current_origin_verified','check_url','camera_transport','tailnet_ice_configured'}
assert OUT['wire']['url'] == 'https://odin.example.ts.net'
assert configured_base_url(None) == settings.odin_public_url
request = Request({'type':'http','scheme':'https','server':('odin.example.ts.net',443),'path':'/','headers':[(b'host',b'odin.example.ts.net')]})
assert _redirect_uri(request) == settings.odin_public_url+'/api/auth/oidc/callback'
settings.odin_public_url = ''
assert admin.get('/api/settings/remote-access').json()['url'] is None
OUT['cookies_and_rbac'] = True
'''


def test_real_login_cookies_status_rbac_and_wire(tmp_path):
    result = run_probe('remote-access', PROBE, tmp_path,
                       extra_env={'ODIN_TRUSTED_PROXIES': '127.0.0.1,::1'})
    assert result['_net_attempts'] == []
    assert result['cookies_and_rbac']
    fixture = ROOT / 'frontend/src/test-fixtures/remote-access.json'
    import os
    if os.environ.get('CAPTURE') == '1':
        fixture.write_text(json.dumps(result['wire'], indent=2) + '\n')
    assert result['wire'] == json.loads(fixture.read_text())


def test_compose_transport_and_pin():
    import yaml
    compose = yaml.safe_load((ROOT/'install/docker-compose.yml').read_text())
    ts = compose['services']['tailscale']
    assert ts['image'] == 'tailscale/tailscale:v1.90.9'
    assert ts['profiles'] == ['tailscale']
    assert 'network_mode' not in ts and 'networks' not in ts
    assert 'depends_on' not in ts
    assert ts['labels']['com.centurylinklabs.watchtower.enable'] == 'false'
    assert 'ODIN_TRUSTED_PROXIES=${ODIN_TRUSTED_PROXIES:-}' in compose['services']['odin']['environment']
    assert Settings(_env_file=None).odin_trusted_proxies == ''
    assert 'ports' not in ts and 'devices' not in ts and 'cap_add' not in ts
    serve = json.loads(compose['configs']['tailscale_serve']['content'])
    assert serve['TCP']['8555'] == {'TCPForward':'odin:8555'}
    assert serve['AllowFunnel'] == {}
    assert serve['Web']['$${TS_CERT_DOMAIN}:443']['Handlers']['/']['Proxy'] == 'http://odin:8000'
    for name in ('supervisord.conf', 'supervisord.api.conf'):
        assert '--no-proxy-headers' in (ROOT/'docker'/name).read_text()

SNAPSHOT_PROBE = r'''
from sqlalchemy.orm import Session
from modules.printers.models import Printer
from modules.printers import routes_camera_snapshot as snapshots
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'own',1),(2,'other',1)")
seed_user('camera-viewer','viewer',1)
seed_user('camera-outsider','viewer',2)
with Session(engine) as db:
    db.add(Printer(id=501,name='Remote fixture',api_type='moonraker',camera_enabled=True,is_active=True,org_id=1,shared=False,camera_url='rtsp://192.0.2.20:554/cam'))
    db.commit()
import httpx
calls = []
mode = [200]
def transport(req):
    calls.append(str(req.url))
    return httpx.Response(mode[0],content=b'\xff\xd8\xfffixture',headers={'Content-Type':'image/jpeg'})
original = httpx.AsyncClient
snapshots.httpx.AsyncClient = lambda **kw: original(transport=httpx.MockTransport(transport), **kw)
try:
    viewer = login('camera-viewer')
    outsider = login('camera-outsider')
    assert client().get('/api/cameras/501/snapshot').status_code == 401
    assert outsider.get('/api/cameras/501/snapshot').status_code == 404
    assert not calls
    for prefix in ['/api','/api/v1']:
        response = viewer.get(prefix+'/cameras/501/snapshot')
        assert response.status_code == 200, response.text
        assert response.content == b'\xff\xd8\xfffixture'
        assert response.headers['content-type'] == 'image/jpeg'
        assert response.headers['cache-control'] == 'no-store'
    assert all(url.endswith('/api/frame.jpeg?src=printer_501_mjpeg') for url in calls)
    mode[0] = 503
    failed = viewer.get('/api/cameras/501/snapshot')
    assert failed.status_code == 502, failed.text
    assert failed.json().get('detail') == 'Camera snapshot unavailable', failed.text
    calls.clear()
    sql('UPDATE printers SET camera_enabled=FALSE WHERE id=501')
    assert viewer.get('/api/cameras/501/snapshot').status_code == 404
    assert not calls
finally:
    snapshots.httpx.AsyncClient = original
OUT['snapshot_fences'] = True
'''


def test_snapshot_transport_and_authorization(tmp_path):
    result = run_probe('remote-snapshot', SNAPSHOT_PROBE, tmp_path)
    assert result['snapshot_fences'] and result['_net_attempts'] == []


def test_operator_production_compose_patch_is_parseable():
    import re
    import yaml
    doc = (ROOT/'docs/REMOTE_ACCESS.md').read_text()
    patch = re.search(r'```diff\n--- /dev/null\n\+\+\+ /opt/odin/runsodin/tailscale-remote.yml\n@@[^\n]+\n(.*?)```', doc, re.S).group(1)
    production = yaml.safe_load('\n'.join(line[1:] for line in patch.splitlines()))
    odin = production['services']['odin']
    assert odin['image'] == 'ghcr.io/hughkantsime/odin:latest'
    assert odin['container_name'] == 'odin'
    assert odin['ports'] == ['8000:8000', '1984:1984', '8555:8555']
    assert odin['labels']['com.centurylinklabs.watchtower.enable'] == 'true'
    assert odin['environment']['ODIN_TRUSTED_PROXIES'] == '${ODIN_TRUSTED_PROXIES:-}'
    ts = production['services']['tailscale']
    assert 'TS_AUTHKEY' not in ts['environment']
    assert ts['labels']['com.centurylinklabs.watchtower.enable'] == 'false'
    assert 'network_mode' not in ts and 'depends_on' not in ts
    serve = json.loads(production['configs']['tailscale_serve']['content'])
    assert serve['AllowFunnel'] == {}
    assert serve['TCP']['8555'] == {'TCPForward': 'odin:8555'}
    assert serve['Web']['$${TS_CERT_DOMAIN}:443']['Handlers']['/']['Proxy'] == 'http://odin:8000'


@pytest.mark.parametrize("value", [b"127.0.0.1", b"::1", b"::", b"0.0.0.0", b"::ffff:127.0.0.1", b"::ffff:0.0.0.0", b"::ffff:100.64.1.2", b"203.0.113.9, 127.0.0.1"])
def test_forwarded_loopback_client_never_inherits_loopback(value):
    import asyncio
    from core.remote_access import TrustedProxySchemeMiddleware, UNKNOWN_PROXY_CLIENT
    seen = {}
    async def app(scope, receive, send):
        seen.update(scope)
    middleware = TrustedProxySchemeMiddleware(app, '127.0.0.1,::1')
    scope = {"type": "http", "client": ("127.0.0.1", 1), "scheme": "http",
             "headers": [(b"x-forwarded-for", value), (b"x-forwarded-proto", b"https")]}
    asyncio.run(middleware(scope, None, None))
    assert seen["client"][0] == UNKNOWN_PROXY_CLIENT


@pytest.mark.parametrize('url', [
    'https://100.101.102.103', 'https://127.0.0.1', 'https://[::1]',
    'https://[fd7a:115c:a1e0::1]', 'https://2130706433', 'https://0x7f000001',
    'https://127.1', 'https://0177.0.0.1', '127.0.0.1,::1',
    'https://user:password@odin.example.ts.net', 'https://odin.example.ts.net?proxy=::1',
])
def test_status_never_exposes_ip_or_invalid_origins(monkeypatch, url):
    from modules.system import routes_remote_access as route
    from starlette.requests import Request
    monkeypatch.setattr(route.settings, 'odin_public_url', url)
    monkeypatch.setattr(route.settings, 'odin_tailnet_ip', '100.101.102.103')
    monkeypatch.setattr(route.settings, 'odin_trusted_proxies', '127.0.0.1,::1')
    request = Request({'type': 'http', 'scheme': 'https', 'server': ('odin.example.ts.net', 443), 'path': '/', 'headers': []})
    data = route.remote_access_status(request, user={'role': 'admin'})
    assert data == {
        'configured': True, 'url': None, 'tailnet': False,
        'current_origin_verified': False, 'check_url': None,
        'camera_transport': 'tcp-with-snapshot-fallback', 'tailnet_ice_configured': True,
    }


def test_unconfigured_readiness_fixture_matches_status(monkeypatch):
    from modules.system import routes_remote_access as route
    from starlette.requests import Request
    monkeypatch.setattr(route.settings, 'odin_public_url', '')
    monkeypatch.setattr(route.settings, 'odin_tailnet_ip', '')
    fixture_path = ROOT / 'tests/accessibility/fixtures/admin.json'
    fixture = json.loads(fixture_path.read_text())
    request = Request({'type': 'http', 'scheme': 'http', 'server': ('testserver', 80), 'path': '/', 'headers': []})
    result = route.remote_access_status(request, user={'role': 'admin'})
    import os
    if os.environ.get('CAPTURE') == '1':
        fixture['GET /api/settings/remote-access'] = result
        fixture_path.write_text(json.dumps(fixture, indent=2) + '\n')
    assert result == fixture['GET /api/settings/remote-access']


def test_shipped_tailnet_profile_never_routes_lan_or_dns():
    import re
    import yaml
    compose = yaml.safe_load((ROOT / 'install/docker-compose.yml').read_text())
    sidecar = compose['services']['tailscale']
    rendered = json.dumps(sidecar).lower()
    for forbidden in ('ts_routes', 'ts_extra_args', '--advertise-routes', '--accept-dns', '--exit-node', '--advertise-exit-node'):
        assert forbidden not in rendered
    # Pin all installer shell text, including prompts/help and generated commands.
    for installer in (ROOT / 'install').rglob('*.sh'):
        source = installer.read_text().lower()
        assert not re.search(r'advertis[\w-]*[^\n]{0,60}(?:lan|subnet|route)', source)
        assert '--accept-dns' not in source and '--exit-node' not in source


@pytest.mark.parametrize('candidate,expected', [('', False), ('100.101.102.103', True)])
def test_status_returns_only_candidate_presence(monkeypatch, candidate, expected):
    from modules.system import routes_remote_access as route
    monkeypatch.setattr(route.settings, 'odin_public_url', URL)
    monkeypatch.setattr(route.settings, 'odin_tailnet_ip', candidate)
    request = Request({'type': 'http', 'scheme': 'https', 'server': ('odin.example.ts.net', 443), 'path': '/', 'headers': []})
    result = route.remote_access_status(request, user={'role': 'admin'})
    assert result['tailnet'] is True
    assert result['tailnet_ice_configured'] is expected
    if candidate:
        assert candidate not in json.dumps(result)
