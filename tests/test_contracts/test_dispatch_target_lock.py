"""Actual owned SQLite files/processes only; no printer or recovery calls."""
import os
from pathlib import Path
import subprocess
import sys
import pytest
from sqlalchemy import create_engine
from core import dispatch_target_lock as module

@pytest.fixture
def engine(tmp_path):
    path=tmp_path/'owned.db';path.touch()
    engine=create_engine('sqlite:///'+str(path))
    yield engine
    engine.dispose()


def probe(engine,key):
    source="from sqlalchemy import create_engine;from core.dispatch_target_lock import acquire_target_lock;h=acquire_target_lock(create_engine(%r),%r);print(h.status);h.handle.close() if h.handle else None"%(str(engine.url),key)
    return subprocess.check_output([sys.executable,'-c',source],env=dict(os.environ,PYTHONPATH=str(Path.cwd()/'backend')),text=True,timeout=5).strip()


def test_actual_subprocess_same_target_conflicts_and_release_preserves_inode(engine):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle
    try:
        assert handle.held()
        assert probe(engine,'a'*64)=='conflict'
        assert probe(engine,'b'*64)=='acquired'
        path=handle._lock_path;before=path.stat().st_ino
        assert handle.close().released
        assert path.exists() and path.stat().st_ino==before
        assert probe(engine,'a'*64)=='acquired'
        assert not handle.held()
        assert handle.close() is handle.close()
    finally:handle.close()


@pytest.mark.parametrize('database',[':memory:',''])
def test_nonfile_database_refuses(database):
    engine=create_engine('sqlite:///'+database)
    try:assert module.acquire_target_lock(engine,'a'*64).status=='unsupported'
    finally:engine.dispose()


def test_invalid_key_refuses_without_access(engine):
    for key in ('G'*64,'../escape',None):
        assert module.acquire_target_lock(engine,key).status=='unsupported'


def test_symlink_database_refuses(tmp_path):
    target=tmp_path/'actual.db';target.touch();link=tmp_path/'linked.db';link.symlink_to(target)
    engine=create_engine('sqlite:///'+str(link))
    try:assert module.acquire_target_lock(engine,'a'*64).status!='acquired'
    finally:engine.dispose()


def test_hardlinked_database_refuses(engine,tmp_path):
    os.link(engine.url.database,tmp_path/'alias.db')
    assert module.acquire_target_lock(engine,'a'*64).status!='acquired'


def test_symlink_or_public_lock_directory_refuses(engine,tmp_path):
    directory=Path(engine.url.database+'.odin-sender-locks')
    target=tmp_path/'other';target.mkdir();directory.symlink_to(target,target_is_directory=True)
    assert module.acquire_target_lock(engine,'a'*64).status!='acquired'
    directory.unlink();directory.mkdir(mode=0o755)
    assert module.acquire_target_lock(engine,'a'*64).status!='acquired'


def test_symlink_lock_file_refuses(engine,tmp_path):
    directory=Path(engine.url.database+'.odin-sender-locks');directory.mkdir(mode=0o700)
    target=tmp_path/'other';target.touch();(directory/('a'*64+'.lock')).symlink_to(target)
    assert module.acquire_target_lock(engine,'a'*64).status!='acquired'


def test_database_replacement_permanently_loses_handle(engine):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle
    path=Path(engine.url.database);old=path.with_suffix('.old');path.rename(old);path.touch()
    try:
        assert not handle.held()
        path.unlink();old.rename(path)
        assert not handle.held()
        assert not handle.close().released
    finally:handle.close()


def test_lock_file_replacement_permanently_loses_handle(engine):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle;path=handle._lock_path;path.rename(path.with_suffix('.old'));path.touch(mode=0o600)
    try:assert not handle.held() and not handle.close().released
    finally:handle.close()


def test_unknown_filesystem_refuses(engine,monkeypatch):
    monkeypatch.setattr(module,'_local_filesystem',lambda fd:False)
    assert module.acquire_target_lock(engine,'a'*64).status=='unsupported'


def test_forked_child_cannot_unlock_parent(engine):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle;pid=os.fork()
    if pid==0:
        try:
            assert not handle.held()
            assert not handle.close().released
            os._exit(0)
        except BaseException:os._exit(1)
    try:
        _,status=os.waitpid(pid,0);assert os.waitstatus_to_exitcode(status)==0
        assert handle.held() and probe(engine,'a'*64)=='conflict'
    finally:handle.close()


def test_descriptors_are_private_noninheritable_and_permission_drift_loses(engine):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle
    try:
        assert all(not os.get_inheritable(fd) for fd in handle._fds)
        handle._directory.chmod(0o755)
        assert not handle.held()
        handle._directory.chmod(0o700)
        assert not handle.held() and not handle.close().released
    finally:handle.close()


def test_lock_directory_replacement_loses_ownership(engine):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle;old=handle._directory.with_suffix('.old')
    try:
        handle._directory.rename(old);handle._directory.mkdir(mode=0o700)
        assert not handle.held() and not handle.close().released
    finally:handle.close()


def test_hardlinked_or_public_lock_file_refuses(engine,tmp_path):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle;path=handle._lock_path;handle.close()
    path.chmod(0o644)
    assert module.acquire_target_lock(engine,'a'*64).status!='acquired'
    path.chmod(0o600);os.link(path,tmp_path/'alias.lock')
    assert module.acquire_target_lock(engine,'a'*64).status!='acquired'


def test_public_parent_refuses_acquisition(engine):
    parent=Path(engine.url.database).parent;before=parent.stat().st_mode&0o777
    try:
        parent.chmod(0o777)
        assert module.acquire_target_lock(engine,'a'*64).status!='acquired'
    finally:parent.chmod(before)


def test_parent_permission_drift_permanently_loses(engine):
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle;parent=Path(engine.url.database).parent;before=parent.stat().st_mode&0o777
    try:
        parent.chmod(0o777)
        assert not handle.held()
        parent.chmod(before)
        assert not handle.held() and not handle.close().released
    finally:parent.chmod(before);handle.close()


def test_parent_owner_drift_refuses_held_handle(engine,monkeypatch):
    from types import SimpleNamespace
    result=module.acquire_target_lock(engine,'a'*64);assert result.status=='acquired'
    handle=result.handle;real=module.os.stat
    def changed(path,*args,**kwargs):
        value=real(path,*args,**kwargs)
        if Path(path)==handle._parent:
            return SimpleNamespace(st_dev=value.st_dev,st_ino=value.st_ino,st_mode=value.st_mode,st_uid=-1)
        return value
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.os,'stat',changed)
            assert not handle.held()
        assert not handle.held() and not handle.close().released
    finally:handle.close()
