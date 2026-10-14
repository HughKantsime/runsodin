"""Real monitor insertion paths must record future-only unresolved births.

These ordinary observations deliberately have no trusted commerce parent.
They must not acquire a billed identity, but still need a durable source lifetime.
"""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_attempt_spool_capture_behavior import capture_case
from tests.test_contracts.test_attempt_spool_capture_behavior import (
    test_actual_start_paths_capture_in_same_commit as exercise_actual_start,
)


@pytest.mark.parametrize('path', ['mqtt', 'generic-events', 'moonraker'])
def test_actual_monitor_start_creates_unresolved_source_lifetime(capture_case, monkeypatch, path):
    # Reuse the existing actual callback fixture, including only owned DB access
    # and event-delivery suppression. No command or printer transport is invoked.
    exercise_actual_start(capture_case, monkeypatch, path)
    with capture_case.connect() as connection:
        row = connection.execute(text(
            'SELECT id,economic_birth_nonce,economic_uid,scheduled_job_id '
            'FROM print_jobs'
        )).one()
        assert isinstance(row.economic_birth_nonce, str) and len(row.economic_birth_nonce) == 32
        assert row.economic_uid is None and row.scheduled_job_id is None
        lifetime = connection.execute(text(
            'SELECT resource_kind,resource_id,state,identity_uid,owner_scope,owner_key '
            'FROM cost_source_lifetimes WHERE birth_nonce=:nonce'
        ), {'nonce': row.economic_birth_nonce}).one()
        assert tuple(lifetime) == ('attempt', row.id, 'unresolved', None, None, None)
