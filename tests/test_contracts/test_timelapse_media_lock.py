"""Owned stable-file contention and real child descriptor lifetime."""
import importlib
import os
import subprocess
import sys
import pytest

def module():return importlib.import_module('core.timelapse_media_lock')


def test_root_namespace_contends_even_for_different_timelapse_ids(tmp_path):
    implementation=module()
    with implementation.acquire_media_guard(tmp_path,7):
        with pytest.raises(implementation.MediaGuardUnavailable):implementation.acquire_media_guard(tmp_path,7)
        with pytest.raises(implementation.MediaGuardUnavailable):implementation.acquire_media_guard(tmp_path,8)
    with implementation.acquire_media_guard(tmp_path,7):pass
    assert (tmp_path/'.locks/media.lock').is_file()


def test_child_keeps_lock_after_parent_close_until_actual_exit(tmp_path):
    implementation=module();guard=implementation.acquire_media_guard(tmp_path,7)
    child=None
    try:
        child=subprocess.Popen([sys.executable,'-c','import os,sys;os.fstat(int(sys.argv[1]));print("ready",flush=True);sys.stdin.buffer.read(1)',str(guard.fileno())],pass_fds=(guard.fileno(),),stdin=subprocess.PIPE,stdout=subprocess.PIPE)
        assert child.stdout.readline()==b'ready\n'
        guard.close()
        with pytest.raises(implementation.MediaGuardUnavailable):implementation.acquire_media_guard(tmp_path,7)
        child.stdin.write(b'x');child.stdin.flush();assert child.wait(timeout=5)==0
        with implementation.acquire_media_guard(tmp_path,7):pass
    finally:
        guard.close()
        if child is not None:
            if child.poll() is None:child.kill();child.wait(timeout=5)
            child.stdin.close();child.stdout.close()

@pytest.mark.parametrize('identifier',[0,-1,True,'7',2**63])
def test_unknown_target_identity_refuses(tmp_path,identifier):
    implementation=module()
    with pytest.raises(implementation.MediaGuardUnavailable):implementation.acquire_media_guard(tmp_path,identifier)
    assert not (tmp_path/'.locks').exists()

@pytest.mark.parametrize('unsafe',['symlink','hardlink','permissions'])
def test_existing_unsafe_lock_entry_is_not_adopted(tmp_path,unsafe):
    implementation=module();folder=tmp_path/'.locks';folder.mkdir(mode=0o700)
    lock=folder/'media.lock'
    if unsafe=='symlink':
        original=tmp_path/'owned-external';original.write_bytes(b'owned');lock.symlink_to(original)
    else:
        lock.write_bytes(b'')
        lock.chmod(0o600)
        if unsafe=='hardlink':os.link(lock,folder/'alias')
        else:lock.chmod(0o666)
    with pytest.raises(implementation.MediaGuardUnavailable):implementation.acquire_media_guard(tmp_path,7)

def test_lock_file_identity_is_stable_across_acquisitions(tmp_path):
    implementation=module()
    with implementation.acquire_media_guard(tmp_path,7) as guard:
        assert os.get_inheritable(guard.fileno()) is False
        inode=os.fstat(guard.fileno()).st_ino
    with implementation.acquire_media_guard(tmp_path,8) as guard:assert os.fstat(guard.fileno()).st_ino==inode

def test_closed_guard_cannot_be_reentered(tmp_path):
    implementation=module();guard=implementation.acquire_media_guard(tmp_path,7);guard.close()
    with pytest.raises(implementation.MediaGuardUnavailable):
        with guard:pass

@pytest.mark.parametrize('unsafe',['folder_symlink','folder_permissions','nonempty_file'])
def test_unsafe_namespace_or_nonempty_file_refuses(tmp_path,unsafe):
    implementation=module();folder=tmp_path/'.locks'
    if unsafe=='folder_symlink':
        outside=tmp_path/'outside';outside.mkdir();folder.symlink_to(outside,target_is_directory=True)
    else:
        folder.mkdir(mode=0o700)
        if unsafe=='folder_permissions':folder.chmod(0o777)
        else:
            (folder/'media.lock').write_bytes(b'owned retained data');(folder/'media.lock').chmod(0o600)
    with pytest.raises(implementation.MediaGuardUnavailable):implementation.acquire_media_guard(tmp_path,7)

def test_folder_close_failure_is_not_retried_on_reused_descriptor(tmp_path,monkeypatch):
    import errno
    implementation=module();original_open=os.open;original_close=os.close
    tracked=[];reused=[];attempts=[]
    def observe_open(path,*args,**kwargs):
        descriptor=original_open(path,*args,**kwargs)
        if path=='.locks':tracked.append(descriptor)
        return descriptor
    def fail_close(descriptor):
        if tracked and descriptor==tracked[0]:
            attempts.append(descriptor)
            if len(attempts)==1:
                original_close(descriptor)
                new=original_open(tmp_path/'owned-replacement',os.O_CREAT|os.O_RDWR,0o600)
                if new!=descriptor:os.dup2(new,descriptor);original_close(new)
                reused.append(descriptor)
                raise OSError(errno.EINTR,'Owned close uncertainty')
        return original_close(descriptor)
    monkeypatch.setattr(implementation.os,'open',observe_open)
    monkeypatch.setattr(implementation.os,'close',fail_close)
    try:
        guard=implementation.acquire_media_guard(tmp_path,7)
        with pytest.raises(implementation.MediaGuardUnavailable):guard.close()
        with pytest.raises(implementation.MediaGuardUnavailable):guard.close()
        assert len(attempts)==1
        assert os.fstat(reused[0])
    finally:
        for descriptor in reused:
            try:original_close(descriptor)
            except OSError:pass

@pytest.mark.parametrize('replacement',['file','folder','permissions'])
def test_live_guard_revalidates_namespace_before_exposing_descriptor(tmp_path,replacement):
    implementation=module();guard=implementation.acquire_media_guard(tmp_path,7)
    try:
        if replacement=='file':
            (tmp_path/'.locks/media.lock').rename(tmp_path/'.locks/old.lock')
            (tmp_path/'.locks/media.lock').write_bytes(b'');(tmp_path/'.locks/media.lock').chmod(0o600)
        elif replacement=='folder':
            (tmp_path/'.locks').rename(tmp_path/'.old-locks');(tmp_path/'.locks').mkdir(mode=0o700)
        else:(tmp_path/'.locks/media.lock').chmod(0o666)
        with pytest.raises(implementation.MediaGuardUnavailable):guard.assert_owned()
        with pytest.raises(implementation.MediaGuardUnavailable):guard.fileno()
    finally:guard.close()


def test_detected_permission_drift_stays_lost_after_restoration(tmp_path):
    implementation=module();guard=implementation.acquire_media_guard(tmp_path,7)
    try:
        path=tmp_path/'.locks/media.lock'
        path.chmod(0o666)
        with pytest.raises(implementation.MediaGuardUnavailable):guard.assert_owned()
        path.chmod(0o600)
        with pytest.raises(implementation.MediaGuardUnavailable):guard.assert_owned()
        with pytest.raises(implementation.MediaGuardUnavailable):guard.fileno()
    finally:guard.close()
    with implementation.acquire_media_guard(tmp_path,8):pass


def test_captured_root_binding_is_exact_and_sticky(tmp_path):
    from dataclasses import replace
    from core.timelapse_manifest import _root
    implementation=module();guard=implementation.acquire_media_guard(tmp_path,7)
    try:
        with _root(tmp_path) as (_,root):guard.assert_root_identity(root)
        with pytest.raises(implementation.MediaGuardUnavailable):guard.assert_root_identity(replace(root,inode=root.inode+1))
        with pytest.raises(implementation.MediaGuardUnavailable):guard.assert_root_identity(root)
    finally:guard.close()
