"""Already-admitted authority recheck; actual disposable application database."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_dispatch_final_admission_behavior import setup, review_db, state
from modules.printers.dispatch_admission import admit


def test_recheck_retains_admitted_attempt_and_original_receipt(setup):
    from modules.printers.dispatch_admission import recheck_admitted
    engine, provider, inputs = setup
    assert admit(engine, **inputs).admitted
    with engine.connect() as conn:
        before = conn.execute(text('SELECT * FROM printer_dispatch_claims')).mappings().one()
    assert recheck_admitted(engine, **inputs).admitted
    with engine.connect() as conn:
        after = conn.execute(text('SELECT * FROM printer_dispatch_claims')).mappings().one()
    assert dict(after) == dict(before)
    assert state(engine) == 'adapter_entered'


@pytest.mark.parametrize('statement', [
    "UPDATE jobs SET hold=1 WHERE id=53",
    "UPDATE jobs SET ams_mapping='[1]' WHERE id=53",
    "UPDATE print_files SET stored_path='/changed.gcode' WHERE id=52",
    "UPDATE printers SET api_host='changed.example.test' WHERE id=59",
    "UPDATE printers SET org_id=2 WHERE id=59",
    "UPDATE printer_dispatch_claims SET state='uncertain'",
    "UPDATE printer_dispatch_claims SET token='ffffffffffffffffffffffffffffffff'",
    "UPDATE printer_dispatch_claims SET payload_sha256='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
])
def test_post_upload_drift_refuses_and_preserves_existing_fence(setup, statement):
    from modules.printers.dispatch_admission import recheck_admitted
    engine, provider, inputs = setup
    assert admit(engine, **inputs).admitted
    with engine.begin() as conn:
        conn.execute(text(statement))
    assert not recheck_admitted(engine, **inputs).admitted
    assert state(engine) in ('adapter_entered', 'uncertain')


def test_preparing_claim_cannot_be_rechecked_as_admitted(setup):
    from modules.printers.dispatch_admission import recheck_admitted
    engine, provider, inputs = setup
    assert not recheck_admitted(engine, **inputs).admitted
    assert state(engine) == 'preparing'


@pytest.mark.parametrize('change', ['none', 'token', 'revoked', 'center', 'revision', 'claim_state'])
def test_post_upload_education_reservation_and_authority_still_required(setup, monkeypatch, change):
    from tests.test_contracts.test_dispatch_final_admission_behavior import education_inputs
    from modules.printers.dispatch_admission import recheck_admitted
    engine, provider, _ = setup
    inputs = education_inputs(setup, monkeypatch)
    assert admit(engine, **inputs).admitted
    if change == 'token':
        inputs['education_reservation'] = {**inputs['education_reservation'], 'token': 'f' * 32}
    elif change != 'none':
        statement = {
            'revoked': "UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9",
            'center': "UPDATE education_cost_centers SET state='archived' WHERE id=7",
            'revision': "UPDATE education_submissions SET lifecycle_revision=2 WHERE id=14",
            'claim_state': "UPDATE education_monitor_claims SET state='awaiting'",
        }[change]
        with engine.begin() as conn:
            conn.execute(text(statement))
    assert recheck_admitted(engine, **inputs).admitted is (change == 'none')
    assert state(engine) == 'adapter_entered'


@pytest.mark.parametrize('change', ['filename', 'receipt_missing', 'already_observed', 'power_unconfirmed'])
def test_post_upload_requires_original_unobserved_receipt(setup, change):
    from modules.printers.dispatch_admission import recheck_admitted
    engine, provider, inputs = setup
    assert admit(engine, **inputs).admitted
    if change == 'filename':
        inputs['transport_filename'] = 'different.gcode'
    elif change == 'power_unconfirmed':
        inputs['power_accepted'] = False
    else:
        with engine.begin() as conn:
            conn.execute(text('DELETE FROM dispatch_attempt_receipts' if change == 'receipt_missing'
                              else 'UPDATE dispatch_attempt_receipts SET observed_print_job_id=99'))
    assert not recheck_admitted(engine, **inputs).admitted
    assert state(engine) == 'adapter_entered'
