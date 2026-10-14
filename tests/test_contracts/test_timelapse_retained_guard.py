"""Actual owned directories/FDs; retained resources never cache identity proof."""
import os,errno,stat,subprocess,sys,json
from pathlib import Path
import pytest
from core import timelapse_media_lock as m

@pytest.fixture
def root(tmp_path):
    value=tmp_path/'one'/'two'/'media';value.mkdir(parents=True,mode=0o700);return value

def test_each_check_is_live_without_reopen_and_all_fds_noninheritable(root,monkeypatch):
    guard=m.acquire_media_guard(root,7);fds=list(guard._chain.descriptors)+[guard._folder_fd,guard._descriptor]
    assert len(fds)<=67 and all(not os.get_inheritable(fd) for fd in fds)
    expected=[name for _,name,_,_ in guard._chain.nodes]
    opened=[];closed=[];names=[];fstats=[];op=os.open;cl=os.close;st=os.stat;fs=os.fstat
    def open_(*a,**kw):opened.append(1);return op(*a,**kw)
    def close_(fd):closed.append(fd);return cl(fd)
    def stat_(name,*a,**kw):names.append(name);return st(name,*a,**kw)
    def fstat_(fd):fstats.append(fd);return fs(fd)
    monkeypatch.setattr(m.os,'open',open_);monkeypatch.setattr(m.os,'close',close_);monkeypatch.setattr(m.os,'stat',stat_);monkeypatch.setattr(m.os,'fstat',fstat_)
    try:
        for _ in range(3):guard.assert_owned()
        assert not opened and not closed
        assert all(names.count(name)>=6 for name in expected)
        assert names.count('/')>=6 and names.count('.locks')>=6 and names.count('media.lock')>=6
        assert all(fstats.count(fd)>=6 for fd in fds)
    finally:guard.close()
    assert len(closed)==len(fds)==len(set(closed))

@pytest.mark.parametrize('level',('one','two','media','.locks'))
@pytest.mark.parametrize('replacement',('directory','symlink'))
def test_every_owned_ancestor_root_and_lock_directory_replacement_refuses(root,tmp_path,level,replacement):
    guard=m.acquire_media_guard(root,7)
    path={'one':tmp_path/'one','two':tmp_path/'one/two','media':root,'.locks':root/'.locks'}[level]
    try:
        path.rename(path.with_name(path.name+'-original'))
        if replacement=='directory':path.mkdir(mode=0o700)
        else:
            outside=tmp_path/'outside';outside.mkdir(mode=0o700);path.symlink_to(outside,target_is_directory=True)
        with pytest.raises(m.MediaGuardUnavailable):guard.assert_owned()
        with pytest.raises(m.MediaGuardUnavailable):guard.fileno()
    finally:guard.close()

def test_change_between_full_passes_refuses(root,monkeypatch):
    guard=m.acquire_media_guard(root,7);original=guard._locks_match;calls=[]
    def change():
        original();calls.append(1)
        if len(calls)==1:
            (root/'.locks').rename(root/'.original-locks');(root/'.locks').mkdir(mode=0o700)
    monkeypatch.setattr(guard,'_locks_match',change)
    try:
        with pytest.raises(m.MediaGuardUnavailable):guard.assert_owned()
    finally:guard.close()

@pytest.mark.parametrize('fault',('open','flag','fstat','flock'))
def test_acquisition_failures_close_all_opened_descriptors_once(root,monkeypatch,fault):
    opened=[];closed=[];original_open=os.open;original_close=os.close;original_flag=os.set_inheritable;original_stat=os.fstat;original_flock=m.fcntl.flock
    def open_(path,*a,**kw):
        if fault=='open' and path=='.locks':raise OSError(errno.EIO,'Owned acquisition fault')
        fd=original_open(path,*a,**kw);opened.append(fd);return fd
    def flag(fd,value):
        if fault=='flag' and len(opened)==2:raise OSError(errno.EIO,'Owned flag fault')
        return original_flag(fd,value)
    def fstat(fd):
        if fault=='fstat' and len(opened)==2:raise OSError(errno.EIO,'Owned stat fault')
        return original_stat(fd)
    def flock(*a):
        if fault=='flock':raise OSError(errno.EIO,'Owned flock fault')
        return original_flock(*a)
    def close(fd):closed.append(fd);return original_close(fd)
    monkeypatch.setattr(m.os,'open',open_);monkeypatch.setattr(m.os,'close',close);monkeypatch.setattr(m.os,'set_inheritable',flag);monkeypatch.setattr(m.os,'fstat',fstat);monkeypatch.setattr(m.fcntl,'flock',flock)
    with pytest.raises(m.MediaGuardUnavailable):m.acquire_media_guard(root,7)
    assert set(opened)==set(closed) and len(closed)==len(set(closed))
    for fd in opened:
        with pytest.raises(OSError):original_stat(fd)

def test_close_uncertainty_retires_vector_without_retry(root,monkeypatch):
    guard=m.acquire_media_guard(root,7);owned=set(guard._chain.descriptors+[guard._folder_fd,guard._descriptor]);target=guard._folder_fd;attempts=[];close=os.close
    def uncertain(fd):
        attempts.append(fd);close(fd)
        if fd==target:raise OSError(errno.EINTR,'Owned close uncertainty')
    monkeypatch.setattr(m.os,'close',uncertain)
    with pytest.raises(m.MediaGuardUnavailable):guard.close()
    assert set(attempts)==owned and len(attempts)==len(owned)
    with pytest.raises(m.MediaGuardUnavailable):guard.close()
    with pytest.raises(m.MediaGuardUnavailable):guard.assert_owned()
    assert len(attempts)==len(owned)

def test_boundaries_refuse_before_open_and_exact_limit_is_owned(tmp_path,monkeypatch):
    calls=[];op=os.open
    def open_(*a,**kw):calls.append(1);return op(*a,**kw)
    monkeypatch.setattr(m.os,'open',open_)
    for value in ('/'+'/'.join(['x']*65),'/'+'x'*4096):
        with pytest.raises(m.MediaGuardUnavailable):m.acquire_media_guard(value,7)
    assert not calls
    # Build a real exact64-component absolute path under the owned fixture.
    base=tmp_path.resolve();value=base
    for _ in range(64-len(base.parts[1:])):value=value/'x'
    value.mkdir(parents=True,mode=0o700)
    guard=m.acquire_media_guard(value,7)
    try:assert len(guard._chain.nodes)==64 and len(guard._chain.descriptors)+2==67
    finally:guard.close()

def test_retained_directories_are_not_inherited_but_lock_is(root):
    guard=m.acquire_media_guard(root,7)
    try:
        entries=[(fd,os.fstat(fd).st_dev,os.fstat(fd).st_ino) for fd in guard._chain.descriptors+[guard._folder_fd]]
        code='import os,sys,json,stat; os.fstat(int(sys.argv[1])); entries=json.loads(sys.argv[2]);\nfor fd,dev,ino in entries:\n try: s=os.fstat(fd)\n except OSError: continue\n assert not (stat.S_ISDIR(s.st_mode) and (s.st_dev,s.st_ino)==(dev,ino))\nprint("owned-no-directory-inheritance")'
        result=subprocess.run([sys.executable,'-c',code,str(guard.fileno()),json.dumps(entries)],pass_fds=(guard.fileno(),),capture_output=True,text=True,timeout=5)
        assert result.returncode==0 and result.stdout.strip()=='owned-no-directory-inheritance'
    finally:guard.close()

def test_process_root_anchor_mismatch_refuses(root,monkeypatch):
    from types import SimpleNamespace
    guard=m.acquire_media_guard(root,7);original=os.stat
    fields=('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
    def changed(name,*a,**kw):
        value=original(name,*a,**kw)
        if name=='/':return SimpleNamespace(**{k:getattr(value,k)+(1 if k=='st_ino' else 0) for k in fields})
        return value
    monkeypatch.setattr(m.os,'stat',changed)
    try:
        with pytest.raises(m.MediaGuardUnavailable):guard.assert_owned()
    finally:guard.close()

@pytest.mark.parametrize('field,value',(('st_uid',True),('st_mtime_ns',-1),('st_ctime_ns',2**63),('st_nlink',0),('st_ino',0)))
def test_original_numeric_root_checks_remain_live(root,monkeypatch,field,value):
    from types import SimpleNamespace
    guard=m.acquire_media_guard(root,7);target=guard._chain.root_fd;original=os.fstat
    fields=('st_dev','st_ino','st_uid','st_gid','st_mode','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
    def changed(fd):
        row=original(fd)
        return SimpleNamespace(**{k:value if k==field else getattr(row,k) for k in fields}) if fd==target else row
    monkeypatch.setattr(m.os,'fstat',changed)
    try:
        with pytest.raises(m.MediaGuardUnavailable):guard.assert_owned()
    finally:guard.close()

def test_every_open_failure_cleans_known_vector_once(root,monkeypatch):
    op=os.open;cl=os.close
    for position in range(1,len(root.absolute().parts[1:])+4):
        opened=[];closed=[];calls=[]
        def fail(path,*a,**kw):
            calls.append(1)
            if len(calls)==position:raise OSError(errno.EIO,'Owned open fault')
            fd=op(path,*a,**kw);opened.append(fd);return fd
        def close(fd):closed.append(fd);return cl(fd)
        monkeypatch.setattr(m.os,'open',fail);monkeypatch.setattr(m.os,'close',close)
        with pytest.raises(m.MediaGuardUnavailable):m.acquire_media_guard(root,7)
        assert set(opened)==set(closed) and len(closed)==len(set(closed))

def test_close_uncertainty_after_failed_acquisition_never_retries(root,monkeypatch):
    opened=[];closed=[];op=os.open;cl=os.close;realflock=m.fcntl.flock
    def open_(path,*a,**kw):fd=op(path,*a,**kw);opened.append(fd);return fd
    def close(fd):
        closed.append(fd);cl(fd)
        if fd==opened[0]:raise OSError(errno.EINTR,'Owned acquisition cleanup uncertainty')
    def flock(*args):raise OSError(errno.EIO,'Owned acquisition failed')
    monkeypatch.setattr(m.os,'open',open_);monkeypatch.setattr(m.os,'close',close);monkeypatch.setattr(m.fcntl,'flock',flock)
    with pytest.raises(m.MediaGuardUnavailable):m.acquire_media_guard(root,7)
    assert set(opened)==set(closed) and len(closed)==len(set(closed))
