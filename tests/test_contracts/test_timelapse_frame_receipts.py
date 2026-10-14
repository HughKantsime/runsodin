"""Real files and durable frame phases; synthetic camera, no hardware."""
import os
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import text,select
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_frame_publication import frame_writer
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db
from core.timelapse_publication_schema import publications,facts,bindings
from core.timelapse_publication_validation import operation
from modules.archives.timelapse_frame_publication import FrameJournal


class Interrupted(BaseException):pass


def receipt(db):
    rows=[dict(row) for row in db.execute(select(publications).order_by(publications.c.frame_number)).mappings()]
    db.rollback()
    return rows


def test_complete_frame_receipt_matches_exact_file_and_validates(review_db,frame_writer):
    writer,tid,root,nonce=frame_writer
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0]
    assert (row['phase'],row['state'],row['frame_number'])==('complete','complete',1)
    operation(review_db,row)
    output=review_db.execute(select(facts).where(facts.c.operation_id==row['operation_id'],facts.c.role=='output')).mappings().one()
    value=(root/row['destination_path']).stat()
    assert value.st_ino==output['inode'] and value.st_ctime_ns==row['published_ctime_ns']
    assert output['digest_sha256']==row['payload_digest_sha256'] and value.st_size==row['payload_byte_count']
    assert (root/row['private_directory']).is_dir() and not (root/row['private_directory']/'output.jpg').exists()
    review_db.rollback()


@pytest.mark.parametrize('phase',['private_created','output_ready','publish_pending','published','complete'])
def test_restart_at_each_committed_phase_preserves_exact_receipt(review_db,frame_writer,monkeypatch,phase):
    writer,tid,root,nonce=frame_writer
    original=FrameJournal._advance
    def interrupt(self,identifier,expected,target,**kwargs):
        result=original(self,identifier,expected,target,**kwargs)
        if target==phase:raise Interrupted()
        return result
    monkeypatch.setattr(FrameJournal,'_advance',interrupt)
    with pytest.raises(Interrupted):writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0];assert row['phase']==phase
    operation(review_db,row);review_db.rollback()
    monkeypatch.setattr(FrameJournal,'_advance',original)
    if phase in ('output_ready','publish_pending','published'):
        review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
        @contextmanager
        def forbidden(*args,**kwargs):raise AssertionError('Recovery fetched a new camera frame');yield
        monkeypatch.setattr(writer.httpx,'stream',forbidden)
        assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
        current=receipt(review_db)[0]
        assert current['phase']=='complete' and current['destination_path']==row['destination_path']
        assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
        review_db.rollback()
    elif phase=='private_created':
        assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
        current=receipt(review_db)
        assert current[0]['state']=='blocked' and current[0]['phase']=='private_created'
        assert current[1]['frame_number']==2 and current[1]['phase']=='complete'
        assert (root/row['private_directory']/'output.jpg').read_bytes()==b''
    else:
        review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));review_db.commit()
        assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
        assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
        review_db.rollback()


@pytest.mark.parametrize('phase',['admitted','private_created','output_ready','publish_pending','published','complete'])
def test_lost_commit_ack_reloads_same_phase_and_counts_once(review_db,frame_writer,monkeypatch,phase):
    writer,tid,root,nonce=frame_writer
    engine=review_db.get_bind();lost=[]
    class AckSession(Session):
        def commit(self):
            values=self.execute(select(publications.c.phase)).scalars().all()
            super().commit()
            if phase in values and not lost:
                lost.append(True);raise RuntimeError('Synthetic lost acknowledgment')
    monkeypatch.setattr(writer,'SessionLocal',lambda:AckSession(engine))
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    rows=receipt(review_db)
    assert lost and len(rows)==1 and rows[0]['phase']=='complete'
    assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
    review_db.rollback()


def test_partial_payload_is_preserved_blocked_never_reconstructed(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer
    from modules.archives import timelapse_frame_publication as journal
    original=os.write;calls=[]
    def partial(fd,value):
        if calls:raise OSError('Partial payload interruption')
        calls.append(True);return original(fd,value[:5])
    monkeypatch.setattr(journal.os,'write',partial)
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0];path=root/row['private_directory']/'output.jpg';original_inode=path.stat().st_ino;original_bytes=path.read_bytes()
    assert len(original_bytes)==5 and row['phase']=='private_created'
    monkeypatch.setattr(journal.os,'write',original)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert path.stat().st_ino==original_inode and path.read_bytes()==original_bytes
    assert receipt(review_db)[0]['state']=='blocked'


def test_unknown_private_creation_is_not_adopted(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer
    original=FrameJournal._advance
    def interrupt(self,identifier,expected,target,**kwargs):
        if target=='private_created':raise Interrupted()
        return original(self,identifier,expected,target,**kwargs)
    monkeypatch.setattr(FrameJournal,'_advance',interrupt)
    with pytest.raises(Interrupted):writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0];path=root/row['private_directory']/'output.jpg';inode=path.stat().st_ino
    assert row['phase']=='admitted'
    monkeypatch.setattr(FrameJournal,'_advance',original)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert path.stat().st_ino==inode and path.read_bytes()==b''
    assert receipt(review_db)[0]['state']=='blocked'


def test_occupied_destination_is_preserved_and_never_counted(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer
    original=FrameJournal._advance
    def occupy(self,identifier,expected,target,**kwargs):
        result=original(self,identifier,expected,target,**kwargs)
        if target=='publish_pending':
            row,_=result;(root/row['destination_path']).write_bytes(b'unowned destination')
        return result
    monkeypatch.setattr(FrameJournal,'_advance',occupy)
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0]
    assert row['phase']=='publish_pending' and (root/row['destination_path']).read_bytes()==b'unowned destination'
    assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==0
    review_db.rollback()


def test_renamed_destination_reconciles_after_directory_sync_failure(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer
    from modules.archives import timelapse_frame_publication as journal
    original_rename=journal.rename_noreplace;original_sync=os.fsync;renamed=[];failed=[]
    def rename(*args):original_rename(*args);renamed.append(True)
    def sync(fd):
        if renamed and not failed:failed.append(True);raise OSError('Directory sync interruption')
        return original_sync(fd)
    monkeypatch.setattr(journal,'rename_noreplace',rename);monkeypatch.setattr(journal.os,'fsync',sync)
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0];assert row['phase']=='publish_pending';inode=(root/row['destination_path']).stat().st_ino
    monkeypatch.setattr(journal.os,'fsync',original_sync)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert (root/row['destination_path']).stat().st_ino==inode and receipt(review_db)[0]['phase']=='complete'


def test_payload_io_runs_with_no_db_transaction(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer;engine=review_db.get_bind();sessions=[]
    def factory():
        db=Session(engine);sessions.append(db);return db
    monkeypatch.setattr(writer,'SessionLocal',factory)
    from modules.archives import timelapse_frame_publication as journal
    original_write=os.write;original_verify=FrameJournal._verify
    def write(fd,payload):
        assert not any(db.in_transaction() for db in sessions)
        return original_write(fd,payload)
    def verify(self,*args,**kwargs):
        assert not any(db.in_transaction() for db in sessions)
        return original_verify(self,*args,**kwargs)
    monkeypatch.setattr(journal.os,'write',write);monkeypatch.setattr(FrameJournal,'_verify',verify)
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)


@pytest.mark.parametrize('call,phase',[(3,'admitted'),(4,'admitted'),(5,'admitted'),(6,'private_created'),(7,'private_created'),(8,'publish_pending'),(9,'publish_pending')])
def test_each_creation_output_and_publish_sync_failure_preserves_phase(review_db,frame_writer,monkeypatch,call,phase):
    writer,tid,root,nonce=frame_writer
    from modules.archives import timelapse_frame_publication as journal
    original=os.fsync;seen=[]
    def fail(fd):
        seen.append(fd)
        if len(seen)==call:raise OSError('Synthetic fsync failure')
        return original(fd)
    monkeypatch.setattr(journal.os,'fsync',fail)
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0];assert row['phase']==phase
    operation(review_db,row);review_db.rollback()
    monkeypatch.setattr(journal.os,'fsync',original)
    before=list(root.glob('publish_*/output.jpg'))
    originals={path:(path.stat().st_ino,path.read_bytes()) for path in before}
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    if phase in ('admitted','private_created'):
        for path,expected in originals.items():assert (path.stat().st_ino,path.read_bytes())==expected
        assert receipt(review_db)[0]['state']=='blocked'
    else:assert receipt(review_db)[0]['phase']=='complete'


def test_complete_commit_rollback_recovers_count_once(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer;engine=review_db.get_bind();failed=[]
    class RefusedCommit(Session):
        def commit(self):
            if 'complete' in self.execute(select(publications.c.phase)).scalars().all() and not failed:
                failed.append(True);raise RuntimeError('Commit refused before durable count')
            super().commit()
    monkeypatch.setattr(writer,'SessionLocal',lambda:RefusedCommit(engine))
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert receipt(review_db)[0]['phase']=='published'
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
    review_db.rollback()


def test_unknown_final_frame_never_gets_adopted(review_db,frame_writer):
    writer,tid,root,nonce=frame_writer
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    namespace=next(root.glob('frames_*'));unknown=namespace/'frame_000099.jpg';unknown.write_bytes(b'unknown')
    inode=unknown.stat().st_ino
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert len(receipt(review_db))==1 and unknown.stat().st_ino==inode and unknown.read_bytes()==b'unknown'


def test_counter_exhaustion_refuses_without_wrap(review_db,frame_writer):
    writer,tid,root,nonce=frame_writer
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    review_db.execute(bindings.update().where(bindings.c.lifetime_nonce==nonce).values(next_number=(1<<63)-1));review_db.commit()
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert len(receipt(review_db))==1
    assert review_db.execute(select(bindings.c.next_number)).scalar_one()==(1<<63)-1
    review_db.rollback()


def test_new_output_ready_facts_are_not_created_after_job_ends(review_db,frame_writer,monkeypatch):
    writer,tid,root,nonce=frame_writer;original=FrameJournal._advance
    def ended(self,identifier,expected,target,**kwargs):
        if target=='output_ready':
            with Session(review_db.get_bind()) as db:db.execute(text("UPDATE jobs SET status='completed' WHERE id=444"));db.commit()
        return original(self,identifier,expected,target,**kwargs)
    monkeypatch.setattr(FrameJournal,'_advance',ended)
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    row=receipt(review_db)[0];assert row['phase']=='private_created'
    assert not list(root.glob('frames_*/*.jpg'))
    assert not writer.capture_frame(77,tid,nonce=nonce,job_id=444)
    assert receipt(review_db)[0]['state']=='blocked'


@pytest.mark.parametrize('failure',['after_success','camera','complete_ack'])
def test_namespace_close_uncertainty_refuses_ack_preserves_receipt_without_retry(review_db,frame_writer,monkeypatch,failure):
    writer,tid,root,nonce=frame_writer
    from modules.archives import timelapse_frame_capture as capture
    original_open=capture.bind_or_open_namespace;original_close=os.close;owned=[];attempts=[]
    def namespace(*args,**kwargs):
        descriptor=original_open(*args,**kwargs);owned.append(descriptor);return descriptor
    def uncertain_close(descriptor):
        if owned and descriptor==owned[0]:
            attempts.append(descriptor)
            # Simulate a successful close whose caller receives uncertainty:
            # retrying this number could close another owner's later descriptor.
            original_close(descriptor)
            raise OSError('Synthetic close result uncertainty')
        return original_close(descriptor)
    monkeypatch.setattr(capture,'bind_or_open_namespace',namespace)
    monkeypatch.setattr(capture.os,'close',uncertain_close)
    if failure=='camera':
        @contextmanager
        def camera(*args,**kwargs):raise RuntimeError('Synthetic transport refusal');yield
        monkeypatch.setattr(writer.httpx,'stream',camera)
    elif failure=='complete_ack':
        engine=review_db.get_bind();lost=[]
        class LostAck(Session):
            def commit(self):
                complete='complete' in self.execute(select(publications.c.phase)).scalars().all()
                super().commit()
                if complete and not lost:lost.append(True);raise RuntimeError('Synthetic complete ACK loss')
        monkeypatch.setattr(writer,'SessionLocal',lambda:LostAck(engine))
    assert writer.capture_frame(77,tid,nonce=nonce,job_id=444) is False
    assert attempts==[owned[0]]
    rows=receipt(review_db)
    if failure=='camera':assert rows==[]
    else:
        assert len(rows)==1 and rows[0]['phase']=='complete'
        operation(review_db,rows[0])
        assert review_db.execute(text('SELECT frame_count FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==1
        assert len(list(root.glob('frames_*/*.jpg')))==1
    review_db.rollback()
