"""Closed Education predicates and exact private-reader provider boundary."""
import ast,hashlib,subprocess,sys
from pathlib import Path
import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from core.registry import ModuleRegistry,registry
from tests.test_contracts.test_timelapse_content_audience import media_app,managed_plain_media,review_db,owned_runtime


def test_exact_sql_objects_and_original_bytes():
    from core import education_archive_audience as core
    from modules.organizations import education_review_service as owner
    for name in ('_BASE_SELECT','_SUBMISSION_AUDIENCE','archive_audience_sql'):
        assert getattr(owner,name) is getattr(core,name)
    original=Path('tests/fixtures/certification/archive-provider-boundary/education-sql-before.py').read_text()
    namespace={};exec(compile(original,'trusted-original','exec'),namespace)
    assert core.archive_audience_sql()==namespace['archive_audience_sql']()
    current=Path(core.__file__).read_text()
    for node in ast.parse(original).body:
        match=next(n for n in ast.parse(current).body if type(n) is type(node) and ast.dump(n,include_attributes=False)==ast.dump(node,include_attributes=False))
        assert ast.get_source_segment(current,match)==ast.get_source_segment(original,node)


def test_pure_sql_import_never_imports_implementation():
    script="""
import sys,importlib.abc
class Deny(importlib.abc.MetaPathFinder):
 def find_spec(self,name,*args):
  if name.startswith('modules'):raise AssertionError('implementation import')
sys.meta_path.insert(0,Deny())
from core.education_archive_audience import archive_audience_sql
assert 'education_submissions' in archive_audience_sql()
"""
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


@pytest.mark.parametrize('mode',['canonical','generic','altered'])
def test_real_chosen_authority_keeps_closed_sql_whitelist(media_app,managed_plain_media,monkeypatch,mode):
    from core.education_archive_audience import archive_audience_sql
    from core.interfaces.education_policy import generic_archive_audience_sql
    from core.auth import create_access_token
    client,_,_,provider=media_app
    value={'canonical':archive_audience_sql(),'generic':generic_archive_audience_sql(),'altered':'SELECT 1'}[mode]
    monkeypatch.setattr(provider,'archive_audience_sql',lambda:value)
    result=client.get(f'/timelapses/{managed_plain_media}/download',headers={'Authorization':'Bearer '+create_access_token({'sub':'foreign'})})
    assert result.status_code==(503 if mode=='altered' else 200),result.text
    if mode=='altered':assert b'owned synthetic MP4 publication evidence' not in result.content


def test_registration_preserves_valid_repeat_and_refuses_invalid():
    from modules.archives import register_providers
    r=ModuleRegistry();register_providers(r);first=r.get_provider('AttemptValuationProvider')
    register_providers(r);assert r.get_provider('AttemptValuationProvider') is first
    r.register_provider('AttemptValuationProvider',object())
    with pytest.raises(TypeError,match='Invalid private valuation'):register_providers(r)
    assert type(r.get_provider('AttemptValuationProvider')) is object


def test_exact_argument_identity_return_and_failure_no_hidden_transaction(monkeypatch):
    from modules.archives.attempt_valuation_provider import AttemptValuationService
    from modules.archives import valuation_private_read as implementation
    args=tuple(object() for _ in range(4));result=object();seen=[]
    def read(*values):
        assert all(x is y for x,y in zip(values,args));seen.append(values);return result
    monkeypatch.setattr(implementation,'read_private_valuation',read)
    assert AttemptValuationService().read_private_valuation(*args) is result
    failure=HTTPException(404,'opaque',headers={'X-Owned':'retained'})
    def denied(*values):raise failure
    monkeypatch.setattr(implementation,'read_private_valuation',denied)
    with pytest.raises(HTTPException) as caught:AttemptValuationService().read_private_valuation(*args)
    assert caught.value is failure and len(seen)==1


@pytest.mark.parametrize('provider',[None,object(),type('Broken',(),{'read_private_valuation':None})()])
def test_actual_http_missing_invalid_is_no_store_before_reader(monkeypatch,provider):
    from modules.orders.routes import attempt_valuation as route
    from core.db import get_db
    monkeypatch.setitem(registry._providers,'AttemptValuationProvider',provider)
    app=FastAPI();app.include_router(route.router)
    actor_dependency=next(d.call for r in app.routes if getattr(r,'path','')=='/orders/attempts/{attempt_id}/unit-valuation' for d in r.dependant.dependencies if d.name=='actor')
    app.dependency_overrides[actor_dependency]=lambda:{'id':3,'role':'admin','group_id':None}
    app.dependency_overrides[get_db]=lambda:object()
    result=TestClient(app).get('/orders/attempts/1/unit-valuation')
    assert result.status_code==503 and result.headers['cache-control']=='private, no-store'


def test_actual_http_reader_denial_preserves_headers(monkeypatch):
    from modules.orders.routes import attempt_valuation as route
    from modules.archives import register_providers,valuation_private_read
    from core.db import get_db
    monkeypatch.setattr(registry,'_providers',dict(registry._providers));register_providers(registry)
    def denied(*args):raise HTTPException(404,'opaque',headers={'X-Owned':'retained','Cache-Control':'public'})
    monkeypatch.setattr(valuation_private_read,'read_private_valuation',denied)
    app=FastAPI();app.include_router(route.router)
    actor_dependency=next(d.call for r in app.routes if getattr(r,'path','')=='/orders/attempts/{attempt_id}/unit-valuation' for d in r.dependant.dependencies if d.name=='actor')
    app.dependency_overrides[actor_dependency]=lambda:{'id':3,'role':'admin','group_id':None}
    app.dependency_overrides[get_db]=lambda:object()
    result=TestClient(app).get('/orders/attempts/1/unit-valuation')
    assert result.status_code==404 and result.headers['cache-control']=='private, no-store'
    assert result.headers['x-owned']=='retained'


def test_actual_startup_order_installs_reader_before_orders_routes(monkeypatch):
    import importlib
    from core.app import _resolve_load_order
    packages=_resolve_load_order(['modules.orders','modules.archives'])
    assert packages==['modules.archives','modules.orders']
    r=ModuleRegistry();monkeypatch.setattr(registry,'_providers',r._providers)
    app=FastAPI();observed=[]
    real=app.include_router
    def include(router,*args,**kwargs):
        if any(getattr(x,'path','')=='/orders/attempts/{attempt_id}/unit-valuation' for x in router.routes):
            observed.append(r.get_provider('AttemptValuationProvider'));assert observed[-1] is not None
        return real(router,*args,**kwargs)
    monkeypatch.setattr(app,'include_router',include)
    for package in packages:importlib.import_module(package).register(app,r)
    assert len(observed)==2
    assert r.get_provider('AttemptValuationProvider') is observed[0] is observed[1]

from tests.test_contracts.test_private_attempt_valuation_route import private_attempt,creators


@pytest.mark.parametrize('provider',[None,object()])
def test_real_authenticated_missing_provider_has_no_effects(private_attempt,review_db,monkeypatch,provider):
    from modules.archives import valuation_private_read
    client,auth,_,identifier=private_attempt
    before=tuple(review_db.execute(__import__('sqlalchemy').text('SELECT id,economic_birth_nonce,economic_uid,spool_snapshot FROM print_jobs ORDER BY id')).all())
    review_db.rollback()
    monkeypatch.setitem(registry._providers,'AttemptValuationProvider',provider)
    monkeypatch.setattr(valuation_private_read,'read_private_valuation',lambda *args:pytest.fail('Missing provider must not enter reader'))
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('platform'))
    assert result.status_code==503 and result.headers['cache-control']=='private, no-store'
    assert tuple(review_db.execute(__import__('sqlalchemy').text('SELECT id,economic_birth_nonce,economic_uid,spool_snapshot FROM print_jobs ORDER BY id')).all())==before


def test_real_authenticated_reader_denial_is_private_no_store(private_attempt):
    client,auth,_,identifier=private_attempt
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('admin'))
    assert result.status_code==404 and result.headers['cache-control']=='private, no-store'
    assert '12.99' not in result.text


def test_real_authenticated_lowercase_error_cache_is_single_private_header(private_attempt,monkeypatch):
    from modules.archives import valuation_private_read
    client,auth,_,identifier=private_attempt
    def denied(*args):raise HTTPException(404,'opaque',headers={'cache-control':'public','X-Owned':'retained'})
    monkeypatch.setattr(valuation_private_read,'read_private_valuation',denied)
    result=client.get(f'/orders/attempts/{identifier}/unit-valuation',headers=auth('platform'))
    assert result.status_code==404
    assert result.headers.get_list('cache-control')==['private, no-store']
    assert result.headers['x-owned']=='retained'
