"""Preparation persists original recovery proof; retries never recreate it."""
import hashlib
import pytest
from sqlalchemy import text,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from tests.test_contracts.test_timelapse_retirement_prepare import owned_plan,prepare,preserved


def test_original_parent_identities_and_file_digests_commit_together(owned_plan):
    result=prepare(owned_plan);engine,root=owned_plan[:2]
    with Session(engine) as db:
        plan=db.execute(text('SELECT object_index,relative_path,kind,state FROM timelapse_retirement_objects ORDER BY object_index')).all()
        recorded=db.execute(text('SELECT relative_path,device,inode,uid,gid,mode FROM timelapse_retirement_parents')).all()
        expected={''}
        for _,path,_,_ in plan:
            parts=path.split('/');expected.update('/'.join(parts[:n]) for n in range(1,len(parts)))
        assert {row[0] for row in recorded}==expected
        for path,*identity in recorded:
            actual=(root/path).stat()
            assert tuple(identity)==(actual.st_dev,actual.st_ino,actual.st_uid,actual.st_gid,actual.st_mode)
        moves=db.execute(text('SELECT object_index,kind,digest_sha256,phase,verified_ctime_ns FROM timelapse_retirement_moves ORDER BY object_index')).all()
        assert len(moves)==len(plan)
        for index,kind,digest,phase,ctime in moves:
            path=plan[index][1]
            assert phase=='planned' and ctime is None and kind==plan[index][2]
            assert digest==(hashlib.sha256((root/path).read_bytes()).hexdigest() if kind=='file' else None)
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_quarantine')).scalar_one()==0
    preserved(owned_plan)


@pytest.mark.parametrize('fault',['parents','moves'])
def test_evidence_insert_fault_rolls_back_entire_plan_without_media_action(owned_plan,fault):
    from modules.archives.timelapse_retirement import RetirementUnavailable
    engine=owned_plan[0]
    def refuse(_db,_cursor,statement,_params,_context,_many):
        if statement.lstrip().startswith('INSERT INTO timelapse_retirement_'+fault):raise RuntimeError('owned evidence insertion fault')
    event.listen(engine,'before_cursor_execute',refuse)
    try:
        with pytest.raises(RetirementUnavailable):prepare(owned_plan)
    finally:event.remove(engine,'before_cursor_execute',refuse)
    with Session(engine) as db:
        for table in ['timelapse_retirements','timelapse_retirement_objects','timelapse_retirement_parents','timelapse_retirement_moves']:
            assert db.execute(text('SELECT COUNT(*) FROM '+table)).scalar_one()==0
        assert db.execute(text('SELECT status,retirement_operation_id FROM timelapses')).one()==('ready',None)
    preserved(owned_plan)


@pytest.mark.parametrize('damage',['all_parents','one_parent','all_moves','one_move','wrong_kind','wrong_root','wrong_directory_parent','extra_parent','legacy_evidence'])
def test_retry_refuses_incomplete_or_changed_original_evidence_without_recapture(owned_plan,monkeypatch,damage):
    from modules.archives import timelapse_retirement as module
    prepare(owned_plan)
    with Session(owned_plan[0]) as db:
        if damage in ('all_parents','legacy_evidence'):db.execute(text('DELETE FROM timelapse_retirement_parents'))
        elif damage=='one_parent':db.execute(text("DELETE FROM timelapse_retirement_parents WHERE relative_path=(SELECT relative_path FROM timelapse_retirement_parents WHERE relative_path<>'' LIMIT 1)"))
        elif damage=='wrong_root':db.execute(text("UPDATE timelapse_retirement_parents SET inode=inode+1 WHERE relative_path=''"))
        elif damage=='wrong_directory_parent':db.execute(text("UPDATE timelapse_retirement_parents SET inode=inode+1 WHERE relative_path=:path"),{'path':str(owned_plan[2])+'/frames'})
        elif damage=='extra_parent':db.execute(text("INSERT INTO timelapse_retirement_parents SELECT operation_id,'extra',device,inode,uid,gid,mode FROM timelapse_retirement_parents WHERE relative_path=''"))
        if damage in ('all_moves','legacy_evidence'):db.execute(text('DELETE FROM timelapse_retirement_moves'))
        elif damage=='one_move':db.execute(text('DELETE FROM timelapse_retirement_moves WHERE object_index=(SELECT MIN(object_index) FROM timelapse_retirement_moves)'))
        elif damage=='wrong_kind':db.execute(text("UPDATE timelapse_retirement_moves SET kind='directory',digest_sha256=NULL WHERE object_index=(SELECT MIN(object_index) FROM timelapse_retirement_moves WHERE kind='file')"))
        db.commit()
    monkeypatch.setattr(module,'capture_manifest',lambda *a,**k:pytest.fail('Retry recaptured original evidence'))
    with pytest.raises(module.RetirementUnavailable):prepare(owned_plan)
    preserved(owned_plan)


def test_retry_uses_saved_digest_after_source_replacement(owned_plan,monkeypatch):
    from modules.archives import timelapse_retirement as module
    result=prepare(owned_plan)
    with Session(owned_plan[0]) as db:before=db.execute(text('SELECT digest_sha256 FROM timelapse_retirement_moves ORDER BY object_index')).all()
    video=owned_plan[4];video.rename(video.with_name('held.mp4'));video.write_bytes(b'owned replacement')
    monkeypatch.setattr(module,'capture_manifest',lambda *a,**k:pytest.fail('Retry traversed source'))
    from modules.archives import timelapse_recovery_evidence as evidence
    monkeypatch.setattr(evidence,'capture_file_digest',lambda *a,**k:pytest.fail('Retry rehashed replacement'))
    assert not prepare(owned_plan).created
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT digest_sha256 FROM timelapse_retirement_moves ORDER BY object_index')).all()==before
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_prepare'")).scalar_one()==1
    assert video.read_bytes()==b'owned replacement'


def test_aggregate_byte_limit_refuses_before_any_file_digest(owned_plan,monkeypatch):
    from modules.archives import timelapse_recovery_evidence as evidence
    from modules.archives.timelapse_retirement import RetirementUnavailable
    monkeypatch.setattr(evidence,'MAX_BYTES',len(owned_plan[4].read_bytes()))
    monkeypatch.setattr(evidence,'capture_file_digest',lambda *a,**k:pytest.fail('Opened despite aggregate limit'))
    with pytest.raises(RetirementUnavailable) as error:prepare(owned_plan)
    assert error.value.reason=='manifest_limit'
    preserved(owned_plan)
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==0


@pytest.mark.parametrize('phase',['manifest','digest'])
def test_shared_deadline_refuses_after_capture_or_digest_before_write(owned_plan,monkeypatch,phase):
    from modules.archives import timelapse_retirement as module,timelapse_recovery_evidence as evidence
    expired=False
    monkeypatch.setattr(evidence.time,'monotonic',lambda:200 if expired else 100)
    if phase=='manifest':
        actual=module.capture_manifest
        def capture(*args,**kwargs):
            nonlocal expired
            result=actual(*args,**kwargs);expired=True;return result
        monkeypatch.setattr(module,'capture_manifest',capture)
        monkeypatch.setattr(evidence,'capture_file_digest',lambda *a,**k:pytest.fail('Hash opened after capture deadline'))
    else:
        actual=evidence.capture_file_digest
        def capture(*args,**kwargs):
            nonlocal expired
            result=actual(*args,**kwargs);expired=True;return result
        monkeypatch.setattr(evidence,'capture_file_digest',capture)
    with pytest.raises(module.RetirementUnavailable) as error:prepare(owned_plan)
    assert error.value.reason=='manifest_limit'
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirements')).scalar_one()==0
        assert db.execute(text('SELECT status FROM timelapses')).scalar_one()=='ready'
    preserved(owned_plan)


def test_absent_roots_have_no_digest_or_move_and_retry_does_not_adopt_new_files(owned_plan):
    import shutil
    owned_plan[4].unlink();shutil.rmtree(owned_plan[5].parent)
    result=prepare(owned_plan)
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT state FROM timelapse_retirement_objects')).all()==[('originally_absent',),('originally_absent',)]
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_moves')).scalar_one()==0
        assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_parents')).scalar_one()>=1
    owned_plan[4].write_bytes(b'new owned content');owned_plan[5].mkdir(parents=True)
    assert prepare(owned_plan).operation_id==result.operation_id
    assert owned_plan[4].read_bytes()==b'new owned content'
    with Session(owned_plan[0]) as db:assert db.execute(text('SELECT COUNT(*) FROM timelapse_retirement_moves')).scalar_one()==0


def test_acknowledgment_loss_retry_discovers_same_evidence_without_rehash(owned_plan,monkeypatch):
    from modules.archives import timelapse_retirement as module,timelapse_recovery_evidence as evidence
    class AcknowledgmentLost(Session):
        def commit(self):
            super().commit();raise RuntimeError('owned persisted acknowledgment fault')
    monkeypatch.setattr(module,'Session',AcknowledgmentLost)
    with pytest.raises(module.RetirementUnavailable):prepare(owned_plan)
    with Session(owned_plan[0]) as db:
        original=db.execute(text('SELECT object_index,digest_sha256 FROM timelapse_retirement_moves ORDER BY object_index')).all()
        assert len(original)==5
    monkeypatch.setattr(module,'Session',Session)
    monkeypatch.setattr(module,'capture_manifest',lambda *a,**k:pytest.fail('Recaptured persisted plan'))
    monkeypatch.setattr(evidence,'capture_file_digest',lambda *a,**k:pytest.fail('Rehashed persisted plan'))
    assert not prepare(owned_plan).created
    with Session(owned_plan[0]) as db:
        assert db.execute(text('SELECT object_index,digest_sha256 FROM timelapse_retirement_moves ORDER BY object_index')).all()==original
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='retirement_prepare'")).scalar_one()==1
    preserved(owned_plan)


def test_retry_refuses_absent_directory_with_recorded_children(owned_plan):
    from modules.archives.timelapse_retirement import RetirementUnavailable
    prepare(owned_plan)
    with Session(owned_plan[0]) as db:
        index=db.execute(text('SELECT object_index FROM timelapse_retirement_objects WHERE relative_path=:path'),{'path':str(owned_plan[2])}).scalar_one()
        db.execute(text('DELETE FROM timelapse_retirement_moves WHERE object_index=:index'),{'index':index})
        db.execute(text("UPDATE timelapse_retirement_objects SET state='originally_absent',quarantine_name=NULL,device=NULL,inode=NULL,uid=NULL,gid=NULL,mode=NULL,link_count=NULL,byte_count=NULL,mtime_ns=NULL,ctime_ns=NULL WHERE object_index=:index"),{'index':index})
        db.commit()
    with pytest.raises(RetirementUnavailable):prepare(owned_plan)
    preserved(owned_plan)
