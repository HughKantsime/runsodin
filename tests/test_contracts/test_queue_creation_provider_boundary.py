"""Four real caller HTTP paths retain exact staging and caller transactions."""
from pathlib import Path
import zipfile

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_archive_reprint_behavior import reprint_client


@pytest.fixture
def queue_api(creators,reprint_client,review_db,tmp_path,monkeypatch):
    from core.registry import registry
    from modules.jobs import register_providers
    from modules.models_library.routes.models_crud import router as models
    from modules.models_library.routes.print_files import router as files
    from modules.archives.routes.archives_crud import router as archives
    client,auth,_,_=creators
    client.app.include_router(models,prefix='/api');client.app.include_router(files,prefix='/api');client.app.include_router(files,prefix='/api/v1');client.app.include_router(archives,prefix='/api')
    monkeypatch.setattr(registry,'_providers',dict(registry._providers))
    registry._providers.pop('QueueCreationProvider',None)
    register_providers(registry)
    storage=tmp_path/'owned-storage';storage.mkdir()
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',str(storage))
    source=tmp_path/'owned.gcode.3mf'
    with zipfile.ZipFile(source,'w') as package:package.writestr('Metadata/plate_1.gcode','; owned no hardware')
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(501,'Boundary model',1)"))
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json) VALUES(501,501,'owned.gcode.3mf','owned.gcode.3mf',:path,1,'[]')"),{'path':str(source)})
    review_db.commit()
    return client,auth,storage


def _facts(db):
    with Session(db.get_bind()) as observer:
        return tuple(observer.execute(text('SELECT count(*) FROM '+table)).scalar_one() for table in ('jobs','cost_identities','models','print_files','quota_usage')) + (observer.execute(text('SELECT coalesce(sum(jobs_used),0) FROM quota_usage')).scalar_one(),)


def _request(queue_api,caller,*,dry=False):
    client,auth,_=queue_api
    headers=auth()
    if dry:headers['X-Dry-Run']='true'
    if caller=='model':return client.post('/api/models/501/schedule',headers=headers,json={'quantity':2,'priority':4})
    if caller=='file':return client.post('/api/print-files/501/schedule?printer_id=9',headers=headers)
    if caller=='archive':return client.post('/api/archives/101/reprint',headers=headers,json={'printer_id':9})
    payload=(Path(__file__).parents[1]/'fixtures/telemetry/a1-petg-source-sanitized.3mf').read_bytes()
    return client.post('/api/v1/prints' if dry else '/api/prints',headers=headers,data={'target_printer_id':'9'},files={'file':('owned.3mf',payload,'application/octet-stream')})


@pytest.mark.parametrize('caller',['model','file','archive','studio'])
@pytest.mark.parametrize('mode',['missing','invalid'])
def test_all_callers_refuse_before_first_mutation(queue_api,review_db,monkeypatch,caller,mode):
    from core.registry import registry
    if mode=='missing':registry._providers.pop('QueueCreationProvider')
    else:monkeypatch.setitem(registry._providers,'QueueCreationProvider',object())
    before=_facts(review_db)
    response=_request(queue_api,caller)
    assert response.status_code==503,response.text
    assert response.json()['detail']=='Queue creation service unavailable'
    assert _facts(review_db)==before
    assert not list(queue_api[2].iterdir())
    assert review_db.execute(text('SELECT job_id FROM print_files WHERE id=501')).scalar_one() is None


@pytest.mark.parametrize('caller',['model','file','archive','studio'])
def test_real_provider_one_insertion_identity_and_exact_caller(queue_api,review_db,monkeypatch,caller):
    from core.registry import registry
    provider=registry.get_provider('QueueCreationProvider')
    method='stage_job' if caller=='archive' else 'stage_values'
    actual=getattr(provider,method);calls=[]
    def capture(db,value,actor):
        result=actual(db,value,actor)
        assert result in db
        assert actor['id']==3 and actor['group_id']==1
        calls.append((db,actor,result))
        return result
    monkeypatch.setattr(provider,method,capture)
    before=_facts(review_db)
    response=_request(queue_api,caller)
    assert response.status_code==(201 if caller=='studio' else 200),response.text
    assert len(calls)==1
    identifier=int(response.json()['job_id'])
    with Session(review_db.get_bind()) as observer:
        row=observer.execute(text('SELECT j.id,j.status,j.charged_to_org_id,c.owner_scope,c.owner_key,c.resource_id FROM jobs j JOIN cost_identities c ON c.identity_uid=j.economic_uid WHERE j.id=:id'),{'id':identifier}).one()
    assert tuple(row)==(identifier,'pending',1,'org',1,identifier)
    assert _facts(review_db)[0]==before[0]+1


def test_studio_dry_run_preserved_without_provider(queue_api,review_db):
    from core.registry import registry
    registry._providers.pop('QueueCreationProvider')
    before=_facts(review_db)
    response=_request(queue_api,'studio',dry=True)
    assert response.status_code==200,response.text
    assert _facts(review_db)==before and not list(queue_api[2].iterdir())


def test_registration_idempotent_invalid_before_routes():
    from core.registry import ModuleRegistry
    from modules.jobs import register_providers,register
    registry=ModuleRegistry();register_providers(registry)
    provider=registry.get_provider('QueueCreationProvider')
    register_providers(registry)
    assert registry.get_provider('QueueCreationProvider') is provider
    registry.register_provider('QueueCreationProvider',object())
    class NoRoutes:
        def include_router(self,*args,**kwargs):pytest.fail('invalid provider exposed routes')
    with pytest.raises(TypeError,match='Invalid queue creation service'):register(NoRoutes(),registry)
    assert type(registry.get_provider('QueueCreationProvider')) is object


def test_staging_default_parity_and_caller_rollback(review_db):
    from modules.jobs.queue_creation_provider import QueueCreationService
    provider=QueueCreationService();actor={'id':3,'role':'admin','group_id':1}
    values={'item_name':'exact defaults','status':'pending','submitted_by':3,'charged_to_org_id':1}
    before=_facts(review_db)
    job=provider.stage_values(review_db,values,actor)
    assert review_db.in_transaction() and job.id
    assert review_db.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':job.id}).scalar_one()
    columns=','.join(values)
    reference=review_db.execute(text('INSERT INTO jobs ('+columns+') VALUES (:item_name,:status,:submitted_by,:charged_to_org_id) RETURNING id'),values).scalar_one()
    rows=review_db.execute(text('SELECT * FROM jobs WHERE id IN (:new,:old) ORDER BY id'),{'new':job.id,'old':reference}).mappings().all()
    actual,original=map(dict,rows)
    for row in (actual,original):row.pop('id');row.pop('economic_uid')
    assert actual==original
    review_db.rollback()
    assert _facts(review_db)==before


@pytest.mark.parametrize('caller',['model','file','archive','studio'])
def test_provider_late_failure_rolls_back_uncommitted_queue_effects(queue_api,review_db,monkeypatch,caller):
    from core.registry import registry
    from fastapi.testclient import TestClient
    client,auth,storage=queue_api
    safe_client=TestClient(client.app,raise_server_exceptions=False)
    queue_api=(safe_client,auth,storage)
    provider=registry.get_provider('QueueCreationProvider')
    method='stage_job' if caller=='archive' else 'stage_values'
    actual=getattr(provider,method)
    def fail(db,values,actor):
        actual(db,values,actor)
        db.flush()
        raise RuntimeError('synthetic caller staging failure')
    monkeypatch.setattr(provider,method,fail)
    before=_facts(review_db)
    response=_request(queue_api,caller)
    assert response.status_code==500,response.text
    after=_facts(review_db)
    assert (after[0],after[1],after[4],after[5]) == (before[0],before[1],before[4],before[5])
    if caller=='studio':
        # Existing upload commits separately. Only unavailable provider is
        # rejected before that upload; later staging failure preserves it.
        assert after[3]==before[3]+1
        assert list(storage.iterdir())
    else:assert after==before


def test_jobs_adapter_passes_exact_objects_and_errors(monkeypatch):
    from modules.jobs import queue_creation_provider as module
    service=module.QueueCreationService();db=object();value={};actor={}
    returned=object();calls=[]
    def stage(a,b,c):
        calls.append((a,b,c));return returned
    monkeypatch.setattr(module,'_stage_prospective_queue_job',stage)
    monkeypatch.setattr(module,'_stage_prospective_queue_values',stage)
    assert service.stage_job(db,value,actor) is returned
    assert service.stage_values(db,value,actor) is returned
    assert all(a is db and b is value and c is actor for a,b,c in calls)
    failure=RuntimeError('synthetic stage failure')
    def fail(*_):raise failure
    monkeypatch.setattr(module,'_stage_prospective_queue_values',fail)
    with pytest.raises(RuntimeError) as caught:service.stage_values(db,value,actor)
    assert caught.value is failure


def test_fresh_registration_has_no_api_import_or_database_connection(tmp_path):
    import os,subprocess,sys
    script='''
import builtins,sqlite3,socket
real_import=builtins.__import__
def guarded(name,*args,**kwargs):
    if name.startswith('modules.jobs.routes') or name=='modules.jobs.scheduler_routes':
        raise AssertionError('provider imported API')
    return real_import(name,*args,**kwargs)
builtins.__import__=guarded
sqlite3.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('provider opened DB'))
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(AssertionError('provider network'))
from core.registry import ModuleRegistry
from modules.jobs import register_providers
registry=ModuleRegistry();register_providers(registry)
provider=registry.get_provider('QueueCreationProvider')
register_providers(registry)
assert registry.get_provider('QueueCreationProvider') is provider
print('pure registration verified')
'''
    root=Path(__file__).resolve().parents[2]
    env=dict(os.environ,DATABASE_URL='sqlite:///'+str(tmp_path/'unused.sqlite'),DATABASE_PATH=str(tmp_path/'unused.sqlite'),BASE_URL='http://127.0.0.1:9',
             PYTHONPATH=os.pathsep.join((str(root/'backend'),str(root))))
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='pure registration verified'
    assert not (tmp_path/'unused.sqlite').exists()


@pytest.mark.parametrize('name,scope,key',[('admin','org',1),('platform','global',0),('unassigned',None,None)])
def test_model_route_existing_owner_and_raw_default_oracles(queue_api,creators,review_db,name,scope,key):
    from tests.test_contracts.test_model_schedule_prospective_birth import (
        test_model_schedule_has_fresh_owned_identity,
        test_model_schedule_matches_original_raw_insert_omitted_column_semantics,
    )
    test_model_schedule_has_fresh_owned_identity(creators,review_db,name,scope,key)
    test_model_schedule_matches_original_raw_insert_omitted_column_semantics(creators,review_db,name)
