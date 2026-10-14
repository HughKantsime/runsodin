"""Owned actual PostgreSQL024 and transaction races; no application bootstrap claim."""
import os
import threading
import time
from sqlalchemy import create_engine,text,event
from sqlalchemy.orm import Session
from core.models import AuditLog
import importlib
_migration=importlib.import_module('core.schema.migrations.024_audit_actor')
apply,validate=_migration.apply,_migration.validate
from core.audit_actor import RequestAuditScope,bind_verified_actor,stage_actor,stamp_actor,erase_actor_history

engine=create_engine(os.environ['AUDIT_ACTOR_NATIVE_URL'])
with engine.begin() as c:
    AuditLog.__table__.create(c)
    c.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,is_active BOOLEAN)'))
    c.execute(text("INSERT INTO users VALUES(1,'alice',true),(2,'admin',true)"))
    apply(c);validate(c);apply(c)
from core.schema.migrator import MigrationError
for mutation in (
    'ALTER TABLE audit_logs DROP CONSTRAINT audit_actor_bundle',
    'ALTER TABLE audit_actor_erasures DISABLE TRIGGER audit_actor_erasures_no_change',
    'ALTER FUNCTION audit_actor_erasure_immutable() SECURITY DEFINER',
    'ALTER FUNCTION audit_actor_erasure_immutable() COST 200',
):
    with engine.begin() as c:
        savepoint=c.begin_nested();c.execute(text(mutation))
        try:validate(c)
        except MigrationError:savepoint.rollback()
        else:raise AssertionError('Native drift adopted')
for invalid in (
    'INSERT INTO audit_actor_erasures(user_id) VALUES(0)',
    "INSERT INTO audit_actor_erasures(user_id,erased_at) VALUES(9,'infinity')",
    "INSERT INTO audit_logs(action,actor_user_id) VALUES('partial',1)",
    "INSERT INTO audit_logs(action,actor_auth_kind) VALUES('bad','untrusted')",
):
    with engine.connect() as c:
        try:c.execute(text(invalid))
        except Exception as error:assert error.orig.sqlstate=='23514';c.rollback()
        else:raise AssertionError('Native actor constraint bypass')
event.listen(AuditLog,'after_insert',stamp_actor)
def staged(db,uid=1):
    scope=RequestAuditScope('127.0.0.1');scope.register(db)
    bind_verified_actor(db,{'id':uid,'username':'alice','_auth_kind':'bearer_jwt'})
    item=AuditLog(action='native');stage_actor(db,item);db.add(item)
    return item

def tuple_for(id):
    with engine.connect() as c:return tuple(c.execute(text('SELECT actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs WHERE id=:id'),{'id':id}).one())

with Session(engine) as db:
    item=staged(db);db.commit();first=item.id
assert tuple_for(first)==(1,'alice','bearer_jwt','127.0.0.1')
# Stamp-first: exact current-user FOR UPDATE remains until caller commit.
with Session(engine) as db:
    item=staged(db);db.flush();second=item.id
    with engine.connect() as other:
        other.execute(text("SET LOCAL lock_timeout='100ms'"))
        try:other.execute(text("UPDATE users SET is_active=false WHERE id=1"))
        except Exception as error:assert error.orig.sqlstate=='55P03';other.rollback()
        else:raise AssertionError('Actual erasure UPDATE bypassed stamp user lock')
    db.commit()
# Erasure-first blocks stamp, then committed marker suppresses peer already INSERTed.
started=threading.Event();done=threading.Event();result={}
def stamp_waiting():
    try:
        with Session(engine) as db:
            item=staged(db);started.set();db.commit();result['id']=item.id
    except Exception as error:result['error']=type(error).__name__
    finally:done.set()
with Session(engine) as eraser:
    eraser.execute(text("UPDATE users SET username='[deleted-1]',is_active=false WHERE id=1"))
    erase_actor_history(eraser,1)
    thread=threading.Thread(target=stamp_waiting);thread.start();assert started.wait(3)
    assert not done.wait(.15)
    eraser.commit()
assert done.wait(5);thread.join();assert 'error' not in result
assert tuple_for(first)==(None,None,None,None) and tuple_for(second)==(None,None,None,None)
assert tuple_for(result['id'])==(None,None,None,None)
# Explicit self-erasure final staged audit, marker survives physical delete/ID reuse.
with Session(engine) as db:
    db.execute(text("UPDATE users SET is_active=false WHERE id=1"));erase_actor_history(db,1)
    item=staged(db);db.commit();assert tuple_for(item.id)==(None,None,None,None)
with engine.begin() as c:
    c.execute(text('DELETE FROM users WHERE id=1'));c.execute(text("INSERT INTO users VALUES(1,'reused',true)"))
with Session(engine) as db:
    item=staged(db);db.commit();assert tuple_for(item.id)==(None,None,None,None)
# Missing ordinary-deleted row: ledger SHARE closes absent-marker insertion race.
with engine.begin() as c:c.execute(text('DELETE FROM users WHERE id=2'))
with Session(engine) as db:
    item=staged(db,2);db.flush();missing=item.id
    with engine.connect() as other:
        other.execute(text("SET LOCAL lock_timeout='100ms'"))
        try:other.execute(text('INSERT INTO audit_actor_erasures(user_id) VALUES(2)'))
        except Exception as error:assert error.orig.sqlstate=='55P03';other.rollback()
        else:raise AssertionError('Missing-user stamp failed to fence marker absence')
    db.commit()
assert tuple_for(missing)==(2,'alice','bearer_jwt','127.0.0.1')
with engine.begin() as c:c.execute(text('INSERT INTO audit_actor_erasures(user_id) VALUES(2)'));c.execute(text('UPDATE audit_logs SET actor_user_id=NULL,actor_username=NULL,actor_auth_kind=NULL,ip_address=NULL WHERE actor_user_id=2'))
with Session(engine) as db:
    item=staged(db,2);db.commit();assert tuple_for(item.id)==(None,None,None,None)
# Unsupported snapshot isolation is a hard transaction refusal, business mutation rolls back.
with engine.connect().execution_options(isolation_level='REPEATABLE READ') as c:
    with Session(bind=c) as db:
        db.execute(text("UPDATE users SET username='must-rollback' WHERE id=1"));staged(db)
        try:db.commit()
        except RuntimeError as error:assert str(error)=='Audit actor requires READ COMMITTED';db.rollback()
        else:raise AssertionError('Unsafe marker snapshot accepted')
for mutation in ('UPDATE audit_actor_erasures SET user_id=8','DELETE FROM audit_actor_erasures','TRUNCATE audit_actor_erasures'):
    with engine.connect() as c:
        try:c.execute(text(mutation))
        except Exception as error:assert error.orig.sqlstate=='P0001';c.rollback()
        else:raise AssertionError('Erasure ledger mutability accepted')
# Missing-row marker-first: in-progress INSERT blocks SHARE, then fresh marker suppresses.
result={};started.clear();done.clear()
def missing_waiting():
    try:
        with Session(engine) as db:
            item=staged(db,4);started.set();db.commit();result['id']=item.id
    except Exception as error:result['error']=type(error).__name__
    finally:done.set()
with engine.begin() as eraser:
    eraser.execute(text('INSERT INTO audit_actor_erasures(user_id) VALUES(4)'))
    thread=threading.Thread(target=missing_waiting);thread.start();assert started.wait(3)
    assert not done.wait(.15)
assert done.wait(5);thread.join();assert 'error' not in result
assert tuple_for(result['id'])==(None,None,None,None)
# Real opposing audit/user locks produce a deadlock; exactly one whole mutation rolls back.
with engine.begin() as c:c.execute(text("INSERT INTO users VALUES(3,'third',true)"))
with Session(engine) as db:item=staged(db,3);db.commit();seed=item.id
result={};started.clear();done.clear()
def opposing_stamp():
    try:
        with Session(engine) as db:
            db.execute(text("UPDATE audit_logs SET action='opposing-business' WHERE id=:id"),{'id':seed})
            item=staged(db,3);started.set();db.commit();result['stamp']='committed'
    except Exception as error:result['stamp']=getattr(getattr(error,'orig',None),'sqlstate',type(error).__name__)
    finally:done.set()
with Session(engine) as eraser:
    eraser.execute(text("UPDATE users SET username='[deleted-3]',is_active=false WHERE id=3"))
    thread=threading.Thread(target=opposing_stamp);thread.start();assert started.wait(3);assert not done.wait(.15)
    try:erase_actor_history(eraser,3);eraser.commit();result['erase']='committed'
    except Exception as error:result['erase']=getattr(getattr(error,'orig',None),'sqlstate',type(error).__name__);eraser.rollback()
assert done.wait(5);thread.join()
assert sorted([result['stamp'],result['erase']])==['40P01','committed']
with engine.connect() as c:
    marker=c.execute(text('SELECT 1 FROM audit_actor_erasures WHERE user_id=3')).first()
    action=c.execute(text('SELECT action FROM audit_logs WHERE id=:id'),{'id':seed}).scalar_one()
    if result['erase']=='40P01':
        assert marker is None and action=='opposing-business'
        assert c.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()=='third'
    else:assert marker is not None and action=='native' and tuple_for(seed)==(None,None,None,None)
with engine.begin() as c:validate(c)
print('audit-actor-native-pass')
engine.dispose()
# Explicit024 on actual recorded full bootstrap, frozen ORM unchanged; no registry activation.
import subprocess
from sqlalchemy.engine import make_url
from core.schema import bootstrap_database
fullurl=make_url(os.environ['AUDIT_ACTOR_NATIVE_URL']).set(database='audit_actor_bootstrap')
admin=create_engine(os.environ['AUDIT_ACTOR_NATIVE_URL'],isolation_level='AUTOCOMMIT')
with admin.connect() as c:
    c.execute(text('CREATE DATABASE audit_actor_bootstrap'))
    c.execute(text('CREATE DATABASE audit_actor_restored'))
full=create_engine(fullurl)
bootstrap_database(full)
with full.begin() as c:apply(c);validate(c)
from modules.system.postgres_canonical_executables import canonical_functions,canonical_triggers
from core.audit_actor_schema import PG_BODY,FUNCTION
with full.connect() as c:
    actual=set(c.execute(text("SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public'")).scalars())
    from core.timelapse_retirement_membership_validation import pg_functions,pg_triggers
    expected=set(canonical_functions())|set(pg_functions())|{FUNCTION}
    if actual!=expected:raise AssertionError('Canonical function difference extra='+repr(sorted(actual-expected))+' missing='+repr(sorted(expected-actual)))
    actualtriggers=set(c.execute(text("SELECT r.relname,t.tgname FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal")).all())
    membership_triggers={(name.removesuffix('_membership_guard'),name) for name in pg_triggers()}
    assert actualtriggers==set(canonical_triggers())|membership_triggers|{('audit_actor_erasures','audit_actor_erasures_no_change'),('audit_actor_erasures','audit_actor_erasures_no_truncate')}
    print('audit-actor-canonical-extra-function='+repr(tuple(c.execute(text("SELECT p.proname,p.prosrc,p.proconfig,p.prosecdef,p.provolatile,p.proparallel,pg_get_function_result(p.oid),pg_get_function_identity_arguments(p.oid),l.lanname,p.proisstrict,p.proleakproof,p.prokind,p.proretset,p.pronargdefaults,p.procost,p.prorows,p.prosupport::text FROM pg_proc p JOIN pg_language l ON l.oid=p.prolang WHERE p.proname=:n"),{'n':FUNCTION}).one())))
    print('audit-actor-canonical-extra-triggers='+repr([tuple(r) for r in c.execute(text("SELECT r.relname,t.tgname,pg_get_triggerdef(t.oid),t.tgenabled FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid WHERE r.relname='audit_actor_erasures'")).all()]))
archive='/tmp/audit_actor_owned.dump'
subprocess.run(['pg_dump','-h','pg','-U','postgres','-Fc','-f',archive,'audit_actor_bootstrap'],check=True,capture_output=True)
subprocess.run(['pg_restore','-h','pg','-U','postgres','-d','audit_actor_restored','--exit-on-error',archive],check=True,capture_output=True)
restored=create_engine(fullurl.set(database='audit_actor_restored'))
with restored.begin() as c:
    from core.audit_actor_schema import _pg_checks
    with full.connect() as original:
        for table in ('audit_logs','audit_actor_erasures'):
            a,b=_pg_checks(c,table),_pg_checks(original,table)
            if a!=b:raise AssertionError('Restored check deparser difference '+table+' restored='+repr(a)+' original='+repr(b))
    validate(c)
full.dispose();restored.dispose();admin.dispose()
print('audit-actor-native-full-bootstrap-explicit024-dump-restore-pass')
