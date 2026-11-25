"""Owned actual configuration/middleware acceptance; no host firewall changes."""
import ipaddress
import json
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core import ip_allowlist_cache
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture(autouse=True)
def owned_runtime(monkeypatch):
    import secrets
    monkeypatch.setenv('JWT_SECRET_KEY',secrets.token_urlsafe(48))
    monkeypatch.delenv('API_KEY',raising=False)
    ip_allowlist_cache.invalidate()
    yield
    ip_allowlist_cache.invalidate()


@pytest.mark.parametrize("enabled", [True, False])
def test_policy_cache_isolated_between_behavior_cases(enabled):
    # Each in-memory database is independent, but both have the same cache key.
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE system_config (key TEXT, value TEXT)"))
            conn.execute(text("INSERT INTO system_config VALUES ('ip_allowlist', :policy)"),
                         {"policy": json.dumps({"enabled": enabled})})
        policy = ip_allowlist_cache.policy(lambda: Session(engine), clock=lambda: 1000.0)
        assert policy["enabled"] is enabled
    finally:
        engine.dispose()


@pytest.fixture
def allowlist_app(review_db,monkeypatch):
    from core.app import _register_http_middleware,_setup_middleware
    from core.config import settings
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system.routes_config import router
    monkeypatch.setattr(settings,'cors_origins','https://web.example.test')
    monkeypatch.setattr(settings,'api_key',None)
    monkeypatch.setattr(settings,'trusted_hosts','*')
    monkeypatch.setattr('core.db.SessionLocal',lambda:Session(review_db.get_bind()))
    app=FastAPI();app.include_router(router,prefix='/api')
    @app.get('/', response_class=HTMLResponse)
    @app.get('/printers/42', response_class=HTMLResponse)
    def page():
        return '<html>Farm dashboard</html>'
    app.mount('/static', StaticFiles(directory=Path(__file__).resolve().parents[2] / 'backend/static'))
    @app.get('/health')
    @app.get('/health/ready')
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

def test_allowlist_covers_web_pages_assets_and_api(allowlist_app):
    paths = ["/", "/printers/42", "/static/branding/favicon.png", "/openapi.json", "/api/probe"]
    with TestClient(allowlist_app, client=("198.51.100.9", 50000)) as outside:
        for path in paths:
            assert outside.get(path).status_code == 200
        with TestClient(allowlist_app, client=("127.0.0.1", 50000)) as local:
            assert local.put("/api/config/ip-allowlist", json={
                "enabled": True, "cidrs": ["203.0.113.0/24"],
            }).status_code == 200
            assert local.get("/").text == "<html>Farm dashboard</html>"
        with TestClient(allowlist_app, client=("203.0.113.8", 50000)) as inside:
            assert inside.get("/").text == "<html>Farm dashboard</html>"
        for path in [*paths, "/health/ready/", "/health/other"]:
            response = outside.get(path, follow_redirects=False)
            assert response.status_code == 403, path
            assert response.json() == {"detail": "IP address not allowed"}
        for path in ["/health", "/health/ready"]:
            assert outside.get(path).status_code == 200
        response = outside.options("/", headers={
            "Origin": "https://web.example.test", "Access-Control-Request-Method": "GET",
        })
        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == "https://web.example.test"
        bare = outside.options("/")
        assert bare.status_code == 403
        assert bare.json() == {"detail": "IP address not allowed"}
        with TestClient(allowlist_app, client=("127.0.0.1", 50000)) as local:
            assert local.put("/api/config/ip-allowlist", json={
                "enabled": False, "cidrs": ["203.0.113.0/24"],
            }).status_code == 200
        for path in paths:
            assert outside.get(path).status_code == 200, path


def test_policy_read_failure_refuses_web_page(allowlist_app, monkeypatch):
    def failed_read(_factory):
        raise RuntimeError("Policy unavailable")

    monkeypatch.setattr("core.ip_allowlist_cache.policy", failed_read)
    with TestClient(allowlist_app, client=("198.51.100.9", 50000)) as client:
        response = client.get("/")
        assert response.status_code == 503
        assert response.json() == {"detail": "Service temporarily unavailable"}
        assert response.headers["X-Frame-Options"] == "DENY"


def test_internal_camera_relay_keeps_its_own_admission_check(allowlist_app, monkeypatch):
    from modules.printers import camera_relay

    allowlist_app.include_router(camera_relay.router)
    @allowlist_app.get("/{path:path}", response_class=HTMLResponse)
    def web_fallback(path: str):
        return "<html>Farm dashboard</html>"

    # This peer is trusted by the relay but is outside the web allowlist.
    monkeypatch.setenv("ODIN_CAMERA_RELAY_PEERS", "198.51.100.9")
    checked = []
    def test_token(printer_id):
        checked.append(printer_id)
        return "fixture-token"

    monkeypatch.setattr(camera_relay, "relay_token", test_token)
    with TestClient(allowlist_app, client=("127.0.0.1", 50000)) as local:
        assert local.put("/api/config/ip-allowlist", json={
            "enabled": True, "cidrs": ["203.0.113.0/24"],
        }).status_code == 200
    with TestClient(allowlist_app, client=("198.51.100.9", 50000)) as peer:
        relay = str(allowlist_app.url_path_for("camera_source", printer_id=42, token="wrong-token"))
        assert peer.get(relay).status_code == 404
        assert checked == [42]
        refused = peer.post(relay)
        assert refused.status_code == 403
        assert refused.json() == {"detail": "IP address not allowed"}
        directory, printer, token = relay.rsplit("/", 2)
        for path in ("/internal/other", directory, directory + "/x", relay + "/extra", relay + "/",
                     f"{directory}/unknown/{token}", f"{directory}//{token}", f"{directory}/{printer}//{token}",
                     relay.replace("/" + token, "%2F" + token), relay.replace("42", "%34%32")):
            response = peer.get(path, follow_redirects=False)
            assert response.status_code == 403
            assert response.json() == {"detail": "IP address not allowed"}


def test_relay_without_raw_path_has_no_allowlist_exception():
    from core.camera_relay_path import RELAY_PATH, is_relay_request

    path = RELAY_PATH.format(printer_id=42, token="fixture")
    assert not is_relay_request({"method": "GET", "path": path})


def test_restored_address_is_checked_before_license_resolution(allowlist_app, monkeypatch):
    from unittest.mock import Mock
    from core.license_lock import LicenseLockError
    from core.remote_access import TrustedProxySchemeMiddleware

    allowlist_app.add_middleware(TrustedProxySchemeMiddleware, trusted="127.0.0.1")
    monkeypatch.setenv("ODIN_LICENSE_FLEET_LOCK_ENABLED", "1")
    locked = Mock(side_effect=LicenseLockError("license_user_locked", revision=2, resource_id=3))
    monkeypatch.setattr("core.dependencies.resolve_current_user_sync", locked)
    monkeypatch.setattr("core.ip_allowlist_cache.policy", lambda _: {"enabled": True, "cidrs": ["203.0.113.0/24"]})
    with TestClient(allowlist_app, client=("127.0.0.1", 50000)) as proxy:
        headers = {"Authorization": "Bearer fixture", "X-Forwarded-For": "198.51.100.9",
                   "Origin": "https://web.example.test"}
        refused = proxy.get("/api/probe", headers=headers)
        assert refused.status_code == 403
        assert refused.json() == {"detail": "IP address not allowed"}
        assert refused.headers["X-Frame-Options"] == "DENY"
        assert "Strict-Transport-Security" in refused.headers
        assert refused.headers["Access-Control-Allow-Origin"] == headers["Origin"]
        locked.assert_not_called()
        headers["X-Forwarded-For"] = "203.0.113.8"
        admitted = proxy.get("/api/probe", headers=headers)
        assert admitted.json()["detail"]["code"] == "license_user_locked"
        locked.assert_called_once()
