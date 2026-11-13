import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run_frozen(runner, body, *args, timeout=120):
    """Run ``body`` in the bundle's isolated interpreter, with its closed-source importer active."""
    return subprocess.run([sys.executable, '-I', '-c', RESTORE_ENV_PROGRAM + body, str(runner), *map(str, args)],
                          capture_output=True, text=True, timeout=timeout, cwd=tempfile.gettempdir())

RESTORE_CLOSURE = '''
from core.database_config import create_database_engine
from core.schema import bootstrap_database,validate_schema
from modules.system import postgres_backup_service as b
from modules.system.postgres_canonical_executables import validate_executables
assert b.__file__.startswith(str(bundle)),b.__file__
assert callable(b._validate_current_postgres_restore) and callable(b._restore_postgres_archive) and callable(validate_executables)
print('closure-imported',len(b.canonical_tables()))
'''

RESTORE_ENV_PROGRAM = '''
import os,runpy,sys,tempfile
folder=tempfile.mkdtemp(prefix='frozen-child-')
os.environ.update(DATABASE_URL='postgresql://owned@127.0.0.1:1/odin_validate_'+'a'*20,DATABASE_PASSWORD_FILE='/dev/null',
                  ODIN_DATA_DIR=folder,ODIN_DB_ROLE='restore',JWT_SECRET_KEY='k'*64,LANG='C.UTF-8',PYTHONWARNINGS='ignore')
namespace=runpy.run_path(sys.argv[1],run_name='frozen')
bundle,manifest=namespace['prepare_sources']()
'''
