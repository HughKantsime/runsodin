"""Advertised Swagger UI path /api/docs works on the real app (F14-14.4-01)."""
import os,subprocess,sys,textwrap
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
PROBE=textwrap.dedent('''
import os
from pathlib import Path
from sqlalchemy import create_engine
from core.schema import bootstrap_database
bootstrap_database(create_engine(os.environ['DATABASE_URL']),Path('.'))
from fastapi.testclient import TestClient
from core.app import create_app
client=TestClient(create_app())
key=os.environ.get('API_KEY')
if key:
    # With a perimeter key the docs are gated like the API; browsers pass with their session cookie.
    assert client.get('/api/docs').status_code==401
    client.headers['X-API-Key']=key
first=client.get('/api/docs',follow_redirects=False)
assert first.status_code in (307,308) and first.headers['location'].endswith('/api/v1/docs'),(first.status_code,dict(first.headers))
page=client.get('/api/docs')
assert page.status_code==200 and 'swagger-ui' in page.text.lower(),page.status_code
spec_url=page.text.split("url: '")[1].split("'")[0]
spec=client.get(spec_url)
assert spec.status_code==200 and spec.json()['info']['title'],spec.status_code
assert client.get('/api/redoc').status_code==200
print('api-docs-runtime-pass',spec_url)
''')


import pytest


@pytest.mark.parametrize('perimeter_key',[None,'docs-perimeter-key-only'])
def test_advertised_api_docs_path_serves_swagger_ui(tmp_path,perimeter_key):
    env={**os.environ,'DATABASE_URL':f'sqlite:///{tmp_path}/docs.db','JWT_SECRET_KEY':'docs-test-key-only','PYTHONPATH':str(ROOT/'backend')}
    env.pop('API_KEY',None)
    if perimeter_key:env['API_KEY']=perimeter_key
    result=subprocess.run([sys.executable,'-c',PROBE],cwd=ROOT/'backend',env=env,capture_output=True,text=True,timeout=180)
    assert result.returncode==0,result.stdout[-2000:]+result.stderr[-3000:]
    assert 'api-docs-runtime-pass' in result.stdout
