"""Real SQLite/journal/FD acceptance. Codec and probe seams are synthetic."""
import os
import pytest
from sqlalchemy import text,select
from sqlalchemy.orm import Session
from starlette.requests import Request
from core.audit_actor import RequestAuditScope
from core.auth import create_access_token
from core.timelapse_recipes import make_recipe
from core.timelapse_derivative_timing import frame_plan
from core.timelapse_media_worker import file_facts
from core.timelapse_publication_schema import publications,retained
from tests.test_contracts.test_timelapse_lifetime_membership import prepared_source,encoder,frame_writer,review_db,owned_runtime

@pytest.fixture
def edit(prepared_source,monkeypatch):
    from modules.archives import timelapse_edit as service
    from modules.archives.timelapse_action_authority import capture_action_credential
    from core.sender_process_group import GroupRetirement
    engine,root,tid,_,nonce=prepared_source
    with Session(engine) as db:
        username=db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
        token=create_access_token({'sub':username,'role':'admin'})
        request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())], 'query_string':b'',
                         'odin.private.audit_actor_scope':RequestAuditScope(None)})
        evidence=capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer',purpose='edit')
    def probe(guard,fd,facts,deadline,identifier):
        payload=os.pread(fd,1000,0)
        count=int(payload.split(b':')[1]) if payload.startswith(b'synthetic:') else 3
        assert file_facts(fd)==facts
        return {'count':count,'width':40,'height':40}
    class Group:
        def __init__(self,*a,**kw):self._media_capability=kw['media']
        def retire(self):return GroupRetirement(True,'explicit synthetic codec seam',channels_closed=True,media_copies_closed=True)
    def codec(group,manifest,**kw):
        recipe=service.recipe_from_row({'kind':manifest['kind'],**manifest['recipe']})
        source=probe(group._media_capability.guard,kw['source_fd'],manifest['source'],kw['deadline'],manifest['operation_id'])
        count=len(frame_plan(recipe,source['count']))
        os.write(kw['output_fd'],('synthetic:'+str(count)).encode());os.fsync(kw['output_fd'])
        return {'operation_id':manifest['operation_id'],'kind':manifest['kind'],'recipe':manifest['recipe'],
                'source_frame_count':source['count'],'frame_count':count,'output':file_facts(kw['output_fd'])}
    monkeypatch.setattr(service,'probe_owned_media',probe);monkeypatch.setattr(service,'OwnedSenderGroup',Group)
    monkeypatch.setattr(service,'complete_derivative_transport',codec)
    return service,engine,root,tid,nonce,evidence

def run(edit,identifier='a'*32,recipe=None,nonce=None):
    service,engine,root,tid,original,evidence=edit
    return service.execute_edit(engine,root,tid,evidence,identifier,nonce=nonce or original,recipe=recipe or make_recipe('trim',start_seconds=0,end_seconds=.09))

def test_trim_retains_original_and_completed_replay_before_stale_precondition(edit):
    service,engine,root,tid,nonce,evidence=edit
    with Session(engine) as db:old=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    original=(root/old).read_bytes()
    receipt=run(edit)
    assert receipt['frame_count']==3 and receipt['result_nonce']!=nonce and receipt['playback_available']
    assert (root/old).read_bytes()==original
    assert run(edit)==receipt
    with Session(engine) as db:
        assert db.execute(select(retained.c.version_nonce)).scalar_one()==nonce
        assert db.execute(select(publications.c.operation_id).where(publications.c.kind=='trim')).scalar_one()=='a'*32

def test_speed_creates_separate_lifetime_and_replays_without_duplicate(edit):
    receipt=run(edit,recipe=make_recipe('speed',multiplier=.5))
    assert receipt['result_tid']!=edit[3] and receipt['result_lifetime']==receipt['result_nonce'] and receipt['frame_count']==6
    assert run(edit,recipe=make_recipe('speed',multiplier=.5))==receipt

def test_completed_old_trim_metadata_has_no_newer_playback(edit):
    first=run(edit)
    second=run(edit,'b'*32,nonce=first['result_nonce'],recipe=make_recipe('trim',start_seconds=0,end_seconds=.05))
    replay=run(edit)
    assert replay['result_nonce']==first['result_nonce'] and replay['frame_count']==first['frame_count']
    assert not replay['playback_available'] and replay['playback_unavailable_reason']=='version_superseded'
    assert second['playback_available']

def test_operation_recipe_collision_rejects(edit):
    run(edit)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:run(edit,recipe=make_recipe('speed',multiplier=2))
    assert caught.value.status_code==409

@pytest.mark.parametrize('boundary',['private_created','worker_admitted','output_ready','publish_pending','published','complete'])
def test_phase_failure_preserves_original_and_only_durable_output_resumes(edit,monkeypatch,boundary):
    service,engine,root,tid,nonce,evidence=edit
    with Session(engine) as db:filename=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    before=(root/filename).read_bytes();actual=service.EditJournal.advance;fired=[]
    def fail(self,row,saved,target,**kw):
        if target==boundary and not fired:fired.append(True);raise RuntimeError('owned phase failure')
        return actual(self,row,saved,target,**kw)
    monkeypatch.setattr(service.EditJournal,'advance',fail)
    with pytest.raises(RuntimeError):run(edit)
    assert fired and (root/filename).read_bytes()==before
    monkeypatch.setattr(service.EditJournal,'advance',actual)
    if boundary in ('private_created','worker_admitted','output_ready'):
        with pytest.raises(service.EditUnavailable):run(edit)
    else:
        receipt=run(edit)
        assert receipt['phase']=='complete' and receipt['result_tid']==tid
    with Session(engine) as db:
        assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE kind='trim'")).scalar_one()==1

def test_complete_commit_lost_ack_resolves_exact_receipt(edit,monkeypatch):
    service,engine,root,tid,nonce,evidence=edit
    actual=Session.commit;fired=[]
    def commit(db):
        phase=db.execute(text("SELECT phase FROM timelapse_publications WHERE operation_id=:id"),{'id':'a'*32}).scalar_one_or_none()
        actual(db)
        if phase=='complete' and not fired:fired.append(True);raise RuntimeError('owned completed ACK loss')
    monkeypatch.setattr(Session,'commit',commit)
    receipt=run(edit)
    assert fired and receipt['phase']=='complete'
    assert run(edit)==receipt

def test_authority_revoked_during_codec_never_publishes(edit,monkeypatch):
    service,engine,root,tid,nonce,evidence=edit;actual=service.complete_derivative_transport
    def codec(*a,**kw):
        result=actual(*a,**kw)
        with engine.begin() as c:c.execute(text('UPDATE users SET is_active=FALSE WHERE id=3'))
        return result
    monkeypatch.setattr(service,'complete_derivative_transport',codec)
    with pytest.raises(Exception):run(edit)
    with Session(engine) as db:
        assert db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()==nonce
        assert db.execute(text("SELECT phase FROM timelapse_publications WHERE operation_id=:id"),{'id':'a'*32}).scalar_one()=='worker_admitted'
    assert not list(root.glob('trim_*.mp4'))

@pytest.mark.parametrize('fault',['output','private_parent','creation'])
def test_durable_output_resume_requires_exact_creation_bundle(edit,monkeypatch,fault):
    service,engine,root,tid,nonce,evidence=edit;actual=service.EditJournal.advance
    def stop(self,row,saved,target,**kw):
        if target=='publish_pending':raise RuntimeError('owned pause after durable output')
        return actual(self,row,saved,target,**kw)
    monkeypatch.setattr(service.EditJournal,'advance',stop)
    with pytest.raises(RuntimeError):run(edit)
    monkeypatch.setattr(service.EditJournal,'advance',actual)
    private=root/('publish_'+'a'*32)
    if fault=='output':
        old=private/'output.mp4';old.rename(private/'held-original.mp4');old.write_bytes(b'unknown replacement');old.chmod(0o600)
    elif fault=='private_parent':
        private.rename(root/'held-private');private.mkdir(mode=0o700);(private/'output.mp4').write_bytes(b'unknown replacement')
    else:
        # Same inode with altered payload also violates immutable creation/output
        # facts; no complete-live version receipt exists yet to excuse it.
        (private/'output.mp4').write_bytes(b'changed bytes')
    before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file() and '.locks' not in p.parts}
    with pytest.raises(Exception):run(edit)
    assert before=={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file() and '.locks' not in p.parts}
    with Session(engine) as db:assert db.execute(text("SELECT phase FROM timelapse_publications WHERE operation_id=:id"),{'id':'a'*32}).scalar_one()=='output_ready'

def test_unknown_admitted_private_directory_is_not_adopted(edit):
    service,engine,root,tid,nonce,evidence=edit
    private=root/('publish_'+'a'*32);private.mkdir(mode=0o700);original=private/'output.mp4';original.write_bytes(b'unknown prior private bytes')
    with pytest.raises(FileExistsError):run(edit)
    assert original.read_bytes()==b'unknown prior private bytes'
    with Session(engine) as db:assert db.execute(text("SELECT phase FROM timelapse_publications WHERE operation_id=:id"),{'id':'a'*32}).scalar_one()=='admitted'

def test_retired_or_reused_numeric_id_does_not_replay_old_version(edit):
    service,engine,root,tid,nonce,evidence=edit
    receipt=run(edit)
    with engine.begin() as c:
        c.execute(text('DELETE FROM timelapses WHERE id=:id'),{'id':tid})
        c.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,ownership_operation_id) VALUES(:id,77,444,'new-life.mp4','ready',NULL)"),{'id':tid})
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:run(edit)
    assert caught.value.status_code==404
    assert receipt['result_nonce'] is not None

def test_successful_publication_close_uncertainty_preserves_receipt_and_never_retries_fd(edit,monkeypatch):
    service,engine,root,tid,nonce,evidence=edit
    actual=os.close;uncertain=[];calls=[];completed=[];advance=service.EditJournal.advance
    def transition(self,row,saved,target,**kw):
        result=advance(self,row,saved,target,**kw)
        if target=='complete':completed.append(True)
        return result
    def close(fd):
        # No extra DB checkout inside close: fixture PRAGMA enforcement and
        # caller transaction must be unaffected by fault instrumentation.
        if completed and not uncertain:
            stat=os.fstat(fd)
            import stat as mode
            if mode.S_ISREG(stat.st_mode) and stat.st_size>0 and os.pread(fd,10,0).startswith(b'synthetic:'):
                uncertain.append(fd);calls.append(fd);actual(fd);raise OSError('owned close acknowledgement uncertainty')
        if uncertain:calls.append(fd)
        return actual(fd)
    monkeypatch.setattr(service.EditJournal,'advance',transition);monkeypatch.setattr(os,'close',close)
    with pytest.raises(OSError):run(edit)
    assert uncertain and calls.count(uncertain[0])==1
    monkeypatch.setattr(os,'close',actual)
    receipt=run(edit)
    assert receipt['phase']=='complete'
    with Session(engine) as db:
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='timelapse_edit_complete'")).scalar_one()==1
