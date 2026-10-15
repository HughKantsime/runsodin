"""cert-D jobsC F04-4.10: batch send contrast and exact API contract (real router + SQLite, no hardware)."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from tests.test_contracts.test_archive_reprint_behavior import reprint_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_job_batch_contract_behavior import batch_context  # noqa: F401


def request(queue_only, printer_ids=(9, 12)):
    return {'item_name': 'CertC batch', 'model_id': 101, 'printer_ids': list(printer_ids), 'priority': 1,
            'duration_hours': 1.0, 'filament_type': 'PLA', 'colors_required': None, 'notes': 'CertC', 'queue_only': queue_only}


def test_batch_response_is_one_pending_row_per_printer_in_request_order(batch_context):  # noqa: F811
    client, identity, db = batch_context
    response = client.post('/jobs/batch', json=request(False, (12, 9)))
    assert response.status_code == 200, response.text
    rows = response.json()
    assert [(r['printer_id'], r['status']) for r in rows] == [(12, 'pending'), (9, 'pending')]
    assert set(rows[0]) == {'id', 'printer_id', 'status'}
    db.expire_all()
    saved = db.execute(text('SELECT printer_id,hold,priority,filament_type,quantity,model_id FROM jobs WHERE id IN (:a,:b) ORDER BY id'),
                       {'a': rows[0]['id'], 'b': rows[1]['id']}).all()
    assert [tuple(s) for s in saved] == [(12, 0, 1, 'PLA', 1, 101), (9, 0, 1, 'PLA', 1, 101)]


def test_priority_defaults_to_three_and_limit_is_fifty(batch_context):  # noqa: F811
    client, identity, db = batch_context
    body = request(True, (9,)); del body['priority']; del body['queue_only']
    response = client.post('/jobs/batch', json=body)
    assert response.status_code == 200, response.text
    db.expire_all()
    assert tuple(db.execute(text('SELECT priority,hold FROM jobs WHERE id=:id'), {'id': response.json()[0]['id']}).one()) == (3, 0)
    before = db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
    too_many = client.post('/jobs/batch', json=request(False, range(1000, 1051)))
    assert too_many.status_code == 400 and 'Maximum 50' in too_many.text
    assert db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one() == before


def test_scheduler_schedules_normal_batch_but_leaves_queue_only_batch_staged(batch_context, monkeypatch):  # noqa: F811
    import core.db
    from modules.jobs.scheduler import Scheduler
    from modules.printers.dispatch import _get_next_scheduled_job
    client, identity, db = batch_context
    db.execute(text("UPDATE jobs SET status='completed' WHERE status IN ('pending','scheduled')"))
    db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(12,1,'PLA')"))
    db.commit()
    staged = [r['id'] for r in client.post('/jobs/batch', json=request(True)).json()]
    normal = [r['id'] for r in client.post('/jobs/batch', json=request(False)).json()]
    assert len(staged) == 2 and len(normal) == 2
    Scheduler().run(db, start_date=datetime(2026, 10, 20, tzinfo=timezone.utc)); db.commit(); db.expire_all()
    status = dict(db.execute(text('SELECT id,status FROM jobs WHERE id IN (:a,:b,:c,:d)'),
                             dict(zip('abcd', staged + normal))).all())
    assert [status[i] for i in staged] == ['pending', 'pending']
    assert [status[i] for i in normal] == ['scheduled', 'scheduled']
    # Auto-dispatch selection only ever considers scheduled jobs; staged jobs are never selected.
    monkeypatch.setattr(core.db, 'engine', db.get_bind())
    for printer in (9, 12):
        chosen = _get_next_scheduled_job(printer)
        assert chosen is None or chosen['id'] not in staged
