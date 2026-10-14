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
engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=private.read_text(),host='pg',database='postgres'),hide_parameters=True)
bootstrap_database(engine,Path('/source/backend'));NOW=datetime(2026,10,13,12,tzinfo=timezone.utc);checks=[]
with engine.begin() as c:c.exec_driver_sql('SELECT 1');s.install(c)

def birth(c,identifier):
 if isinstance(c,Session):c.execute(text('SELECT 1'))
 elif hasattr(c,'exec_driver_sql'):c.exec_driver_sql('SELECT 1')
 else:c.execute('SELECT 1')
 with _account_creation_scope(c) as scope:return _create_account(c,scope,{'id':identifier,'username':'owned-account-'+str(identifier),'password_hash':'storage-fixture','role':'viewer'},born_at=NOW)
def retire(c,a,time='2026-10-13T12:01:00.000Z'):
 s.execute(c,"UPDATE account_lifetimes SET state='retired',current_user_id=NULL,retired_at=? WHERE account_nonce=?",(time,a.account_nonce));s.execute(c,'DELETE FROM account_bindings WHERE account_nonce=?',(a.account_nonce,));s.execute(c,'DELETE FROM users WHERE id=?',(a.id,))
def facts():
 with engine.connect() as c:return tuple(tuple(tuple(r) for r in c.exec_driver_sql('SELECT * FROM public.'+t+' ORDER BY 1')) for t in (*s.TABLES,'users'))
def reject(sql,args=(),expected=None):
 before=facts()
 with engine.connect() as c:
  tx=c.begin();c.exec_driver_sql('SELECT 1')
  try:s.execute(c,sql,args)
  except Exception as e:
   if expected:assert getattr(e.orig,'sqlstate',None)==expected,type(e).__name__
  else:raise AssertionError('Storage negative accepted')
  tx.rollback()
 assert facts()==before

with engine.begin() as c:a=birth(c,500);actor=birth(c,501);validate(c)
checks.append('source-created completecycle persists andcatalog/retained validate')
# Actual four styles: SQLite covered host; PostgreSQL Session/Connection/existing daemon adapter.
for kind in ('session','connection','adapter'):
 before=facts()
 if kind=='session':
  with Session(engine) as c:
   c.begin();c.execute(text('SELECT 1'));b=birth(c,600);validate(c);c.rollback()
 elif kind=='connection':
  with engine.connect() as c:
   c.begin();c.exec_driver_sql('SELECT 1');b=birth(c,600);validate(c);c.rollback()
 else:
  raw=engine.raw_connection();c=_ConnectionAdapter(raw,True)
  try:c.execute('BEGIN');b=birth(c,600);validate(c);c.rollback()
  finally:raw.close()
 assert facts()==before
 checks.append(kind+' factory/validator entirecallerrollback')
for ending in ('commit','rollback'):
 raw=engine.raw_connection();c=_ConnectionAdapter(raw,True)
 try:
  c.execute('BEGIN')
  with _account_creation_scope(c) as scope:
   getattr(c,ending)();c.execute('BEGIN')
   try:_create_account(c,scope,{'username':'expired','password_hash':'storage-fixture'},born_at=NOW)
   except s.AccountLifetimeError as e:assert str(e)=='Account creation scope expired'
   else:raise AssertionError('Expired physical marker accepted')
  c.rollback()
 finally:raw.close()
 checks.append('adapter raw'+ending+'/reBEGIN markerexpiry')
# Real deferred COMMIT, fresh independent observer confirms complete caller rollback.
before=facts()
with engine.connect() as c:
 tx=c.begin();c.exec_driver_sql('SELECT 1');born=birth(c,700)
 s.execute(c,"INSERT INTO users(id,username,password_hash,account_nonce) VALUES(701,'missing-relay','storage-fixture',?)",('a'*32,))
 try:tx.commit()
 except Exception as e:assert getattr(e.orig,'sqlstate',None)=='23503';c.rollback()
 else:raise AssertionError('Missing deferred relay committed')
assert facts()==before;checks.append('real deferredCOMMIT23503 wholecaller/freshbirth rollback observer')
# Retirement and self receipt exact branch, numeric and username reuse retains old history.
with engine.begin() as c:
 c.exec_driver_sql('SELECT 1');retire(c,a)
 s.execute(c,"INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.000Z','retired')",('b'*32,a.account_nonce,a.account_nonce,'c'*64))
 fresh=birth(c,500);assert fresh.account_nonce!=a.account_nonce;validate(c)
checks.append('retirementrelay/selfreceipt numeric+username reuse freshnonce oldhistoryretained')
reject('DELETE FROM account_lifetimes WHERE account_nonce=?',(a.account_nonce,))
reject('UPDATE account_lifetimes SET born_at=born_at WHERE account_nonce=?',(a.account_nonce,))
reject('INSERT INTO account_bindings VALUES(?,500)',(a.account_nonce,))
reject("INSERT INTO account_auth_activation VALUES(1,2,'2026-10-13T12:00:00.000Z')")
reject('TRUNCATE TABLE account_authorities')
reject('UPDATE users SET account_nonce=NULL WHERE id=500')
reject("INSERT INTO account_authorities VALUES('invalid',?,'other','2026-10-13T12:00:00.000Z','2026-10-13T12:01:00.000Z')",(fresh.account_nonce,),'23514')
for invalid in ('infinity','-infinity','10000-01-01','0001-01-01 BC','2026-10-13 12:00:00.000001+00'):
 reject("INSERT INTO account_lifetimes VALUES(?,800,800,'active',CAST(? AS TIMESTAMPTZ),NULL)",('d'*32,invalid),'23514')
checks.append('immutablehistory/retirednonce/activation/TRUNCATE/closedpurpose/finiteUTC guards')
# Both real writer orders: admission predicates cannot admit competing inserts.
with engine.connect() as writer:
 writer.begin();writer.exec_driver_sql('SELECT 1')
 s.execute(writer,"INSERT INTO account_authorities VALUES('writer-first',?,'access','2026-10-13T12:00:00.000Z','2026-10-13T12:02:00.000Z')",(actor.account_nonce,))
 with engine.connect() as contender:
  contender.begin();contender.exec_driver_sql('SELECT 1')
  assert writer.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()!=contender.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()
  try:
   with _account_creation_scope(contender):pass
  except Exception as e:assert getattr(e.orig,'sqlstate',None)=='55P03'
  else:raise AssertionError('Writer-first predicate admission accepted')
  contender.rollback()
 writer.rollback()
with engine.connect() as creator:
 creator.begin();creator.exec_driver_sql('SELECT 1')
 with _account_creation_scope(creator):
  with engine.connect() as contender:
   contender.begin();contender.exec_driver_sql("SET LOCAL lock_timeout='30ms'")
   assert creator.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()!=contender.exec_driver_sql('SELECT pg_backend_pid()').scalar_one()
   try:s.execute(contender,"INSERT INTO account_authorities VALUES('creator-first',?,'access','2026-10-13T12:00:00.000Z','2026-10-13T12:02:00.000Z')",(actor.account_nonce,))
   except Exception as e:assert getattr(e.orig,'sqlstate',None)=='55P03'
   else:raise AssertionError('Creator-first insertion accepted')
   contender.rollback()
 creator.rollback()
checks.append('real distinctPGconnections writerfirst/creatorfirst55P03 predicatefences')
for setting,expected in [('0','2s'),('5s','2s'),('25ms','25ms')]:
 with engine.connect() as c:
  c.begin();c.exec_driver_sql("SET LOCAL lock_timeout='"+setting+"'")
  with _account_creation_scope(c):assert c.exec_driver_sql('SHOW lock_timeout').scalar_one()==expected
  c.rollback()
checks.append('boundedtimeout caps0/large preservesstricterpositive')
# Temp same-name and catalog function/options tamper fail closed; rollback restores authority.
for target in ('users','account_bindings'):
 with engine.connect() as c:
  c.begin();c.exec_driver_sql('CREATE TEMP TABLE '+target+'(id INTEGER)')
  try:
   with _account_creation_scope(c):pass
  except s.AccountLifetimeError as e:assert str(e)=='Temporary account relation refused'
  else:raise AssertionError('Temporary shadow accepted')
  c.rollback()
for alteration in ["ALTER FUNCTION public.account_028_users_guard() COST 200","ALTER FUNCTION public.account_028_users_guard() SET search_path TO public","ALTER TABLE public.account_bindings DROP CONSTRAINT account_bindings_account_nonce_user_id_fkey","ALTER TABLE public.account_authorities DROP CONSTRAINT aca_purpose"]:
 with engine.connect() as c:
  c.begin();c.exec_driver_sql(alteration)
  try:validate(c)
  except s.AccountLifetimeError:pass
  else:raise AssertionError('Altered catalog accepted')
  c.rollback()
 with engine.begin() as c:c.exec_driver_sql('SELECT 1');validate(c)
checks.append('tempshadow andexactcost/searchpath/FK/CHECK tamper refusal rollback')
with engine.begin() as c:c.exec_driver_sql('SELECT 1');validate(c)
engine.dispose();owned.cleanup()
print(json.dumps({'checks':checks,'scope':'Unregistered storage only; no authentication/issuer/API/legacyenrollment'}),flush=True)
print('account-lifetime-parity-native-pass',flush=True)
