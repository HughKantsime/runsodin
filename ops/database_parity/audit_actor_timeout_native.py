"""Affected finite-lock proof on actual ordinary ODIN engine and owned PostgreSQL."""
import importlib
import os
from pathlib import Path
import tempfile
import secrets
import time
from sqlalchemy import create_engine,event,text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from core.models import AuditLog
from core.audit_actor import RequestAuditScope,bind_verified_actor,stage_actor,stamp_actor,_bound_postgres_actor_locks
from core.database_config import create_database_engine
migration=importlib.import_module('core.schema.migrations.024_audit_actor')
url=os.environ['AUDIT_ACTOR_TIMEOUT_NATIVE_URL']
with tempfile.TemporaryDirectory(prefix='actor-timeout-owned-') as folder:
    Path(folder).chmod(0o700);os.environ['ODIN_DATA_DIR']=folder
    credential=Path(folder)/'owned-credential'
    credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
    engine=create_database_engine(url,role='api',password_file=str(credential))
    control=create_engine(url)
    with engine.begin() as c:
        AuditLog.__table__.create(c)
        c.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,is_active BOOLEAN)'))
        c.execute(text("INSERT INTO users VALUES(1,'actor',true)"))
        c.execute(text('CREATE TABLE actor_timeout_business(id INTEGER PRIMARY KEY,value INTEGER NOT NULL)'))
        c.execute(text('INSERT INTO actor_timeout_business VALUES(1,0)'))
        migration.apply(c)
    event.listen(AuditLog,'after_insert',stamp_actor)
    statements=[]
    @event.listens_for(engine,'before_cursor_execute')
    def track(connection,cursor,statement,parameters,context,many):statements.append(statement)
    def stage(db,uid):
        scope=RequestAuditScope('127.0.0.1');scope.register(db)
        bind_verified_actor(db,{'id':uid,'username':'actor','_auth_kind':'bearer_jwt'})
        item=AuditLog(action='timeout');stage_actor(db,item);db.add(item)
    def unchanged():
        with engine.connect() as c:
            assert c.execute(text('SELECT value FROM actor_timeout_business WHERE id=1')).scalar_one()==0
            assert c.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()==0
            assert c.execute(text('SELECT count(*) FROM audit_actor_erasures')).scalar_one()==0
            assert c.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one()=='0'
    for path,shorter in [('user_row',False),('missing_user_ledger',False),('user_row',True)]:
        with control.begin() as blocker:
            if path=='user_row':blocker.execute(text('SELECT id FROM users WHERE id=1 FOR UPDATE')).all()
            else:blocker.execute(text('LOCK TABLE audit_actor_erasures IN ROW EXCLUSIVE MODE'))
            with Session(engine) as db:
                connection=db.connection()
                # The actual ordinary engine resets its physical-session timeout to0.
                assert connection.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one()=='0'
                if shorter:
                    connection.execute(text("SET LOCAL lock_timeout='75ms'"))
                    assert _bound_postgres_actor_locks(connection)==75
                    assert connection.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one()=='75'
                statements.clear()
                db.execute(text('UPDATE actor_timeout_business SET value=1 WHERE id=1'))
                stage(db,1 if path=='user_row' else 9)
                started=time.monotonic()
                try:db.commit()
                except DBAPIError as error:
                    assert error.orig.sqlstate=='55P03'
                    elapsed=time.monotonic()-started
                    assert elapsed<5
                    if shorter:assert elapsed<1.5
                    else:assert elapsed>=1.8
                    db.rollback()
                else:raise AssertionError('Contended actor lock did not refuse')
                if shorter:assert not any('set_config' in statement for statement in statements)
        unchanged()
        print('audit-actor-timeout-native-'+path+('-stricter75ms' if shorter else '-bound2000ms')+'-55P03-full-rollback-pass')
    # Positive stricter setting survives successful mapper stamp, then local bound resets at commit.
    with Session(engine) as db:
        c=db.connection();c.execute(text("SET LOCAL lock_timeout='75ms'"));stage(db,1);db.flush()
        assert c.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one()=='75'
        db.commit()
    with engine.connect() as c:
        assert c.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one()=='0'
        assert c.execute(text('SELECT actor_user_id FROM audit_logs')).scalar_one()==1
    with control.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
        try:_bound_postgres_actor_locks(c)
        except RuntimeError as error:assert str(error)=='Audit actor locks require a caller transaction'
        else:raise AssertionError('Autocommit lock setter admitted')
    event.remove(AuditLog,'after_insert',stamp_actor)
    engine.dispose();control.dispose()
print('audit-actor-timeout-native-pass')
