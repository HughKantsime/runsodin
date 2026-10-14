"""Actual signed/scoped HTTP and transactional prospective currency acceptance."""
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import Session
from core.finance_currency import read_currency, write_currency, SUPPORTED_CURRENCIES
from tests.test_contracts.test_api_token_delegation_behavior import token_client, interactive_headers
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def finance_client(token_client):
    from modules.orders.routes import router
    client, issue = token_client
    client.app.include_router(router, prefix='/api')
    client.app.include_router(router, prefix='/api/v1')
    return client, issue


def rows(db, table):
    assert table in ('finance_currency_settings', 'audit_logs', 'cost_identities', 'cost_obligations', 'cost_attempt_participation')
    with Session(db.get_bind()) as fresh:
        return fresh.execute(text('SELECT count(*) FROM '+table)).scalar_one()


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
def test_missing_get_registered_before_order_id_read_only(finance_client, review_db, prefix):
    client, _ = finance_client
    before = [rows(review_db,t) for t in ('finance_currency_settings','audit_logs','cost_identities','cost_obligations','cost_attempt_participation')]
    response = client.get(prefix+'/orders/finance-settings', headers=interactive_headers())
    assert response.status_code == 200
    assert response.json() == {'owner_scope':'org','owner_key':1,'currency':None}
    assert [rows(review_db,t) for t in ('finance_currency_settings','audit_logs','cost_identities','cost_obligations','cost_attempt_participation')] == before


@pytest.mark.parametrize('currency', [*SUPPORTED_CURRENCIES, None])
def test_explicit_currency_and_clear_audited(finance_client, review_db, currency):
    client, _ = finance_client
    response = client.put('/api/orders/finance-settings', headers=interactive_headers(), json={'currency':currency})
    assert response.status_code == 200
    assert response.json()['currency'] == currency
    assert client.get('/api/orders/finance-settings',headers=interactive_headers()).json()['currency'] == currency
    assert rows(review_db,'finance_currency_settings') == rows(review_db,'audit_logs') == 1
    assert rows(review_db,'cost_identities') == rows(review_db,'cost_obligations') == rows(review_db,'cost_attempt_participation') == 0


@pytest.mark.parametrize('body', [{}, {'currency':'usd'}, {'currency':' USD'}, {'currency':'XXX'}, {'currency':0}, {'currency':False}, {'currency':[]}, {'currency':'USD','other':1}])
def test_invalid_payload_no_mutation(finance_client, review_db, body):
    client, _ = finance_client
    assert client.put('/api/orders/finance-settings',headers=interactive_headers(),json=body).status_code == 422
    assert rows(review_db,'finance_currency_settings') == rows(review_db,'audit_logs') == 0


@pytest.mark.parametrize('query', ['?org_id=2','?global_scope=true','?org_id=0','?org_id=-1','?org_id=garbage'])
def test_org_admin_foreign_global_invalid_scope_denied(finance_client, review_db, query):
    client, _ = finance_client
    for method in ('get','put'):
        kwargs={'headers':interactive_headers()}
        if method=='put':kwargs['json']={'currency':'USD'}
        assert getattr(client,method)('/api/orders/finance-settings'+query,**kwargs).status_code in (403,422)
    assert rows(review_db,'finance_currency_settings') == rows(review_db,'audit_logs') == 0


@pytest.mark.parametrize('scopes,read_ok,write_ok', [(['read'],True,False),(['write'],False,False),(['admin'],True,True),(['read:jobs'],False,False),(['write:jobs'],False,False),(['agent:read'],False,False),(['agent:write'],False,False),([],False,False)])
def test_actual_scoped_credentials(finance_client, review_db, scopes, read_ok, write_ok):
    client, issue = finance_client
    headers=issue(scopes)
    assert (client.get('/api/orders/finance-settings',headers=headers).status_code==200) == read_ok
    assert (client.put('/api/orders/finance-settings',headers=headers,json={'currency':'USD'}).status_code==200) == write_ok
    assert rows(review_db,'finance_currency_settings') == int(write_ok)
    assert rows(review_db,'audit_logs') == int(write_ok)


def signed(username):
    from core.auth import create_access_token
    return {'Authorization':'Bearer '+create_access_token({'sub':username})}


@pytest.mark.parametrize('username',['student','manager'])
def test_signed_nonadmin_denied(finance_client, review_db, username):
    client,_=finance_client
    for method in ('get','put'):
        kwargs={'headers':signed(username)}
        if method=='put':kwargs['json']={'currency':'USD'}
        assert getattr(client,method)('/api/orders/finance-settings',**kwargs).status_code==403
    assert rows(review_db,'finance_currency_settings')==0


def test_superadmin_explicit_selection_and_invalid_group_never_global(finance_client, review_db):
    client,_=finance_client
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'));review_db.commit()
    headers=interactive_headers()
    for query in ('','?org_id=0','?org_id=-1','?org_id=1&global_scope=true'):
        assert client.put('/api/orders/finance-settings'+query,headers=headers,json={'currency':'USD'}).status_code==422
    assert client.put('/api/orders/finance-settings?global_scope=true',headers=headers,json={'currency':'EUR'}).status_code==200
    assert client.put('/api/orders/finance-settings?org_id=2',headers=headers,json={'currency':'CAD'}).status_code==200
    assert client.get('/api/orders/finance-settings?global_scope=true',headers=headers).json()['currency']=='EUR'
    assert client.get('/api/orders/finance-settings?org_id=2',headers=headers).json()['currency']=='CAD'
    from modules.orders.routes.finance_settings import finance_owner
    for group in (0,-1,False,'1'):
        with pytest.raises(Exception):finance_owner({'role':'admin','group_id':group},global_scope=True)


def test_audit_failure_http_rolls_back_setting(finance_client, review_db, monkeypatch):
    import core.finance_currency as service
    client,_=finance_client
    assert client.put('/api/orders/finance-settings',headers=interactive_headers(),json={'currency':'GBP'}).status_code==200
    def fail(*args,**kwargs):raise RuntimeError('Synthetic audit failure')
    monkeypatch.setattr(service,'log_audit',fail)
    with pytest.raises(RuntimeError,match='Synthetic audit failure'):
        client.put('/api/orders/finance-settings',headers=interactive_headers(),json={'currency':'USD'})
    assert client.get('/api/orders/finance-settings',headers=interactive_headers()).json()['currency']=='GBP'
    assert rows(review_db,'audit_logs')==1


@pytest.fixture
def currency_engine(tmp_path):
    url=os.environ.get('ODIN_OWNED_CURRENCY_DB')
    admin=None
    if url:
        schema='currency_'+uuid4().hex
        admin=create_engine(url,hide_parameters=True)
        with admin.begin() as c:c.exec_driver_sql('CREATE SCHEMA '+schema)
        engine=create_engine(url,connect_args={'options':'-csearch_path='+schema},hide_parameters=True)
    else:
        engine=create_engine('sqlite:///'+str(tmp_path/'currency.db'),connect_args={'timeout':5},hide_parameters=True)
    with engine.begin() as c:
        importlib.import_module('core.schema.migrations.009_cost_foundation').apply(c)
        from core.models import AuditLog
        AuditLog.__table__.create(c)
    yield engine
    engine.dispose()
    if admin:
        with admin.begin() as c:c.exec_driver_sql('DROP SCHEMA '+schema+' CASCADE')
        admin.dispose()


def test_transactional_currency_caller_rollback_and_audit_insert_failure(currency_engine):
    engine=currency_engine
    with Session(engine) as s:
        write_currency(s,owner_scope='org',owner_key=7,currency='JPY');s.rollback()
    with Session(engine) as s:
        assert read_currency(s,owner_scope='org',owner_key=7) is None
        assert s.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()==0
    def reject(conn,cursor,statement,parameters,context,many):
        if statement.lstrip().upper().startswith('INSERT INTO AUDIT_LOGS'):raise RuntimeError('Synthetic audit flush failure')
    event.listen(engine,'before_cursor_execute',reject)
    try:
        with Session(engine) as s:
            write_currency(s,owner_scope='global',owner_key=0,currency='CHF')
            with pytest.raises(RuntimeError,match='audit flush'):s.commit()
            s.rollback()
    finally:event.remove(engine,'before_cursor_execute',reject)
    with Session(engine) as s:
        assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==0


def test_transactional_currency_concurrent_first_upsert_and_replay(currency_engine):
    entered,attempted,release=Event(),Event(),Event()
    def held():
        with Session(currency_engine) as s:
            write_currency(s,owner_scope='org',owner_key=7,currency='USD')
            entered.set();assert release.wait(5);s.commit()
    def competing():
        assert entered.wait(5)
        with Session(currency_engine) as s:
            attempted.set();write_currency(s,owner_scope='org',owner_key=7,currency='CAD');s.commit()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(held);second=pool.submit(competing)
        assert attempted.wait(5);sleep(.2);assert not second.done();release.set();first.result();second.result()
    with Session(currency_engine) as s:
        assert read_currency(s,owner_scope='org',owner_key=7)=='CAD'
        assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==1
        assert s.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()==2
        # Lost response replay is another explicit audited assignment, no duplicate scoped row.
        write_currency(s,owner_scope='org',owner_key=7,currency='CAD');s.commit()
        assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==1


@pytest.mark.parametrize('scope,key,currency',[('global',1,'USD'),('org',0,'USD'),('org',False,'USD'),('org',1,'usd'),('org',1,False)])
def test_transactional_currency_invalid_service_inputs_no_mutation(currency_engine,scope,key,currency):
    with Session(currency_engine) as s:
        with pytest.raises(ValueError):write_currency(s,owner_scope=scope,owner_key=key,currency=currency)
        assert s.execute(text('SELECT count(*) FROM finance_currency_settings')).scalar_one()==0


def test_currency_update_preserves_recorded_financials_snapshot_and_unknown_actuals(finance_client,review_db):
    from modules.orders.models import Order
    from modules.jobs.models import Job
    client,_=finance_client
    order=Order(org_id=1,order_number='Synthetic original currency unknown',revenue=55,
                platform_fees=0,payment_fees=2,shipping_charged=3,shipping_cost=4)
    job=Job(item_name='Synthetic estimated job',charged_to_org_id=1,estimated_cost=12)
    review_db.add_all([order,job]);review_db.commit()
    order_id=order.id
    review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,spool_snapshot) VALUES(9,'Synthetic original attempt','failed','2026-10-12T12:00:00',:snapshot)"),
                      {'snapshot':'{"version":1,"original_purchase_currency":null}'})
    review_db.commit()
    tables=('orders','order_items','jobs','print_jobs','spools','cost_identities','cost_obligations','cost_attempt_participation')
    def originals():
        with Session(review_db.get_bind()) as fresh:
            return {table:[tuple(row) for row in fresh.execute(text('SELECT * FROM '+table)).all()] for table in tables}
    before=originals()
    for currency in ('USD','EUR',None):
        assert client.put('/api/orders/finance-settings',headers=interactive_headers(),json={'currency':currency}).status_code==200
        assert originals()==before
        detail=client.get(f'/api/orders/{order_id}',headers=interactive_headers())
        assert detail.status_code==200
        assert all(detail.json()[field] is None for field in ('actual_cost','profit','margin_percent'))
        assert detail.json()['revenue']==55
