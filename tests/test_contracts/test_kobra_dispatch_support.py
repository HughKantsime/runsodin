"""Kobra admission and placement are blocked; observed prints remain linkable."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch
from tests.test_contracts.test_cert_D_jobsB_scheduler import fleet, run, rows, add_job, only_active
from tests.test_contracts.test_cert_D_job_state_machine import _pin_client, _job, _status
from tests.test_contracts.test_license_user_limits import signed_license
from modules.printers.dispatch_support import dispatch_unsupported_reason, KOBRA_DISPATCH_DETAIL


@pytest.mark.parametrize('field', ['model', 'machine_type'])
@pytest.mark.parametrize('model', ['Kobra S1', 'kObRa s1 mAx', 'Anycubic Kobra 3', 'Kobra 3 V2', 'Kobra 3 Max'])
def test_identity_matches_either_field(field, model):
    assert dispatch_unsupported_reason({'api_type': 'moonraker', field: model}) == (
        'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL)


@pytest.mark.parametrize('printer', [
    {'api_type': 'bambu', 'model': 'Kobra S1'},
    {'api_type': 'moonraker', 'model': 'Voron 2.4', 'ip': 'Kobra S1'},
    {'api_type': 'moonraker', 'model': 'Kobra 2'},
    {'api_type': 'moonraker', 'model': 'Kobra S10'},
])
def test_other_printers_unaffected(printer):
    assert dispatch_unsupported_reason(printer) is None


def test_dispatch_refuses_before_power_read_or_upload(review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch, dispatch_acquisition, smart_plug
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET model='Kobra S1',machine_type='Rinkhals' WHERE id=59"))
        conn.execute(text("UPDATE jobs SET status='pending' WHERE id=53"))
    def forbidden(*a, **kw):
        pytest.fail('Blocked printer must not reach device I/O')
    monkeypatch.setattr(dispatch, '_dispatch_moonraker', forbidden)
    monkeypatch.setattr(dispatch_acquisition, 'bounded_status_is_idle', forbidden)
    monkeypatch.setattr(smart_plug, 'prepare_bounded_dispatch_power', forbidden)
    assert dispatch.dispatch_job(59, 53) == (False, 'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'pending'
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0


def test_final_admission_rechecks_kobra_identity():
    from modules.printers.dispatch_admission import admit
    result = admit(None, token=None, job=None, creds={'api_type': 'moonraker', 'machine_type': 'Kobra 3 Max'},
                   key=None, payload_sha256=None, transport_filename=None, power_accepted=False)
    assert not result.admitted and result.reason == 'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL


@pytest.mark.parametrize('pinned', [True, False])
def test_scheduler_excludes_kobra_and_keeps_targeted_job_pending(fleet, pinned):
    only_active(fleet, [11, 12])
    fleet.execute(text("UPDATE printers SET api_type='moonraker',model='Kobra S1',machine_type='Rinkhals' WHERE id=11"))
    fleet.commit()
    add_job(fleet, 990, printer=11 if pinned else None)
    run(fleet)
    assert rows(fleet, [990])[990][:2] == (('pending', 11) if pinned else ('scheduled', 12))


def test_manual_link_print_on_kobra_remains_available(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    _job(fleet, 991, 'pending')
    fleet.execute(text("UPDATE jobs SET hold=1 WHERE id=991"))
    fleet.execute(text("UPDATE printers SET api_type='moonraker',model='Kobra S1' WHERE id=9"))
    fleet.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status) "
                       "VALUES(7991,9,'attended.gcode','attended',CURRENT_TIMESTAMP,'printing')"))
    fleet.commit()
    response = client.post('/api/v1/jobs/991/link-print', params={'print_job_id': 7991})
    assert response.status_code == 200, response.text
    assert response.json()['status_applied'] is True
    assert _status(fleet, 991) == 'printing'
    assert fleet.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=7991')).scalar_one() == 991


def test_next_job_suggestion_excludes_kobra(fleet):
    from modules.printers.models import Printer
    from modules.jobs.completion_summary import next_suggestion, eligible
    from modules.jobs.models import Job
    fleet.execute(text("UPDATE printers SET api_type='moonraker',model='Kobra S1' WHERE id=9"))
    fleet.commit()
    add_job(fleet, 992)
    printer = fleet.get(Printer, 9)
    assert not eligible(fleet, fleet.get(Job, 992), printer)
    suggestion = next_suggestion(fleet, printer)
    assert suggestion['job_id'] is None
    assert suggestion['reason'] == 'printer_dispatch_unsupported: ' + KOBRA_DISPATCH_DETAIL
