"""Actual owned preparation DB/files; no API activation or source deletion."""
import pytest
from sqlalchemy import text,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_content_audience import capture_plain

@pytest.fixture
def owned_plan(review_db,tmp_path):
    tid=capture_plain(review_db,org=1)
    root=tmp_path/'owned-media';root.mkdir(mode=0o700)
    filename=review_db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one();review_db.rollback()
    video=root/filename;video.parent.mkdir();video.write_bytes(b'OWNED_ORIGINAL_VIDEO')
    frames=root/str(tid)/'frames';frames.mkdir(parents=True)
    (frames/'one.jpg').write_bytes(b'OWNED_FRAME_ONE');(frames/'two.jpg').write_bytes(b'OWNED_FRAME_TWO')
    principal={'id':3,'role':'admin','group_id':1,'_auth_kind':'session_jwt'}
    return review_db.get_bind(),root,tid,principal,video,frames


def prepare(plan):
    from modules.archives.timelapse_retirement import prepare_retirement
    return prepare_retirement(*plan[:4])


def preserved(plan):
    assert plan[4].read_bytes()==b'OWNED_ORIGINAL_VIDEO'
    assert (plan[5]/'one.jpg').read_bytes()==b'OWNED_FRAME_ONE'
    assert (plan[5]/'two.jpg').read_bytes()==b'OWNED_FRAME_TWO'


def test_exact_plan_commits_before_any_media_action(owned_plan):
    result=prepare(owned_plan);assert result.created and result.state=='pending'
    preserved(owned_plan)
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT status,retirement_operation_id,ownership_operation_id FROM timelapses')).one()==('deleting',result.operation_id,result.operation_id)
        rows=db.execute(text('SELECT object_index,relative_path,quarantine_name,inode,state FROM timelapse_retirement_objects ORDER BY object_index')).all()
        assert len(rows)==5 and [r[0] for r in rows]==list(range(5))
        assert all(r[2]==result.operation_id+'_'+str(r[0]) and r[3]>0 and r[4]=='planned' for r in rows)
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_prepare'")).scalar_one()==1


@pytest.mark.parametrize('fault',['intent','objects','audit','commit','acknowledgment'])
def test_prepare_write_or_commit_uncertainty_never_moves_media(owned_plan,monkeypatch,fault):
    from modules.archives import timelapse_retirement as implementation
    from core import audit as dependencies
    engine=owned_plan[0]
    class FaultSession(Session):
        def commit(self):
            if fault=='commit':raise RuntimeError('owned commit refused')
            super().commit()
            if fault=='acknowledgment':raise RuntimeError('owned persisted acknowledgment uncertain')
    monkeypatch.setattr(implementation,'Session',FaultSession)
    def refuse(_db,_cursor,statement,_parameters,_context,_many):
        target='timelapse_retirements' if fault=='intent' else 'timelapse_retirement_objects' if fault=='objects' else None
        if target and statement.lstrip().startswith('INSERT INTO '+target):raise RuntimeError('owned insert refused')
    if fault=='audit':monkeypatch.setattr(dependencies,'log_audit',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('owned audit refused')))
    event.listen(engine,'before_cursor_execute',refuse)
    try:
        with pytest.raises(implementation.RetirementUnavailable):prepare(owned_plan)
    finally:event.remove(engine,'before_cursor_execute',refuse)
    preserved(owned_plan)
    with Session(engine) as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==(1 if fault=='acknowledgment' else 0)
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()==('deleting' if fault=='acknowledgment' else 'ready')
    if fault=='acknowledgment':
        monkeypatch.setattr(implementation,'Session',Session)
        retried=prepare(owned_plan);assert retried.created is False
        with Session(engine) as db:assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_prepare'")).scalar_one()==1


def test_retry_never_recaptures_or_adopts_replaced_media(owned_plan,monkeypatch):
    from modules.archives import timelapse_retirement as implementation
    first=prepare(owned_plan);original=owned_plan[4].stat().st_ino
    owned_plan[4].rename(owned_plan[1]/'held-original.mp4');owned_plan[4].write_bytes(b'OWNED_REPLACEMENT')
    monkeypatch.setattr(implementation,'capture_manifest',lambda *a,**k:pytest.fail('retry must use persisted manifest'))
    second=prepare(owned_plan);assert second.operation_id==first.operation_id and not second.created
    with Session(owned_plan[0]) as db:
        saved=db.execute(text("SELECT inode FROM timelapse_retirement_objects WHERE kind='file' AND relative_path=:path"),{'path':str(owned_plan[4].relative_to(owned_plan[1]))}).scalar_one()
        assert saved==original and saved!=owned_plan[4].stat().st_ino
    assert owned_plan[4].read_bytes()==b'OWNED_REPLACEMENT'


def test_malformed_persisted_quarantine_refuses_without_recapture(owned_plan,monkeypatch):
    from modules.archives import timelapse_retirement as implementation
    prepare(owned_plan)
    with Session(owned_plan[0]) as db:db.execute(text("UPDATE timelapse_retirement_objects SET quarantine_name='../bad' WHERE object_index=0"));db.commit()
    monkeypatch.setattr(implementation,'capture_manifest',lambda *a,**k:pytest.fail('retry must not recapture'))
    with pytest.raises(implementation.RetirementUnavailable):prepare(owned_plan)
    preserved(owned_plan)


@pytest.mark.parametrize('drift',['actor','status'])
def test_fresh_write_phase_refuses_authority_drift_after_manifest(owned_plan,monkeypatch,drift):
    from modules.archives import timelapse_retirement as implementation
    capture=implementation.capture_manifest
    def change(*a,**k):
        result=capture(*a,**k)
        with Session(owned_plan[0]) as writer:
            if drift=='actor':writer.execute(text("UPDATE users SET role='operator' WHERE id=3"))
            else:writer.execute(text("UPDATE timelapses SET status='capturing' WHERE id=:id"),{'id':owned_plan[2]})
            writer.commit()
        return result
    monkeypatch.setattr(implementation,'capture_manifest',change)
    with pytest.raises(implementation.RetirementUnavailable):prepare(owned_plan)
    preserved(owned_plan)
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==0


def test_session_input_is_rejected_without_touching_unrelated_caller_transaction(owned_plan):
    from modules.archives import timelapse_retirement as implementation
    with Session(owned_plan[0]) as caller:
        caller.execute(text("UPDATE users SET username='owned-uncommitted' WHERE id=3"))
        with pytest.raises(implementation.RetirementUnavailable):implementation.prepare_retirement(caller,*owned_plan[1:4])
        assert caller.in_transaction() and caller.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()=='owned-uncommitted'
        caller.rollback()
    preserved(owned_plan)


def test_temporarily_substituted_manifest_root_cannot_borrow_original_guard(owned_plan,monkeypatch):
    from modules.archives import timelapse_retirement as implementation
    capture=implementation.capture_manifest;root=owned_plan[1]
    def substitute(*args,**kwargs):
        original=root.parent/'owned-original-root'
        root.rename(original)
        try:
            root.mkdir(mode=0o700)
            relative=owned_plan[4].relative_to(root)
            video=root/relative;video.parent.mkdir();video.write_bytes(b'OWNED_FOREIGN_VIDEO')
            frames=root/str(owned_plan[2])/'frames';frames.mkdir(parents=True)
            (frames/'one.jpg').write_bytes(b'OWNED_FOREIGN_FRAME')
            result=capture(*args,**kwargs)
        finally:
            root.rename(root.parent/'owned-foreign-held-root')
            original.rename(root)
        return result
    monkeypatch.setattr(implementation,'capture_manifest',substitute)
    with pytest.raises(implementation.RetirementUnavailable):prepare(owned_plan)
    preserved(owned_plan)
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==0
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='ready'


def test_duplicate_live_filename_refuses_without_plan_or_media_mutation(owned_plan):
    from modules.archives import timelapse_retirement as implementation
    with Session(owned_plan[0]) as db:
        db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(999,77,:filename,'capturing')"),{'filename':str(owned_plan[4].relative_to(owned_plan[1]))});db.commit()
    with pytest.raises(implementation.RetirementUnavailable):prepare(owned_plan)
    preserved(owned_plan)
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==0


def test_originally_absent_leaf_is_explicit_without_adopting_new_file(owned_plan):
    owned_plan[4].unlink()  # Deliberately remove only this synthetic owned leaf.
    result=prepare(owned_plan)
    with Session(owned_plan[0]) as db:
        row=db.execute(text("SELECT state,inode,quarantine_name FROM timelapse_retirement_objects WHERE operation_id=:op AND kind='file' AND relative_path=:path"),{'op':result.operation_id,'path':str(owned_plan[4].relative_to(owned_plan[1]))}).one()
        assert tuple(row)==('originally_absent',None,None)
    owned_plan[4].write_bytes(b'OWNED_REPLACEMENT_AFTER_ABSENCE')
    assert prepare(owned_plan).operation_id==result.operation_id
    assert owned_plan[4].read_bytes()==b'OWNED_REPLACEMENT_AFTER_ABSENCE'


def test_owner_nonce_drift_between_capture_and_write_cannot_adopt_new_lifetime(owned_plan,monkeypatch):
    from core.timelapse_ownership_schema import anchors
    from modules.archives import timelapse_retirement as implementation
    capture=implementation.capture_manifest
    def change(*args,**kwargs):
        result=capture(*args,**kwargs)
        with Session(owned_plan[0]) as writer:
            saved=dict(writer.execute(anchors.select()).mappings().one())
            saved.pop('created_at');saved['operation_id']='owned-new-lifetime'
            writer.execute(anchors.insert(),saved)
            writer.execute(text("UPDATE timelapses SET ownership_operation_id='owned-new-lifetime' WHERE id=:id"),{'id':owned_plan[2]});writer.commit()
        return result
    monkeypatch.setattr(implementation,'capture_manifest',change)
    with pytest.raises(implementation.RetirementUnavailable):prepare(owned_plan)
    preserved(owned_plan)
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==0


@pytest.mark.parametrize('drift',['delete','scope','expiry'])
def test_fresh_scoped_token_authority_refuses_changes_during_manifest(owned_plan,monkeypatch,drift):
    from modules.archives import timelapse_retirement as implementation
    with Session(owned_plan[0]) as db:
        db.execute(text("INSERT INTO api_tokens(id,user_id,name,token_hash,token_prefix,scopes) VALUES(555,3,'Owned token','owned-placeholder','owned','[\"write:printers\"]')"));db.commit()
    principal=dict(owned_plan[3],_auth_kind='user_api_token',_token_id=555,_token_scopes=['write:printers'])
    plan=(*owned_plan[:3],principal,*owned_plan[4:])
    capture=implementation.capture_manifest
    def change(*args,**kwargs):
        result=capture(*args,**kwargs)
        with Session(owned_plan[0]) as writer:
            if drift=='delete':writer.execute(text('DELETE FROM api_tokens WHERE id=555'))
            elif drift=='scope':writer.execute(text("UPDATE api_tokens SET scopes='[\"read:printers\"]' WHERE id=555"))
            else:writer.execute(text("UPDATE api_tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=555"))
            writer.commit()
        return result
    monkeypatch.setattr(implementation,'capture_manifest',change)
    with pytest.raises(implementation.RetirementUnavailable):prepare(plan)
    preserved(owned_plan)
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==0
