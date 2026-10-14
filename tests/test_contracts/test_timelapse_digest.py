"""Read-only digest evidence binds captured file and intermediate directories."""
import hashlib
import os
from dataclasses import replace
import pytest
from core.timelapse_manifest import capture_manifest


@pytest.fixture
def owned_digest(tmp_path):
    root=tmp_path/'owned';root.mkdir()
    (root/'printer_9').mkdir()
    video=root/'printer_9/video.mp4';video.write_bytes(b'owned synthetic content')
    (root/'199/frames').mkdir(parents=True)
    manifest=capture_manifest(root,'printer_9/video.mp4','199')
    identity=next(value for value in manifest.objects if value.relative_path=='printer_9/video.mp4')
    return root,video,manifest,identity


def digest(fixture):
    from core.timelapse_digest import capture_file_digest
    root,_,manifest,identity=fixture
    return capture_file_digest(root,manifest.root,identity,manifest.parents)


def test_exact_owned_bytes_digest_does_not_change_media(owned_digest):
    _,video,_,identity=owned_digest
    result=digest(owned_digest)
    assert result==hashlib.sha256(b'owned synthetic content').hexdigest()
    after=video.stat()
    assert (after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)==(identity.inode,identity.byte_count,identity.mtime_ns,identity.ctime_ns)


@pytest.mark.parametrize('change',['replacement','same_size_change','truncate','growth','symlink','hardlink'])
def test_changed_file_refuses_without_touching_replacement(owned_digest,change):
    from core.timelapse_digest import DigestUnavailable
    root,video,_,_=owned_digest
    if change=='replacement':
        video.rename(root/'old.mp4');video.write_bytes(b'owned synthetic content')
    elif change=='same_size_change':video.write_bytes(b'foreign synthetic bytes')
    elif change=='truncate':video.write_bytes(b'x')
    elif change=='growth':
        with video.open('ab') as stream:stream.write(b'new')
    elif change=='symlink':
        video.rename(root/'old.mp4');video.symlink_to(root/'old.mp4')
    else:os.link(video,root/'alias.mp4')
    before=video.lstat()
    with pytest.raises(DigestUnavailable,match='^Media digest could not be confirmed$'):digest(owned_digest)
    after=video.lstat()
    assert (before.st_ino,before.st_size,before.st_ctime_ns)==(after.st_ino,after.st_size,after.st_ctime_ns)


def test_replaced_parent_containing_original_file_refuses(owned_digest):
    from core.timelapse_digest import DigestUnavailable
    root,video,_,_=owned_digest
    (root/'printer_9').rename(root/'old_parent')
    (root/'printer_9').mkdir()
    (root/'old_parent/video.mp4').rename(video)
    with pytest.raises(DigestUnavailable):digest(owned_digest)


def test_missing_parent_evidence_refuses(owned_digest):
    from core.timelapse_digest import capture_file_digest,DigestUnavailable
    root,_,manifest,identity=owned_digest
    with pytest.raises(DigestUnavailable):capture_file_digest(root,manifest.root,identity,())


def test_root_identity_mismatch_refuses(owned_digest):
    from core.timelapse_digest import capture_file_digest,DigestUnavailable
    root,_,manifest,identity=owned_digest
    with pytest.raises(DigestUnavailable):capture_file_digest(root,replace(manifest.root,inode=manifest.root.inode+1),identity,manifest.parents)


def test_file_changed_during_stream_refuses(owned_digest,monkeypatch):
    from core.timelapse_digest import DigestUnavailable
    root,video,_,_=owned_digest
    actual=os.read;changed=False
    def read(descriptor,size):
        nonlocal changed
        result=actual(descriptor,size)
        if not changed:
            changed=True
            with video.open('ab') as stream:stream.write(b'changed during digest')
        return result
    monkeypatch.setattr(os,'read',read)
    with pytest.raises(DigestUnavailable):digest(owned_digest)
    assert video.read_bytes().endswith(b'changed during digest')


@pytest.mark.parametrize('limit',[0,1,True,-1,2**63])
def test_byte_limit_refuses_before_open(owned_digest,monkeypatch,limit):
    from core.timelapse_digest import capture_file_digest,DigestUnavailable
    root,_,manifest,identity=owned_digest
    monkeypatch.setattr(os,'open',lambda *a,**k:pytest.fail('Opened media despite invalid/insufficient bound'))
    with pytest.raises(DigestUnavailable):capture_file_digest(root,manifest.root,identity,manifest.parents,max_bytes=limit)


def test_deadline_refuses_before_open(owned_digest,monkeypatch):
    import core.timelapse_digest as module
    root,_,manifest,identity=owned_digest
    values=iter([100,131]);monkeypatch.setattr(module.time,'monotonic',lambda:next(values))
    monkeypatch.setattr(os,'open',lambda *a,**k:pytest.fail('Opened after deadline'))
    with pytest.raises(module.DigestUnavailable):module.capture_file_digest(root,manifest.root,identity,manifest.parents)


@pytest.mark.parametrize('fault',['early_eof','read_error','path_replacement','fifo_before_open','late_read'])
def test_mid_operation_failure_closes_every_opened_descriptor(owned_digest,monkeypatch,fault):
    from core.timelapse_digest import DigestUnavailable
    root,video,_,_=owned_digest
    actual_open=os.open;actual_close=os.close;actual_read=os.read
    outstanding=set();changed=False;expired=False
    if fault=='late_read':
        import core.timelapse_digest as module
        monkeypatch.setattr(module.time,'monotonic',lambda:100 if expired else 0)
    def opened(name,*args,**kwargs):
        nonlocal changed
        if fault=='fifo_before_open' and name=='video.mp4':
            video.unlink();os.mkfifo(video);changed=True
        fd=actual_open(name,*args,**kwargs);outstanding.add(fd);return fd
    def closed(fd):
        actual_close(fd);outstanding.discard(fd)
    def read(fd,size):
        nonlocal changed,expired
        if fault=='early_eof':return b''
        if fault=='read_error':raise OSError('owned injected read fault')
        block=actual_read(fd,size)
        if fault=='late_read':expired=True
        if fault=='path_replacement' and not changed:
            changed=True;video.rename(root/'held.mp4');video.write_bytes(b'owned synthetic content')
        return block
    monkeypatch.setattr(os,'open',opened);monkeypatch.setattr(os,'close',closed);monkeypatch.setattr(os,'read',read)
    with pytest.raises(DigestUnavailable):digest(owned_digest)
    assert outstanding==set()
    if fault in ('path_replacement','fifo_before_open'):assert changed


def test_excessive_parent_evidence_refuses_before_open(owned_digest,monkeypatch):
    from core.timelapse_digest import capture_file_digest,DigestUnavailable
    from core.timelapse_manifest import MAX_OBJECTS
    root,_,manifest,identity=owned_digest
    monkeypatch.setattr(os,'open',lambda *a,**k:pytest.fail('Opened despite excessive evidence'))
    with pytest.raises(DigestUnavailable):capture_file_digest(root,manifest.root,identity,(manifest.parents[0],)*(MAX_OBJECTS+1))
