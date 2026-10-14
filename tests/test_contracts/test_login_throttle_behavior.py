"""Owned database evidence for persisted login failure windows; no live services."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

NOW = 2_000_000_000.0

@pytest.fixture
def clock(monkeypatch):
    from core import auth_helpers as helpers
    monkeypatch.setattr(helpers._time, 'time', lambda: NOW)
    monkeypatch.setattr(helpers, '_last_login_cleanup', NOW)
    return helpers


def failures(db, count, *, ip='192.0.2.1', username='student', age=0, success=0):
    for _ in range(count):
        db.execute(text('INSERT INTO login_attempts(ip,username,attempted_at,success) VALUES(:ip,:u,:t,:s)'),
                   {'ip':ip,'u':username,'t':NOW-age,'s':success})
    db.commit()


@pytest.mark.parametrize('count,blocked', [(0,False),(9,False),(10,True),(11,True)])
def test_ip_failure_threshold(clock, review_db, count, blocked):
    failures(review_db,count)
    assert clock._check_rate_limit(review_db,'192.0.2.1') is blocked


@pytest.mark.parametrize('count,blocked', [(0,False),(4,False),(5,True),(6,True)])
def test_account_failure_threshold(clock, review_db, count, blocked):
    failures(review_db,count)
    assert clock._is_locked_out(review_db,'student') is blocked


@pytest.mark.parametrize('age,blocked', [(299.999,True),(300,False),(300.001,False)])
def test_ip_exact_window_boundary(clock,review_db,age,blocked):
    failures(review_db,10,age=age)
    assert clock._check_rate_limit(review_db,'192.0.2.1') is blocked


@pytest.mark.parametrize('age,blocked', [(899.999,True),(900,False),(900.001,False)])
def test_account_exact_window_boundary(clock,review_db,age,blocked):
    failures(review_db,5,age=age)
    assert clock._is_locked_out(review_db,'student') is blocked


def test_successes_do_not_count_and_dimensions_are_independent(clock,review_db):
    failures(review_db,20,success=1)
    failures(review_db,5,ip='192.0.2.2',username='other')
    assert clock._check_rate_limit(review_db,'192.0.2.1') is False
    assert clock._is_locked_out(review_db,'student') is False
    assert clock._is_locked_out(review_db,'other') is True
    assert clock._check_rate_limit(review_db,'192.0.2.2') is False


def test_account_failures_aggregate_across_ips_and_persist_new_session(clock,review_db):
    for i in range(5):
        failures(review_db,1,ip=f'192.0.2.{i+1}')
    with Session(review_db.get_bind()) as fresh:
        assert clock._is_locked_out(fresh,'student') is True
        assert clock._check_rate_limit(fresh,'192.0.2.1') is False


@pytest.mark.parametrize('method,subject', [('_check_rate_limit','192.0.2.1'),('_is_locked_out','student')])
def test_query_failure_blocks_login(clock,method,subject):
    class BrokenDatabase:
        def execute(self,*args,**kwargs):
            raise RuntimeError('synthetic unavailable database')
    assert getattr(clock,method)(BrokenDatabase(),subject) is True


def test_record_commits_failure_and_audit_visible_in_independent_session(clock,review_db):
    clock._record_login_attempt(review_db,'192.0.2.1','student',False)
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM login_attempts WHERE success=0')).scalar_one()==1
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='login_failed'")).scalar_one()==1


def test_record_attempt_and_audit_are_atomic_under_audit_write_failure(clock,review_db,monkeypatch):
    def reject_audit(entry):
        raise RuntimeError('synthetic audit write failure')
    monkeypatch.setattr(review_db,'add',reject_audit)
    clock._record_login_attempt(review_db,'192.0.2.1','student',False)
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM login_attempts')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()==0

@pytest.fixture
def login_client(review_db,monkeypatch):
    import secrets
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.auth import hash_password
    from core.db import get_db
    from modules.organizations import routes_auth
    password=secrets.token_urlsafe(24)+'aA1'
    review_db.get_bind().hide_parameters=True
    review_db.execute(text('UPDATE users SET password_hash=:h WHERE id=1'),{'h':hash_password(password)})
    review_db.commit()
    routes_auth.limiter._storage.reset()
    app=FastAPI()
    app.include_router(routes_auth.router)
    def database():
        with Session(review_db.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db]=database
    client=TestClient(app,raise_server_exceptions=False)
    class LoginFixture:
        def __repr__(self): return '<owned login fixture; credentials redacted>'
        def request(self,valid=True):
            return client.post('/auth/login',data={'username':'student','password':password if valid else 'synthetic-invalid'})
    return LoginFixture()


def test_http_login_success_has_persisted_attempt(login_client,review_db):
    response=login_client.request()
    assert response.status_code==200
    assert bool(response.json().get('access_token'))
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM login_attempts WHERE success=1')).scalar_one()==1


@pytest.mark.parametrize('valid',[True,False])
@pytest.mark.parametrize('mode',['ordinary','mfa','mfa_setup'])
def test_http_attempt_insert_failure_blocks_session(login_client,review_db,monkeypatch,valid,mode):
    if mode=='mfa':
        review_db.execute(text('UPDATE users SET mfa_enabled=1 WHERE id=1'))
    elif mode=='mfa_setup':
        review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true')"))
    review_db.commit()
    original=Session.execute
    def fail_attempt(self,statement,*args,**kwargs):
        if str(statement).lstrip().startswith('INSERT INTO login_attempts'):
            raise RuntimeError('synthetic attempt storage failure')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',fail_attempt)
    response=login_client.request(valid)
    assert response.status_code==503
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0


@pytest.mark.parametrize('valid',[True,False])
def test_http_attempt_commit_failure_blocks_session(login_client,review_db,monkeypatch,valid):
    original=Session.commit
    failed=False
    def fail_first(self):
        nonlocal failed
        if not failed:
            failed=True
            raise RuntimeError('synthetic first commit failure')
        return original(self)
    monkeypatch.setattr(Session,'commit',fail_first)
    response=login_client.request(valid)
    assert response.status_code==503
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0


def test_http_ambiguous_attempt_commit_does_not_issue_session(login_client,review_db,monkeypatch):
    original=Session.commit
    failed=False
    def uncertain_first(self):
        nonlocal failed
        original(self)
        if not failed:
            failed=True
            raise RuntimeError('synthetic committed but confirmation lost')
    monkeypatch.setattr(Session,'commit',uncertain_first)
    response=login_client.request()
    assert response.status_code==503
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM login_attempts')).scalar_one()==1


def test_http_audit_staging_failure_does_not_issue_session(login_client,review_db,monkeypatch):
    original=Session.add
    def fail_login_audit(self,entry,*args,**kwargs):
        if getattr(entry,'action',None)=='login_success':
            raise RuntimeError('synthetic login audit failure')
        return original(self,entry,*args,**kwargs)
    monkeypatch.setattr(Session,'add',fail_login_audit)
    response=login_client.request()
    assert response.status_code==503
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0


def test_cleanup_failure_does_not_undo_confirmed_record(clock,review_db,monkeypatch):
    monkeypatch.setattr(clock,'_last_login_cleanup',0)
    original=review_db.execute
    def fail_cleanup(statement,*args,**kwargs):
        if str(statement).lstrip().startswith('DELETE FROM login_attempts'):
            raise RuntimeError('synthetic cleanup failure')
        return original(statement,*args,**kwargs)
    monkeypatch.setattr(review_db,'execute',fail_cleanup)
    confirmed=clock._record_login_attempt(review_db,'192.0.2.1','student',False)
    assert confirmed is True
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM login_attempts')).scalar_one()==1
        assert fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='login_failed'")).scalar_one()==1


def test_http_session_insert_failure_does_not_issue_untracked_token(login_client,review_db,monkeypatch):
    original=Session.execute
    def fail_session(self,statement,*args,**kwargs):
        sql=str(statement)
        if 'INSERT' in sql and 'active_sessions' in sql:
            raise RuntimeError('synthetic session storage failure')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',fail_session)
    response=login_client.request()
    assert response.status_code==503
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one()==0
