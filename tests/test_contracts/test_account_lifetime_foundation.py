"""Unregistered additive storage controls; no authentication/retirement API claim."""
import sqlite3
from datetime import datetime,timezone
from pathlib import Path
import pytest
from core import account_lifetime_schema as s
from core.account_lifetime_validation import validate,validate_retained
from core.account_lifetime_creation import _account_creation_scope,_create_account
NOW=datetime(2026,10,13,12,tzinfo=timezone.utc)

@pytest.fixture
def db(tmp_path):
 c=sqlite3.connect(tmp_path/'owned.db');c.execute('PRAGMA foreign_keys=ON')
 c.execute('CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role TEXT DEFAULT \'viewer\',group_id INTEGER,is_active INTEGER DEFAULT 1,oidc_issuer TEXT,oidc_subject TEXT)')
 c.execute("INSERT INTO users(id,username,password_hash) VALUES(10,'legacy','synthetic')");c.commit();c.execute('BEGIN IMMEDIATE');s.install(c);c.commit()
 yield c
 c.close()

def birth(c,identifier=20):
 with _account_creation_scope(c) as scope:return _create_account(c,scope,{'id':identifier,'username':'owned'+str(identifier),'password_hash':'synthetic','role':'viewer'},born_at=NOW)
def retire(c,value):
 c.execute("UPDATE account_lifetimes SET state='retired',current_user_id=NULL,retired_at='2026-10-13T12:01:00.000Z' WHERE account_nonce=?",(value.account_nonce,));c.execute('DELETE FROM account_bindings WHERE account_nonce=?',(value.account_nonce,));c.execute('DELETE FROM users WHERE id=?',(value.id,))

def test_catalog_and_source_factory_outer_rollback(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);validate(db);assert db.in_transaction
 db.rollback();assert db.execute('SELECT count(*) FROM account_lifetimes').fetchone()==(0,)
 db.execute('BEGIN IMMEDIATE');a=birth(db);db.commit();db.execute('BEGIN');validate(db);db.rollback()
 assert db.execute('SELECT account_nonce FROM users WHERE id=10').fetchone()==(None,)
 assert len(s.sqlite_triggers())==17 and len(s.pg_functions())==6 and len(s.pg_triggers())==12

@pytest.mark.parametrize('missing',['lifetime','binding','user'])
def test_deferred_commit_cycle_missing_leg(db,missing):
 db.execute('BEGIN IMMEDIATE');n='a'*32
 if missing!='user':db.execute("INSERT INTO users(id,username,password_hash,account_nonce) VALUES(20,'owned','synthetic',?)",(n,))
 if missing!='lifetime':db.execute("INSERT INTO account_lifetimes VALUES(?,20,20,'active','2026-10-13T12:00:00.000Z',NULL)",(n,))
 if missing!='binding':
  if missing=='lifetime':
   with pytest.raises(sqlite3.IntegrityError):db.execute('INSERT INTO account_bindings VALUES(?,20)',(n,))
  else:db.execute('INSERT INTO account_bindings VALUES(?,20)',(n,))
 with pytest.raises(sqlite3.IntegrityError):db.commit()
 db.rollback();assert db.execute('SELECT count(*) FROM account_lifetimes').fetchone()==(0,)

@pytest.mark.parametrize('recursive',[0,1])
@pytest.mark.parametrize('table',['users','account_lifetimes','account_bindings','account_authorities','account_retirement_receipts'])
def test_all_replace_keys_refuse(db,recursive,table):
 db.execute('PRAGMA recursive_triggers='+str(recursive));db.execute('BEGIN IMMEDIATE');a=birth(db);actor=birth(db,21)
 db.execute("INSERT INTO account_authorities VALUES('owned-jti',?,'access','2026-10-13T12:00:00.000Z','2026-10-13T12:02:00.000Z')",(a.account_nonce,));retire(db,a)
 db.execute("INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.000Z','retired')",('b'*32,a.account_nonce,actor.account_nonce,'c'*64));db.commit();db.execute('BEGIN IMMEDIATE')
 if table=='users':sql="INSERT OR REPLACE INTO users(id,username,password_hash) VALUES(30,'owned21','replacement')";args=()
 elif table=='account_lifetimes':sql="INSERT OR REPLACE INTO account_lifetimes VALUES(?,20,20,'active','2026-10-13T12:00:00.000Z',NULL)";args=(a.account_nonce,)
 elif table=='account_bindings':sql='INSERT OR REPLACE INTO account_bindings VALUES(?,21)';args=('d'*32,)
 elif table=='account_authorities':sql="INSERT OR REPLACE INTO account_authorities VALUES('owned-jti',?,'access','2026-10-13T12:00:00.000Z','2026-10-13T12:02:00.000Z')";args=(actor.account_nonce,)
 else:sql="INSERT OR REPLACE INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.000Z','retired')";args=('d'*32,a.account_nonce,actor.account_nonce,'c'*64)
 with pytest.raises(sqlite3.IntegrityError):db.execute(sql,args)
 db.rollback();db.execute('BEGIN');validate(db);db.rollback()

@pytest.mark.parametrize('time',['2026-02-30T12:00:00.000Z','2026-10-13T24:00:00.000Z','2026-10-13T12:00:00.000001Z','0000-10-13T12:00:00.000Z','2026-10-13T12:60:00.000Z'])
def test_sql_timestamp_guards(db,time):
 db.execute('BEGIN IMMEDIATE')
 with pytest.raises(sqlite3.IntegrityError):db.execute("INSERT INTO account_lifetimes VALUES(?,20,20,'active',?,NULL)",('a'*32,time))
 db.rollback()

def test_retirement_cycle_reuse_immutable_and_activation(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);db.commit();db.execute('BEGIN IMMEDIATE')
 for sql,args in [('UPDATE users SET id=100 WHERE id=20',()),('UPDATE users SET account_nonce=? WHERE id=10',('b'*32,)),('DELETE FROM account_bindings WHERE account_nonce=?',(a.account_nonce,)),('DELETE FROM account_lifetimes WHERE account_nonce=?',(a.account_nonce,)),("INSERT INTO account_auth_activation VALUES(1,2,'2026-10-13T12:00:00.000Z')",())]:
  with pytest.raises(sqlite3.IntegrityError):db.execute(sql,args)
 db.execute('DELETE FROM users WHERE id=20')
 with pytest.raises(sqlite3.IntegrityError):db.commit()
 db.rollback();db.execute('BEGIN IMMEDIATE');retire(db,a);db.rollback();db.execute('BEGIN IMMEDIATE');retire(db,a);db.commit()
 db.execute('BEGIN IMMEDIATE');b=birth(db);assert b.account_nonce!=a.account_nonce;validate(db);db.commit()
 db.execute('BEGIN IMMEDIATE')
 with pytest.raises(sqlite3.IntegrityError):db.execute('UPDATE account_lifetimes SET born_at=born_at WHERE account_nonce=?',(a.account_nonce,))
 db.rollback()

@pytest.mark.parametrize('end',['commit','rollback'])
def test_raw_scope_transaction_marker_expiry(db,end):
 db.execute('BEGIN IMMEDIATE')
 with _account_creation_scope(db) as scope:
  getattr(db,end)();db.execute('BEGIN IMMEDIATE')
  with pytest.raises(s.AccountLifetimeError,match='Account creation scope expired'):_create_account(db,scope,{'username':'new','password_hash':'synthetic'},born_at=NOW)
 db.rollback()
 with pytest.raises(s.AccountLifetimeError,match='Account creation scope expired'):_create_account(db,scope,{'username':'new','password_hash':'synthetic'},born_at=NOW)

@pytest.mark.parametrize('kind',['session','connection'])
def test_sqlalchemy_physical_scope_and_no_hidden_commit(db,tmp_path,kind):
 from sqlalchemy import create_engine,event
 from sqlalchemy.orm import Session
 engine=create_engine('sqlite:///'+str(tmp_path/'owned.db'))
 @event.listens_for(engine,'connect')
 def fk(c,_):c.execute('PRAGMA foreign_keys=ON')
 with (Session(engine) if kind=='session' else engine.connect()) as executor:
  executor.begin()
  with pytest.raises(s.AccountLifetimeError,match='Physical caller transaction required'):
   with _account_creation_scope(executor):pass
  executor.connection().exec_driver_sql('BEGIN IMMEDIATE') if kind=='session' else executor.exec_driver_sql('BEGIN IMMEDIATE')
  with _account_creation_scope(executor) as scope:
   a=_create_account(executor,scope,{'username':'sqlalchemy','password_hash':'synthetic'},born_at=NOW)
  executor.rollback()
 assert db.execute("SELECT count(*) FROM users WHERE username='sqlalchemy'").fetchone()==(0,)
 engine.dispose()

def test_retained_validator_rejects_bypassed_guard_and_shape(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);db.commit();db.execute('BEGIN IMMEDIATE')
 db.execute('DROP TRIGGER account_028_lifetimes_update')
 with pytest.raises(s.AccountLifetimeError,match='guard shape'):validate(db)
 db.rollback()
 db.execute('BEGIN IMMEDIATE');db.execute('PRAGMA ignore_check_constraints=ON');db.execute('DROP TRIGGER account_028_lifetimes_update');db.execute("UPDATE account_lifetimes SET born_at='2026-02-30T12:00:00.000Z' WHERE account_nonce=?",(a.account_nonce,))
 with pytest.raises(s.AccountLifetimeError,match='timestamp'):validate_retained(db)
 db.rollback();db.execute('PRAGMA ignore_check_constraints=OFF')

def test_current_bootstrap_catalog_preserved(tmp_path):
 from sqlalchemy import create_engine,event
 from core.schema import bootstrap_database
 file=tmp_path/'catalog.db';engine=create_engine('sqlite:///'+str(file))
 @event.listens_for(engine,'connect')
 def fk(c,_):c.execute('PRAGMA foreign_keys=ON')
 bootstrap_database(engine,Path(__file__).resolve().parents[2]/'backend');engine.dispose()
 c=sqlite3.connect(file);c.execute('PRAGMA foreign_keys=ON')
 c.execute("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(100,'owned-retained','synthetic','viewer',1),(101,'owned-highwater','synthetic','viewer',1)");c.execute('DELETE FROM users WHERE id=101');c.commit()
 c.execute('CREATE TABLE owned_incoming(user_id INTEGER REFERENCES users(id))');c.execute('INSERT INTO owned_incoming VALUES(100)');c.commit()
 data=c.execute('SELECT * FROM users ORDER BY id').fetchall();seq=c.execute("SELECT seq FROM sqlite_sequence WHERE name='users'").fetchone()
 before=c.execute('PRAGMA table_xinfo(users)').fetchall();objects=c.execute("SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='users' AND type!='table' ORDER BY name").fetchall()
 incoming=lambda:[(t,r) for (t,) in c.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall() for r in c.execute('PRAGMA foreign_key_list("'+t+'")') if r[2]=='users']
 previous=incoming();c.execute('BEGIN IMMEDIATE');s.install(c);c.commit()
 assert c.execute('PRAGMA table_xinfo(users)').fetchall()[:-1]==before
 assert c.execute('SELECT '+','.join('"'+row[1]+'"' for row in before)+' FROM users ORDER BY id').fetchall()==data
 assert c.execute("SELECT seq FROM sqlite_sequence WHERE name='users'").fetchone()==seq
 assert all(x in c.execute("SELECT type,name,sql FROM sqlite_schema WHERE tbl_name='users' AND type!='table'").fetchall() for x in objects)
 assert [x for x in incoming() if x[0]!='account_lifetimes']==previous
 c.execute('BEGIN');validate(c);c.rollback();c.close()

@pytest.mark.parametrize('bad',['A'*32,'a'*31,'a'*33,'á'*32])
def test_nonce_sql_guard(db,bad):
 db.execute('BEGIN IMMEDIATE')
 with pytest.raises(sqlite3.IntegrityError):db.execute("INSERT INTO users(username,password_hash,account_nonce) VALUES('bad','synthetic',?)",(bad,))
 db.rollback()

@pytest.mark.parametrize('field,value',[('purpose','unknown'),('jti','has space'),('jti','é'),('issued_at','2026-10-13T11:59:59.999Z'),('expires_at','2026-10-13T12:00:00.000Z')])
def test_authority_closed_shape_and_birth_chronology(db,field,value):
 db.execute('BEGIN IMMEDIATE');a=birth(db)
 data={'jti':'owned','account_nonce':a.account_nonce,'purpose':'access','issued_at':'2026-10-13T12:00:00.000Z','expires_at':'2026-10-13T12:02:00.000Z'};data[field]=value
 with pytest.raises(sqlite3.IntegrityError):db.execute('INSERT INTO account_authorities VALUES(?,?,?,?,?)',tuple(data.values()))
 db.rollback()

def test_retirement_chronology_includes_issued_authority(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db)
 db.execute("INSERT INTO account_authorities VALUES('later',?,'access','2026-10-13T12:02:00.000Z','2026-10-13T12:03:00.000Z')",(a.account_nonce,))
 with pytest.raises(sqlite3.IntegrityError):retire(db,a)
 db.rollback()

def test_source_step_fault_caller_rollback_and_poison(db):
 db.execute('BEGIN IMMEDIATE');db.execute("INSERT INTO users(username,password_hash) VALUES('unrelated','synthetic')")
 db.execute("CREATE TRIGGER owned_fault BEFORE INSERT ON account_bindings BEGIN SELECT RAISE(ABORT,'owned fault'); END")
 with _account_creation_scope(db) as scope:
  with pytest.raises(sqlite3.IntegrityError,match='owned fault'):birth_inside=_create_account(db,scope,{'username':'faulted','password_hash':'synthetic'},born_at=NOW)
  assert db.execute("SELECT count(*) FROM users WHERE username='faulted'").fetchone()==(0,)
  with pytest.raises(s.AccountLifetimeError,match='expired'):_create_account(db,scope,{'username':'retry','password_hash':'synthetic'},born_at=NOW)
 db.rollback();assert db.execute("SELECT count(*) FROM users WHERE username='unrelated'").fetchone()==(0,)

def test_replace_oidc_identity_legacy_and_same_id_legacy(db):
 db.execute("UPDATE users SET oidc_issuer='issuer',oidc_subject='subject' WHERE id=10");db.commit();db.execute('BEGIN IMMEDIATE')
 for sql in ["INSERT OR REPLACE INTO users(id,username,password_hash) VALUES(10,'different','synthetic')","INSERT OR REPLACE INTO users(username,password_hash,oidc_issuer,oidc_subject) VALUES('different','synthetic','issuer','subject')"]:
  with pytest.raises(sqlite3.IntegrityError):db.execute(sql)
 db.rollback();assert db.execute('SELECT username FROM users WHERE id=10').fetchone()==('legacy',)

def test_scope_exception_clears_private_marker_and_references(db):
 db.execute('BEGIN IMMEDIATE')
 with pytest.raises(RuntimeError,match='owned failure'):
  with _account_creation_scope(db) as scope:raise RuntimeError('owned failure')
 assert not scope.alive and scope.executor is None and scope.connection is None and scope.marker is None
 assert db.in_transaction
 db.rollback()

def test_relay_removal_without_user_delete_actual_commit_rolls_back(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);db.commit();db.execute('BEGIN IMMEDIATE')
 db.execute("UPDATE account_lifetimes SET state='retired',current_user_id=NULL,retired_at='2026-10-13T12:01:00.000Z' WHERE account_nonce=?",(a.account_nonce,));db.execute('DELETE FROM account_bindings WHERE account_nonce=?',(a.account_nonce,))
 with pytest.raises(sqlite3.IntegrityError):db.commit()
 db.rollback();db.execute('BEGIN');validate(db);db.rollback()
 assert db.execute('SELECT state FROM account_lifetimes WHERE account_nonce=?',(a.account_nonce,)).fetchone()==('active',)

def test_retired_nonce_rebind_and_direct_terminal_insert_refuse(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);retire(db,a);db.commit();db.execute('BEGIN IMMEDIATE')
 with pytest.raises(sqlite3.IntegrityError):db.execute('INSERT INTO account_bindings VALUES(?,20)',(a.account_nonce,))
 with pytest.raises(sqlite3.IntegrityError):db.execute("INSERT INTO account_lifetimes VALUES(?,20,NULL,'retired','2026-10-13T12:00:00.000Z','2026-10-13T12:01:00.000Z')",('a'*32,))
 db.execute("INSERT INTO users(id,username,password_hash,account_nonce) VALUES(20,'new','synthetic',?)",(a.account_nonce,))
 with pytest.raises(sqlite3.IntegrityError):db.commit()
 db.rollback()

@pytest.mark.parametrize('step',['users','account_lifetimes','account_bindings'])
def test_factory_each_step_failure_no_birth_or_outer_commit(db,step):
 db.execute('BEGIN IMMEDIATE')
 db.execute("INSERT INTO users(username,password_hash) VALUES('caller','synthetic')")
 db.execute("CREATE TRIGGER owned_fault BEFORE INSERT ON "+step+" BEGIN SELECT RAISE(ABORT,'owned step'); END")
 with _account_creation_scope(db) as scope:
  with pytest.raises(sqlite3.IntegrityError,match='owned step'):_create_account(db,scope,{'username':'attempt','password_hash':'synthetic'},born_at=NOW)
  assert db.execute("SELECT count(*) FROM users WHERE username='attempt'").fetchone()==(0,)
  assert db.execute('SELECT count(*) FROM account_lifetimes').fetchone()==(0,)
 assert db.in_transaction;db.rollback()
 assert db.execute("SELECT count(*) FROM users WHERE username='caller'").fetchone()==(0,)

def test_crossed_cycle_actual_commit_refused(db):
 db.execute('BEGIN IMMEDIATE');n='a'*32
 db.execute("INSERT INTO users(id,username,password_hash,account_nonce) VALUES(20,'crossed','synthetic',?)",(n,))
 db.execute("INSERT INTO account_lifetimes VALUES(?,21,21,'active','2026-10-13T12:00:00.000Z',NULL)",(n,));db.execute('INSERT INTO account_bindings VALUES(?,21)',(n,))
 with pytest.raises(sqlite3.IntegrityError):db.commit()
 db.rollback()

def test_receipt_actor_and_retirement_exactness(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);actor=birth(db,21)
 args=('a'*32,a.account_nonce,actor.account_nonce,'b'*64)
 with pytest.raises(sqlite3.IntegrityError):db.execute("INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.000Z','retired')",args)
 retire(db,a)
 with pytest.raises(sqlite3.IntegrityError):db.execute("INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.001Z','retired')",args)
 db.execute("INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.000Z','retired')",args)
 db.commit();db.execute('BEGIN');validate(db);db.rollback()

def test_install_partial_and_used_footprint_refusal(db):
 db.execute('BEGIN IMMEDIATE')
 with pytest.raises(s.AccountLifetimeError,match='Fresh account foundation footprint'):s.install(db)
 db.rollback()

def test_private_scope_constructor_refuses_fabrication(db):
 from core.account_lifetime_creation import _Scope
 db.execute('BEGIN IMMEDIATE')
 with pytest.raises(s.AccountLifetimeError,match='Private account creation scope required'):_Scope(db,object())
 db.rollback()

def test_exact_retired_self_receipt_and_foreign_retired_actor_refusal(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);other=birth(db,21);retire(db,a);retire(db,other)
 with pytest.raises(sqlite3.IntegrityError):db.execute("INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.000Z','retired')",('a'*32,a.account_nonce,other.account_nonce,'c'*64))
 db.execute("INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:01:00.000Z','retired')",('a'*32,a.account_nonce,a.account_nonce,'c'*64));db.commit();db.execute('BEGIN');validate(db);db.rollback()

def test_actor_retirement_cannot_backdate_prior_attributed_receipt(db):
 db.execute('BEGIN IMMEDIATE');a=birth(db);actor=birth(db,21)
 db.execute("UPDATE account_lifetimes SET state='retired',current_user_id=NULL,retired_at='2026-10-13T12:02:00.000Z' WHERE account_nonce=?",(a.account_nonce,));db.execute('DELETE FROM account_bindings WHERE account_nonce=?',(a.account_nonce,));db.execute('DELETE FROM users WHERE id=?',(a.id,))
 db.execute("INSERT INTO account_retirement_receipts VALUES(?,?,?,?,'2026-10-13T12:02:00.000Z','retired')",('a'*32,a.account_nonce,actor.account_nonce,'c'*64))
 with pytest.raises(sqlite3.IntegrityError):retire(db,actor)
 db.rollback()

def test_unknown_info_and_connection_duck_types_refuse_without_sql(db):
 from types import SimpleNamespace
 class Unknown:
  info=SimpleNamespace(transaction_status=2)
  _connection=db
  def execute(self,*a):raise AssertionError('unknown executor used SQL')
 with pytest.raises(s.AccountLifetimeError,match='Unsupported account executor'):s.connection(Unknown())

@pytest.mark.parametrize('kind',['session','connection'])
def test_nested_and_invalidated_transactions_refused(db,tmp_path,kind):
 from sqlalchemy import create_engine,event
 from sqlalchemy.orm import Session
 engine=create_engine('sqlite:///'+str(tmp_path/'owned.db'))
 @event.listens_for(engine,'connect')
 def fk(c,_):c.execute('PRAGMA foreign_keys=ON')
 with (Session(engine) if kind=='session' else engine.connect()) as executor:
  executor.begin();c=executor.connection() if kind=='session' else executor;c.exec_driver_sql('BEGIN IMMEDIATE')
  nested=executor.begin_nested()
  with pytest.raises(s.AccountLifetimeError,match='Physical root caller transaction required'):s.connection(executor)
  nested.rollback();c.invalidate()
  with pytest.raises(s.AccountLifetimeError,match='Physical root caller transaction required'):s.connection(executor)
  executor.rollback()
 engine.dispose()

def test_wrong_sqlite_isolation_and_closed_driver_refusal(db):
 db.execute('PRAGMA read_uncommitted=ON');db.execute('BEGIN IMMEDIATE')
 with pytest.raises(s.AccountLifetimeError,match='isolation'):s.connection(db)
 db.rollback();db.execute('PRAGMA read_uncommitted=OFF')
 c=sqlite3.connect(':memory:');c.close()
 with pytest.raises(s.AccountLifetimeError,match='Closed account driver'):s.connection(c)

def test_actual_adapter_driver_replacement_expires_scope(tmp_path):
 from core.db_utils import _ConnectionAdapter
 from sqlalchemy import create_engine,event
 engine=create_engine('sqlite:///'+str(tmp_path/'adapter.db'))
 @event.listens_for(engine,'connect')
 def fk(c,_):c.execute('PRAGMA foreign_keys=ON')
 # Use source-owned adapter type with actual SQLAlchemy physical driver, no duck facade.
 with engine.connect() as c:
  c.exec_driver_sql('BEGIN IMMEDIATE')
  c.exec_driver_sql('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,password_hash TEXT,oidc_issuer TEXT,oidc_subject TEXT)')
  s.install(c);c.commit()
 raw=engine.raw_connection();driver=raw.driver_connection;driver.execute('BEGIN IMMEDIATE');adapter=_ConnectionAdapter(raw,False)
 replacement=engine.raw_connection();replacement.driver_connection.execute('BEGIN')
 with _account_creation_scope(adapter) as scope:
  adapter._connection=replacement
  with pytest.raises(s.AccountLifetimeError,match='scope expired'):_create_account(adapter,scope,{'username':'wrong','password_hash':'synthetic'},born_at=NOW)
 driver.rollback();replacement.rollback();raw.close();replacement.close();engine.dispose()

def test_pg_sources_qualify_owned_relations_and_preserve_literals():
 assert s.public_sql("SELECT * FROM users WHERE username='users account_bindings'")=="SELECT * FROM public.users WHERE username='users account_bindings'"
 assert s.public_sql('SELECT * FROM public.users')=='SELECT * FROM public.users'
 for body,_ in s.pg_functions().values():
  for table in s.BOUNDARY:
   import re
   assert not re.search(r'(?<![.A-Za-z_])'+table+r'\b',body)

def test_compiled_pg_catalog_is_closed_source_qualified_footprint():
 from core.account_lifetime_validation import PG_CATALOG
 assert set(PG_CATALOG['columns'])==set((*s.TABLES,'users'))
 assert sum(len(v) for v in PG_CATALOG['triggers'].values())==12
 assert {r['tgname'] for v in PG_CATALOG['triggers'].values() for r in v}==set(s.pg_triggers())
 assert all(r['function_schema']=='public' and r['tgenabled']=='O' for v in PG_CATALOG['triggers'].values() for r in v)
 assert all(r['target_schema']=='public' for v in PG_CATALOG['oid_references'].values() for r in v)
 assert sum(r['contype']=='c' for table in s.TABLES for r in PG_CATALOG['constraints'][table])==23

def test_pg_catalog_index_state_and_source_function_defaults():
 from core.account_lifetime_validation import PG_CATALOG
 assert all(x['indisvalid'] and x['indisready'] and x['indimmediate'] for v in PG_CATALOG['indexes'].values() for x in v)
 source=Path(__file__).resolve().parents[2]/'backend/core/account_lifetime_validation.py'
 code=source.read_text()
 assert 'p.prosupport::oid' in code
 assert 'cost!=100' in code and 'result_rows!=0' in code and 'support!=0' in code
 assert 'default_count!=0' in code and 'defaults is not None' in code
