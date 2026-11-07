"""Restore a genuine 2.0.45 SQLite backup constructed only by preserved053 sources."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy import create_engine
from core.schema import bootstrap_database, preserved053
from modules.system import backup_service as backup


def test_released_2045_backup_restores_and_boots_054_055(tmp_path):
    historical = tmp_path / 'released.db'
    # -I plus the pinned loader makes importing any live E3 code impossible.
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen053')['prepare_sources']()
from sqlalchemy import create_engine
from core.schema import bootstrap_database
from modules.system.backup_service import create_online_backup
engine=create_engine('sqlite:///'+sys.argv[2])
result=bootstrap_database(engine)
assert 'python:053-term-retention' in result['applied']
assert not any('054-' in m or '055-' in m for m in result['applied'])
with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO groups(id,name) VALUES(91,'released-2.0.45')")
 c.exec_driver_sql("INSERT INTO jobs(id,item_name) VALUES(7,'retained-release-job')")
engine.dispose()
path,_=create_online_backup('sqlite:///'+sys.argv[2])
print('BACKUP='+str(path))
'''
    run = subprocess.run([sys.executable, '-I', '-c', code,
                          str(Path(preserved053.__file__).with_name('runner.py')), str(historical)],
                         env={**os.environ, 'PYTHONPYCACHEPREFIX': '/tmp/e3pyc'},
                         capture_output=True, text=True, timeout=180)
    assert run.returncode == 0, run.stderr
    candidate = Path(next(line.removeprefix('BACKUP=') for line in run.stdout.splitlines() if line.startswith('BACKUP=')))
    with sqlite3.connect(candidate) as c:
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'controlled_%'").fetchone()
    live = tmp_path / 'current' / 'odin.db'
    live.parent.mkdir()
    url = 'sqlite:///' + str(live)
    engine = create_engine(url)
    bootstrap_database(engine)
    engine.dispose()
    backup.stage_restore(candidate, url)
    assert backup.apply_pending_restore(url, pid_file=None)['status'] == 'applied_pending_validation'
    engine = create_engine(url)
    assert bootstrap_database(engine)['applied'] == ['python:054-controlled-parts', 'python:055-controlled-history']
    assert bootstrap_database(engine)['applied'] == []
    engine.dispose()
    backup.finalize_pending_restore(url, pid_file=None)
    with sqlite3.connect(live) as c:
        assert c.execute('SELECT name FROM groups WHERE id=91').fetchone()[0] == 'released-2.0.45'
        assert c.execute('SELECT item_name FROM jobs WHERE id=7').fetchone()[0] == 'retained-release-job'
        assert c.execute('SELECT high_water FROM job_id_high_water').fetchone()[0] >= 7
