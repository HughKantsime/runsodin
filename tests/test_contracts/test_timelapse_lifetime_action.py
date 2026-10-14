"""Owned real-FS sealed lifetime service; routes remain unactivated."""
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.requests import Request
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source,prepare,encoder,frame_writer,review_db,owned_runtime,zero_source,planned_absence
from core.audit_actor import RequestAuditScope
from core.auth import create_access_token
from modules.archives.timelapse_action_authority import capture_action_credential
from modules.archives.timelapse_lifetime_action import retire_sealed_lifetime
from modules.archives.timelapse_retirement import RetirementUnavailable


def _sqlite_foreign_keys_on_checkout(connection,record,proxy):
    # Match core.db's per-physical-connection safety policy for this plain fixture Engine.
    cursor=connection.cursor()
    try:cursor.execute('PRAGMA foreign_keys=ON')
    finally:cursor.close()


def credential(engine):
    with Session(engine) as db:
        username=db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
        token=create_access_token({'sub':username,'role':'admin'})
        request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())], 'query_string':b'', 'odin.private.audit_actor_scope':RequestAuditScope(None)})
        return capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer')


def test_real_sealed_lifetime_retires_owned_members_atomically(prepared_source):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    assert retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    with Session(engine) as db:
        assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==0
        assert db.execute(text('SELECT state FROM timelapse_retirements')).scalar_one()=='complete'
        assert db.execute(text('SELECT count(*) FROM timelapse_retirement_container_receipts')).scalar_one()>0
        assert db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE state NOT IN ('retired','originally_absent')")).scalar_one()==0
    assert not list(root.glob('frames_*')) and not list(root.glob('encode_*.mp4'))


def test_zero_no_phantom_physical_receipt(zero_source):
    engine,root,tid,_,nonce=zero_source;prepare(zero_source)
    assert retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    assert not (root/'.retirement').exists()
    with Session(engine) as db:assert db.execute(text('SELECT count(*) FROM timelapse_retirement_container_receipts')).scalar_one()==0


def test_principal_cannot_self_attest(prepared_source):
    engine,root,tid,principal,nonce=prepared_source;prepare(prepared_source)
    with pytest.raises(RetirementUnavailable):retire_sealed_lifetime(engine,root,tid,principal,expected_nonce=nonce)
    assert list(root.glob('frames_*'))

@pytest.mark.parametrize('boundary',['rename','unlink','fsync','complete','ack'])
def test_crash_reconciles_same_durable_operation(prepared_source,monkeypatch,boundary):
    from sqlalchemy import event
    import os
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    evidence=credential(engine);fired=[]
    def fail(c,cur,sql,params,ctx,many):
        hit=(boundary=='rename' and sql.startswith('UPDATE timelapse_retirement_moves') and 'staged' in str(params)) or (boundary=='unlink' and sql.startswith('UPDATE timelapse_retirement_unlinks') and 'unlink_confirmed' in str(params)) or (boundary=='complete' and sql.startswith('DELETE FROM timelapses'))
        if hit and not fired:fired.append(True);raise RuntimeError('owned post-syscall database fault')
    actual_fsync=os.fsync
    def sync(fd):
        if boundary=='fsync' and not fired and not list(root.glob('encode_*.mp4')):
            fired.append(True);raise OSError('owned fsync fault')
        return actual_fsync(fd)
    actual_commit=Session.commit
    def commit(db):
        actual_commit(db)
        if boundary=='ack' and not fired:
            fired.append(True);raise RuntimeError('owned commit ACK loss')
    event.listen(engine,'after_cursor_execute',fail)
    monkeypatch.setattr(os,'fsync',sync);monkeypatch.setattr(Session,'commit',commit)
    with pytest.raises(Exception):retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    assert fired
    event.remove(engine,'after_cursor_execute',fail)
    monkeypatch.setattr(os,'fsync',actual_fsync);monkeypatch.setattr(Session,'commit',actual_commit)
    foreign_keys=_sqlite_foreign_keys_on_checkout
    event.listen(engine,'checkout',foreign_keys)
    try:assert retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce).state=='complete'
    finally:event.remove(engine,'checkout',foreign_keys)

@pytest.mark.parametrize('change',['blacklist','inactive','expiry','source','quarantine','revision'])
def test_drift_after_db_closed_verification_refuses_preserving_pending(prepared_source,monkeypatch,change):
    from modules.archives import timelapse_lifetime_action as service
    import os,time
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);evidence=credential(engine)
    actual=service.verify_action;fired=[]
    def verify(*args,**kwargs):
        token=actual(*args,**kwargs)
        if not fired:
            fired.append(token.row['object_index'])
            if change=='source':
                os.utime(token.name,ns=(token.value.mtime_ns+1,token.value.mtime_ns+1),dir_fd=token.parent,follow_symlinks=False)
            elif change=='quarantine':
                (root/'.retirement').rename(root/'preserved-quarantine');(root/'.retirement').mkdir(mode=0o700)
            elif change=='expiry':monkeypatch.setattr(time,'time',lambda:evidence.expiry+1)
            else:
                with engine.begin() as c:
                    if change=='blacklist':c.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expiry)'),{'jti':evidence.jti,'expiry':'2099-01-01'})
                    elif change=='inactive':c.execute(text('UPDATE users SET is_active=FALSE WHERE id=3'))
                    else:c.execute(text('UPDATE timelapse_retirements SET revision=revision+1'))
        return token
    monkeypatch.setattr(service,'verify_action',verify)
    with pytest.raises(Exception):retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    assert fired and list(root.glob('frames_*'))
    with Session(engine) as db:
        assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='deleting'
        assert db.execute(text('SELECT phase FROM timelapse_retirement_moves WHERE object_index=:index'),{'index':fired[0]}).scalar_one()=='rename_pending'


def test_occupied_quarantine_target_preserved(prepared_source):
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source)
    quarantine=root/'.retirement';quarantine.mkdir(mode=0o700)
    foreign=quarantine/(nonce+'_0');foreign.write_bytes(b'unknown occupied target')
    inode=foreign.stat().st_ino
    with pytest.raises(Exception):retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce)
    assert foreign.read_bytes()==b'unknown occupied target' and foreign.stat().st_ino==inode


def test_completed_replay_does_not_require_deleted_metadata(prepared_source):
    from modules.archives.timelapse_lifetime_action import sealed_lifetime_receipt
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);evidence=credential(engine)
    assert retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce).state=='complete'
    with engine.begin() as c:c.execute(text("DELETE FROM audit_logs WHERE action LIKE 'retirement_%'"))
    assert sealed_lifetime_receipt(engine,evidence,operation_id=nonce).operation_id==nonce


def test_originally_absent_namespace_rechecked_without_receipt(planned_absence):
    engine,root,tid,_,nonce=planned_absence[0]
    assert retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    with Session(engine) as db:
        assert db.execute(text('SELECT count(*) FROM timelapse_retirement_container_receipts')).scalar_one()==0
        assert db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE state='originally_absent'")).scalar_one()==1


def test_appeared_originally_absent_namespace_refuses(planned_absence):
    engine,root,tid,_,nonce=planned_absence[0]
    with Session(engine) as db:path=db.execute(text('SELECT relative_path FROM timelapse_retirement_objects')).scalar_one()
    namespace=root/path;namespace.mkdir(mode=0o700);(namespace/'unknown.jpg').write_bytes(b'original unknown evidence')
    with pytest.raises(Exception):retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce)
    assert (namespace/'unknown.jpg').read_bytes()==b'original unknown evidence'

@pytest.mark.parametrize('mutation',['rename','replace'])
def test_pinned_frame_parent_relinked_before_action_refuses_without_syscall(prepared_source,monkeypatch,mutation):
    from modules.archives import timelapse_lifetime_action as service
    from core import timelapse_action_fs as filesystem
    import os
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);evidence=credential(engine)
    actual=service.verify_action;fired=[];calls=[]
    original_rename=filesystem.rename_noreplace;original_unlink=os.unlink
    def track_rename(*args):calls.append('rename');return original_rename(*args)
    def track_unlink(*args,**kwargs):calls.append('unlink');return original_unlink(*args,**kwargs)
    def verify(*args,**kwargs):
        token=actual(*args,**kwargs)
        if not fired:
            fired.append((token.row['object_index'],token.snapshot['intent']['revision']))
            parent=root/token.row['relative_path'].rpartition('/')[0]
            saved=root/'preserved-pinned-parent';parent.rename(saved)
            if mutation=='replace':parent.mkdir(mode=0o700)
        return token
    monkeypatch.setattr(service,'verify_action',verify)
    monkeypatch.setattr(filesystem,'rename_noreplace',track_rename);monkeypatch.setattr(os,'unlink',track_unlink)
    with pytest.raises(Exception):retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    assert fired and not calls
    assert len(list((root/'preserved-pinned-parent').glob('*.jpg')))==3
    with Session(engine) as db:
        assert db.execute(text('SELECT revision FROM timelapse_retirements')).scalar_one()==fired[0][1]
        assert db.execute(text('SELECT phase FROM timelapse_retirement_moves WHERE object_index=:i'),{'i':fired[0][0]}).scalar_one()=='rename_pending'

@pytest.mark.parametrize('action',['rename','unlink'])
@pytest.mark.parametrize('mutation',['rename','replace'])
def test_intermediate_parent_link_is_checked_before_syscall(tmp_path,monkeypatch,action,mutation):
    from dataclasses import asdict
    import hashlib,os
    from core.timelapse_manifest import _root,_facts
    from core.timelapse_media_lock import acquire_media_guard
    from core.timelapse_staging_fs import StageFilesystem
    from core.timelapse_drain_fs import DrainFilesystem
    from core import timelapse_action_fs as module
    root=tmp_path/'owned';root.mkdir(mode=0o700);(root/'outer').mkdir(mode=0o700);(root/'outer/inner').mkdir(mode=0o700);(root/'.retirement').mkdir(mode=0o700)
    source=root/'outer/inner/source.mp4';source.write_bytes(b'owned source bytes')
    identity=_facts('outer/inner/source.mp4',source.stat())
    row=dict(asdict(identity),operation_id='owned',object_index=0,state='planned',quarantine_name='owned_0')
    move=dict(operation_id='owned',object_index=0,kind='file',phase='rename_pending',verified_ctime_ns=None,digest_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    if action=='unlink':
        source.rename(root/'.retirement/owned_0');row['state']='staged';move.update(phase='staged',verified_ctime_ns=(root/'.retirement/owned_0').stat().st_ctime_ns)
    calls=[];actual_rename=module.rename_noreplace;actual_unlink=os.unlink
    monkeypatch.setattr(module,'rename_noreplace',lambda *a:(calls.append('rename'),actual_rename(*a))[-1])
    monkeypatch.setattr(os,'unlink',lambda *a,**kw:(calls.append('unlink'),actual_unlink(*a,**kw))[-1])
    with acquire_media_guard(root,99) as guard,_root(root) as (root_fd,root_identity):
        qfd=os.open(root/'.retirement',os.O_RDONLY|os.O_DIRECTORY)
        try:
            quarantine=_facts('.retirement',os.fstat(qfd))
            parents={p:_facts(p,(root/p).stat()) for p in ('outer','outer/inner')}
            args=(root_fd,qfd,root_identity,quarantine,[row],parents,{0:move})
            fs=StageFilesystem(*args,guard) if action=='rename' else DrainFilesystem(*args,{0:dict(operation_id='owned',object_index=0,phase='unlink_pending')},guard)
            token=module.verify_action(fs,row,action=action,snapshot={'revision':1})
            (root/'outer').rename(root/'preserved-outer')
            if mutation=='replace':(root/'outer').mkdir(mode=0o700);(root/'outer/inner').mkdir(mode=0o700)
            try:
                with pytest.raises(Exception):module.apply_verified_action(token,snapshot={'revision':1})
            finally:
                try:token.close()
                except Exception:pass # context-exit also reports the same replaced link
            assert not calls
            kept=root/'preserved-outer/inner/source.mp4' if action=='rename' else root/'.retirement/owned_0'
            assert kept.read_bytes()==b'owned source bytes'
        finally:os.close(qfd)


@pytest.mark.parametrize('fixture_name',['zero_source','prepared_source'])
def test_final_audit_failure_rolls_back_completion_and_retry_counts_once(request,fixture_name,monkeypatch):
    import core.audit as audit
    source=request.getfixturevalue(fixture_name)
    engine,root,tid,_,nonce=source
    prepare(source)
    from sqlalchemy import select
    from core.timelapse_publication_schema import namespaces,retained
    from core.timelapse_retirement_membership_schema import receipts
    def completion_metadata(db):
        return tuple(tuple(db.execute(select(table).order_by(*table.primary_key.columns)).all())
                     for table in (namespaces,retained,receipts))
    with Session(engine) as db:
        before=completion_metadata(db)
    original=audit.log_audit
    def fail_final(db,action,*args,**kwargs):
        if action=='retirement_complete':
            db.flush()
            raise RuntimeError('owned final audit failure')
        return original(db,action,*args,**kwargs)
    monkeypatch.setattr(audit,'log_audit',fail_final)
    with pytest.raises(RuntimeError,match='owned final audit failure'):
        retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce)
    with Session(engine) as db:
        assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
        assert db.execute(text('SELECT state FROM timelapse_retirements')).scalar_one()!='complete'
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==0
        assert completion_metadata(db)==before
    monkeypatch.setattr(audit,'log_audit',original)
    assert retire_sealed_lifetime(engine,root,tid,credential(engine),expected_nonce=nonce).state=='complete'
    with Session(engine) as db:
        assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==0
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1


def _final_completion_metadata(engine):
    from sqlalchemy import select
    from core.timelapse_publication_schema import namespaces,retained
    from core.timelapse_retirement_membership_schema import receipts
    with Session(engine) as db:
        return tuple(tuple(db.execute(select(table).order_by(*table.primary_key.columns)).all())
                     for table in (namespaces,retained,receipts))


@pytest.mark.parametrize('fixture_name',['zero_source','prepared_source'])
@pytest.mark.parametrize('boundary',['audit_insert','actor_stamp','definite_commit','deferred_commit','lost_ack'])
def test_actual_final_audit_and_commit_faults_preserve_retry_identity(request,fixture_name,boundary,monkeypatch):
    """Actual INSERT/stamp SQL and SQLite deferred FK COMMIT, not helper stubs."""
    import os
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError
    from core.models import AuditLog
    from modules.archives import timelapse_lifetime_action as service
    from core import timelapse_action_fs as filesystem
    engine,root,tid,_,nonce=source=request.getfixturevalue(fixture_name)
    prepare(source);evidence=credential(engine);before=_final_completion_metadata(engine)
    destructive=[];audit_inserted=[];stamp_seen=[];fired=[]
    actual_rename=filesystem.rename_noreplace;actual_unlink=os.unlink;actual_rmdir=os.rmdir;actual_commit=Session.commit
    def rename(*args,**kwargs):destructive.append('rename');return actual_rename(*args,**kwargs)
    def unlink(*args,**kwargs):destructive.append('unlink');return actual_unlink(*args,**kwargs)
    def rmdir(*args,**kwargs):destructive.append('rmdir');return actual_rmdir(*args,**kwargs)
    monkeypatch.setattr(filesystem,'rename_noreplace',rename);monkeypatch.setattr(os,'unlink',unlink);monkeypatch.setattr(os,'rmdir',rmdir)
    def before_sql(c,cursor,sql,params,context,many):
        if sql.startswith('INSERT INTO audit_logs') and 'retirement_complete' in params:
            if boundary=='audit_insert' and not fired:fired.append(True);raise RuntimeError('owned final audit INSERT fault')
    def after_sql(c,cursor,sql,params,context,many):
        if sql.startswith('INSERT INTO audit_logs') and 'retirement_complete' in params:audit_inserted.append(True)
        if audit_inserted and sql.startswith('UPDATE audit_logs SET actor_user_id'):
            stamp_seen.append(True)
            if boundary=='actor_stamp' and not fired:fired.append(True);raise RuntimeError('owned final actor stamp SQL fault')
    def commit(db):
        final=any(isinstance(entry,AuditLog) and entry.action=='retirement_complete' for entry in db.new)
        if final and boundary in ('definite_commit','lost_ack') and not fired:
            db.flush();assert audit_inserted and stamp_seen
            if boundary=='lost_ack':actual_commit(db)
            fired.append(True);raise RuntimeError('owned final '+boundary+' fault')
        if boundary=='deferred_commit':
            driver=db.connection().connection.driver_connection
            try:return actual_commit(db)
            except IntegrityError:
                assert driver.in_transaction,'Real deferred SQLite COMMIT leaves physical transaction pending'
                raise
        return actual_commit(db)
    foreign_keys=_sqlite_foreign_keys_on_checkout
    event.listen(engine,'checkout',foreign_keys)
    if boundary=='deferred_commit':
        assert engine.dialect.name=='sqlite'
        with engine.begin() as c:
            assert c.exec_driver_sql('PRAGMA foreign_keys').scalar_one()==1
            c.exec_driver_sql('CREATE TABLE owned_retirement_parent(id INTEGER PRIMARY KEY)')
            c.exec_driver_sql('CREATE TABLE owned_retirement_deferred(audit_id INTEGER, parent_id INTEGER REFERENCES owned_retirement_parent(id) DEFERRABLE INITIALLY DEFERRED)')
            c.exec_driver_sql("CREATE TRIGGER owned_retirement_deferred AFTER INSERT ON audit_logs WHEN NEW.action='retirement_complete' BEGIN INSERT INTO owned_retirement_deferred VALUES(NEW.id,-1); END")
    event.listen(engine,'before_cursor_execute',before_sql);event.listen(engine,'after_cursor_execute',after_sql);monkeypatch.setattr(Session,'commit',commit)
    try:
        if boundary=='deferred_commit':
            with pytest.raises(IntegrityError) as caught:service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
            assert getattr(caught.value.orig,'sqlite_errorname',None)=='SQLITE_CONSTRAINT_FOREIGNKEY'
            assert audit_inserted and stamp_seen
            import sqlite3
            with sqlite3.connect(str(engine.url.database)) as independent:
                assert independent.execute('SELECT count(*) FROM timelapses WHERE id=?',(tid,)).fetchone()[0]==1
                assert independent.execute("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'").fetchone()[0]==0
                assert independent.execute('SELECT state FROM timelapse_retirements').fetchone()[0]!='complete'
        else:
            with pytest.raises(RuntimeError,match='owned final'):service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
            assert fired
        if boundary=='audit_insert':assert not audit_inserted
        else:assert audit_inserted and stamp_seen
        prior_calls=tuple(destructive)
        if fixture_name=='prepared_source':assert prior_calls
        else:assert prior_calls==()
        with Session(engine) as db:
            physical=db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE state NOT IN ('retired','originally_absent')")).scalar_one()
            assert physical==0
            if boundary=='lost_ack':
                assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==0
                assert db.execute(text('SELECT state FROM timelapse_retirements')).scalar_one()=='complete'
                assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
            else:
                assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
                assert db.execute(text('SELECT state FROM timelapse_retirements')).scalar_one()!='complete'
                assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==0
        if boundary!='lost_ack':assert _final_completion_metadata(engine)==before
        if boundary=='deferred_commit':
            with engine.begin() as c:
                assert c.exec_driver_sql('SELECT count(*) FROM owned_retirement_deferred').scalar_one()==0
                c.exec_driver_sql('DROP TRIGGER owned_retirement_deferred')
        event.remove(engine,'before_cursor_execute',before_sql);event.remove(engine,'after_cursor_execute',after_sql)
        monkeypatch.setattr(Session,'commit',actual_commit)
        if boundary=='lost_ack':
            assert service.sealed_lifetime_receipt(engine,evidence,operation_id=nonce,original_id=tid).operation_id==nonce
        else:assert service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce).state=='complete'
        assert tuple(destructive)==prior_calls,'Final retry/replay must not repeat rename/unlink/rmdir'
        with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
    finally:
        for name,listener in (('before_cursor_execute',before_sql),('after_cursor_execute',after_sql)):
            if event.contains(engine,name,listener):event.remove(engine,name,listener)
        if event.contains(engine,'checkout',foreign_keys):event.remove(engine,'checkout',foreign_keys)


@pytest.mark.parametrize('fault',['rollback','invalidate_before','invalidate_after','both_invalidates','detach_after','detach_before'])
def test_failed_final_commit_cleanup_preserves_error_and_discards_original(prepared_source,monkeypatch,fault):
    import sqlite3
    from sqlalchemy import event
    from sqlalchemy.engine import Connection
    from core.models import AuditLog
    from modules.archives import timelapse_lifetime_action as service
    engine,root,tid,_,nonce=prepared_source;prepare(prepared_source);evidence=credential(engine)
    with engine.connect() as connection:
        proxy_type=type(connection.connection);driver_property=proxy_type.driver_connection
    before_metadata=_final_completion_metadata(engine);armed=[];target={};calls=[];connections=[];taken=[]
    actual_commit=Session.commit;invalidate=Connection.invalidate;proxy_invalidate=proxy_type.invalidate;detach=proxy_type.detach;take=service._take_handoff
    foreign_keys=_sqlite_foreign_keys_on_checkout
    event.listen(engine,'checkout',foreign_keys)
    def connect(driver,record):connections.append(driver)
    event.listen(engine,'connect',connect)
    class Driver:
        def __init__(self,raw):self.raw=raw
        def __getattr__(self,key):return getattr(self.raw,key)
        def rollback(self):
            if armed and fault=='rollback':calls.append('rollback_failure');raise RuntimeError('owned rollback cleanup failure')
            return self.raw.rollback()
    if fault=='rollback':
        facades={}
        def fault_driver(proxy):
            raw=driver_property.__get__(proxy,proxy_type)
            if raw not in facades:facades[raw]=Driver(raw)
            return facades[raw]
        monkeypatch.setattr(proxy_type,'driver_connection',property(fault_driver))
    def hard(connection,*args,**kwargs):
        if armed and connection is target['connection'] and fault!='rollback':
            calls.append('hard_invalidate')
            if fault=='invalidate_after':invalidate(connection,*args,**kwargs)
            raise RuntimeError('owned hard invalidate cleanup failure')
        return invalidate(connection,*args,**kwargs)
    def soft(proxy,*args,**kwargs):
        if armed and proxy is target['proxy'] and fault in ('both_invalidates','detach_after','detach_before'):
            calls.append('proxy_invalidate_failure');raise RuntimeError('owned proxy invalidate cleanup failure')
        return proxy_invalidate(proxy,*args,**kwargs)
    def detached(proxy,*args,**kwargs):
        if armed and proxy is target['proxy'] and fault in ('detach_after','detach_before'):
            calls.append('detach_failure')
            if fault=='detach_after':detach(proxy,*args,**kwargs)
            raise RuntimeError('owned detach cleanup failure')
        return detach(proxy,*args,**kwargs)
    def counted(*args,**kwargs):taken.append(True);return take(*args,**kwargs)
    def commit(db):
        if any(isinstance(entry,AuditLog) and entry.action=='retirement_complete' for entry in db.new) and not armed:
            connection=db.connection();proxy=connection.connection;driver=driver_property.__get__(proxy,proxy_type)
            db.flush();target.update(connection=connection,proxy=proxy,driver=driver,connect_count=len(connections),handoff_count=len(taken))
            armed.append(True);raise RuntimeError('owned original final COMMIT fault')
        return actual_commit(db)
    monkeypatch.setattr(Connection,'invalidate',hard);monkeypatch.setattr(proxy_type,'invalidate',soft);monkeypatch.setattr(proxy_type,'detach',detached)
    monkeypatch.setattr(Session,'commit',commit);monkeypatch.setattr(service,'_take_handoff',counted)
    try:
        with pytest.raises(RuntimeError,match='owned original final COMMIT fault') as caught:service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
        assert calls and any('cleanup' in note for note in caught.value.__notes__)
        assert len(connections)==target['connect_count'],'Cleanup must not reconnect'
        assert len(taken)==target['handoff_count'],'Failed COMMIT must not emit handoff'
        with sqlite3.connect(str(engine.url.database)) as independent:
            assert independent.execute('SELECT count(*) FROM timelapses WHERE id=?',(tid,)).fetchone()[0]==1
            assert independent.execute("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'").fetchone()[0]==0
        with Session(engine) as db:
            received=db.connection().connection
            assert driver_property.__get__(received,proxy_type) is not target['driver'],'Discarded driver must not be reused'
            assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
        assert _final_completion_metadata(engine)==before_metadata
        if fault=='detach_before':
            assert engine._odin_timelapse_transaction_cleanup_blocked
            before=len(connections)
            with pytest.raises(service.RetirementUnavailable):
                with service._transaction(engine,evidence,tid,nonce,None,guard=None):pytest.fail('Uncertain engine admitted a new transaction')
            assert len(connections)==before
        else:
            monkeypatch.setattr(Session,'commit',actual_commit)
            assert service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce).state=='complete'
            with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
    finally:
        event.remove(engine,'checkout',foreign_keys);event.remove(engine,'connect',connect)

@pytest.mark.parametrize('single_connection',[False,True])
def test_lost_ack_retains_checkout_until_cleanup(prepared_source,monkeypatch,single_connection):
    """A real COMMIT barrier must not lend its driver to another transaction."""
    import threading
    from sqlalchemy import create_engine,event
    from sqlalchemy.exc import TimeoutError
    from core.models import AuditLog
    from modules.archives import timelapse_lifetime_action as service
    engine,root,tid,_,nonce=source=prepared_source
    prepare(source)
    if single_connection:
        engine=create_engine(engine.url,pool_size=1,max_overflow=0,pool_timeout=.15)
    foreign_keys=_sqlite_foreign_keys_on_checkout
    event.listen(engine,'checkout',foreign_keys)
    with engine.begin() as c:c.exec_driver_sql('CREATE TABLE owned_lost_ack_competitor(id INTEGER PRIMARY KEY)')
    evidence=credential(engine);actual_commit=Session.commit
    ready=threading.Event();released=threading.Event();observed={};errors=[];threads=[];fired=[]
    def competitor():
        try:
            with engine.connect() as connection:
                observed['driver']=connection.connection.driver_connection
                connection.execute(text('INSERT INTO owned_lost_ack_competitor VALUES(1)'))
                ready.set()
                assert released.wait(3),'Cleanup did not release barrier'
                connection.commit()
        except TimeoutError:
            observed['timeout']=True;ready.set()
        except BaseException as error:
            errors.append(error);ready.set()
    def commit(db):
        final=any(isinstance(entry,AuditLog) and entry.action=='retirement_complete' for entry in db.new)
        if final and not fired:
            observed['original']=db.connection().connection.driver_connection
            actual_commit(db);fired.append(True)
            thread=threading.Thread(target=competitor);threads.append(thread);thread.start()
            assert ready.wait(3),'Competitor did not reach bounded checkout barrier'
            raise RuntimeError('owned acknowledged COMMIT then lost ACK')
        return actual_commit(db)
    monkeypatch.setattr(Session,'commit',commit)
    try:
        with pytest.raises(RuntimeError,match='owned acknowledged COMMIT then lost ACK'):
            service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
        released.set()
        for thread in threads:thread.join(3);assert not thread.is_alive()
        assert not errors
        if single_connection:
            assert observed.get('timeout') is True,'One-slot pool must retain the action lease through ACK cleanup'
        else:
            assert observed['driver'] is not observed['original'],'Lost-ACK cleanup must still own its original checkout'
            with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM owned_lost_ack_competitor')).scalar_one()==1
        assert service.sealed_lifetime_receipt(engine,evidence,operation_id=nonce,original_id=tid).operation_id==nonce
        with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
    finally:
        released.set()
        for thread in threads:thread.join(3)
        event.remove(engine,'checkout',foreign_keys)
        if single_connection:engine.dispose()

@pytest.mark.parametrize('drift',['other_engine','unbound','prebegun','physical_prebegun','autocommit','nested','closed','invalidated','root_replaced','driver_replaced','session_replaced','scope_replaced','credential_replaced','expired'])
def test_owned_action_lease_binding_refuses_drift(zero_source,drift,monkeypatch):
    from dataclasses import replace
    from sqlalchemy import create_engine,event
    from modules.archives import timelapse_action_authority as authority
    engine,_,_,_,_=zero_source;evidence=credential(engine);other=None;lease=None;db=None
    foreign_keys=_sqlite_foreign_keys_on_checkout
    event.listen(engine,'checkout',foreign_keys)
    connection=engine.connect()
    try:
        if drift=='other_engine':
            other=create_engine(engine.url);connection.close();connection=other.connect()
        db=Session(bind=connection)
        if drift in ('prebegun','nested'):connection.begin()
        if drift=='nested':connection.begin_nested()
        if drift=='physical_prebegun':connection.connection.driver_connection.execute('BEGIN')
        if drift=='autocommit':connection.execution_options(isolation_level='AUTOCOMMIT')
        if drift in ('other_engine','prebegun','physical_prebegun','autocommit','nested'):
            with pytest.raises(RetirementUnavailable):authority._admit_action_lease(db,evidence,connection)
            return
        if drift=='unbound':
            with pytest.raises(RetirementUnavailable):authority.fresh_action_principal(db,evidence)
            return
        lease=authority._admit_action_lease(db,evidence,connection)
        authority.fence_authorization(db);authority._mint_action_lease(lease)
        assert authority.fresh_action_principal(db,evidence)['id']==3
        if drift=='closed':connection.close()
        elif drift=='invalidated':connection.invalidate()
        elif drift=='root_replaced':connection.commit();connection.begin()
        elif drift=='driver_replaced':monkeypatch.setattr(type(connection.connection),'driver_connection',property(lambda proxy:object()))
        elif drift=='session_replaced':
            replacement=Session(bind=connection)
            try:
                with pytest.raises(RetirementUnavailable):authority.fresh_action_principal(replacement,evidence)
            finally:replacement.close()
            return
        elif drift=='scope_replaced':evidence.request.scope['odin.private.audit_actor_scope']=RequestAuditScope(None)
        elif drift=='credential_replaced':evidence=replace(evidence,subject=evidence.subject)
        elif drift=='expired':authority._expire_action_lease(db,lease);db.commit();connection.begin()
        with pytest.raises(RetirementUnavailable):authority.fresh_action_principal(db,evidence)
    finally:
        if db is not None:
            if lease is not None:authority._expire_action_lease(db,lease)
            db.close()
        connection.close();event.remove(engine,'checkout',foreign_keys)
        if other is not None:other.dispose()


def test_captured_connection_authority_remains_exact(zero_source):
    from sqlalchemy import event
    from modules.archives import timelapse_action_authority as authority
    engine,_,_,_,_=zero_source
    with engine.connect() as connection:
        with Session(bind=connection) as db:
            username=db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
            token=create_access_token({'sub':username,'role':'admin'})
            request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())],'query_string':b'','odin.private.audit_actor_scope':RequestAuditScope(None)})
            evidence=authority.capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer')
            assert evidence.database is connection
            assert authority.fresh_action_principal(db,evidence)['id']==3
        with engine.connect() as other:
            with Session(bind=other) as db:
                with pytest.raises(RetirementUnavailable):authority.fresh_action_principal(db,evidence)


def test_successful_final_commit_preserves_first_close_failure(zero_source,monkeypatch):
    from sqlalchemy.engine import Connection
    from core.models import AuditLog
    from modules.archives import timelapse_lifetime_action as service
    engine,root,tid,_,nonce=source=zero_source;prepare(source);evidence=credential(engine);target={};closed=[]
    actual_commit=Session.commit;session_close=Session.close;connection_close=Connection.close
    def commit(db):
        final=any(isinstance(entry,AuditLog) and entry.action=='retirement_complete' for entry in db.new)
        if final:target.update(db=db,connection=db.connection())
        return actual_commit(db)
    def close_session(db):
        session_close(db)
        if db is target.get('db'):closed.append('session');raise RuntimeError('owned first Session close failure')
    def close_connection(connection):
        connection_close(connection)
        if connection is target.get('connection'):closed.append('connection');raise RuntimeError('owned second Connection close failure')
    monkeypatch.setattr(Session,'commit',commit);monkeypatch.setattr(Session,'close',close_session);monkeypatch.setattr(Connection,'close',close_connection)
    with pytest.raises(RuntimeError,match='owned first Session close failure') as caught:service.retire_sealed_lifetime(engine,root,tid,evidence,expected_nonce=nonce)
    assert closed==['session','connection']
    assert any('lease cleanup' in note for note in caught.value.__notes__)
    assert service.sealed_lifetime_receipt(engine,evidence,operation_id=nonce,original_id=tid).operation_id==nonce
    with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete'")).scalar_one()==1
