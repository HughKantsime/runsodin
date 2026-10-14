"""Fresh-process audit helper has no engine side effects and preserves caller TX."""
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path


def test_pure_import_rollback_commit_and_compatibility_reexport(tmp_path):
    repo=Path(__file__).resolve().parents[2];database=tmp_path/'owned-audit.db'
    script="""
import json,sys
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from core.audit import log_audit
from core.models import AuditLog
assert 'core.db' not in sys.modules
engine=create_engine(sys.argv[1],hide_parameters=True)
AuditLog.__table__.create(engine)
with Session(engine) as db:
    log_audit(db,'owned_rollback','owned',1,{'marker':'synthetic'})
    db.flush();db.rollback()
with engine.connect() as db:assert db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()==0
with Session(engine) as db:
    log_audit(db,'owned_commit','owned',1,{'marker':'synthetic'})
    db.commit()
with engine.connect() as db:assert db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()==1
assert 'core.db' not in sys.modules
from core.dependencies import log_audit as compatible
assert compatible is log_audit
print(json.dumps({'pure_import_no_engine':True,'rollback':True,'commit':True,'same_callable':True}))
engine.dispose()
"""
    environment=dict(os.environ,PYTHONPATH=str(repo/'backend'),DATABASE_URL='sqlite:///'+str(database),JWT_SECRET_KEY=secrets.token_urlsafe(48))
    environment.pop('API_KEY',None)
    result=subprocess.run([sys.executable,'-c',script,'sqlite:///'+str(database)],cwd=repo,env=environment,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,{'exit':result.returncode}
    assert json.loads(result.stdout)=={'pure_import_no_engine':True,'rollback':True,'commit':True,'same_callable':True}
