"""Ordinary queue routes and durable quota admission using disposable databases."""
import asyncio
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.quota import charge_job_quota, _get_period_key
from core.errors import OdinError, ErrorCode

@pytest.fixture
def quota_engine(tmp_path):
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'quota.db'}", connect_args={'timeout': 15})
    bootstrap_database(engine, Path(__file__).resolve().parents[2] / 'backend')
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(1,'quota','quota@example.test','fixture','operator',true)"))
    yield engine
    engine.dispose()

def actor(**limits):
    return dict(id=1, role='operator', group_id=None, quota_period='monthly', **limits)

def usage(engine):
    with engine.connect() as conn:
        return conn.execute(text('SELECT jobs_used,grams_used,hours_used FROM quota_usage')).all()

@pytest.mark.parametrize('kind', ['jobs', 'grams', 'hours'])
def test_recorded_exhaustion_rejects_without_charge(quota_engine, kind):
    with quota_engine.begin() as conn:
        conn.execute(text(f'INSERT INTO quota_usage(user_id,period_key,{kind}_used) VALUES(1,:key,5)'), {'key': _get_period_key('monthly')})
    with Session(quota_engine) as db, pytest.raises(OdinError) as error:
        charge_job_quota(db, actor(**{'quota_'+kind:5}))
    assert error.value.status == 429 and error.value.code == ErrorCode.quota_exceeded
    assert usage(quota_engine)[0]._mapping[kind+'_used'] == 5

@pytest.mark.parametrize('limits', [dict(quota_jobs=0), dict(quota_jobs=-1), dict(quota_jobs=None)])
def test_nonpositive_unlimited(quota_engine, limits):
    with Session(quota_engine) as db:
        charge_job_quota(db, actor(**limits), 100)
        db.commit()
    assert usage(quota_engine) == []

def test_one_captured_period_and_rollback(quota_engine, monkeypatch):
    import core.quota as quota
    calls=[]
    monkeypatch.setattr(quota, '_get_period_key', lambda period: calls.append(period) or '2026-10')
    with Session(quota_engine) as db:
        charge_job_quota(db, actor(quota_jobs=1))
        db.execute(text("INSERT INTO jobs(item_name,status) VALUES('rollback','pending')"))
        db.rollback()
    assert calls == ['monthly'] and usage(quota_engine) == []
    with quota_engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM jobs')) == 0

def test_utc_legacy_weekly_keys():
    assert _get_period_key('weekly', now=datetime(2026,1,1,tzinfo=timezone.utc)) == '2026-W00'

@pytest.mark.parametrize('existing', [False, True])
def test_concurrent_one_allowance(quota_engine, existing):
    if existing:
        with quota_engine.begin() as conn:
            conn.execute(text('INSERT INTO quota_usage(user_id,period_key) VALUES(1,:key)'), {'key':_get_period_key('monthly')})
    barrier=Barrier(2)
    def create():
        with Session(quota_engine) as db:
            barrier.wait()
            try:
                charge_job_quota(db, actor(quota_jobs=1))
                db.execute(text("INSERT INTO jobs(item_name,status) VALUES('concurrent','pending')"))
                db.commit()
                return 'accepted'
            except OdinError as exc:
                db.rollback()
                assert exc.status == 429
                return 'denied'
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:create(), range(2)))
    assert sorted(results) == ['accepted','denied']
    assert usage(quota_engine)[0].jobs_used == 1
    with quota_engine.connect() as conn:
        assert conn.scalar(text('SELECT count(*) FROM jobs')) == 1

@pytest.mark.parametrize('route', ['single','bulk', 'batch', 'preset', 'model', 'repeat','file','studio'])
@pytest.mark.parametrize('kind', ['jobs','grams','hours'])
def test_real_ordinary_route_gates(quota_engine, route, kind, monkeypatch, tmp_path):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setattr(registry, '_providers', registry.providers | {'EducationPolicyProvider': EducationPolicyService()})
    from modules.jobs import register_providers
    register_providers(registry)
    from modules.jobs.routes import jobs_crud, presets
    from modules.models_library.routes import models_crud
    from modules.jobs.models import Job, PrintPreset
    from modules.models_library.models import Model
    from modules.printers.models import Printer
    from modules.jobs.schemas import JobCreate
    # External delivery is never contacted by this disposable contract fixture.
    monkeypatch.setattr(jobs_crud, '_stage_queue_entries', lambda *args: [])
    monkeypatch.setattr(jobs_crud, '_deliver_queue_entries', lambda *args: None)
    from modules.notifications import alert_dispatcher
    monkeypatch.setattr(alert_dispatcher, 'stage_alert', lambda *args,**kwargs: None)
    monkeypatch.setattr(alert_dispatcher, 'deliver_staged_alert', lambda *args: None)
    with Session(quota_engine) as db:
        db.add_all([Job(id=1,item_name='original'), PrintPreset(id=1,name='preset'), Model(id=1,name='model'),Printer(id=1,name='printer',api_type='bambu',is_active=True)])
        db.commit()
        import zipfile
        for identifier in (1,2):
            source=tmp_path/f'owned-quota-{identifier}.3mf'
            with zipfile.ZipFile(source,'w') as package:
                package.writestr('Metadata/plate_1.gcode','; owned quota payload')
            db.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path) VALUES(:id,'owned.3mf','owned.3mf',:path)"), {'id':identifier,'path':str(source)})
        db.commit()
        who=actor(**{"quota_"+kind:1})
        invocation=[0]
        def invoke():
            invocation[0] += 1
            if route=='single':
                from starlette.requests import Request
                return jobs_crud.create_job(JobCreate(item_name='single'),Request({'type':'http','headers':[], 'query_string':b''}),db,who)
            if route=='studio':
                from modules.models_library.routes import print_files
                from starlette.requests import Request
                from fastapi import UploadFile
                async def uploaded(**kwargs):
                    return dict(id=invocation[0], model_id=None,project_name='studio')
                monkeypatch.setattr(print_files, 'upload_3mf', uploaded)
                return asyncio.run(print_files.submit_studio_print(Request({'type':'http','headers':[], 'query_string':b''}),UploadFile(file=BytesIO(b'owned'),filename='file.3mf'),1,None,None,who,None,db))
            if route=='file':
                from modules.models_library.file_management import schedule_generic_file
                return schedule_generic_file(db, invocation[0], None, who)
            if route=='bulk':return jobs_crud.create_jobs_bulk([JobCreate(item_name='bulk',quantity=99)],who,db)
            if route=='batch':return jobs_crud.create_jobs_batch(jobs_crud.JobBatchRequest(item_name='batch',printer_ids=[1]),who,db)
            if route=='preset':return presets.schedule_from_preset(1,who,db)
            if route=='model':return models_crud.schedule_from_model(1,printer_id=None,current_user=who,db=db)
            return asyncio.run(jobs_crud.repeat_job(1,who,db))
        invoke()
        assert db.scalar(text('SELECT jobs_used FROM quota_usage')) == 1
        assert db.execute(text('SELECT grams_used,hours_used FROM quota_usage')).one() == (0,0)
        if kind!='jobs':
            db.execute(text(f'UPDATE quota_usage SET {kind}_used=1'))
            db.commit()
        with pytest.raises(OdinError) as error:invoke()
        assert error.value.status==429
        db.rollback()
        assert db.scalar(text('SELECT count(*) FROM jobs')) == 2

def test_bulk_failure_rolls_back_jobs_charge_and_prevents_delivery(quota_engine, monkeypatch):
    from modules.jobs.routes import jobs_crud
    from modules.jobs.schemas import JobCreate
    delivered=[]
    def fail_stage(db, jobs, user):
        db.flush()
        raise RuntimeError('owned injected audit/notification failure')
    monkeypatch.setattr(jobs_crud, '_stage_queue_entries', fail_stage)
    monkeypatch.setattr(jobs_crud, '_deliver_queue_entries', lambda *args:delivered.append(True))
    with Session(quota_engine) as db:
        with pytest.raises(RuntimeError):
            jobs_crud.create_jobs_bulk([JobCreate(item_name='first'),JobCreate(item_name='second')],actor(quota_jobs=2),db)
        db.rollback()
    assert usage(quota_engine)==[] and delivered==[]
    with quota_engine.connect() as conn:assert conn.scalar(text('SELECT count(*) FROM jobs'))==0

def test_bulk_over_allowance_creates_nothing(quota_engine):
    from modules.jobs.routes import jobs_crud
    from modules.jobs.schemas import JobCreate
    with Session(quota_engine) as db:
        with pytest.raises(OdinError):
            jobs_crud.create_jobs_bulk([JobCreate(item_name='first'),JobCreate(item_name='second')],actor(quota_jobs=1),db)
        db.rollback()
    assert usage(quota_engine)==[]
    with quota_engine.connect() as conn:assert conn.scalar(text('SELECT count(*) FROM jobs'))==0


from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize('exhausted', [False, True])
def test_bulk_education_guard_precedes_quota_http(review_db, monkeypatch, exhausted):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_crud
    monkeypatch.setattr(registry, '_providers', registry.providers | {'EducationPolicyProvider': EducationPolicyService()})
    identity=dict(id=3,username='admin',role='admin',group_id=1,is_active=True,
                  _auth_kind='session_jwt',quota_jobs=1,quota_period='monthly')
    if exhausted:
        review_db.execute(text('INSERT INTO quota_usage(user_id,period_key,jobs_used) VALUES(3,:pk,1)'),{'pk':_get_period_key('monthly')})
        review_db.commit()
    before_jobs=review_db.scalar(text('SELECT count(*) FROM jobs'))
    before_quota=review_db.execute(text('SELECT user_id,period_key,jobs_used FROM quota_usage')).all()
    app=FastAPI();app.include_router(jobs_crud.router)
    def request_db():
        with Session(review_db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=request_db
    app.dependency_overrides[get_current_user]=lambda:identity
    with TestClient(app) as client:
        # An ordinary row precedes the Education row: every reference must be preflighted.
        response=client.post('/jobs/bulk',json=[{'item_name':'ordinary'},{'item_name':'education','model_id':12}])
    assert response.status_code==409,response.text
    assert 'Education' in response.json()['detail']
    with Session(review_db.get_bind()) as fresh:
        assert fresh.scalar(text('SELECT count(*) FROM jobs'))==before_jobs
        assert fresh.execute(text('SELECT user_id,period_key,jobs_used FROM quota_usage')).all()==before_quota

def test_single_real_audit_alert_quota_rollback(quota_engine, monkeypatch):
    from modules.jobs.routes import jobs_crud
    from modules.jobs.schemas import JobCreate
    from modules.notifications import alert_dispatcher
    from starlette.requests import Request
    delivered=[]
    from modules.notifications.models import AlertPreference
    from core.base import AlertType
    with Session(quota_engine) as setup:
        setup.add(AlertPreference(user_id=1,alert_type=AlertType.QUEUE_ADDED,in_app=True,browser_push=False,email=False))
        setup.commit()
    native_stage=alert_dispatcher.stage_alert
    # Quiet-hours config normally reads the runtime global engine; own this boundary.
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda **kwargs:True)
    def stage_then_fail(db,*args,**kwargs):
        native_stage(db,*args,**kwargs)
        db.flush()
        assert db.scalar(text('SELECT count(*) FROM jobs'))==1
        assert db.scalar(text('SELECT count(*) FROM audit_logs'))==1
        assert db.scalar(text('SELECT count(*) FROM alerts'))==1
        assert db.scalar(text('SELECT jobs_used FROM quota_usage'))==1
        raise RuntimeError('owned injected failure after real staging')
    monkeypatch.setattr(alert_dispatcher,'stage_alert',stage_then_fail)
    monkeypatch.setattr(alert_dispatcher,'deliver_staged_alert',lambda *args:delivered.append(True))
    with Session(quota_engine) as db:
        with pytest.raises(RuntimeError,match='after real staging'):
            jobs_crud.create_job(JobCreate(item_name='rollback'),Request({'type':'http','headers':[], 'query_string':b''}),db,actor(quota_jobs=1))
        db.rollback()
    with quota_engine.connect() as conn:
        for table in ('jobs','audit_logs','alerts','quota_usage'):
            assert conn.scalar(text(f'SELECT count(*) FROM {table}'))==0
    assert delivered==[]
