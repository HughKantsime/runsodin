"""Real SQLite/FD/frame journal acceptance; codec seam is explicitly synthetic."""
import os
from contextlib import contextmanager
from types import SimpleNamespace
import cv2
import numpy as np
import pytest
from sqlalchemy import text,select
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_frame_publication import frame_writer,review_db,owned_runtime
from core.timelapse_publication_schema import publications,facts


def seed_frames(writer,tid,root,nonce,numbers=(1,3,10)):
    from core.timelapse_media_lock import acquire_media_guard
    from core.timelapse_manifest import _root
    from modules.archives.timelapse_frame_capture import _capture_authority
    from modules.archives.timelapse_frame_publication import FrameJournal
    next_number=1
    for number in numbers:
        while next_number<number:
            with acquire_media_guard(root,tid) as guard,_root(root) as (fd,identity):
                def authority(db,*,allow_ended):return _capture_authority(db,nonce=nonce,timelapse_id=tid,printer_id=77,job_id=444,allow_ended=allow_ended)
                FrameJournal(sessions=writer.SessionLocal,root_fd=fd,root_identity=identity,guard=guard,authority=authority,
                    nonce=nonce,timelapse_id=tid,printer_id=77).admit(b'reserved unpublished evidence')
            next_number+=1
        assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
        next_number+=1


@pytest.fixture
def encoder(frame_writer,review_db,monkeypatch):
    from modules.archives import timelapse_encode as module
    from core.sender_process_group import GroupRetirement
    from core.timelapse_media_worker import file_facts
    writer,tid,root,nonce=frame_writer
    seed_frames(writer,tid,root,nonce)
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    orders=[]
    class SyntheticOwnedGroup:
        def __init__(self,*args,**kwargs):
            assert kwargs['defer_start'];self._media_capability=kwargs['media']
        def retire(self):return GroupRetirement(True,'explicit synthetic codec fixture',channels_closed=True,media_copies_closed=True)
    def codec(group,manifest,**kwargs):
        orders.append([frame['number'] for frame in manifest['frames']])
        with writer.SessionLocal() as db:
            row=db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
            assert row['phase']=='worker_admitted'
        fd=kwargs['output_fd'];os.write(fd,b'explicit synthetic MP4 phase evidence');os.fsync(fd)
        os.fsync(kwargs['private_parent_fd'])
        return file_facts(fd)
    monkeypatch.setattr(module,'OwnedSenderGroup',SyntheticOwnedGroup)
    monkeypatch.setattr(module,'complete_media_transport',codec)
    return writer,tid,root,nonce,module,orders


def encode(value):
    writer,tid,root,nonce,_,_=value
    return writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)


def test_real_gap_receipts_publish_unique_result_and_preserve_originals(encoder,review_db):
    writer,tid,root,nonce,module,orders=encoder
    original={path.name:(path.stat().st_ino,path.read_bytes()) for path in next(root.glob('frames_*')).iterdir()}
    assert encode(encoder)
    assert orders==[[1,3,10]]
    row=review_db.execute(text('SELECT * FROM timelapses WHERE id=:id'),{'id':tid}).mappings().one()
    assert row['status']=='ready' and row['ownership_operation_id']!=nonce and row['filename'].startswith('encode_')
    assert row['frame_count']==3 and row['duration_seconds']==.1
    assert (root/row['filename']).is_file()
    op=review_db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
    assert op['phase']=='complete' and op['state']=='complete'
    review_db.rollback()
    assert encode(encoder) and orders==[[1,3,10]]
    assert {path.name:(path.stat().st_ino,path.read_bytes()) for path in next(root.glob('frames_*')).iterdir()}==original


@pytest.mark.parametrize('change',['stale','numeric','printing','retirement','filename','owner','namespace','source','unreceipted'])
def test_refusals_preserve_jpegs_and_never_launch(encoder,review_db,change):
    writer,tid,root,nonce,module,orders=encoder
    if change=='stale':
        assert not writer.encode_timelapse(tid,printer_id=77,nonce='0'*32,job_id=444);return
    if change=='numeric':assert not writer.encode_timelapse(tid);return
    if change=='printing':review_db.execute(text("UPDATE jobs SET status='printing' WHERE id=444"))
    elif change=='retirement':review_db.execute(text("UPDATE timelapses SET status='deleting' WHERE id=:id"),{'id':tid})
    elif change=='filename':review_db.execute(text("UPDATE timelapses SET filename='foreign.mp4' WHERE id=:id"),{'id':tid})
    elif change=='owner':review_db.execute(text('UPDATE print_files SET org_id=2 WHERE id=111'))
    elif change=='namespace':
        original=next(root.glob('frames_*'));original.rename(root/'preserved-original');original.mkdir(mode=0o700)
    elif change=='source':next(root.glob('frames_*/*.jpg')).write_bytes(b'replacement')
    elif change=='unreceipted':(next(root.glob('frames_*'))/'frame_000099.jpg').write_bytes(b'unknown')
    review_db.commit()
    assert not encode(encoder) and not orders
    assert not list(root.glob('encode_*.mp4'))


@pytest.mark.parametrize('phase',['admitted','private_created','worker_admitted','output_ready','publish_pending','published','complete'])
def test_phase_commit_ack_loss_reconciles_exact_operation_once(encoder,review_db,monkeypatch,phase):
    writer,tid,root,nonce,module,orders=encoder
    original=Session.commit;fired=[]
    def ack(db):
        value=db.execute(select(publications.c.phase).where(publications.c.kind=='encode')).scalar_one_or_none()
        original(db)
        if value==phase and not fired:fired.append(True);raise RuntimeError('Exact ACK loss after commit')
    monkeypatch.setattr(Session,'commit',ack)
    assert encode(encoder),phase
    assert fired==[True]
    assert len(orders)==1
    assert review_db.execute(select(publications.c.operation_id).where(publications.c.kind=='encode')).all().__len__()==1
    assert review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='timelapse_encode_complete'")).scalar_one()==1
    review_db.rollback()
    assert encode(encoder) and len(orders)==1


@pytest.mark.parametrize('phase',['private_created','worker_admitted','output_ready','publish_pending','published'])
def test_restart_never_relaunches_uncertain_worker_and_recovers_saved_output(encoder,monkeypatch,review_db,phase):
    writer,tid,root,nonce,module,orders=encoder
    actual=module.EncodeJournal._advance;interrupted=[]
    def cut(self,identifier,expected,target,**kwargs):
        result=actual(self,identifier,expected,target,**kwargs)
        if target==phase and not interrupted:interrupted.append(True);raise RuntimeError('Process interruption after durable phase')
        return result
    monkeypatch.setattr(module.EncodeJournal,'_advance',cut)
    assert not encode(encoder)
    monkeypatch.setattr(module.EncodeJournal,'_advance',actual)
    success=phase in ('output_ready','publish_pending','published')
    assert encode(encoder) is success
    op=review_db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
    assert op['phase']==('complete' if success else phase)
    assert op['state']==('complete' if success else 'blocked')
    assert len(orders)==(1 if success else 0)
    assert len(list(root.glob('frames_*/*.jpg')))==3


@pytest.mark.parametrize('number',[0,1])
def test_insufficient_frames_are_terminal_with_truthful_audit_and_no_output(frame_writer,review_db,number):
    writer,tid,root,nonce=frame_writer
    if number:assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    else:
        from core.timelapse_media_lock import acquire_media_guard
        from core.timelapse_manifest import _root
        from modules.archives.timelapse_frame_namespace import bind_or_open_namespace
        with acquire_media_guard(root,tid) as guard,_root(root) as (fd,identity),writer.SessionLocal() as db:
            namespace=bind_or_open_namespace(db,fd,identity,guard,nonce=nonce,timelapse_id=tid,printer_id=77,job_id=444)
            db.commit();os.close(namespace)
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
    row=review_db.execute(text('SELECT status,completed_at,duration_seconds,file_size_mb FROM timelapses WHERE id=:id'),{'id':tid}).mappings().one()
    assert row['status']=='failed' and row['completed_at'] is not None and row['duration_seconds'] is None and row['file_size_mb'] is None
    import json
    details=review_db.execute(text("SELECT details FROM audit_logs WHERE action='timelapse_encode_failed' AND entity_id=:id"),{'id':tid}).scalar_one()
    details=json.loads(details) if type(details) is str else details
    assert details=={'reason':'insufficient_frames','ownership_nonce':nonce,'frame_count':number}
    assert not review_db.execute(select(publications.c.operation_id).where(publications.c.kind=='encode')).all()
    review_db.rollback()
    assert len(list(root.glob('frames_*/*.jpg')))==number and not list(root.glob('encode_*.mp4'))
    assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)


def test_destination_collision_never_overwrites_and_reconciles_only_own_inode(encoder,monkeypatch,review_db):
    writer,tid,root,nonce,module,orders=encoder;actual=module.rename_noreplace;foreign=[]
    def collide(source_fd,source_name,destination_fd,destination_name):
        target=root/destination_name;target.write_bytes(b'foreign output');foreign.append(target)
        return actual(source_fd,source_name,destination_fd,destination_name)
    monkeypatch.setattr(module,'rename_noreplace',collide)
    assert not encode(encoder)
    assert foreign[0].read_bytes()==b'foreign output'
    monkeypatch.setattr(module,'rename_noreplace',actual)
    assert not encode(encoder) and len(orders)==1
    assert foreign[0].read_bytes()==b'foreign output'
    assert (next(root.glob('publish_*/output.mp4'))).read_bytes()==b'explicit synthetic MP4 phase evidence'
    op=review_db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
    assert op['phase']=='publish_pending'


def test_rename_ack_loss_reconciles_exact_destination_without_reencoding(encoder,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder;actual=module.rename_noreplace;fired=[]
    def lost(*args):
        actual(*args);fired.append(True);raise RuntimeError('Rename completed before interruption')
    monkeypatch.setattr(module,'rename_noreplace',lost)
    assert not encode(encoder) and fired==[True]
    monkeypatch.setattr(module,'rename_noreplace',actual)
    assert encode(encoder) and orders==[[1,3,10]]


def test_current_ownership_mutation_is_unlocked_during_codec_then_refused(encoder,monkeypatch,review_db):
    writer,tid,root,nonce,module,orders=encoder;codec=module.complete_media_transport
    def changed(*args,**kwargs):
        with writer.SessionLocal() as db:
            db.execute(text('UPDATE print_files SET org_id=2 WHERE id=111'));db.commit()
        return codec(*args,**kwargs)
    monkeypatch.setattr(module,'complete_media_transport',changed)
    assert not encode(encoder) and orders==[[1,3,10]]
    op=review_db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
    assert op['phase']=='worker_admitted' and not list(root.glob('encode_*.mp4'))
    assert next(root.glob('publish_*/output.mp4')).read_bytes()==b'explicit synthetic MP4 phase evidence'


def test_source_substitution_after_output_ready_refuses_resume(encoder,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder;actual=module.EncodeJournal._advance
    def swap(self,identifier,expected,target,**kwargs):
        result=actual(self,identifier,expected,target,**kwargs)
        if target=='output_ready':
            next(root.glob('frames_*/*.jpg')).write_bytes(b'replaced after codec')
            raise RuntimeError('Interruption and source substitution')
        return result
    monkeypatch.setattr(module.EncodeJournal,'_advance',swap)
    assert not encode(encoder)
    monkeypatch.setattr(module.EncodeJournal,'_advance',actual)
    assert not encode(encoder) and orders==[[1,3,10]] and not list(root.glob('encode_*.mp4'))


def test_stale_finalizer_copies_tickets_and_closes_reader_before_encode(encoder,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder;seen=[];actual=writer.encode_timelapse
    def wrapped(identifier,**kwargs):
        assert (identifier,kwargs)==(tid,{'printer_id':77,'nonce':nonce,'job_id':444})
        seen.append(True);return actual(identifier,**kwargs)
    monkeypatch.setattr(writer,'encode_timelapse',wrapped)
    writer.finalize_stale_timelapses()
    assert seen==[True] and orders==[[1,3,10]]


@pytest.mark.parametrize('status',['paused','pending','scheduled','submitted','rejected',None,'unknown'])
def test_nonterminal_job_never_encodes_or_enters_restart_candidates(encoder,review_db,monkeypatch,status):
    writer,tid,root,nonce,module,orders=encoder
    # Unknown/NULL are physically possible legacy facts, not inferred terminal aliases.
    review_db.execute(text('UPDATE jobs SET status=:status WHERE id=444'),{'status':status});review_db.commit()
    assert not encode(encoder) and not orders and not list(root.glob('encode_*.mp4'))
    seen=[];monkeypatch.setattr(writer,'encode_timelapse',lambda *args,**kwargs:seen.append(True))
    writer.finalize_stale_timelapses();assert seen==[]
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='capturing'


def test_paused_capture_resumes_same_ticket_lifetime_and_receipts_then_finishes(encoder,review_db):
    from core.timelapse_publication_schema import lifetimes
    from sqlalchemy import func
    writer,tid,root,nonce,module,orders=encoder
    original=next(root.glob('frames_*'));inode=original.stat().st_ino
    review_db.execute(text("UPDATE jobs SET status='paused' WHERE id=444"));review_db.commit()
    assert not encode(encoder) and not orders and len(list(original.glob('*.jpg')))==3
    assert review_db.execute(select(func.count()).select_from(lifetimes)).scalar_one()==1;review_db.rollback()
    review_db.execute(text("UPDATE jobs SET status='printing' WHERE id=444"));review_db.commit()
    with writer.SessionLocal() as db:ticket=writer.get_or_create_timelapse(db,77,444,return_ticket=True)
    assert ticket.timelapse_id==tid and ticket.nonce==nonce
    assert writer.capture_frame(77,tid,nonce=ticket.nonce,job_id=ticket.job_id)
    assert original.stat().st_ino==inode and len(list(original.glob('*.jpg')))==4
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    assert encode(encoder) and orders==[[1,3,10,11]]
    assert review_db.execute(select(func.count()).select_from(lifetimes)).scalar_one()==1
    assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==4


def test_known_new_ticket_ending_before_namespace_is_terminal_without_phantom_lifetime(frame_writer,review_db):
    from core.timelapse_publication_schema import lifetimes,bindings
    writer,tid,root,nonce=frame_writer
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='failed'
    assert not review_db.execute(select(lifetimes.c.lifetime_nonce)).all()
    assert not review_db.execute(select(bindings.c.namespace_id)).all()
    assert not review_db.execute(select(publications.c.operation_id)).all()
    assert review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='timelapse_encode_failed'")).scalar_one()==1
    assert list(root.glob('*.mp4'))==[]


@pytest.mark.parametrize('component',['numeric','nonce','random','symlink','malformed'])
def test_zero_unbound_evidence_refuses_and_preserves_all_bytes(frame_writer,review_db,component,tmp_path):
    writer,tid,root,nonce=frame_writer
    name=str(tid) if component=='numeric' else 'frames_'+(nonce if component=='nonce' else 'f'*32)
    if component=='malformed':name='frames_bad_component'
    if component=='symlink':
        outside=tmp_path/'preserved-outside';outside.mkdir();(outside/'evidence.jpg').write_bytes(b'unknown original bytes')
        (root/name).symlink_to(outside,target_is_directory=True);evidence=outside/'evidence.jpg'
    else:
        directory=root/name;directory.mkdir(mode=0o700);evidence=directory/'evidence.jpg';evidence.write_bytes(b'unknown original bytes')
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='capturing'
    assert review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='timelapse_encode_failed'")).scalar_one()==0
    assert evidence.read_bytes()==b'unknown original bytes'


def test_zero_allows_other_verified_bound_namespace_without_adopting_its_frames(frame_writer,review_db):
    from core.timelapse_ownership_schema import anchors
    writer,tid,root,nonce=frame_writer;other_nonce='b'*32
    original=dict(review_db.execute(select(anchors).where(anchors.c.operation_id==nonce)).mappings().one())
    review_db.execute(text("INSERT INTO timelapses(printer_id,print_job_id,filename,status,ownership_operation_id,frame_count) "
        "VALUES(77,444,'printer_77/other.mp4','capturing',:nonce,0)"),{'nonce':other_nonce})
    other=review_db.execute(text('SELECT id FROM timelapses WHERE ownership_operation_id=:nonce'),{'nonce':other_nonce}).scalar_one()
    copied={key:value for key,value in original.items() if key!='created_at'};copied.update(operation_id=other_nonce,timelapse_id=other,filename='printer_77/other.mp4')
    review_db.execute(anchors.insert().values(**copied));review_db.commit()
    assert writer.capture_frame(77,other,nonce=other_nonce,job_id=444)
    physical=next(root.glob('frames_*/*.jpg'));preserved=(physical.stat().st_ino,physical.read_bytes())
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='failed'
    assert (physical.stat().st_ino,physical.read_bytes())==preserved
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':other}).scalar_one()=='capturing'


def test_zero_rechecks_namespace_database_inventory_before_terminal_commit(frame_writer,review_db,monkeypatch):
    from core.timelapse_ownership_schema import anchors
    from core.timelapse_publication_schema import lifetimes,versions,namespaces
    from modules.archives import timelapse_encode as module
    writer,tid,root,nonce=frame_writer
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    actual=module.EncodeJournal.verify_zero_inventory
    def change(self,snapshot):
        actual(self,snapshot)
        with writer.SessionLocal() as db:
            stat=root.stat();db.execute(lifetimes.insert().values(lifetime_nonce=nonce,original_timelapse_id=tid,original_printer_id=77,
                root_device=stat.st_dev,root_inode=stat.st_ino,root_uid=stat.st_uid,root_gid=stat.st_gid,root_mode=stat.st_mode))
            db.execute(versions.insert().values(version_nonce=nonce,lifetime_nonce=nonce))
            db.execute(namespaces.insert().values(namespace_id='c'*32,lifetime_nonce=nonce,relative_path='frames_'+'c'*32,phase='planned'))
            db.commit()
    monkeypatch.setattr(module.EncodeJournal,'verify_zero_inventory',change)
    assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444)
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='capturing'


def test_admission_interruption_keeps_original_manifest_and_blocks_without_launch(encoder,monkeypatch,review_db):
    writer,tid,root,nonce,module,orders=encoder;actual=module.EncodeJournal.admit
    def interrupted(self,*args):
        actual(self,*args);raise RuntimeError('Stopped after exact durable admission')
    monkeypatch.setattr(module.EncodeJournal,'admit',interrupted)
    assert not encode(encoder) and not orders
    monkeypatch.setattr(module.EncodeJournal,'admit',actual)
    assert not encode(encoder) and not orders
    op=review_db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
    assert (op['phase'],op['state'],op['reason_code'])==('admitted','blocked','phase_uncertain')
    assert len(list(root.glob('frames_*/*.jpg')))==3 and not list(root.glob('publish_*/output.mp4'))


def test_unknown_private_creation_bundle_is_preserved_and_never_adopted(encoder,monkeypatch,review_db):
    writer,tid,root,nonce,module,orders=encoder;actual=module.EncodeJournal._advance
    def failed_bundle(self,identifier,expected,target,**kwargs):
        if target=='private_created':raise RuntimeError('No creation bundle confirmation')
        return actual(self,identifier,expected,target,**kwargs)
    monkeypatch.setattr(module.EncodeJournal,'_advance',failed_bundle)
    assert not encode(encoder) and not orders
    private=next(root.glob('publish_*/output.mp4'));inode=private.stat().st_ino
    monkeypatch.setattr(module.EncodeJournal,'_advance',actual)
    assert not encode(encoder) and not orders
    assert private.stat().st_ino==inode and private.read_bytes()==b''
    op=review_db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
    assert op['phase']=='admitted' and op['state']=='blocked'
    assert not review_db.execute(select(facts).where(facts.c.operation_id==op['operation_id'],facts.c.role=='private_creation')).all()


def test_namespace_substitution_after_output_ready_refuses_and_preserves_originals(encoder,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder;actual=module.EncodeJournal._advance
    preserved=[]
    def changed(self,identifier,expected,target,**kwargs):
        result=actual(self,identifier,expected,target,**kwargs)
        if target=='output_ready':
            original=next(root.glob('frames_*'));moved=root/'preserved-original-namespace';original.rename(moved);original.mkdir(mode=0o700)
            preserved.append(moved);raise RuntimeError('Interrupted with namespace replaced')
        return result
    monkeypatch.setattr(module.EncodeJournal,'_advance',changed)
    assert not encode(encoder)
    monkeypatch.setattr(module.EncodeJournal,'_advance',actual)
    assert not encode(encoder) and orders==[[1,3,10]]
    assert len(list(preserved[0].glob('*.jpg')))==3 and not list(root.glob('encode_*.mp4'))


@pytest.mark.parametrize('phase',['output_ready','publish_pending','published','complete'])
def test_metadata_rollback_before_commit_restarts_same_saved_output(encoder,monkeypatch,review_db,phase):
    writer,tid,root,nonce,module,orders=encoder;original=Session.commit;fired=[]
    def rollback(db):
        value=db.execute(select(publications.c.phase).where(publications.c.kind=='encode')).scalar_one_or_none()
        if value==phase and not fired:fired.append(True);raise RuntimeError('Actual rollback before commit')
        return original(db)
    monkeypatch.setattr(Session,'commit',rollback)
    assert not encode(encoder) and fired==[True]
    monkeypatch.setattr(Session,'commit',original)
    if phase=='output_ready':
        assert not encode(encoder)
        op=review_db.execute(select(publications).where(publications.c.kind=='encode')).mappings().one()
        assert op['phase']=='worker_admitted' and op['state']=='blocked'
        assert next(root.glob('publish_*/output.mp4')).read_bytes()==b'explicit synthetic MP4 phase evidence'
    else:
        assert encode(encoder)
        assert review_db.execute(select(publications.c.phase).where(publications.c.kind=='encode')).scalar_one()=='complete'
    assert orders==[[1,3,10]] and len(list(root.glob('frames_*/*.jpg')))==3


def test_source_hashing_runs_without_database_writer_lock(encoder,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder;actual=module.EncodeJournal._verify;checked=[]
    def verify(self,directory,name,expected,**kwargs):
        if name.endswith('.jpg'):
            with writer.SessionLocal() as db:
                db.execute(text('UPDATE print_files SET org_id=org_id WHERE id=111'));db.commit()
            checked.append(True)
        return actual(self,directory,name,expected,**kwargs)
    monkeypatch.setattr(module.EncodeJournal,'_verify',verify)
    assert encode(encoder) and checked and orders==[[1,3,10]]


def test_output_original_inode_replacement_refuses_exact_replay(encoder,monkeypatch):
    writer,tid,root,nonce,module,orders=encoder;actual=module.EncodeJournal._advance
    def cut(self,identifier,expected,target,**kwargs):
        result=actual(self,identifier,expected,target,**kwargs)
        if target=='output_ready':raise RuntimeError('Interrupted before publish')
        return result
    monkeypatch.setattr(module.EncodeJournal,'_advance',cut)
    assert not encode(encoder)
    private=next(root.glob('publish_*/output.mp4'));data=private.read_bytes();private.rename(private.with_name('preserved-output.mp4'));private.write_bytes(data);private.chmod(0o600)
    monkeypatch.setattr(module.EncodeJournal,'_advance',actual)
    assert not encode(encoder) and orders==[[1,3,10]] and not list(root.glob('encode_*.mp4'))


def test_insufficient_terminal_commit_ack_loss_is_exact_and_single_audit(frame_writer,review_db,monkeypatch):
    writer,tid,root,nonce=frame_writer
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
    original=Session.commit;fired=[]
    def ack(db):
        status=db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
        original(db)
        if status=='failed' and not fired:fired.append(True);raise RuntimeError('Failed terminal ACK lost')
    monkeypatch.setattr(Session,'commit',ack)
    assert not writer.encode_timelapse(tid,printer_id=77,nonce=nonce,job_id=444) and fired==[True]
    assert review_db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='failed'
    assert review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='timelapse_encode_failed'")).scalar_one()==1
