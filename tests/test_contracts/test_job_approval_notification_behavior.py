"""Actual job HTTP workflow and production notification recipient persistence."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_user_limits import signed_license

@pytest.fixture
def approval_client(review_db,signed_license,monkeypatch):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.errors import OdinError
    from modules.jobs.routes import jobs_crud,jobs_lifecycle
    from modules.notifications import alert_dispatcher
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    signed_license(tier='education',features=[])
    monkeypatch.setattr(alert_dispatcher,'should_suppress_notification',lambda **_:True)
    def forbidden(*args,**kwargs):pytest.fail('External channel called in quiet approval test')
    monkeypatch.setattr(alert_dispatcher,'_deliver_email',forbidden)
    monkeypatch.setattr(alert_dispatcher,'_deliver_browser_push',forbidden)
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_job_approval','true')"));review_db.commit()
    app=FastAPI();app.include_router(jobs_crud.router,prefix="/api/v1");app.include_router(jobs_lifecycle.router,prefix="/api/v1")
    from core.middleware.dry_run import dry_run_middleware
    app.middleware("http")(dry_run_middleware)
    @app.exception_handler(OdinError)
    async def error(request,exc):return JSONResponse(status_code=exc.status,content=exc.to_envelope())
    def request_db():
        with Session(review_db.get_bind()) as db:yield db
    user={'id':1,'username':'Synthetic submitter','role':'viewer','group_id':1,'is_active':True}
    app.dependency_overrides[get_db]=request_db
    app.dependency_overrides[get_current_user]=lambda:user
    with TestClient(app) as client:yield client,user

@pytest.mark.parametrize('operation',['submit','resubmit'])
# An owner who cannot open the group's jobs (outside the group, or inactive) is not targeted;
# the alert falls back to the group's reviewers instead of reaching nobody (F11-11.4-02).
@pytest.mark.parametrize('owner,active,recipients',[(2,True,{2}),(None,True,{3}),(5,True,{3}),(2,False,{3})])
def test_actual_job_workflow_targets_owner_or_scoped_fallback(approval_client,review_db,operation,owner,active,recipients):
    client,user=approval_client
    review_db.execute(text('UPDATE groups SET owner_id=:owner WHERE id=1'),{'owner':owner})
    if not active:review_db.execute(text('UPDATE users SET is_active=0 WHERE id=2'))
    if operation=='resubmit':
        review_db.execute(text("INSERT INTO jobs(id,item_name,status,submitted_by,charged_to_user_id,charged_to_org_id,rejected_reason) VALUES(801,'Synthetic approval','rejected',1,1,1,'Synthetic prior rejection')"))
    review_db.commit()
    response=client.post('/api/v1/jobs',json={'item_name':'Synthetic approval'}) if operation=='submit' else client.post('/api/v1/jobs/801/resubmit')
    assert response.status_code==(201 if operation=='submit' else 200),response.text
    job_id=response.json()['id'] if operation=='submit' else 801
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':job_id}).scalar_one()=='submitted'
    rows=review_db.execute(text('SELECT user_id,alert_type,job_id,is_read FROM alerts')).all()
    assert set(tuple(row) for row in rows)=={(uid,'job_submitted',job_id,0) for uid in recipients}

@pytest.mark.parametrize('dry_run',[False,True])
@pytest.mark.parametrize('tier',['community','pro','expired_education'])
def test_approval_submission_cannot_bypass_current_feature_entitlement(approval_client,review_db,signed_license,tier,dry_run):
    client,user=approval_client;signed_license(tier='education' if tier=='expired_education' else tier,features=[],expires_at='2000-01-01' if tier=='expired_education' else '2099-01-01')
    before={table:review_db.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in ['jobs','alerts','audit_logs','quota_usage']}
    response=client.post('/api/v1/jobs',json={'item_name':'Synthetic unlicensed approval'},headers={'X-Dry-Run':'true'} if dry_run else {})
    assert response.status_code==403
    assert {table:review_db.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar_one() for table in before}==before

@pytest.mark.parametrize('role,enabled',[('viewer',False),('operator',True)])
def test_unlicensed_nonapproval_creation_remains_available(approval_client,review_db,signed_license,role,enabled):
    client,user=approval_client;user['role']=role;signed_license(tier='community',features=[])
    review_db.execute(text("UPDATE system_config SET value=:enabled WHERE key='require_job_approval'"),{'enabled':'true' if enabled else 'false'});review_db.commit()
    response=client.post('/api/v1/jobs',json={'item_name':'Synthetic ordinary job'})
    assert response.status_code==201,response.text
    assert response.json()['status']=='pending'
    assert review_db.execute(text('SELECT COUNT(*) FROM alerts')).scalar_one()==0


@pytest.mark.parametrize("held", [False, True])
def test_new_pending_job_emits_configured_queue_added_notification(approval_client,review_db,held):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    client,user=approval_client;user.update(id=3,role='operator',username='Synthetic operator')
    review_db.add(AlertPreference(user_id=3,alert_type=AlertType.QUEUE_ADDED,in_app=True,email=False,browser_push=False));review_db.commit()
    response=client.post('/api/v1/jobs',json={'item_name':'Synthetic queued origin','hold':held})
    assert response.status_code==201,response.text
    job_id=response.json()['id'];assert response.json()['status']=='pending'
    assert tuple(review_db.execute(text("SELECT user_id,alert_type,job_id FROM alerts WHERE alert_type='queue_added'")).one())==(3,'queue_added',job_id)


@pytest.mark.parametrize("case", ["disabled", "dry_run", "approval"])
def test_queue_origin_does_not_notify_without_real_pending_opt_in(approval_client, review_db, case):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    client, user = approval_client
    if case != "approval":
        user.update(id=3, role="operator", username="Synthetic operator")
    for uid in [1, 3, 5]:
        review_db.add(AlertPreference(user_id=uid, alert_type=AlertType.QUEUE_ADDED,
            in_app=case != "disabled", email=False, browser_push=False))
    review_db.commit()
    before_jobs = review_db.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one()
    response = client.post("/api/v1/jobs", json={"item_name": "Synthetic queue negative"},
        headers={"X-Dry-Run": "true"} if case == "dry_run" else {})
    assert response.status_code in (200, 201), response.text
    assert review_db.execute(text("SELECT COUNT(*) FROM alerts WHERE alert_type='queue_added'")).scalar_one() == 0
    if case == "dry_run":
        assert review_db.execute(text("SELECT COUNT(*) FROM jobs")).scalar_one() == before_jobs


def test_queue_added_origin_targets_creator_not_other_configured_users(approval_client, review_db):
    from core.base import AlertType
    from modules.notifications.models import AlertPreference
    client, user = approval_client
    user.update(id=3, role="operator", username="Synthetic operator")
    for uid in [2, 3, 5]:
        review_db.add(AlertPreference(user_id=uid, alert_type=AlertType.QUEUE_ADDED,
            in_app=True, email=False, browser_push=False))
    review_db.commit()
    response = client.post("/api/v1/jobs", json={"item_name": "Synthetic creator-only queue"})
    assert response.status_code == 201, response.text
    assert list(review_db.execute(text("SELECT user_id FROM alerts WHERE alert_type='queue_added'")).scalars()) == [3]


@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("failure", ["audit", "quota", "preferences", "alert", "scope", "commit", "uncertain_commit"])
def test_creation_transaction_failures_never_deliver_external(approval_client, review_db, monkeypatch, approval, failure):
    from core.base import AlertType
    from modules.notifications import alert_dispatcher
    from modules.notifications.models import AlertPreference
    from modules.jobs.routes import jobs_crud
    client, user = approval_client
    if not approval:
        user.update(id=3, role="operator", username="Synthetic operator")
    user["quota_jobs"] = 20
    if approval:
        review_db.execute(text("UPDATE groups SET owner_id=2 WHERE id=1"))
    typ = AlertType.JOB_SUBMITTED if approval else AlertType.QUEUE_ADDED
    if failure != "preferences":
        review_db.execute(text("DELETE FROM alert_preferences WHERE alert_type=:typ"), {"typ": typ.value})
        review_db.add(AlertPreference(user_id=2 if approval else 3, alert_type=typ,
            in_app=True, email=True, browser_push=True))
    review_db.commit()
    tables = ["jobs", "audit_logs", "quota_usage", "alerts", "alert_preferences"]
    before = {t: review_db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in tables}
    called = []
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", lambda *args: called.append(True))
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic transaction failure")
    if failure == "scope":
        from modules.notifications import recipient_policy
        monkeypatch.setattr(recipient_policy, "resolve_alert_scope", fail)
    elif failure == "audit":
        monkeypatch.setattr(jobs_crud, "log_audit", fail)
    elif failure == "preferences":
        original = alert_dispatcher.seed_alert_preferences
        def seed_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            fail()
        monkeypatch.setattr(alert_dispatcher, "seed_alert_preferences", seed_then_fail)
    elif failure == "alert":
        original = alert_dispatcher._deliver_in_app
        def alert_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            fail()
        monkeypatch.setattr(alert_dispatcher, "_deliver_in_app", alert_then_fail)
    elif failure == "quota":
        original = Session.execute
        def execute(db, statement, *args, **kwargs):
            if "INSERT INTO quota_usage" in str(statement):
                fail()
            return original(db, statement, *args, **kwargs)
        monkeypatch.setattr(Session, "execute", execute)
    else:
        original = Session.commit
        def commit(db):
            if failure == "uncertain_commit":
                original(db)
            fail()
        monkeypatch.setattr(Session, "commit", commit)
    with pytest.raises(RuntimeError, match="Synthetic transaction failure"):
        client.post("/api/v1/jobs", json={"item_name": "Synthetic failed creation"})
    assert called == []
    review_db.rollback()
    after = {t: review_db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one() for t in tables}
    if failure == "uncertain_commit":
        assert after["jobs"] == before["jobs"] + 1
        assert after["alerts"] == before["alerts"] + 1
        assert after["audit_logs"] == before["audit_logs"] + 1
        assert after["quota_usage"] == before["quota_usage"] + 1
    else:
        assert after == before


@pytest.mark.parametrize("approval", [False, True])
def test_job_external_delivery_runs_only_after_persisted_commit(approval_client, review_db, monkeypatch, approval):
    from core.base import AlertType
    from modules.notifications import alert_dispatcher
    from modules.notifications.models import AlertPreference
    client, user = approval_client
    if not approval:
        user.update(id=3, role="operator", username="Synthetic operator")
    if approval:
        review_db.execute(text("UPDATE groups SET owner_id=2 WHERE id=1"))
    typ = AlertType.JOB_SUBMITTED if approval else AlertType.QUEUE_ADDED
    review_db.execute(text("DELETE FROM alert_preferences WHERE alert_type=:typ"), {"typ":typ.value})
    review_db.add(AlertPreference(user_id=2 if approval else 3, alert_type=typ,
        in_app=True, email=True, browser_push=True))
    review_db.commit()
    observed = []
    def deliver(db, plan):
        assert not db.new
        with Session(review_db.get_bind()) as independent:
            assert independent.execute(text("SELECT COUNT(*) FROM jobs WHERE id=:id"), {"id":plan.job_id}).scalar_one() == 1
            assert independent.execute(text("SELECT COUNT(*) FROM alerts WHERE job_id=:id"), {"id":plan.job_id}).scalar_one() == 1
        observed.append(plan.alert_type)
    monkeypatch.setattr(alert_dispatcher, "deliver_staged_alert", deliver)
    response = client.post("/api/v1/jobs", json={"item_name":"Synthetic committed delivery"})
    assert response.status_code == 201, response.text
    assert observed == [typ]


@pytest.mark.parametrize('approval', [False, True])
def test_quota_enabled_creation_preview_has_no_accounting_writes(approval_client, review_db, approval):
    client, user = approval_client
    user['quota_jobs'] = 20
    if not approval:
        user.update(id=3, role='operator')
    tables = ['jobs', 'alerts', 'audit_logs', 'quota_usage', 'alert_preferences']
    before = {t: review_db.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar_one() for t in tables}
    response = client.post('/api/v1/jobs', json={'item_name': 'Synthetic quota preview'}, headers={'X-Dry-Run':'true'})
    assert response.status_code == 201, response.text
    assert response.json()['dry_run'] is True
    review_db.rollback()
    assert {t: review_db.execute(text(f'SELECT COUNT(*) FROM {t}')).scalar_one() for t in tables} == before
