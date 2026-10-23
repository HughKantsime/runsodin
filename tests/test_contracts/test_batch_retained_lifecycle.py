"""Production qualification after actual dispatch, observation and claim retirement."""
from datetime import datetime, timezone
import hashlib
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


def test_actual_clean_dispatch_lifecycle_has_no_sender_and_remains_eligible(review_db, tmp_path, monkeypatch):
    from core.registry import registry
    from modules.orders.production import OrderFulfillmentService
    monkeypatch.setitem(registry._providers, "OrderFulfillmentProvider", OrderFulfillmentService())
    from core import auth
    from core.economic_identity import _add_created
    from core.economic_source_creation import _create_attempt
    from core.dispatch_observations import bind_generic_observation
    from core.dispatch_claims import retire_observed_terminal
    from modules.jobs.models import Job
    from modules.printers import dispatch
    from modules.orders.batch_production import job_evidence
    engine, path = configure_dispatch(review_db, tmp_path, monkeypatch)
    names = []
    monkeypatch.setattr(dispatch, '_dispatch_moonraker',
                        lambda jid, path, name, creds: (names.append(name) or True, 'Synthetic accepted'))
    with review_db.begin():
        job = _add_created(review_db, Job(item_name='Physical batch', status='scheduled', quantity=1,
            model_id=51, printer_id=59, print_file_id=52, plate_index=0,
            payload_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), charged_to_org_id=1),
            'job', owner_org_id=1)
        jid = job.id
    assert dispatch.dispatch_job(59, jid)[0]
    with engine.begin() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM dispatch_sender_registrations')).scalar_one() == 0
        attempt = _create_attempt(conn, {'printer_id':59, 'scheduled_job_id':jid,
            'status':'running', 'started_at':datetime.now(timezone.utc),
            'observed_received_at':datetime.now(timezone.utc), 'filename':names[0], 'job_name':names[0]})
        observation_id = attempt.id
        # Use the database clock, as the shipped start planner does; SQLite's
        # second-resolution CURRENT_TIMESTAMP must not precede the observation.
        conn.execute(text('UPDATE print_jobs SET started_at=CURRENT_TIMESTAMP,observed_received_at=CURRENT_TIMESTAMP WHERE id=:id'), {'id':observation_id})
    assert bind_generic_observation(engine, printer_id=59, print_job_id=observation_id,
                                    key=auth.SECRET_KEY.encode()) == 'matched'
    with engine.begin() as conn:
        conn.execute(text("UPDATE jobs SET status='completed',actual_start=CURRENT_TIMESTAMP,actual_end=CURRENT_TIMESTAMP WHERE id=:id"), {'id':jid})
        conn.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"), {'id':observation_id})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        job_evidence(review_db, 1, jid)
    assert 'retired' in exc.value.detail
    review_db.rollback()
    assert retire_observed_terminal(engine, printer_id=59, print_job_id=observation_id) == 'retired'
    evidence = job_evidence(review_db, 1, jid)
    assert evidence['observation_id'] == observation_id
    assert evidence['payload_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert review_db.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims WHERE job_id=:id'), {'id':jid}).scalar_one() == 0
