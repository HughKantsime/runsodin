"""Unregistered explicit session schema/staging; active auth routes unchanged."""
import sqlite3
from pathlib import Path
import pytest
from core import account_lifetime_schema as lifetime
from core import account_bound_session_schema as schema
from core.account_bound_session_validation import BASE,validate
from core import account_bound_tokens as tokens
from tests.test_contracts.test_account_bound_tokens import db,decode,issue

@pytest.fixture
def sessions(db):
 db.execute(BASE)
 db.execute('CREATE TABLE token_blacklist(jti VARCHAR(64) PRIMARY KEY,expires_at DATETIME NOT NULL)')
 db.execute("INSERT INTO users(id,username,password_hash,role) VALUES(10,'legacy','storage-fixture','viewer')")
 db.execute("INSERT INTO active_sessions(id,user_id,token_jti) VALUES(100,10,'legacy-session'),(101,10,'highwater')");db.execute('DELETE FROM active_sessions WHERE id=101');db.commit()
 original=db.execute('SELECT * FROM active_sessions').fetchall();columns=db.execute('PRAGMA table_xinfo(active_sessions)').fetchall();sequence=db.execute("SELECT seq FROM sqlite_sequence WHERE name='active_sessions'").fetchone()
 db.execute('BEGIN IMMEDIATE');schema.install(db);db.commit()
 assert db.execute('PRAGMA table_xinfo(active_sessions)').fetchall()[:-1]==columns
 assert db.execute('SELECT '+','.join('"'+r[1]+'"' for r in columns)+' FROM active_sessions').fetchall()==original
 assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='active_sessions'").fetchone()==sequence
 yield db


def fresh(c,boundary):return tokens._create_fresh_bound_account(c,boundary,{'username':'owned','password_hash':'storage-fixture','role':'admin'})

def test_actual_signed_token_to_explicit_nonce_session_one_commit(sessions):
 c=sessions;c.execute('BEGIN IMMEDIATE');trace=[];c.set_trace_callback(trace.append)
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a)
  sid=tokens.stage_bound_session(c,boundary,h,ip='127.0.0.1',user_agent='x'*700)
  assert c.execute('SELECT user_id,token_jti,account_nonce,length(user_agent) FROM active_sessions WHERE id=?',(sid,)).fetchone()==(a.id,h.jti,a.account_nonce,500)
  assert decode(h)['account_nonce']==a.account_nonce
 assert not any(x.strip().upper() in ('COMMIT','ROLLBACK') for x in trace)
 c.commit();assert sum(x.strip().upper()=='COMMIT' for x in trace)==1;c.set_trace_callback(None)
 c.execute('BEGIN');validate(c);c.rollback()
 assert c.execute('SELECT account_nonce FROM active_sessions WHERE id=100').fetchone()==(None,)


def test_repeated_private_handle_refuses_and_caller_rollback(sessions):
 c=sessions;c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a);tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
  with pytest.raises(tokens.AccountTokenError,match='already consumed'):tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
  assert not boundary.alive
 c.rollback();assert c.execute('SELECT count(*) FROM active_sessions').fetchone()==(1,)

@pytest.mark.parametrize('change',['blacklist','inactive','retired'])
def test_late_current_owner_or_blacklist_refuses_zero_session(sessions,change):
 c=sessions;c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a)
  if change=='blacklist':c.execute("INSERT INTO token_blacklist VALUES(?,'2000-01-01')",(h.jti,))
  elif change=='inactive':c.execute('UPDATE users SET is_active=0 WHERE id=?',(a.id,))
  else:
   time=tokens._instant(h.issued_epoch_ms+1000).isoformat(timespec='milliseconds').replace('+00:00','Z')
   c.execute("UPDATE account_lifetimes SET state='retired',current_user_id=NULL,retired_at=? WHERE account_nonce=?",(time,a.account_nonce));c.execute('DELETE FROM account_bindings WHERE account_nonce=?',(a.account_nonce,));c.execute('DELETE FROM users WHERE id=?',(a.id,))
  with pytest.raises(tokens.AccountTokenError):tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
  assert c.execute('SELECT count(*) FROM active_sessions').fetchone()==(1,)
 c.rollback()

@pytest.mark.parametrize('recursive',[0,1])
@pytest.mark.parametrize('which',['legacy_id','legacy_jti','bound_id','bound_jti'])
def test_replace_all_original_unique_keys_preserves_rows(sessions,recursive,which):
 c=sessions;c.execute('PRAGMA recursive_triggers='+str(recursive));c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a);sid=tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
  before=c.execute('SELECT * FROM active_sessions ORDER BY id').fetchall()
  idvalue=100 if which=='legacy_id' else sid if which=='bound_id' else 999
  jti='legacy-session' if which=='legacy_jti' else h.jti if which=='bound_jti' else 'another'
  with pytest.raises(sqlite3.IntegrityError):c.execute('INSERT OR REPLACE INTO active_sessions(id,user_id,token_jti,account_nonce) VALUES(?,?,?,?)',(idvalue,a.id,jti,a.account_nonce))
  assert c.execute('SELECT * FROM active_sessions ORDER BY id').fetchall()==before
 c.rollback()

@pytest.mark.parametrize('field',['id','user_id','token_jti','account_nonce'])
def test_session_identity_immutable_metadata_and_delete_compatible(sessions,field):
 c=sessions;c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a);sid=tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
  value=999 if field in ('id','user_id') else 'f'*32
  with pytest.raises(sqlite3.IntegrityError):c.execute('UPDATE active_sessions SET '+field+'=? WHERE id=?',(value,sid))
  c.execute("UPDATE active_sessions SET user_agent='changed' WHERE id=?",(sid,));c.execute('DELETE FROM active_sessions WHERE id=?',(sid,))
 c.rollback()

@pytest.mark.parametrize('failure',['insert','readback','deferred'])
def test_full_caller_rollback_on_session_fault(sessions,monkeypatch,failure):
 c=sessions;c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a)
  if failure=='insert':
   original_execute=schema.execute
   def insert_failure(c,sql,args=()):
    if sql.startswith('INSERT INTO active_sessions'):
     c.execute("CREATE TRIGGER owned_fault BEFORE INSERT ON active_sessions BEGIN SELECT RAISE(ABORT,'owned session failure'); END")
    return original_execute(c,sql,args)
   monkeypatch.setattr(schema,'execute',insert_failure)
  if failure=='readback':
   original=schema.one
   def broken(c,sql,args=()):
    if sql.startswith('SELECT id,user_id,token_jti,account_nonce'):raise RuntimeError('owned readback failure')
    return original(c,sql,args)
   monkeypatch.setattr(schema,'one',broken)
  if failure in ('insert','readback'):
   with pytest.raises(sqlite3.IntegrityError if failure=='insert' else RuntimeError,match='owned'):tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
   assert not boundary.alive
  else:tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
 if failure=='deferred':
  c.execute("INSERT INTO users(username,password_hash,account_nonce) VALUES('missing','storage-fixture',?)",('f'*32,))
  with pytest.raises(sqlite3.IntegrityError):c.commit()
 c.rollback();assert c.execute('SELECT count(*) FROM active_sessions').fetchone()==(1,);assert c.execute('SELECT count(*) FROM account_authorities').fetchone()==(0,);assert c.execute("SELECT count(*) FROM users WHERE username='owned'").fetchone()==(0,)


def test_missing_first_session_fences_refuses_without_late_lock(sessions):
 c=sessions;c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a)
  with pytest.raises(tokens.AccountTokenError,match='predicate boundary'):tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
 c.rollback()


def test_catalog_tamper_and_bypassed_restore_association_refuse(sessions):
 c=sessions;c.execute('BEGIN IMMEDIATE');c.execute('DROP TRIGGER account_session_insert')
 with pytest.raises(lifetime.AccountLifetimeError,match='guard shape'):validate(c)
 c.rollback();c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a);sid=tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
 c.execute('DROP TRIGGER account_session_update');c.execute('UPDATE active_sessions SET user_id=10 WHERE id=?',(sid,))
 from core.account_bound_session_validation import validate_retained
 with pytest.raises(lifetime.AccountLifetimeError,match='association'):validate_retained(c)
 c.rollback()

@pytest.mark.parametrize('kind',['session','connection','adapter'])
def test_actual_executor_token_to_session_and_expiry(sessions,tmp_path,kind):
 from sqlalchemy import create_engine,event,text
 from sqlalchemy.orm import Session
 from core.db_utils import _ConnectionAdapter
 path=sessions.execute('PRAGMA database_list').fetchone()[2];engine=create_engine('sqlite:///'+path)
 @event.listens_for(engine,'connect')
 def fk(driver,_):driver.execute('PRAGMA foreign_keys=ON')
 if kind=='adapter':
  raw=engine.raw_connection();c=_ConnectionAdapter(raw,False);c.execute('BEGIN IMMEDIATE')
 elif kind=='session':
  c=Session(engine);c.begin();c.connection().exec_driver_sql('BEGIN IMMEDIATE')
 else:c=engine.connect();c.begin();c.exec_driver_sql('BEGIN IMMEDIATE')
 try:
  with tokens._account_token_boundary(c,sessions=True) as boundary:
   a=fresh(c,boundary);h=issue(c,boundary,a);sid=tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
   assert sid>0
   c.rollback()
   c.execute('BEGIN IMMEDIATE') if kind=='adapter' else c.connection().exec_driver_sql('BEGIN IMMEDIATE') if kind=='session' else c.exec_driver_sql('BEGIN IMMEDIATE')
   with pytest.raises(tokens.AccountTokenError,match='boundary expired'):tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
  c.rollback()
 finally:
  raw.close() if kind=='adapter' else c.close();engine.dispose()
 assert sessions.execute('SELECT count(*) FROM active_sessions').fetchone()==(1,)


def test_actual_bootstrap_session_catalog_and_legacydata_preserved(tmp_path):
 from sqlalchemy import create_engine,event
 from core.schema import bootstrap_database
 file=tmp_path/'actual-catalog.db';engine=create_engine('sqlite:///'+str(file))
 @event.listens_for(engine,'connect')
 def fk(driver,_):driver.execute('PRAGMA foreign_keys=ON')
 bootstrap_database(engine,Path(__file__).resolve().parents[2]/'backend');engine.dispose()
 c=sqlite3.connect(file);c.execute('PRAGMA foreign_keys=ON');c.execute("INSERT INTO users(id,username,password_hash,role) VALUES(10,'owned','storage-fixture','viewer')")
 c.execute("INSERT INTO active_sessions(id,user_id,token_jti) VALUES(200,10,'owned-old')");c.commit()
 columns=c.execute('PRAGMA table_xinfo(active_sessions)').fetchall();indexes=c.execute('PRAGMA index_list(active_sessions)').fetchall();fks=c.execute('PRAGMA foreign_key_list(active_sessions)').fetchall();old=c.execute('SELECT * FROM active_sessions').fetchall()
 c.execute('BEGIN IMMEDIATE');lifetime.install(c);schema.install(c);c.commit()
 assert c.execute('PRAGMA table_xinfo(active_sessions)').fetchall()[:-1]==columns
 assert c.execute('PRAGMA index_list(active_sessions)').fetchall()==indexes
 actual_fks=c.execute('PRAGMA foreign_key_list(active_sessions)').fetchall()
 # SQLite renumbers PRAGMA's local FK ordinal when ADD COLUMN adds a new FK.
 assert sorted(x[1:] for x in actual_fks if x[3]!='account_nonce')==sorted(x[1:] for x in fks)
 assert [x[1:] for x in actual_fks if x[3]=='account_nonce']==[(0,'account_lifetimes','account_nonce','account_nonce','NO ACTION','NO ACTION','NONE')]
 assert c.execute('SELECT '+','.join('"'+r[1]+'"' for r in columns)+' FROM active_sessions').fetchall()==old
 c.execute('BEGIN');validate(c);c.rollback();c.close()


def test_expired_authority_current_session_insert_refuses(sessions,monkeypatch):
 c=sessions;c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary);h=issue(c,boundary,a)
  monkeypatch.setattr(tokens,'_now',lambda _:h.expires_epoch_ms)
  with pytest.raises(tokens.AccountTokenError,match='expired'):tokens.stage_bound_session(c,boundary,h,ip=None,user_agent=None)
  assert not boundary.alive
 c.rollback()

@pytest.mark.parametrize('mismatch',['wrong_nonce','wrong_purpose','missing_authority'])
def test_direct_sql_session_insert_association_refusals(sessions,mismatch):
 c=sessions;c.execute('BEGIN IMMEDIATE')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary)
  if mismatch=='wrong_purpose':h=issue(c,boundary,a,purpose='mfa_login')
  else:h=issue(c,boundary,a)
  nonce='f'*32 if mismatch=='wrong_nonce' else a.account_nonce
  jti='unissued-jti' if mismatch=='missing_authority' else h.jti
  before=c.execute('SELECT * FROM active_sessions ORDER BY id').fetchall()
  with pytest.raises(sqlite3.IntegrityError,match='binding refused'):
   c.execute('INSERT INTO active_sessions(user_id,token_jti,account_nonce) VALUES(?,?,?)',(a.id,jti,nonce))
  assert c.execute('SELECT * FROM active_sessions ORDER BY id').fetchall()==before
 c.rollback()
