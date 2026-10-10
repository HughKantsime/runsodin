"""Fail closed before a disposable fixture can reuse an imported application DB."""
import os
from pathlib import Path
import subprocess
import sys


def test_preimported_database_cannot_be_reseeded(tmp_path):
    root=Path(__file__).resolve().parents[2]
    code='''
import os,sys
from pathlib import Path
from sqlalchemy import text
from core.db import engine
with engine.begin() as c:
    c.execute(text('CREATE TABLE protected_canary(value TEXT)'))
    c.execute(text("INSERT INTO protected_canary VALUES('synthetic-original')"))
from ops.edu_readiness.api_load import _prepare_database
before=os.environ['DATABASE_URL']
try:
    _prepare_database(Path(sys.argv[1]))
except RuntimeError as exc:
    assert 'fresh subprocess' in str(exc)
else:
    raise AssertionError('Pre-imported application was not rejected')
assert os.environ['DATABASE_URL']==before
with engine.connect() as c:
    assert c.execute(text('SELECT value FROM protected_canary')).scalar_one()=='synthetic-original'
assert not Path(sys.argv[1]).exists()
'''
    env={k:v for k,v in os.environ.items() if not k.startswith(('ODIN_','DATABASE_','LICENSE_'))}
    env['DATABASE_URL']=f'sqlite:///{tmp_path / "original.db"}'
    env['PYTHONPATH']=str(root/'backend')+os.pathsep+str(root)
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path/'must-not-create.db')],env=env,cwd=tmp_path,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
