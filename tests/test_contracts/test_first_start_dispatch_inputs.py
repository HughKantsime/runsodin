"""Shared dispatch-input proof stays read-only and is not financial authority."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_dispatch_observation_binding_behavior import observed


@pytest.mark.parametrize('drift', [False, True])
def test_exact_dispatch_input_validation_never_binds_receipt(observed, drift):
    from core.dispatch_observations import _matches_prepared_dispatch
    engine, key = observed
    with engine.begin() as connection:
        claim = dict(connection.execute(text('SELECT * FROM printer_dispatch_claims WHERE printer_id=59')).mappings().one())
        receipt = dict(connection.execute(text('SELECT * FROM dispatch_attempt_receipts WHERE job_id=53')).mappings().one())
        observation = dict(connection.execute(text('SELECT * FROM print_jobs WHERE id=70')).mappings().one())
        if drift:
            observation['filename'] = 'owned-wrong-file.gcode'
        assert _matches_prepared_dispatch(connection, printer_id=59, observed=observation,
                                          claim=claim, receipt=receipt, key=key) is (not drift)
        assert connection.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one() is None
        assert connection.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=70')).scalar_one() == 53
        assert connection.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'printing'
