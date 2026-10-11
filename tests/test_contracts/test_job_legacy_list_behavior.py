"""Preserved audit reproduction: legacy nullable job fields break list serialization."""
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from sqlalchemy import text
import pytest


def test_existing_nullable_job_rows_do_not_crash_scoped_listing(bom_client, review_db):
    from modules.jobs.routes import jobs_crud
    bom_client.app.include_router(jobs_crud.router)
    with review_db.get_bind().begin() as connection:
        connection.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,hold,is_locked) VALUES('Synthetic legacy null job','pending',1,NULL,NULL,NULL)"))
    response = bom_client.get('/jobs')
    assert response.status_code == 200, response.text
    assert any(job['item_name'] == 'Synthetic legacy null job' for job in response.json())


def test_nullable_job_detail_and_unrelated_edit_preserve_history(bom_client, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    bom_client.app.include_router(jobs_crud.router)
    with review_db.get_bind().begin() as connection:
        identifier = connection.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,hold,is_locked) VALUES('Synthetic unknown history','pending',1,NULL,NULL,NULL) RETURNING id")).scalar_one()
    response = bom_client.get(f'/jobs/{identifier}')
    assert response.status_code == 200, response.text
    assert [response.json()[field] for field in ('quantity', 'hold', 'is_locked')] == [None, None, None]
    response = bom_client.patch(f'/jobs/{identifier}', json={'notes': 'An unrelated note'})
    assert response.status_code == 200, response.text
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT quantity,hold,is_locked,notes FROM jobs WHERE id=:id'), {'id': identifier}).one() == (None, None, None, 'An unrelated note')
    for field in ('quantity', 'hold', 'is_locked'):
        response = bom_client.patch(f'/jobs/{identifier}', json={field: None})
        assert response.status_code == 422, response.text
    response = bom_client.patch(f'/jobs/{identifier}', json={'quantity': 2, 'hold': False, 'is_locked': True})
    assert response.status_code == 200, response.text
    assert [response.json()[field] for field in ('quantity', 'hold', 'is_locked')] == [2, False, True]
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT quantity,hold,is_locked FROM jobs WHERE id=:id'), {'id': identifier}).one() == (2, 0, 1)


def test_unknown_quantity_has_no_invented_duration_and_scheduler_skips_it(bom_client, review_db):
    from datetime import datetime, timezone
    from modules.jobs.models import Job
    from modules.jobs.scheduler import Scheduler
    with review_db.get_bind().begin() as connection:
        identifier = connection.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,model_id,quantity,hold,is_locked) VALUES('Synthetic unknown duration','pending',1,101,NULL,FALSE,FALSE) RETURNING id")).scalar_one()
    review_db.expire_all()
    job = review_db.query(Job).filter(Job.id == identifier).one()
    assert job.effective_duration is None
    result = Scheduler().run(review_db, start_date=datetime.now(timezone.utc))
    assert any(f'job {identifier}: quantity is unknown' in error for error in result.errors)
    review_db.expire_all()
    job = review_db.query(Job).filter(Job.id == identifier).one()
    assert job.status.value == 'pending' and job.printer_id is None and job.quantity is None


def test_manual_move_rejects_unknown_duration_without_changing_history(bom_client, review_db, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_lifecycle
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    bom_client.app.include_router(jobs_lifecycle.router)
    with review_db.get_bind().begin() as connection:
        identifier = connection.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,model_id,quantity,hold,is_locked) VALUES('Synthetic unknown manual duration','pending',1,101,NULL,FALSE,FALSE) RETURNING id")).scalar_one()
    response = bom_client.patch(f'/jobs/{identifier}/move', json={'printer_id': 9,
        'scheduled_start': (datetime.now(timezone.utc)+timedelta(days=1)).isoformat()})
    assert response.status_code == 409, response.text
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT status,printer_id,scheduled_start,quantity FROM jobs WHERE id=:id'), {'id':identifier}).one() == ('pending', None, None, None)


@pytest.mark.parametrize('history_status', ['completed', 'printing', 'scheduled'])
def test_unknown_capacity_blocks_active_work_but_not_completed_history(bom_client, review_db, history_status):
    from datetime import datetime, timezone, timedelta
    from modules.jobs.scheduler import Scheduler
    from modules.jobs.models import Job
    from core.base import JobStatus
    start = datetime.now(timezone.utc)+timedelta(days=1)
    with review_db.get_bind().begin() as connection:
        connection.execute(text('UPDATE printers SET is_active=FALSE WHERE id != 9'))
    known = Job(item_name='Synthetic schedulable job', quantity=1, duration_hours=1, hold=False,
                is_locked=False, status=JobStatus.PENDING, charged_to_org_id=1)
    review_db.add(known)
    review_db.commit()
    baseline = Scheduler().run(review_db, start_date=start)
    assert baseline.scheduled_count == 1
    review_db.expire_all()
    known = review_db.query(Job).filter(Job.id == known.id).one()
    known.status = JobStatus.PENDING
    known.printer_id = None
    known.scheduled_start = None
    known.scheduled_end = None
    review_db.add(Job(item_name='Synthetic completed unknown history', quantity=None, duration_hours=1,
        hold=False, is_locked=True, status=JobStatus(history_status), charged_to_org_id=1,
        printer_id=9, scheduled_start=start+timedelta(hours=1) if history_status == 'scheduled' else start-timedelta(days=30), actual_end=start-timedelta(days=29) if history_status == 'completed' else None))
    review_db.flush()
    # ORM default1 cannot create NULL; preserve a real legacy unknown row explicitly.
    review_db.execute(text("UPDATE jobs SET quantity=NULL WHERE item_name='Synthetic completed unknown history'"))
    review_db.commit()
    result = Scheduler().run(review_db, start_date=start)
    assert result.scheduled_count == (1 if history_status == "completed" else 0), result.errors
