"""031 (jobs.pinned_printer_id): module behaviour now; runtime registration pending."""
from pathlib import Path
import importlib

import pytest
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[2]
PENDING = 'pending post-027 migration registration'


def _migration():
    return importlib.import_module('core.schema.migrations.031_job_printer_pins')


def test_031_adds_column_and_backfills_existing_explicit_pins_only(tmp_path):
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'pin031.db'}")
    bootstrap_database(engine, ROOT / 'backend')
    with engine.begin() as c:
        c.execute(text("INSERT INTO jobs(id,item_name,status,quantity,priority,hold,is_locked,target_type,target_filter) VALUES "
                       "(1,'pinned','pending',1,3,0,0,'specific','12'),(2,'free','pending',1,3,0,0,'specific',NULL),"
                       "(3,'model target','pending',1,3,0,0,'model','12'),(4,'junk','pending',1,3,0,0,'specific','P1S')"))
    with engine.begin() as c:
        _migration().apply(c)
        _migration().apply(c)  # idempotent
    with engine.connect() as c:
        got = dict(c.execute(text('SELECT id,pinned_printer_id FROM jobs ORDER BY id')).all())
    assert got == {1: 12, 2: None, 3: None, 4: None}
    engine.dispose()


@pytest.mark.xfail(strict=True, reason=PENDING)
def test_031_is_applied_by_bootstrap():
    from core.schema import bootstrap_database
    engine = create_engine('sqlite://')
    result = bootstrap_database(engine, ROOT / 'backend')
    assert 'python:031-job-printer-pins' in result['applied']
    assert 'pinned_printer_id' in {col['name'] for col in inspect(engine).get_columns('jobs')}
    engine.dispose()


@pytest.mark.xfail(strict=True, reason=PENDING)
def test_031_column_is_part_of_the_job_orm():
    from modules.jobs.models import Job
    assert 'pinned_printer_id' in Job.__table__.columns
