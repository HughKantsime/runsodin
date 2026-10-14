"""Owned typed descriptor/recipe negatives; no camera, database or network."""
import os
import stat
import pytest
from core.media_process_capability import MediaEncodeCapability,identity,validate,prepare,worker_command
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_media_worker import checked_manifest,file_facts


@pytest.fixture
def media(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path/'frames').mkdir(mode=0o700)
    ns=os.open(tmp_path/'frames',os.O_RDONLY|os.O_DIRECTORY)
    out=os.open(tmp_path/'output.mp4',os.O_CREAT|os.O_EXCL|os.O_RDWR,0o600)
    with acquire_media_guard(tmp_path,1) as guard:
        cap=MediaEncodeCapability.from_receipts(guard,ns,out,identity(ns),identity(out))
        yield cap,ns,out,tmp_path
    os.close(ns);os.close(out)


@pytest.mark.parametrize('change',['bool','wrongrole','mode','inode','type','access','nonempty','append','alias'])
def test_capability_refuses_invalid_descriptors(media,change):
    cap,ns,out,root=media
    fd=out;facts=identity(out);role='output';extra=None
    if change=='bool':fd=True
    elif change=='wrongrole':role='arbitrary'
    elif change=='mode':facts['mode']=0o100644
    elif change=='inode':facts['inode']+=1
    elif change=='type':fd=ns;facts=identity(ns)
    elif change=='access':extra=os.open(root/'output.mp4',os.O_RDONLY);fd=extra
    elif change=='nonempty':os.write(out,b'x');facts=identity(out)
    elif change=='append':
        import fcntl
        fcntl.fcntl(out,fcntl.F_SETFL,os.O_APPEND)
    elif change=='alias':
        with pytest.raises(ValueError):MediaEncodeCapability.from_receipts(cap.guard,ns,cap.guard.fileno(),identity(ns),identity(cap.guard.fileno()))
        return
    try:
        with pytest.raises(ValueError):validate(fd,role,facts)
    finally:
        if extra is not None:os.close(extra)


def test_only_fixed_worker_command_and_validated_copies(media):
    cap,ns,out,root=media
    with pytest.raises(ValueError):prepare(cap,['ffmpeg','https://untrusted'])
    replacement,copies,record=prepare(cap,worker_command())
    try:
        assert len(copies)==3 and all(not os.get_inheritable(fd) for fd in copies)
        assert [identity(fd) for fd in copies]==list(cap.facts)
    finally:
        for fd in copies:os.close(fd)


@pytest.mark.parametrize('field,value',[('recipe_version',True),('encode_fps',29),('operation_id','https://bad'),('frames',[])])
def test_manifest_refuses_request_injection(field,value):
    data={'operation_id':'a'*32,'recipe_version':1,'encode_fps':30,'frames':[]};data[field]=value
    with pytest.raises(ValueError):checked_manifest(data)


def test_partially_written_output_never_reusable(media):
    cap,ns,out,root=media
    os.write(out,b'uncertain original evidence')
    with pytest.raises(ValueError):prepare(cap,worker_command())
    assert os.pread(out,100,0)==b'uncertain original evidence'


def test_anchor_startup_failure_closes_all_typed_copies(media,monkeypatch):
    import core.sender_process_group as sender
    cap,ns,out,root=media;copies=[];real=os.dup
    def tracked(fd):
        result=real(fd);copies.append(result);return result
    monkeypatch.setattr(os,'dup',tracked)
    monkeypatch.setattr(sender.subprocess,'Popen',lambda *args,**kwargs:(_ for _ in ()).throw(OSError('private failure')))
    a,b=os.pipe();c,d=os.pipe()
    try:
        with pytest.raises(sender.OwnedGroupStartupError) as caught:
            sender.OwnedSenderGroup(worker_command(),defer_start=True,channels={'input':a,'result':d},media=cap)
        assert not caught.value.retirement.group_empty and caught.value.retirement.media_copies_closed
        for fd in copies:
            with pytest.raises(OSError):os.fstat(fd)
        assert os.fstat(out).st_size==0
    finally:
        for fd in (a,b,c,d):os.close(fd)


def test_deferred_cancellation_retains_guard_then_confirms_group_cleanup(media):
    import time
    from core.sender_process_group import OwnedSenderGroup
    cap,ns,out,root=media;a,b=os.pipe();c,d=os.pipe();group=None
    try:
        group=OwnedSenderGroup(worker_command(),defer_start=True,channels={'input':a,'result':d},media=cap)
        assert os.fstat(out).st_size==0
        receipt=group.retire()
        assert receipt.group_empty and receipt.channels_closed and receipt.media_copies_closed
        assert not group.arm().command_may_have_started
    finally:
        if group:group.retire()
        for fd in (a,b,c,d):os.close(fd)


@pytest.mark.parametrize('fault',['group','channels','media','result','source','output','fsync'])
def test_output_gate_refuses_incomplete_or_changed_evidence(media,monkeypatch,fault):
    import json,time
    from core.media_process_capability import complete_media_transport
    from core.sender_process_group import GroupLaunch,GroupCompletion,GroupRetirement
    cap,ns,out,root=media;frames=[]
    for number in (1,3):
        name='frame_'+str(number).zfill(6)+'.jpg';(root/'frames'/name).write_bytes(b'original JPEG evidence');(root/'frames'/name).chmod(0o600)
        fd=os.open(root/'frames'/name,os.O_RDONLY)
        try:frames.append({'number':number,'name':name,'facts':file_facts(fd)})
        finally:os.close(fd)
    # Namespace incidental size may change as files are created; reissue live capability.
    cap=MediaEncodeCapability.from_receipts(cap.guard,ns,out,identity(ns),identity(out))
    manifest={'operation_id':'a'*32,'recipe_version':1,'encode_fps':30,'frames':frames}
    a,b=os.pipe();c,d=os.pipe();parent=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    class ExactSyntheticGroup:
        _media_capability=cap
        def arm(self):
            os.write(out,b'private result evidence')
            result={'operation_id':'a'*32,'recipe_version':1,'encode_fps':30,'frame_count':2,'output':file_facts(out)}
            if fault=='result':result['operation_id']='b'*32
            os.write(d,(json.dumps(result)+'\n').encode())
            return GroupLaunch(True,True,'synthetic owned fixture')
        def wait_worker(self,deadline):
            if fault=='source':(root/'frames'/frames[0]['name']).write_bytes(b'changed')
            if fault=='output':os.pwrite(out,b'changed',0)
            return GroupCompletion(True,'synthetic exact exit')
        def retire(self):return GroupRetirement(fault!='group','synthetic receipt',channels_closed=fault!='channels',media_copies_closed=fault!='media')
    if fault=='fsync':monkeypatch.setattr(os,'fsync',lambda *args:(_ for _ in ()).throw(OSError('sync uncertain')))
    try:
        with pytest.raises((ValueError,OSError)):
            complete_media_transport(ExactSyntheticGroup(),manifest,input_writer=b,result_reader=c,
                namespace_fd=ns,output_fd=out,private_parent_fd=parent,private_parent_facts=identity(parent),deadline=time.monotonic()+2)
        assert os.fstat(out).st_ino==cap.facts[2]['inode'] and os.fstat(out).st_size>0
    finally:
        for fd in (a,b,c,d,parent):os.close(fd)


def test_backpressure_write_has_deadline_and_preserves_output(media):
    import time
    from core.timelapse_media_worker import write_bounded
    cap,ns,out,root=media;a,b=os.pipe();os.set_blocking(b,False)
    try:
        with pytest.raises(ValueError):write_bounded(b,b'x'*(1024*1024),time.monotonic()+.02)
        assert os.fstat(out).st_size==0
    finally:os.close(a);os.close(b)


def test_capability_facts_are_immutable_and_boolean_receipts_refuse(media):
    cap,ns,out,root=media
    with pytest.raises(TypeError):cap.facts[0]['inode']=1
    malformed=identity(ns);malformed['uid']=False
    with pytest.raises(ValueError):MediaEncodeCapability.from_receipts(cap.guard,ns,out,malformed,identity(out))


def test_duplicate_failure_closes_preceding_copy_without_launch(media,monkeypatch):
    cap,ns,out,root=media;real=os.dup;created=[]
    def fail_second(fd):
        if created:raise OSError('duplicate failure')
        value=real(fd);created.append(value);return value
    monkeypatch.setattr(os,'dup',fail_second)
    with pytest.raises(ValueError):prepare(cap,worker_command())
    with pytest.raises(OSError):os.fstat(created[0])


def test_unknown_duplicate_close_remains_in_uncertain_receipt(media,monkeypatch):
    import core.sender_process_group as sender
    cap,ns,out,root=media;real_dup=os.dup;real_close=os.close;created=[];failed=[]
    def duplicate(fd):
        value=real_dup(fd);created.append(value);return value
    def close(fd):
        if created and fd==created[0] and not failed:failed.append(fd);raise OSError('uncertain close')
        real_close(fd)
    monkeypatch.setattr(os,'dup',duplicate);monkeypatch.setattr(os,'close',close)
    a,b=os.pipe();c,d=os.pipe();group=None
    try:
        with pytest.raises(sender.OwnedGroupStartupError) as caught:
            sender.OwnedSenderGroup(worker_command(),defer_start=True,channels={'input':a,'result':d},media=cap)
        group=caught.value.group
        assert not caught.value.retirement.media_copies_closed
        assert not group.retire().media_copies_closed and group._uncertain_media_fds==failed
        assert os.fstat(failed[0]).st_ino==cap.facts[0]['inode']
    finally:
        if group:group.retire()
        # Synthetic close raised before OS close; fixture owns this known still-open copy.
        for fd in failed:real_close(fd)
        for fd in (a,b,c,d):real_close(fd)


def test_same_empty_inode_with_changed_creation_timestamps_refuses(media):
    cap,ns,out,root=media
    before=dict(cap.facts[2]);os.utime(root/'output.mp4',ns=(before['mtime_ns']+1,before['mtime_ns']+1))
    with pytest.raises(ValueError):prepare(cap,worker_command())
    assert os.fstat(out).st_size==0 and os.fstat(out).st_ino==before['inode']


def test_direct_capability_cannot_substitute_another_regular_guard(media):
    cap,ns,out,root=media
    forged=MediaEncodeCapability((out,ns,cap.guard.fileno()),(identity(out),identity(ns),identity(cap.guard.fileno())),cap.guard)
    with pytest.raises(ValueError):prepare(forged,worker_command())


@pytest.mark.parametrize('noisy_fd',[1,2])
def test_probe_stream_overflow_is_bounded_and_exact_child_stopped(monkeypatch,noisy_fd):
    import subprocess,sys,time
    import core.timelapse_media_worker as worker
    actual_popen=subprocess.Popen;actual_read=os.read;owned=[];reads=[];pipe_fds=set()
    def launch(*args,**kwargs):
        child=actual_popen(*args,**kwargs);owned.append(child)
        pipe_fds.update((child.stdout.fileno(),child.stderr.fileno()));return child
    def read(fd,count):
        value=actual_read(fd,count)
        if fd in pipe_fds:reads.append((fd,count,len(value)))
        return value
    monkeypatch.setattr(worker.subprocess,'Popen',launch);monkeypatch.setattr(os,'read',read)
    source='import os\nwhile True:os.write(%d,b"x"*65536)'%noisy_fd
    started=time.monotonic()
    with pytest.raises(ValueError,match='Media probe output limit'):
        worker.bounded_probe([sys.executable,'-c',source],pass_fds=(),deadline=started+2)
    assert time.monotonic()-started<2 and len(owned)==1
    assert owned[0].returncode is not None and owned[0].stdout.closed and owned[0].stderr.closed
    assert reads and max(count for _,count,_ in reads)<=4096
    assert max(sum(size for fd,_,size in reads if fd==target) for target in pipe_fds)==8193


def test_probe_small_both_streams_return_only_stdout():
    import sys,time
    from core.timelapse_media_worker import bounded_probe
    source='import os;os.write(1,b"verified probe");os.write(2,b"bounded diagnostic")'
    assert bounded_probe([sys.executable,'-c',source],pass_fds=(),deadline=time.monotonic()+2)==b'verified probe'


def test_probe_deadline_stops_exact_owned_child(monkeypatch):
    import subprocess,sys,time
    import core.timelapse_media_worker as worker
    actual=subprocess.Popen;owned=[]
    def launch(*args,**kwargs):
        child=actual(*args,**kwargs);owned.append(child);return child
    monkeypatch.setattr(worker.subprocess,'Popen',launch)
    with pytest.raises(ValueError,match='Media probe deadline'):
        worker.bounded_probe([sys.executable,'-c','import time;time.sleep(30)'],pass_fds=(),deadline=time.monotonic()+.1)
    assert owned[0].returncode is not None and owned[0].stdout.closed and owned[0].stderr.closed
