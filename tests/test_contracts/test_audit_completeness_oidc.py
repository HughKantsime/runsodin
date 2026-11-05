"""OIDC audit outcomes, with in-process provider replies and real database writes."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, parse_qs
import pytest
from sqlalchemy import text, event
from sqlalchemy.orm import Session
from cryptography.fernet import Fernet
from tests.test_contracts.test_audit_completeness import client, review_db, rows, assert_row, refuse_audit, PEER
from tests.test_contracts.test_education_oidc_identity import _seed_oidc_config, _CallbackHandler

pytestmark=pytest.mark.usefixtures('enterprise_license')


def seed_code(db, client, *, token=None, expired=False):
    from modules.organizations.routes_oidc import oidc_code_key
    from core.auth import create_access_token
    binding='b'*64;client.cookies.set('odin_oidc_bind',binding)
    token=token or create_access_token({'sub':'admin','role':'admin'})
    db.execute(text('INSERT INTO oidc_auth_codes(code,access_token,expires_at) VALUES(:code,:token,:expiry)'),
               {'code':oidc_code_key('owned',binding),'token':token,'expiry':(datetime.now(timezone.utc)+timedelta(minutes=-1 if expired else 2)).isoformat()})
    db.commit();return 'owned'


@pytest.mark.parametrize('failure',[False,True])
def test_oidc_session_and_audit_commit_together(client,review_db,failure):
    code=seed_code(review_db,client)
    listener=refuse_audit(review_db,'auth.login') if failure else None
    try:response=client.post('/auth/oidc/exchange',json={'code':code})
    finally:
        if listener:event.remove(review_db.get_bind(),'before_cursor_execute',listener)
    assert response.status_code==(503 if failure else 200),response.text
    if failure:
        assert 'access_token' not in response.json() and 'session' not in response.cookies
        assert not rows(review_db,'auth.login')
        assert_row(review_db,'auth.login_failed','user',None,{'method':'oidc','reason':'auth_failed','username':'admin'},actor=False)
    else:assert_row(review_db,'auth.login','user',3,{'method':'oidc','username':'admin'},actor=False)
    assert review_db.execute(text('SELECT count(*) FROM active_sessions')).scalar_one()==(0 if failure else 1)


@pytest.mark.parametrize('mode,reason,status',[
    ('missing','missing_params',400),('binding','invalid_code',400),('unknown','invalid_code',400),
    ('expired','code_expired',400),('jwt','invalid_session',401),('inactive','invalid_session',401),
    ('revoked','invalid_session',401),('consume_db','auth_unavailable',503),
    ('blacklist_db','auth_unavailable',503),('user_db','auth_unavailable',503),
    ('session_db','auth_unavailable',503),('crypto','auth_failed',503),('mfa','auth_failed',503),
])
def test_each_exchange_failure_is_audited(client,review_db,monkeypatch,mode,reason,status):
    code=seed_code(review_db,client,token='bad-token' if mode=='jwt' else None,expired=mode=='expired')
    if mode=='missing':code=''
    if mode=='binding':client.cookies.clear()
    if mode=='unknown':code='unknown'
    if mode=='inactive':review_db.execute(text('UPDATE users SET is_active=FALSE WHERE id=3'));review_db.commit()
    if mode=='revoked':
        import jwt
        from core import auth
        token=review_db.execute(text('SELECT access_token FROM oidc_auth_codes')).scalar_one()
        jti=jwt.decode(token,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])['jti']
        review_db.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:exp)'),{'jti':jti,'exp':datetime.now(timezone.utc)+timedelta(hours=1)});review_db.commit()
    needles={'consume_db':'DELETE FROM oidc_auth_codes WHERE code','blacklist_db':'SELECT 1 FROM token_blacklist','user_db':'SELECT id,is_active FROM users','session_db':'active_sessions'}
    if mode in needles:
        original=Session.execute
        def fail(db,statement,*args,**kwargs):
            if needles[mode] in str(statement):raise RuntimeError('Owned database failure')
            return original(db,statement,*args,**kwargs)
        monkeypatch.setattr(Session,'execute',fail)
    if mode=='crypto':
        monkeypatch.setattr('core.crypto.is_encrypted',lambda value:True)
        def fail(value):raise ValueError('private@example.test')
        monkeypatch.setattr('core.crypto.decrypt',fail)
    if mode=='mfa':
        def fail(*args):raise ValueError('private@example.test')
        monkeypatch.setattr('core.mfa_login.login_mfa_response',fail)
    response=client.post('/auth/oidc/exchange',json={'code':code})
    assert response.status_code==status,response.text
    assert 'access_token' not in response.json() and 'session' not in response.cookies
    assert_row(review_db,'auth.login_failed','user',None,{'method':'oidc','reason':reason,'username': ('admin' if mode in ('inactive','revoked','blacklist_db','user_db','session_db','mfa') else None)},actor=False)


@pytest.fixture
def callback(client,review_db,monkeypatch):
    from modules.organizations import routes_oidc,oidc_handler
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    _seed_oidc_config(review_db)
    monkeypatch.setattr(oidc_handler,'create_handler_from_config',lambda *args:_CallbackHandler())
    client.cookies.set('odin_oidc_bind',routes_oidc.oidc_state_binding('fixture-state'))
    return {'code':'fixture-code','state':'fixture-state'}


@pytest.mark.parametrize('mode,reason',[
    ('provider','access_denied'),('personal_error','auth_failed'),('missing','missing_params'),
    ('config','oidc_not_configured'),('binding','invalid_state'),('state','invalid_state'),
    ('factory','auth_failed'),('transport','auth_failed')])
def test_callback_refusals_audited(client,review_db,callback,monkeypatch,mode,reason):
    from modules.organizations import oidc_handler
    if mode=='provider':callback['error']='access_denied'
    if mode=='personal_error':callback['error']='private@example.test'
    if mode=='missing':callback.pop('code')
    if mode=='config':review_db.execute(text('DELETE FROM oidc_config'));review_db.commit()
    if mode=='binding':client.cookies.clear()
    if mode=='state':monkeypatch.setattr(_CallbackHandler,'validate_state',lambda *args:False)
    if mode=='factory':
        def fail(*args):raise ValueError('owned provider failure')
        monkeypatch.setattr(oidc_handler,'create_handler_from_config',fail)
    if mode=='transport':
        async def fail(*args):raise ValueError('private@example.test')
        monkeypatch.setattr(_CallbackHandler,'exchange_code',fail)
    response=client.get('/auth/oidc/callback',params=callback,follow_redirects=False)
    assert response.status_code==(503 if mode=='factory' else 302)
    assert_row(review_db,'auth.login_failed','user',None,{'method':'oidc','reason':reason,'username':None},actor=False)


@pytest.mark.parametrize('reason',[
    'userinfo_subject_mismatch','missing_email','google_issuer_invalid','google_email_unverified',
    'google_hosted_domain_invalid','google_email_domain_invalid','missing_issuer','missing_subject',
    'user_inactive','oidc_identity_ambiguous','oidc_default_tenant_required','oidc_email_ambiguous',
    'oidc_email_conflict','user_not_found','oidc_role_configuration_invalid','license_user_limit_reached'])
def test_callback_identity_errors_retain_only_code(client,review_db,callback,monkeypatch,reason):
    from modules.organizations import routes_oidc
    def refuse(*args,**kwargs):raise routes_oidc.OIDCIdentityError(reason)
    monkeypatch.setattr(routes_oidc,'_resolve_oidc_user',refuse)
    response=client.get('/auth/oidc/callback',params=callback,follow_redirects=False)
    assert response.status_code==302
    assert_row(review_db,'auth.login_failed','user',None,{'method':'oidc','reason':reason,'username':None},actor=False)


@pytest.mark.parametrize('failure',[False,True])
def test_oidc_provisioning_audited_atomically(client,review_db,callback,failure):
    listener=refuse_audit(review_db,'user.created') if failure else None
    try:response=client.get('/auth/oidc/callback',params=callback,follow_redirects=False)
    finally:
        if listener:event.remove(review_db.get_bind(),'before_cursor_execute',listener)
    assert response.status_code==302
    review_db.rollback()
    uid=review_db.execute(text("SELECT id FROM users WHERE oidc_subject='subject-1'")).scalar_one_or_none()
    if failure:
        assert uid is None and 'oidc_code=' not in response.headers['location']
        assert not rows(review_db,'user.created')
        assert_row(review_db,'auth.login_failed','user',None,{'method':'oidc','reason':'auth_failed','username':None},actor=False)
    else:
        assert uid is not None
        assert_row(review_db,'user.created','user',uid,{'role':'viewer'},actor=False)


def test_failure_audit_failure_refuses_without_token(client,review_db):
    listener=refuse_audit(review_db)
    try:response=client.post('/auth/oidc/exchange',json={})
    finally:event.remove(review_db.get_bind(),'before_cursor_execute',listener)
    assert response.status_code==503
    assert 'access_token' not in response.json() and 'session' not in response.cookies
