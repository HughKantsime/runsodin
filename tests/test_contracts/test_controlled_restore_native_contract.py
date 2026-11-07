"""Portable fixture checks for the real PostgreSQL-only CV05 restore proof."""
from sqlalchemy import text

from ops.database_parity.controlled_restore_native import TABLES, seed, snapshot
from tests.test_contracts.test_education_review_workflow import review_db


def test_native_restore_fixture_has_real_marks_audience_clock_and_tombstone(review_db):
    engine = review_db.get_bind()
    audience, file_id, live_job, deleted_job = seed(engine, 'owned-native-fixture')
    data = snapshot(engine)
    assert set(data) == set(TABLES)
    assert all(data.values()), 'Every controlled archive collection must contain fixture data'
    with engine.connect() as db:
        tombstone = db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed fixture query with bound deleted job ID
            'SELECT audience_id,retention_initial_days,retention_changes,audit_max_id '
            'FROM controlled_job_tombstones WHERE job_id=:job'), {'job': deleted_job}).one()
        assert tombstone[:3] == (audience, 7, '[]')
        assert tombstone.audit_max_id > 0
        assert db.execute(text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text -- fixed fixture query with bound deleted job ID
            'SELECT count(*) FROM jobs WHERE id=:job'), {'job': deleted_job}).scalar_one() == 0
