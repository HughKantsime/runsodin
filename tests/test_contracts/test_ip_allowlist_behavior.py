"""Owned actual configuration/middleware acceptance; no host firewall changes."""
import ipaddress
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture(autouse=True)
def owned_runtime(monkeypatch):
    import secrets
    monkeypatch.setenv('JWT_SECRET_KEY',secrets.token_urlsafe(48))
    monkeypatch.delenv('API_KEY',raising=False)

@pytest.fixture
def allowlist_app(review_db,monkeypatch):
    from core.app import _register_http_middleware,_setup_middleware
    from core.config import settings
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system.routes_config import router
    monkeypatch.setattr(settings,'api_key',None)
    monkeypatch.setattr(settings,'trusted_hosts','*')
    monkeypatch.setattr('core.db.SessionLocal',lambda:Session(review_db.get_bind()))
    app=FastAPI();app.include_router(router,prefix='/api')
    @app.get('/api/probe')
    def probe():return {'status':'owned'}
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','is_active':True,'group_id':None}
    _setup_middleware(app);_register_http_middleware(app)
    return app

@pytest.mark.parametrize('caller,allowed,outsider,host_cidr',[
    ('198.51.100.7','203.0.113.0/24','198.51.100.8','198.51.100.7/32'),
    ('2001:db8:abcd::1','2001:db8:feed::/64','2001:db8:9999::20','2001:db8:abcd::1/128'),
    ('::ffff:192.0.2.7','198.51.100.0/24','::ffff:192.0.2.8','::ffff:c000:207/128'),
])
def test_enabling_without_covering_caller_is_refused_with_the_seen_address(allowlist_app,review_db,caller,allowed,outsider,host_cidr):
    # The caller's address is never added silently (behind a proxy it would admit everyone).
    with TestClient(allowlist_app,client=(caller,50000)) as client:
        response=client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':[allowed]})
        assert response.status_code==400
        assert 'This server sees your address as' in response.json()['detail']
        assert str(ipaddress.ip_address(caller)) in response.json()['detail']
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='ip_allowlist'")).scalar_one()==0
    # Covering the caller explicitly is accepted unchanged and admits only the listed hosts.
    with TestClient(allowlist_app,client=(caller,50000)) as client:
        response=client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':[allowed,host_cidr]})
        assert response.status_code==200 and response.json()['cidrs']==[allowed,host_cidr]
        assert client.get('/api/probe').status_code==200
    with TestClient(allowlist_app,client=(outsider,50000)) as client:
        assert client.get('/api/probe').status_code==403

def test_loopback_caller_is_admitted_without_an_added_entry(allowlist_app):
    with TestClient(allowlist_app,client=('127.0.0.1',50000)) as client:
        response=client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':['203.0.113.0/24']})
        assert response.status_code==200 and response.json()['cidrs']==['203.0.113.0/24']

def test_ipv6_lockout_does_not_open_unrelated_network(allowlist_app):
    with TestClient(allowlist_app,client=('2001:db8:abcd::1',50000)) as client:
        assert client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':['2001:db8:feed::/64','2001:db8:abcd::1/128']}).status_code==200
    with TestClient(allowlist_app,client=('2001:db8:9999::20',50000)) as outsider:
        assert outsider.get('/api/probe').status_code==403

def test_invalid_cidr_does_not_persist_configuration(allowlist_app,review_db):
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':['not-a-network']}).status_code==400
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='ip_allowlist'")).scalar_one()==0

@pytest.mark.parametrize('caller',['198.51.100.7','2001:0db8:abcd:0000:0000:0000:0000:0001'])
def test_mixed_networks_already_covering_caller_are_unchanged(allowlist_app,caller):
    networks=['198.51.100.0/24','2001:db8:abcd::/64']
    with TestClient(allowlist_app,client=(caller,50000)) as client:
        response=client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':networks})
        assert response.status_code==200
        assert response.json()['cidrs']==networks
        assert client.get('/api/probe').status_code==200

@pytest.mark.parametrize('enabled,cidrs',[(False,['203.0.113.0/24']),(True,[])])
def test_disabled_or_empty_policy_keeps_existing_behavior(allowlist_app,enabled,cidrs):
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        response=client.put('/api/config/ip-allowlist',json={'enabled':enabled,'cidrs':cidrs})
        assert response.status_code==200
        assert response.json()['cidrs']==cidrs
        assert client.get('/api/probe').status_code==200

@pytest.mark.parametrize('caller',[None,('not-an-address',50000)])
def test_missing_or_invalid_current_identity_refuses_without_write(allowlist_app,review_db,caller):
    with TestClient(allowlist_app,client=caller,raise_server_exceptions=False) as client:
        response=client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':['203.0.113.0/24']})
        assert response.status_code==400
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='ip_allowlist'")).scalar_one()==0

@pytest.mark.parametrize('caller,networks',[
    ('fe80::1234%en0',['2001:db8::/64']),
    ('2001:db8:abcd::1',['fe80::1234%en0/128']),
    ('2001:db8:abcd::1',['fe80::1234%en0/64']),
])
def test_scoped_ipv6_identity_is_refused_when_it_cannot_be_enforced(allowlist_app,review_db,caller,networks):
    with TestClient(allowlist_app,client=(caller,50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/ip-allowlist',json={'enabled':True,'cidrs':networks}).status_code==400
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='ip_allowlist'")).scalar_one()==0

@pytest.mark.parametrize('caller,enabled,cidrs',[
    (None,False,['203.0.113.0/24']),
    (('not-an-address',50000),True,[]),
    (('fe80::1234%en0',50000),False,['fe80::1234%en0/128']),
])
def test_disabled_or_empty_transition_does_not_require_enforceable_identity(allowlist_app,caller,enabled,cidrs):
    with TestClient(allowlist_app,client=caller,raise_server_exceptions=False) as client:
        response=client.put('/api/config/ip-allowlist',json={'enabled':enabled,'cidrs':cidrs})
        assert response.status_code==200
        assert response.json()['cidrs']==cidrs
