"""Claim F11-11.3-02 adversarial probe: perimeter API key enabled, bogus `odin_` token.

The perimeter middleware forwards any X-API-Key starting with `odin_` to route-level
auth. Routes with no auth dependency are therefore reachable with a fabricated token.
Runs the real app factory in an isolated subprocess with API_KEY set.
"""
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / 'artifacts' / 'application-certification' / 'claims-protocols-backend'

PROBE = r'''
import os,json,secrets,socket,re
from cryptography.fernet import Fernet
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['ENCRYPTION_KEY']=Fernet.generate_key().decode()
original=socket.socket
class L(original):
    def __init__(self,family=socket.AF_INET,*a,**k):
        if family in (socket.AF_INET,socket.AF_INET6): raise OSError('no network')
        super().__init__(family,*a,**k)
socket.socket=L
from pathlib import Path
from sqlalchemy import text
from core.db import engine
from core.schema import bootstrap_database
bootstrap_database(engine,Path('backend').resolve())
with engine.begin() as c:
    c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'tenant-a',1)"))
    c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared,org_id) VALUES(1,'tenant-a-x1c','bambu',1,1,'[]',0,0,1)"))
    c.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color_hex,color) VALUES(1,1,'OTHER','ff0000','red')"))
from core.app import create_app
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
app=create_app()
client=TestClient(app,raise_server_exceptions=False)
out={}
for label,headers in [('no_headers',{}),('bogus_odin_token',{'X-API-Key':'odin_'+'x'*20})]:
    r=client.get('/api/printers/1/unmatched-slots',headers=headers)
    out[label]={'status':r.status_code,'body':r.text[:300]}
# Systematic: every GET route with bogus odin_ token, ids=1
rows=[]
for route in app.routes:
    if isinstance(route,APIRoute) and 'GET' in route.methods and route.path.startswith('/api/') and not route.path.startswith('/api/v1/'):
        url=re.sub(r'\{[^}]+\}','1',route.path).replace(':path','')
        try: s=client.get(url,headers={'X-API-Key':'odin_'+'x'*20}).status_code
        except Exception as e: s='EXC'
        rows.append([route.path,s])
out['bogus_token_get_lt_400']=[r for r in rows if isinstance(r[1],int) and r[1]<400]
out['bogus_token_get_total']=len(rows)
print('BYPASS_JSON='+json.dumps(out))
'''


@pytest.fixture(scope='module')
def bypass(tmp_path_factory):
    d = tmp_path_factory.mktemp('rbac-bypass-owned')
    env = dict(os.environ, DATABASE_URL='sqlite:///' + str(d / 'app.db'), LICENSE_DIR=str(d), API_KEY=secrets.token_urlsafe(32),
               ODIN_ITAR_MODE='0', BASE_URL='http://127.0.0.1:9', ODIN_CONTAINER_EXEC='/usr/bin/false', PYTHONPATH=str(ROOT / 'backend'))
    result = subprocess.run([sys.executable, '-c', PROBE], cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-3000:]
    data = json.loads([l for l in result.stdout.splitlines() if l.startswith('BYPASS_JSON=')][-1][len('BYPASS_JSON='):])
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / 'rbac-bogus-token-probe.json').write_text(json.dumps(data, indent=1))
    return data


def test_perimeter_blocks_requests_without_any_key(bypass):
    assert bypass['no_headers']['status'] == 401


@pytest.mark.xfail(strict=True, reason='DEFECT: GET /api/printers/{id}/unmatched-slots has no auth dependency and the '
                   'perimeter forwards any odin_-prefixed key; a fabricated token reads any tenant printer slots')
def test_fabricated_odin_token_cannot_read_printer_slots(bypass):
    assert bypass['bogus_odin_token']['status'] in (401, 403)
