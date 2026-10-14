"""Owned unregistered account storage parity; no issuer/authentication/API claim."""
import os,stat,secrets,tempfile,json,logging
from pathlib import Path
from datetime import datetime,timezone
private=Path(os.environ['CALLBACK_NATIVE_PASSWORD_FILE'])
assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
print('runtime-private-file uid=10001 mode=600',flush=True)
owned=tempfile.TemporaryDirectory(prefix='owned-account-parity-')
os.environ['ODIN_DATA_DIR']=owned.name;os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
os.environ['DATABASE_URL']='postgresql+psycopg://postgres@pg/postgres';os.environ['DATABASE_PASSWORD_FILE']=str(private)
logging.getLogger('httpx').disabled=True;logging.getLogger('httpcore').disabled=True
from sqlalchemy import create_engine,text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core import account_lifetime_schema as s
from core.account_lifetime_validation import validate,validate_retained
from core.account_lifetime_creation import _account_creation_scope,_create_account
from core.db_utils import _ConnectionAdapter
from core import account_bound_session_schema as session_schema
from core.account_bound_session_validation import validate as validate_session
from core import account_bound_tokens as tokens
import jwt
from core import auth
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
bootstrap_database(engine,Path('/source/backend'));checks=[]
with engine.begin() as c:
 c.exec_driver_sql('SELECT 1');s.install(c);session_schema.install(c)

def facts():
 with engine.connect() as c:return tuple(tuple(tuple(r) for r in c.exec_driver_sql('SELECT * FROM public.'+table+' ORDER BY 1')) for table in (*s.TABLES,'users','active_sessions','token_blacklist'))
def fresh(c,boundary,identifier):return tokens._create_fresh_bound_account(c,boundary,{'id':identifier,'username':'owned-session-'+str(identifier),'password_hash':'storage-fixture','role':'viewer'})
def issue(c,boundary,a,purpose='access'):
 clock=tokens.capture_account_clock(c,boundary)
 return tokens.stage_account_token(c,boundary,account_nonce=a.account_nonce,user_id=a.id,purpose=purpose,issued_epoch_ms=clock.epoch_ms,expires_epoch_ms=clock.epoch_ms+60000,claims={})
def start(c):
 if isinstance(c,Session):c.begin();c.execute(text('SELECT 1'))
 elif hasattr(c,'exec_driver_sql'):c.begin();c.exec_driver_sql('SELECT 1')
 else:c.execute('BEGIN')
def negative(c,sql,args=(),state=None):
 # Caller-owned savepoint isolates each expected PG error, never favorable outercommit.
 session_schema.execute(c,'SAVEPOINT owned_negative')
 try:session_schema.execute(c,sql,args)
 except Exception as e:
  code=getattr(getattr(e,'orig',e),'sqlstate',None)
  if state:assert code==state,(type(e).__name__,code)
 else:raise AssertionError('Direct SQL negative accepted')
 session_schema.execute(c,'ROLLBACK TO SAVEPOINT owned_negative');session_schema.execute(c,'RELEASE SAVEPOINT owned_negative')

with engine.begin() as c:
 c.exec_driver_sql('SELECT 1')
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  a=fresh(c,boundary,500);h=issue(c,boundary,a);sid=tokens.stage_bound_session(c,boundary,h,ip='127.0.0.1',user_agent='owned')
  payload=jwt.decode(h.encoded,auth.SECRET_KEY,algorithms=[auth.ALGORITHM]);assert payload['account_nonce']==a.account_nonce and payload['iat']*1000==h.issued_epoch_ms
  assert session_schema.one(c,'SELECT user_id,token_jti,account_nonce FROM active_sessions WHERE id=?',(sid,))==(a.id,h.jti,a.account_nonce)
 validate_session(c)
with engine.connect() as observer:
 row=observer.exec_driver_sql('SELECT account_nonce FROM public.active_sessions WHERE id='+str(sid)).scalar_one();assert row==a.account_nonce
checks.append('actualDBclock alignedfresh signedJWT authority→explicitnonce session outerCOMMIT freshobserver')
for kind in ('session','connection','adapter'):
 before=facts()
 if kind=='session':c=Session(engine)
 elif kind=='connection':c=engine.connect()
 else:raw=engine.raw_connection();c=_ConnectionAdapter(raw,True)
 try:
  start(c)
  with tokens._account_token_boundary(c,sessions=True) as boundary:
   b=fresh(c,boundary,600);staged=issue(c,boundary,b);tokens.stage_bound_session(c,boundary,staged,ip=None,user_agent=None)
  c.rollback()
 finally:raw.close() if kind=='adapter' else c.close()
 assert facts()==before
 checks.append(kind+' exacttoken/session andallcallerrollback')
# Real deferredCOMMIT failure rolls back fresh user/lifetime/authority/session together.
before=facts()
with engine.connect() as c:
 start(c)
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  b=fresh(c,boundary,700);staged=issue(c,boundary,b);tokens.stage_bound_session(c,boundary,staged,ip=None,user_agent=None)
 session_schema.execute(c,"INSERT INTO users(id,username,password_hash,account_nonce) VALUES(701,'missing-relay','storage-fixture',?)",('f'*32,))
 try:c.commit()
 except Exception as e:assert getattr(e.orig,'sqlstate',None)=='23503';c.rollback()
 else:raise AssertionError('Deferred invalid graph committed')
assert facts()==before;checks.append('actualdeferred23503 wholebirth/token/session rollback independentobserver')
# NULL historical rows are preserved only inactive, and originalunique REPLACE equivalents refuse.
with engine.begin() as c:
 c.exec_driver_sql('SELECT 1');session_schema.execute(c,"INSERT INTO users(id,username,password_hash,role) VALUES(10,'owned-legacy','storage-fixture','viewer')")
 session_schema.execute(c,"INSERT INTO active_sessions(id,user_id,token_jti) VALUES(10,10,'owned-legacy-session')")
 validate_session(c)
with engine.connect() as c:
 start(c)
 with tokens._account_token_boundary(c,sessions=True) as boundary:
  wrong=issue(c,boundary,a,'mfa_login')
  for sql,args in [
   ('INSERT INTO active_sessions(user_id,token_jti,account_nonce) VALUES(?,?,?)',(a.id,'missing-authority',a.account_nonce)),
   ('INSERT INTO active_sessions(user_id,token_jti,account_nonce) VALUES(?,?,?)',(a.id,wrong.jti,a.account_nonce)),
   ('INSERT INTO active_sessions(user_id,token_jti,account_nonce) VALUES(?,?,?)',(a.id,h.jti,'e'*32)),
   ("INSERT INTO active_sessions(id,user_id,token_jti) VALUES(10,10,'replaced') ON CONFLICT(id) DO UPDATE SET token_jti=excluded.token_jti",()),
   ("INSERT INTO active_sessions(user_id,token_jti) VALUES(10,'owned-legacy-session') ON CONFLICT(token_jti) DO UPDATE SET user_id=excluded.user_id",()),
   ('UPDATE active_sessions SET account_nonce=NULL WHERE id=?',(sid,)),
   ('UPDATE active_sessions SET user_id=10 WHERE id=?',(sid,)),
   ('TRUNCATE TABLE active_sessions',())]:negative(c,sql,args)
 c.rollback()
checks.append('directSQL wrongnonce/purpose/missingauthority identity/upsert/TRUNCATE noeffects; legacyNULL retained')
for change in ('blacklist','inactive','retired','expiry','repeat'):
 before=facts()
 with engine.connect() as c:
  start(c)
  with tokens._account_token_boundary(c,sessions=True) as boundary:
   b=fresh(c,boundary,800);staged=issue(c,boundary,b)
   if change=='blacklist':session_schema.execute(c,"INSERT INTO token_blacklist(jti,expires_at) VALUES(?,'2000-01-01')",(staged.jti,))
   elif change=='inactive':session_schema.execute(c,'UPDATE users SET is_active=FALSE WHERE id=?',(b.id,))
   elif change=='retired':
    stamp=tokens._instant(staged.issued_epoch_ms+1000)
    session_schema.execute(c,"UPDATE account_lifetimes SET state='retired',current_user_id=NULL,retired_at=? WHERE account_nonce=?",(stamp,b.account_nonce));session_schema.execute(c,'DELETE FROM account_bindings WHERE account_nonce=?',(b.account_nonce,));session_schema.execute(c,'DELETE FROM users WHERE id=?',(b.id,))
   elif change=='repeat':tokens.stage_bound_session(c,boundary,staged,ip=None,user_agent=None)
   else:
    # Source-owned expired authority negative tests direct SQL guard; no favorableauth handle.
    session_schema.execute(c,"INSERT INTO users(id,username,password_hash,account_nonce) VALUES(801,'expired-owner','storage-fixture',?)",('d'*32,))
    session_schema.execute(c,"INSERT INTO account_lifetimes VALUES(?,801,801,'active','2000-01-01',NULL)",('d'*32,));session_schema.execute(c,'INSERT INTO account_bindings VALUES(?,801)',('d'*32,))
    session_schema.execute(c,"INSERT INTO account_authorities VALUES('owned-expired',?,'access','2000-01-01','2000-01-02')",('d'*32,))
    negative(c,'INSERT INTO active_sessions(user_id,token_jti,account_nonce) VALUES(801,?,?)',('owned-expired','d'*32))
   if change!='expiry':
    try:tokens.stage_bound_session(c,boundary,staged,ip=None,user_agent=None)
    except tokens.AccountTokenError:pass
    else:raise AssertionError('Late session refusal failed')
  c.rollback()
 assert facts()==before
checks.append('currentblacklist/inactive/retired/repeatedhandle/directSQLexpiredauthority refuse allcallerrollback')
# Both session and blacklist actual insert predicates, distinct PG backend processes.
for table in ('active_sessions','token_blacklist'):
 with engine.connect() as writer:
  start(writer)
  if table=='active_sessions':session_schema.execute(writer,"INSERT INTO active_sessions(user_id,token_jti) VALUES(10,'writer-session')")
  else:session_schema.execute(writer,"INSERT INTO token_blacklist VALUES('writer-jti','2099-01-01')")
  with engine.connect() as contender:
   start(contender);assert writer.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()!=contender.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()
   try:
    with tokens._account_token_boundary(contender,sessions=True):pass
   except Exception as e:assert getattr(e.orig,'sqlstate',None)=='55P03'
   else:raise AssertionError('Writer-first admission accepted')
   contender.rollback()
  writer.rollback()
 with engine.connect() as creator:
  start(creator)
  with tokens._account_token_boundary(creator,sessions=True):
   with engine.connect() as writer:
    start(writer);writer.exec_driver_sql("SET LOCAL lock_timeout='25ms'");assert writer.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()!=creator.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()
    try:
     if table=='active_sessions':session_schema.execute(writer,"INSERT INTO active_sessions(user_id,token_jti) VALUES(10,'writer-session')")
     else:session_schema.execute(writer,"INSERT INTO token_blacklist VALUES('writer-jti','2099-01-01')")
    except Exception as e:assert getattr(e.orig,'sqlstate',None)=='55P03'
    else:raise AssertionError('Admission-first insertion accepted')
    writer.rollback()
  creator.rollback()
checks.append('actualsession+blacklist writerfirst/admissionfirst55P03 distinctbackendPIDs')
for timeout,expected in [('0','2s'),('5s','2s'),('25ms','25ms')]:
 with engine.connect() as c:
  start(c);c.exec_driver_sql("SET LOCAL lock_timeout='"+timeout+"'")
  with tokens._account_token_boundary(c,sessions=True):assert c.exec_driver_sql('SHOW lock_timeout').scalar_one()==expected
  c.rollback()
for ddl in ["ALTER FUNCTION public.account_session_guard() COST 200","ALTER TABLE public.active_sessions DROP CONSTRAINT active_sessions_account_nonce_fkey","ALTER TABLE public.active_sessions DISABLE TRIGGER account_session_lifecycle"]:
 with engine.connect() as c:
  start(c);c.exec_driver_sql(ddl)
  try:validate_session(c)
  except s.AccountLifetimeError:pass
  else:raise AssertionError('Catalog tamper accepted')
  c.rollback()
checks.append('boundedtimeout/stricterpositive andsourcecost/FK/disabledguard tamper refuses')
with engine.begin() as c:c.exec_driver_sql('SELECT 1');validate_session(c)
engine.dispose();owned.cleanup()
print(json.dumps({'checks':checks,'scope':'Unregistered session/token storage only; no currentauth/issuer/cutover/APIactivation'}),flush=True)
print('account-bound-session-parity-native-pass',flush=True)
