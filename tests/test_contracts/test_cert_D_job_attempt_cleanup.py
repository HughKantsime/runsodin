"""cert-D: every job-deletion path clears that job's print-attempt markers (SQLite id reuse is safe)."""
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_retention_cleanup_behavior import allowlist_app, owned_runtime, review_db  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
from tests.test_contracts import job_census


def test_census_every_job_deletion_site_clears_attempt_markers():
    """Per call site: the function that deletes jobs also clears their print-attempt markers."""
    _, deletes, clearing = job_census.scan()
    offenders = [site for site in deletes if not clearing.get((site[0], site[1]))]
    assert offenders == [], offenders
    assert {site[1] for site in deletes} >= {'delete_job', 'bulk_update_jobs', 'run_retention_cleanup'}


@pytest.mark.parametrize('planted', [
    "def f(db):\n    db.execute(text('DELETE FROM jobs WHERE id=1'))",
    "def f(db):\n    db.execute(text('DELETE FROM \"jobs\" WHERE id=1'))",
    "def f(db):\n    db.execute(text('delete from jobs as j where j.id=1'))",
    "def f(db, job):\n    db.delete(job)",
    "def f(session, target: Job):\n    session.delete(target)",
    "def f(db):\n    row = db.get(Job, 1)\n    db.delete(row)",
    "def f(db):\n    db.execute(delete(Job).where(Job.id == 1))",
    "def f(db):\n    db.execute(sa_delete(Job))",
    "def f(db):\n    db.execute(Job.__table__.delete())",
    "def f(db):\n    db.query(Job).filter(Job.id == 1).delete()",
])
def test_census_detects_each_deletion_shape_without_a_clear(planted):
    _, deletes, clearing = job_census.scan({'backend/modules/planted.py': planted})
    assert deletes and not any(clearing.get((d[0], d[1])) for d in deletes), planted


def test_census_is_per_call_site_a_clear_elsewhere_in_the_file_does_not_count():
    source = ("def purge(db):\n    db.execute(text('DELETE FROM jobs WHERE id=1'))\n"
              "def tidy(db):\n    clear_job_attempts(run, 1)\n")
    _, deletes, clearing = job_census.scan({'backend/modules/planted.py': source})
    assert [(d[1], clearing[(d[0], d[1])]) for d in deletes] == [('purge', False)]
    assert not job_census.scan({'backend/modules/planted.py': "q = 'DELETE FROM print_jobs WHERE id=1'"})[1]


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
