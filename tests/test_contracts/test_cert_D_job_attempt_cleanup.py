"""cert-D: every job-deletion path clears that job's print-attempt markers (SQLite id reuse is safe)."""
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_retention_cleanup_behavior import allowlist_app, owned_runtime, review_db  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
_DELETES = re.compile(r'DELETE\s+FROM\s+jobs\b|\bdb\.delete\(\s*\w*job\w*\s*\)|query\(\s*Job\s*\)[^\n]*\.delete\(', re.I)


def test_census_every_job_deletion_path_clears_attempt_markers():
    offenders = []
    for path in (ROOT / 'backend').rglob('*.py'):
        rel = path.relative_to(ROOT).as_posix()
        if 'preserved027' in rel or '/migrations/' in rel or rel.endswith('core/print_attempts.py'):
            continue
        source = path.read_text()
        if _DELETES.search(source) and 'clear_job_attempts' not in source:
            offenders.append(rel)
    assert offenders == [], offenders


def test_retention_purge_clears_attempt_markers(allowlist_app, review_db):
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,priority,hold,is_locked,updated_at) "
                           "VALUES(4401,'Old print','completed',1,3,0,0,'2000-01-01')"))
    for key in ('quota_attempt:seq:4401', 'quota_attempt:job:4401:0', 'quota_attempt:filament:job:4401:1'):
        review_db.execute(text("INSERT INTO system_config(key,value) VALUES(:k,'true')"), {'k': key})
    review_db.commit()
    with TestClient(allowlist_app, client=('198.51.100.7', 50000), raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention', json={'completed_jobs_days': 30}).status_code == 200
        assert client.post('/api/admin/retention/cleanup').status_code == 200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=4401')).scalar_one() == 0
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'quota_attempt:%4401%'")).scalar_one() == 0
