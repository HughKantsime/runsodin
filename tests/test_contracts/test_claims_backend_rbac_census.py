"""Claim F11-11.3-02 census: real route count, RBAC matrix coverage and unauthenticated probe.

Runs the real app factory in an isolated subprocess (temp SQLite, generated secrets,
no lifespan, IPv4/IPv6 sockets refused) and writes a JSON receipt.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / 'artifacts' / 'application-certification' / 'claims-protocols-backend'

CODE = r'''
import os,json,secrets,socket,re,sys
from cryptography.fernet import Fernet
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['ENCRYPTION_KEY']=Fernet.generate_key().decode()
original=socket.socket
class LocalOnlySocket(original):
    def __init__(self,family=socket.AF_INET,*args,**kwargs):
        if family in (socket.AF_INET,socket.AF_INET6): raise AssertionError('Unexpected network in owned census')
        super().__init__(family,*args,**kwargs)
socket.socket=LocalOnlySocket
from pathlib import Path
from core.db import engine
from core.schema import bootstrap_database
bootstrap_database(engine,Path('backend').resolve())
from core.app import create_app
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
app=create_app()
pairs=[]
for r in app.routes:
    if isinstance(r,APIRoute):
        for m in sorted(r.methods or []):
            if m in ('HEAD','OPTIONS'): continue
            pairs.append((m,r.path))
spec=app.openapi()
openapi_pairs=[(m.upper(),p) for p,item in spec.get('paths',{}).items() for m in item if m.lower() in ('get','post','put','patch','delete')]
client=TestClient(app,raise_server_exceptions=False)
probe=[]
for m,p in sorted(set(pairs)):
    url=re.sub(r'\{[^}]+\}','99999',p).replace(':path','')
    try:
        resp=client.request(m,url,json={},headers={})
        probe.append([m,p,resp.status_code])
    except Exception as e:
        probe.append([m,p,'EXC:'+type(e).__name__])
print('CENSUS_JSON='+json.dumps({'route_pairs':pairs,'openapi_pairs':openapi_pairs,'probe':probe}))
engine.dispose()
'''


def _norm(path):
    path = re.sub(r'^/api/v1/', '/api/', path)
    return re.sub(r'\{[^}]+\}', '{id}', path)


@pytest.fixture(scope='module')
def census(tmp_path_factory):
    directory = tmp_path_factory.mktemp('rbac-census-owned')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + str(directory / 'app.db'), LICENSE_DIR=str(directory),
               API_KEY='', ODIN_ITAR_MODE='0', BASE_URL='http://127.0.0.1:9', ODIN_CONTAINER_EXEC='/usr/bin/false',
               PYTHONPATH=str(ROOT / 'backend'))
    result = subprocess.run([sys.executable, '-c', CODE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-3000:]
    line = [l for l in result.stdout.splitlines() if l.startswith('CENSUS_JSON=')][-1]
    data = json.loads(line[len('CENSUS_JSON='):])
    sys.path.insert(0, str(ROOT / 'tests'))
    from test_rbac import ENDPOINT_MATRIX
    collect = subprocess.run([sys.executable, '-m', 'pytest', '--collect-only', '-q', 'tests/test_rbac.py', '-p', 'no:cacheprovider'],
                             cwd=ROOT, env=dict(os.environ, PYTHONPATH='backend:.'), capture_output=True, text=True, timeout=300)
    collected = [l for l in collect.stdout.splitlines() if '::' in l]
    pairs = sorted(set(map(tuple, data['route_pairs'])))
    excluded = {'/openapi.json', '/api/v1/openapi.json', '/api/v1/docs', '/api/v1/docs/oauth2-redirect', '/api/v1/redoc',
                '/ws', '/ws/{client_id}', '/{path:path}', '/assets/{path:path}', '/static/{path:path}', '/api/vision/frames/{path:path}'}
    canonical = sorted({(m, _norm(p)) for m, p in pairs if p not in excluded})
    matrix = {(m.upper(), _norm(p)) for m, p, *_ in ENDPOINT_MATRIX}
    uncovered = sorted(set(canonical) - matrix)
    phantom = sorted(matrix - set(canonical))
    public_ok = re.compile(r'^/api/(v1/)?(auth/|setup/|health|license/|docs|redoc|openapi|branding|version|kiosk|config/public|status)')
    probe = data['probe']
    unauth_2xx = [p for p in probe if isinstance(p[2], int) and p[2] < 400]
    summary = {
        'app_route_method_pairs_raw': len(pairs),
        'app_route_method_pairs_canonical_v1_collapsed': len(canonical),
        'openapi_method_pairs': len(set(map(tuple, data['openapi_pairs']))),
        'endpoint_matrix_entries': len(ENDPOINT_MATRIX),
        'endpoint_matrix_unique_canonical': len(matrix),
        'canonical_routes_not_in_matrix': len(uncovered),
        'matrix_entries_without_route': len(phantom),
        'test_rbac_collected_cases': len(collected),
        'test_rbac_collect_returncode': collect.returncode,
        'unauthenticated_probe_total': len(probe),
        'unauthenticated_probe_lt_400': len(unauth_2xx),
        'unauthenticated_probe_status_histogram': {str(k): sum(1 for p in probe if p[2] == k) for k in sorted({str(p[2]) for p in probe}, key=str) for k in [int(k) if k.isdigit() else k]},
        'unauthenticated_write_lt_400': [p for p in unauth_2xx if p[0] != 'GET'],
    }
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / 'rbac-census.json').write_text(json.dumps({
        'summary': summary, 'uncovered_routes': uncovered, 'phantom_matrix_entries': phantom,
        'unauthenticated_lt_400': unauth_2xx,
        'unauthenticated_not_401_403': [p for p in probe if p[2] not in (401, 403) and not (isinstance(p[2], int) and p[2] < 400)],
        'collect_tail': collect.stdout.splitlines()[-5:],
    }, indent=1))
    return summary, uncovered, unauth_2xx


def test_census_executes_and_records(census):
    summary, uncovered, unauth = census
    assert summary['app_route_method_pairs_canonical_v1_collapsed'] > 0
    assert summary['test_rbac_collect_returncode'] == 0
