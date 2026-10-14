"""Unwired actual signed authority leaf. Session schema/integration explicitly held."""
from datetime import datetime,timedelta,timezone
import sqlite3
import pytest
from core import account_lifetime_schema as schema
from core.account_lifetime_creation import _account_creation_scope,_create_account
from core.account_lifetime_validation import validate
from core import account_bound_tokens as tokens

@pytest.fixture
def db(tmp_path):
 c=sqlite3.connect(tmp_path/'tokens.db');c.execute('PRAGMA foreign_keys=ON')
 c.execute("CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE,password_hash TEXT,role TEXT,group_id INTEGER,is_active INTEGER DEFAULT 1,oidc_issuer TEXT,oidc_subject TEXT)")
 c.execute('BEGIN IMMEDIATE');schema.install(c);c.commit()
 yield c
 c.close()

def issue(c,boundary,account,purpose='access',claims=None):
 clock=tokens.capture_account_clock(c,boundary)
 return tokens.stage_account_token(c,boundary,account_nonce=account.account_nonce,user_id=account.id,purpose=purpose,issued_epoch_ms=clock.epoch_ms,expires_epoch_ms=clock.epoch_ms+60000,claims={} if claims is None else claims)

def decode(handle):
 import jwt
 from core import auth
 return jwt.decode(handle.encoded,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])

@pytest.mark.parametrize('purpose',['access','mfa_login','mfa_enrollment','websocket','oidc_pending'])
def test_actual_dbclock_immediate_fresh_birth_signed_issuance(db,purpose):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  clock=tokens.capture_account_clock(db,boundary)
  assert clock.epoch_ms%1000==0 and clock.observed_epoch_ms>=clock.epoch_ms and clock.observed_epoch_ms-clock.epoch_ms<1000
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'owned','password_hash':'storage-fixture','role':'viewer'})
  handle=issue(db,boundary,a,purpose,{'capability_snapshot_id':'owned-current-capability'} if purpose=='websocket' else {})
  payload=decode(handle)
  assert payload['iat']*1000==payload['issued_epoch_ms']==clock.epoch_ms
  assert payload['exp']*1000==payload['expires_epoch_ms']
  assert payload['account_purpose']==purpose and payload['account_nonce']==a.account_nonce
  assert payload['user_id']==a.id and payload['sub']=='owned'
  assert all(type(payload[k]) is int for k in ('iat','exp','issued_epoch_ms','expires_epoch_ms'))
  born=db.execute('SELECT born_at FROM account_lifetimes WHERE account_nonce=?',(a.account_nonce,)).fetchone()[0]
  authority=db.execute('SELECT issued_at FROM account_authorities WHERE jti=?',(handle.jti,)).fetchone()[0]
  assert born==authority==clock.instant.isoformat(timespec='milliseconds').replace('+00:00','Z')
  tokens.validate_staged_account_token(db,boundary,handle,purpose=purpose)
  assert db.in_transaction
 db.commit();db.execute('BEGIN');validate(db);db.rollback()

@pytest.mark.parametrize('bad',[True,1.0,None,1])
def test_reject_ambiguous_or_unaligned_numericdates(db,bad):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  clock=tokens.capture_account_clock(db,boundary);a=tokens._create_fresh_bound_account(db,boundary,{'username':'owned','password_hash':'storage-fixture','role':'viewer'})
  with pytest.raises(tokens.AccountTokenError):tokens.stage_account_token(db,boundary,account_nonce=a.account_nonce,user_id=a.id,purpose='access',issued_epoch_ms=clock.epoch_ms,expires_epoch_ms=bad,claims={})
  assert not boundary.alive
 db.rollback();assert db.execute('SELECT count(*) FROM users').fetchone()==(0,)

@pytest.mark.parametrize('claim',['exp','iat','jti','account_nonce','role','ws','mfa_pending','unexpected'])
def test_claims_cannot_override_authority(db,claim):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'owned','password_hash':'storage-fixture','role':'viewer'})
  with pytest.raises(tokens.AccountTokenError):issue(db,boundary,a,claims={claim:'forged'})
 db.rollback()

def test_existing_ms_birth_refuses_then_new_transaction_clock_retry(db,monkeypatch):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:clock=tokens.capture_account_clock(db,boundary)
 born=clock.instant+timedelta(milliseconds=456)
 with _account_creation_scope(db) as scope:a=_create_account(db,scope,{'username':'existing','password_hash':'storage-fixture','role':'viewer'},born_at=born)
 db.commit();monkeypatch.setattr(tokens,'_now',lambda c:clock.epoch_ms+456)
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  with pytest.raises(tokens.AccountTokenError,match='birth follows'):issue(db,boundary,a)
 db.rollback();assert db.execute('SELECT count(*) FROM account_authorities').fetchone()==(0,)
 monkeypatch.setattr(tokens,'_now',lambda c:clock.epoch_ms+1000)
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  handle=issue(db,boundary,a);assert handle.issued_epoch_ms==clock.epoch_ms+1000
 db.commit();assert db.execute('SELECT born_at FROM account_lifetimes').fetchone()[0]==born.isoformat(timespec='milliseconds').replace('+00:00','Z')

@pytest.mark.parametrize('phase',['sign','readback'])
def test_fault_no_hidden_outer_commit_and_allcaller_rollback(db,monkeypatch,phase):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'caller','password_hash':'storage-fixture','role':'viewer'})
  if phase=='sign':monkeypatch.setattr(tokens,'_sign',lambda p:(_ for _ in ()).throw(RuntimeError('owned sign failure')))
  else:
   original=schema.one
   def failure(c,sql,args=()):
    if 'SELECT account_nonce,purpose,issued_at,expires_at FROM account_authorities' in sql:raise RuntimeError('owned readback failure')
    return original(c,sql,args)
   monkeypatch.setattr(schema,'one',failure)
  with pytest.raises(RuntimeError):issue(db,boundary,a)
  assert not boundary.alive and db.in_transaction
 db.rollback();assert db.execute('SELECT count(*) FROM users').fetchone()==(0,);assert db.execute('SELECT count(*) FROM account_authorities').fetchone()==(0,)

def test_real_deferred_commit_fault_rolls_back_birth_authority(db):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'caller','password_hash':'storage-fixture','role':'viewer'});issue(db,boundary,a)
 db.execute("INSERT INTO users(username,password_hash,account_nonce) VALUES('missing','storage-fixture',?)",('f'*32,))
 with pytest.raises(sqlite3.IntegrityError):db.commit()
 db.rollback();assert db.execute('SELECT count(*) FROM account_authorities').fetchone()==(0,);assert db.execute('SELECT count(*) FROM users').fetchone()==(0,)

@pytest.mark.parametrize('mutation',['inactive','retired'])
def test_current_owner_refusal_after_state_change(db,mutation):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'caller','password_hash':'storage-fixture','role':'viewer'});handle=issue(db,boundary,a)
  if mutation=='inactive':db.execute('UPDATE users SET is_active=0 WHERE id=?',(a.id,))
  else:
   db.execute("UPDATE account_lifetimes SET state='retired',current_user_id=NULL,retired_at=? WHERE account_nonce=?",(tokens._instant(handle.issued_epoch_ms+1000).isoformat(timespec='milliseconds').replace('+00:00','Z'),a.account_nonce));db.execute('DELETE FROM account_bindings WHERE account_nonce=?',(a.account_nonce,));db.execute('DELETE FROM users WHERE id=?',(a.id,))
  with pytest.raises(tokens.AccountTokenError,match='active account'):tokens.validate_staged_account_token(db,boundary,handle,purpose='access')
 db.rollback()

def test_boundary_exit_and_raw_transaction_expiry(db):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  clock=tokens.capture_account_clock(db,boundary);db.commit();db.execute('BEGIN IMMEDIATE')
  with pytest.raises(tokens.AccountTokenError,match='boundary expired'):tokens.capture_account_clock(db,boundary)
 assert boundary.clock is None and not boundary.tokens and boundary.executor is None
 db.rollback()

def test_session_prerequisite_remains_failclosed_not_jti_fallback(db):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'caller','password_hash':'storage-fixture','role':'viewer'});handle=issue(db,boundary,a)
  with pytest.raises(tokens.AccountTokenError,match='nonce-bound session schema'):tokens.stage_bound_session(db,boundary,handle,ip='owned',user_agent='owned')
 db.rollback()

def test_exact_positive_leaf_has_no_transaction_methods_or_wait_under_locks(db,monkeypatch):
 import time
 monkeypatch.setattr(time,'sleep',lambda _:(_ for _ in ()).throw(AssertionError('wait under transaction')))
 db.execute('BEGIN IMMEDIATE');statements=[];db.set_trace_callback(statements.append)
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'initial-admin','password_hash':'storage-fixture','role':'admin'})
  handle=issue(db,boundary,a);assert decode(handle)['role']=='admin'
 assert db.in_transaction
 assert not any(x.strip().upper() in ('COMMIT','ROLLBACK') for x in statements)
 db.set_trace_callback(None);db.rollback()

def test_aligned_subsecond_clock_birth_exact_and_expiredclock_refuses(db,monkeypatch):
 now=int(datetime.now(timezone.utc).timestamp())*1000 # Fixture boundary only; production conversion never uses float.
 monkeypatch.setattr(tokens,'_now',lambda _:now+456)
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'aligned','password_hash':'storage-fixture','role':'viewer'})
  h=issue(db,boundary,a);assert h.issued_epoch_ms==now
  assert db.execute('SELECT born_at FROM account_lifetimes').fetchone()[0].endswith('.000Z')
  monkeypatch.setattr(tokens,'_now',lambda _:now+2457)
  with pytest.raises(tokens.AccountTokenError,match='clock expired'):issue(db,boundary,a)
 db.rollback()

@pytest.mark.parametrize('purpose',['unknown','Access',None,True])
def test_closed_purpose_refuses_no_authority(db,purpose):
 db.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(db) as boundary:
  a=tokens._create_fresh_bound_account(db,boundary,{'username':'owned','password_hash':'storage-fixture','role':'viewer'})
  with pytest.raises(tokens.AccountTokenError,match='purpose'):issue(db,boundary,a,purpose=purpose)
 db.rollback();assert db.execute('SELECT count(*) FROM account_authorities').fetchone()==(0,)
