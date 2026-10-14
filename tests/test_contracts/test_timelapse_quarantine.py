"""Native owned directory checks, sync order and descriptor failure cleanup."""
import os
from dataclasses import replace
import pytest
from core.timelapse_manifest import _root
from core.timelapse_quarantine import quarantine_directory,QuarantineDirectoryUnavailable


@pytest.fixture
def owned_quarantine(tmp_path):
    root=tmp_path/'owned';root.mkdir(mode=0o700)
    with _root(root) as (_,identity):pass
    return root,identity


def test_native_directory_create_syncs_before_yield_and_reopens_exact_identity(owned_quarantine,monkeypatch):
    root,identity=owned_quarantine;actual=os.fsync;synced=[]
    def sync(fd):synced.append(os.fstat(fd).st_ino);actual(fd)
    monkeypatch.setattr(os,'fsync',sync)
    with quarantine_directory(root,identity,create=True) as (fd,captured):
        assert synced==[captured.inode,identity.inode]
        assert os.get_inheritable(fd) is False
        assert captured.mode&0o777==0o700
    with quarantine_directory(root,identity,captured) as (_,again):assert captured.inode==again.inode
    assert synced==[captured.inode,identity.inode]


def test_root_identity_refusal_precedes_directory_creation(owned_quarantine):
    root,identity=owned_quarantine
    with pytest.raises(QuarantineDirectoryUnavailable):
        with quarantine_directory(root,replace(identity,inode=identity.inode+1),create=True):pass
    assert not (root/'.retirement').exists()


@pytest.mark.parametrize('damage',['symlink','file','mode'])
def test_existing_unsafe_entry_is_preserved(owned_quarantine,damage):
    root,identity=owned_quarantine;folder=root/'.retirement'
    if damage=='symlink':
        (root/'other').mkdir();folder.symlink_to(root/'other',target_is_directory=True)
    elif damage=='file':folder.write_bytes(b'owned contents')
    else:folder.mkdir(mode=0o755)
    before=folder.lstat()
    with pytest.raises(QuarantineDirectoryUnavailable):
        with quarantine_directory(root,identity,create=True):pass
    after=folder.lstat();assert (before.st_ino,before.st_mode)==(after.st_ino,after.st_mode)


def test_replacement_after_open_is_refused_on_exit(owned_quarantine):
    root,identity=owned_quarantine;folder=root/'.retirement'
    with pytest.raises(QuarantineDirectoryUnavailable):
        with quarantine_directory(root,identity,create=True):
            folder.rename(root/'held');folder.mkdir(mode=0o700)
    assert (root/'held').is_dir() and folder.is_dir()


@pytest.mark.parametrize('fault',['open','fsync','body'])
def test_native_failure_closes_all_private_descriptors(owned_quarantine,monkeypatch,fault):
    root,identity=owned_quarantine;actual_open=os.open;actual_close=os.close;outstanding=set()
    def opened(name,*args,**kwargs):
        if fault=='open' and name=='.retirement':raise OSError('owned open refusal')
        fd=actual_open(name,*args,**kwargs);outstanding.add(fd);return fd
    def closed(fd):actual_close(fd);outstanding.discard(fd)
    monkeypatch.setattr(os,'open',opened);monkeypatch.setattr(os,'close',closed)
    if fault=='fsync':monkeypatch.setattr(os,'fsync',lambda *a:(_ for _ in ()).throw(OSError('owned sync refusal')))
    expected=RuntimeError if fault=='body' else QuarantineDirectoryUnavailable
    with pytest.raises(expected):
        with quarantine_directory(root,identity,create=True):
            if fault=='body':raise RuntimeError('owned caller refusal')
    assert outstanding==set()


def test_caller_error_identity_preserved_and_cleanup_failure_overrides_once(owned_quarantine,monkeypatch):
    root,identity=owned_quarantine;original=RuntimeError('owned body failure')
    with pytest.raises(RuntimeError) as result:
        with quarantine_directory(root,identity,create=True):raise original
    assert result.value is original
    actual=os.close;quarantine_fd=None;attempts=0
    def close(fd):
        nonlocal attempts
        actual(fd)
        if fd==quarantine_fd:
            attempts+=1;raise OSError('owned uncertain close acknowledgment')
    monkeypatch.setattr(os,'close',close)
    with pytest.raises(QuarantineDirectoryUnavailable):
        with quarantine_directory(root,identity) as (fd,_):
            quarantine_fd=fd;raise original
    assert attempts==1
