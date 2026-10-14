"""Documentation routes of the real app factory, with isolated database and no lifespan."""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

ROOT=Path(__file__).resolve().parents[2]

@pytest.fixture(scope='module')
def documentation_statuses(tmp_path_factory):
    directory=tmp_path_factory.mktemp('documentation-owned')
    env=dict(os.environ,DATABASE_URL='sqlite:///'+str(directory/'app.db'),LICENSE_DIR=str(directory),API_KEY='',ODIN_ITAR_MODE='0',BASE_URL='http://127.0.0.1:9',ODIN_CONTAINER_EXEC='/usr/bin/false',PYTHONPATH=str(ROOT/'backend'))
    code='''
import os,json,secrets,socket
from cryptography.fernet import Fernet
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['ENCRYPTION_KEY']=Fernet.generate_key().decode()
original=socket.socket
class LocalOnlySocket(original):
    def __init__(self,family=socket.AF_INET,*args,**kwargs):
        if family in (socket.AF_INET,socket.AF_INET6): raise AssertionError('Unexpected network in owned documentation test')
        super().__init__(family,*args,**kwargs)
socket.socket=LocalOnlySocket
from core.db import engine
from core.schema import bootstrap_database
from pathlib import Path
bootstrap_database(engine,Path('backend').resolve())
from core.app import create_app
from fastapi.testclient import TestClient
client=TestClient(create_app(),raise_server_exceptions=False)
results={}
for path in ['/api/docs','/api/redoc','/api/v1/docs','/api/v1/redoc']:
    response=client.get(path)
    results[path]={'status':response.status_code,'html':response.headers.get('content-type','').startswith('text/html')}
print(json.dumps(results))
engine.dispose()
'''
    result=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env=env,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])

@pytest.mark.parametrize('path',['/api/docs','/api/redoc'])
def test_advertised_documentation_paths_serve_html(documentation_statuses,path):
    assert documentation_statuses[path]=={'status':200,'html':True}

@pytest.mark.parametrize('path',['/api/v1/docs','/api/v1/redoc'])
def test_versioned_documentation_paths_serve_html(documentation_statuses,path):
    assert documentation_statuses[path]=={'status':200,'html':True}
