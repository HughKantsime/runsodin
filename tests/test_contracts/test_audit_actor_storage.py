"""Explicit024 fixtures; never activates shared bootstrap or real erasure."""
import pytest
from sqlalchemy import create_engine,event,text
from sqlalchemy.orm import Session
from core.models import AuditLog
from core.schema.bootstrap import import_all_models
import importlib
# Instantiating AuditLog configures every mapper; a batch that starts here must not leave Job's Model relationship unresolved.
import_all_models()
_migration=importlib.import_module('core.schema.migrations.024_audit_actor')
apply,validate=_migration.apply,_migration.validate
from core.schema.migrator import MigrationError
from core.audit_actor import (RequestAuditScope,bind_verified_actor,stage_actor,stamp_actor,erase_actor_history)

@pytest.fixture
def engine(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'actor.db'))
    with e.begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE')
        AuditLog.__table__.create(c)
        c.execute(text('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,is_active BOOLEAN)'))
        c.execute(text("INSERT INTO users VALUES (1,'alice',1),(2,'admin',1)"))
        apply(c)
    yield e
    e.dispose()

def entry(db,uid=1,name='alice',kind='bearer_jwt'):
    scope=RequestAuditScope('127.0.0.1');scope.register(db)
    bind_verified_actor(db,{'id':uid,'username':name,'_auth_kind':kind})
    item=AuditLog(action='actor-test')
    stage_actor(db,item,'198.51.100.99');db.add(item)
    return item,scope

@pytest.fixture(autouse=True)
def mapper_listener():
    installed=not event.contains(AuditLog,'after_insert',stamp_actor)
    if installed:event.listen(AuditLog,'after_insert',stamp_actor)
    yield
    if installed:event.remove(AuditLog,'after_insert',stamp_actor)

def rows(e):
    with e.connect() as c:return c.execute(text('SELECT actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs ORDER BY id')).all()

def test_fresh_repeat_and_original_unknown(engine):
    with engine.begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');apply(c);validate(c)
    with Session(engine) as db:db.add(AuditLog(action='old'));db.commit()
    assert rows(engine)==[(None,None,None,None)]

@pytest.mark.parametrize('sql',[
    "INSERT INTO audit_actor_erasures(user_id) VALUES(0)",
    "INSERT INTO audit_actor_erasures(user_id,erased_at) VALUES(5,'infinity')",
    "INSERT INTO audit_logs(action,actor_user_id) VALUES('bad',1)",
    "INSERT INTO audit_logs(action,actor_auth_kind) VALUES('bad','bearer_jwt')",
    "INSERT INTO audit_logs(action,actor_user_id,actor_username,actor_auth_kind) VALUES('bad',1,'x','legacy_global_api_key')",
])
def test_constraints(engine,sql):
    with pytest.raises(Exception):
        with engine.begin() as c:c.execute(text(sql))

@pytest.mark.parametrize('mutation', ['UPDATE audit_actor_erasures SET user_id=2','DELETE FROM audit_actor_erasures'])
def test_marker_immutable(engine,mutation):
    with engine.begin() as c:c.execute(text('INSERT INTO audit_actor_erasures(user_id) VALUES(1)'))
    with pytest.raises(Exception):
        with engine.begin() as c:c.execute(text(mutation))

@pytest.mark.parametrize('drift',[
    'DROP TRIGGER audit_actor_erasures_no_delete',
    'ALTER TABLE audit_actor_erasures ADD COLUMN private_email TEXT',
])
def test_adoption_drift_refuses(engine,drift):
    with engine.begin() as c:c.execute(text(drift))
    with pytest.raises(MigrationError):
        with engine.begin() as c:apply(c)

def test_stage_capture_and_no_forced_flush(engine):
    with Session(engine) as db:
        item,scope=entry(db)
        db.add(AuditLog()) # invalid unrelated pending; stage must not flush
        assert item.id is None
        bad=next(x for x in db.new if x is not item);db.expunge(bad)
        bind_verified_actor(db,{'id':2,'username':'admin','_auth_kind':'session_jwt'})
        scope.clear();db.commit()
    assert rows(engine)==[(1,'alice','bearer_jwt','127.0.0.1')]

def test_service_and_new_sessions_unknown(engine):
    with Session(engine) as db:item,scope=entry(db,kind='legacy_global_api_key');db.commit()
    with Session(engine) as db:
        item=AuditLog(action='background');stage_actor(db,item);db.add(item);db.commit()
    assert rows(engine)==[(None,None,'legacy_global_api_key','127.0.0.1'),(None,None,None,None)]

def test_cross_session_unknown(engine):
    with Session(engine) as a,Session(engine) as b:
        item,_=entry(a);a.expunge(item);b.add(item);b.commit()
    assert rows(engine)==[(None,None,None,None)]

def test_erasure_after_commit_and_later_self_snapshot(engine):
    with Session(engine) as db:item,_=entry(db);db.commit()
    with Session(engine) as db:
        db.execute(text("UPDATE users SET username='[deleted-1]',is_active=0 WHERE id=1"))
        erase_actor_history(db,1)
        item,_=entry(db);db.commit()
    assert rows(engine)==[(None,None,None,None)]*2
    with engine.begin() as c:
        c.execute(text('DELETE FROM users WHERE id=1'))
        c.execute(text("INSERT INTO users VALUES(1,'new-life',1)"))
    with Session(engine) as db:item,_=entry(db,name='new-life');db.commit()
    assert rows(engine)[-1]==(None,None,None,None)

def test_missing_ordinary_deleted_keeps_snapshot(engine):
    with Session(engine) as db:
        item,_=entry(db);db.execute(text('DELETE FROM users WHERE id=1'));db.commit()
    assert rows(engine)==[(1,'alice','bearer_jwt','127.0.0.1')]

def test_marker_error_rolls_back_business_and_audit(engine):
    with engine.begin() as c:c.execute(text('DROP TABLE audit_actor_erasures'))
    with Session(engine) as db:
        item,_=entry(db);db.execute(text("UPDATE users SET username='wrong' WHERE id=1"))
        with pytest.raises(Exception):db.commit()
        db.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT username FROM users WHERE id=1')).scalar_one()=='alice'
        assert c.execute(text('SELECT count(*) FROM audit_logs')).scalar_one()==0

def test_sqlite_writer_upgrade_refuses(engine):
    # Real WAL reader cannot upgrade after another writer commits a tombstone.
    with engine.connect() as c:c.exec_driver_sql('PRAGMA journal_mode=WAL')
    with engine.connect() as a:
        a.exec_driver_sql('BEGIN');a.execute(text('SELECT * FROM users')).all()
        with engine.begin() as b:
            b.execute(text("UPDATE users SET is_active=0 WHERE id=1"))
            b.execute(text('INSERT INTO audit_actor_erasures(user_id) VALUES(1)'))
        with pytest.raises(Exception):a.execute(text("INSERT INTO audit_logs(action) VALUES('stale')"))
        a.rollback()
    assert rows(engine)==[]

def test_sqlite_stamp_writer_first_blocks_erasure_until_commit(engine):
    with Session(engine) as db:
        item,_=entry(db);db.flush()
        with engine.connect() as eraser:
            eraser.exec_driver_sql('PRAGMA busy_timeout=1')
            with pytest.raises(Exception):eraser.execute(text('UPDATE users SET is_active=0 WHERE id=1'))
            eraser.rollback()
        db.commit()
    with Session(engine) as eraser:
        eraser.execute(text('UPDATE users SET is_active=0 WHERE id=1'));erase_actor_history(eraser,1);eraser.commit()
    assert rows(engine)==[(None,None,None,None)]

def test_session_info_copy_cannot_inherit_actor(engine):
    with Session(engine) as original:
        item,_=entry(original);original.expunge(item)
        with Session(engine,info=original.info) as copied:
            item=AuditLog(action='copied');stage_actor(copied,item);copied.add(item);copied.commit()
    assert rows(engine)==[(None,None,None,None)]

def test_fresh_keeps_every_original_legacy_value(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    with e.begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');AuditLog.__table__.create(c)
        c.execute(text("INSERT INTO audit_logs(id,timestamp,action,entity_type,entity_id,details,ip_address) VALUES(99,'2026-10-01 01:02:03','legacy','user',7,'{\"owned\":\"exact\"}','192.0.2.1')"))
        before=c.execute(text('SELECT * FROM audit_logs')).one()
        apply(c)
        assert c.execute(text('SELECT id,timestamp,action,entity_type,entity_id,details,ip_address FROM audit_logs')).one()==before
    e.dispose()

def test_partial024_refuses_without_adoption(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'partial.db'))
    with e.begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');AuditLog.__table__.create(c)
        c.execute(text('ALTER TABLE audit_logs ADD COLUMN actor_user_id INTEGER'))
    with pytest.raises(MigrationError):
        with e.begin() as c:c.exec_driver_sql('BEGIN IMMEDIATE');apply(c)
    from sqlalchemy import inspect
    assert not inspect(e).has_table('audit_actor_erasures')
    e.dispose()
