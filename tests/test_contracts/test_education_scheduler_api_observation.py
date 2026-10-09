"""Real scheduler route/role boundary after Education approval; no hardware."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db,_principal
from tests.test_contracts.test_education_upload_scheduler_integration import _upload_and_approve


def client_for(db,principal,monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.jobs.scheduler_routes import router
    from modules.jobs.scheduler import registry
    from modules.organizations.services import EducationPolicyService
    original=registry.get_provider
    monkeypatch.setattr(registry,'get_provider',lambda name:EducationPolicyService() if name=='EducationPolicyProvider' else original(name))
    app=FastAPI();app.include_router(router,prefix='/api')
    app.dependency_overrides[get_db]=lambda:db
    app.dependency_overrides[get_current_user]=lambda:principal
    return TestClient(app)


def test_manager_denied_operator_schedules_and_repeat_is_safe(review_db,tmp_path,monkeypatch):
    job=_upload_and_approve(review_db,tmp_path,'Bambu Lab P1S')
    manager=client_for(review_db,_principal(2),monkeypatch)
    assert manager.post('/api/scheduler/run').status_code==403
    assert review_db.execute(text('SELECT COUNT(*) FROM scheduler_runs')).scalar_one()==0
    operator=client_for(review_db,_principal(3,role='operator'),monkeypatch)
    response=operator.post('/api/scheduler/run')
    assert response.status_code==200,response.text
    assert response.json()['scheduled']==1
    row=review_db.execute(text('SELECT s.status,j.status,j.printer_id FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'),{'id':job['id']}).one()
    assert tuple(row)==('scheduled','scheduled',9)
    assert operator.post('/api/scheduler/run').json()['scheduled']==0


@pytest.mark.parametrize('obstacle',['held','too_long'])
def test_scheduler_api_does_not_force_unfit_or_held_jobs(review_db,tmp_path,monkeypatch,obstacle):
    job=_upload_and_approve(review_db,tmp_path,'P1S')
    change='hold=1' if obstacle=='held' else 'duration_hours=1000'
    review_db.execute(text('UPDATE jobs SET '+change+' WHERE id=:id'),{'id':job['job_id']});review_db.commit()
    response=client_for(review_db,_principal(3,role='admin'),monkeypatch).post('/api/scheduler/run')
    assert response.status_code==200,response.text
    assert response.json()['scheduled']==0
    row=review_db.execute(text('SELECT s.status,j.status FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'),{'id':job['id']}).one()
    assert tuple(row)==('pending','pending')
    if obstacle=='too_long': assert response.json()['skipped']>=1


@pytest.mark.parametrize('stored_aware,horizon_aware',[(False,True),(True,False),(False,False),(True,True)])
def test_slot_comparison_restores_sqlite_utc_semantics(stored_aware,horizon_aware):
    from datetime import datetime,timezone,timedelta
    from modules.jobs.scheduler import Scheduler
    stored=datetime(2026,10,9,12,15)
    horizon=datetime(2026,10,9,12)
    if stored_aware: stored=stored.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=-4)))
    if horizon_aware: horizon=horizon.replace(tzinfo=timezone.utc)
    assert Scheduler()._time_to_slot(stored,horizon)==1
