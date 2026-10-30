"""Actual owned filesystem/FD tests; no journal/write/route authority."""
import time
import errno
import hashlib
import os
from pathlib import Path

import pytest

from modules.archives import project_zip_storage as storage


@pytest.fixture
def root_path(tmp_path,monkeypatch):
    # Explicit real path; the storage helper itself never resolves symlinks.
    path = Path(os.path.realpath(tmp_path)) / 'owned'
    path.mkdir(mode=0o700)
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',str(path))
    return path


def test_actual_read_hash_zero_and_multiple_files(root_path):
    payload = b'actual-bytes\0'*100000
    (root_path/'one').write_bytes(payload)
    (root_path/'zero').write_bytes(b'')
    with storage.FDLedger(167) as ledger:
        root = storage.open_storage_root('print_files',ledger)
        ledger.reserve_capacity(root.descriptor)
        budget = ledger.budget
        for name,data in [('one',payload),('zero',b'')]:
            selected,parts = storage.relative_source([root],str(root_path/name))
            sink = []
            result = storage.read_regular_source(selected,parts,budget,sink.append,
                expected_size=len(data),expected_sha256=hashlib.sha256(data).hexdigest())
            assert b''.join(sink) == data
            assert result.byte_count == len(data)
        assert budget.total == len(payload)
        assert ledger.peak == ledger.count == 167
        assert all(not os.get_inheritable(s.fd) for s in ledger._slots if s.fd is not None)


@pytest.mark.parametrize('replacement',['file','parent','root','content','hardlink','mode'])
def test_substitution_or_content_drift_discards_read(root_path,replacement):
    parent = root_path/'nested'; parent.mkdir(mode=0o700)
    path = parent/'one'; path.write_bytes(b'unchanged')
    with storage.FDLedger(167) as ledger:
        root = storage.open_storage_root('print_files',ledger)
        def consume(_):
            if replacement == 'file':
                path.rename(parent/'old'); path.write_bytes(b'unchanged')
            elif replacement == 'parent':
                parent.rename(root_path/'old'); parent.mkdir(mode=0o700); path.write_bytes(b'unchanged')
            elif replacement == 'root':
                root_path.rename(root_path.with_name('old')); root_path.mkdir(mode=0o700)
            elif replacement == 'content':
                # Same size: only timestamps can show this, and ext4's tick is about 4 ms.
                time.sleep(0.02)
                path.write_bytes(b'different')
            elif replacement == 'hardlink':
                os.link(path,parent/'alias')
            else:
                path.chmod(0o666)
        with pytest.raises(storage.StorageUnavailable):
            storage.read_regular_source(root,('nested','one'),ledger.budget,consume)


@pytest.mark.parametrize('kind',['symlink','fifo','directory','hardlink','unsafe_mode'])
def test_nonregular_alias_or_unsafe_file_refused(root_path,kind):
    path = root_path/'one'
    if kind == 'symlink':
        path.symlink_to('/etc/passwd')
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'directory':
        path.mkdir()
    else:
        path.write_bytes(b'owned')
        if kind == 'hardlink':os.link(path,root_path/'alias')
        else:path.chmod(0o666)
    with storage.FDLedger(167) as ledger:
        root = storage.open_storage_root('print_files',ledger)
        with pytest.raises(storage.StorageUnavailable):
            storage.read_regular_source(root,('one',),ledger.budget,lambda _:None)


def test_config_and_path_refusals(root_path,monkeypatch):
    with storage.FDLedger(167) as ledger:
        root = storage.open_storage_root('print_files',ledger)
        for path in [str(root_path),str(root_path)+'-sibling/one',str(root_path)+'/../one',
                     str(root_path)+'/.project-imports/000.gcode',str(root_path)+'/x\\y',str(root_path)+'/x//y']:
            with pytest.raises(storage.StorageUnavailable):storage.relative_source([root],path)
        monkeypatch.setenv('EDUCATION_UPLOAD_ROOT',str(root_path))
        other = storage.open_storage_root('education_export',ledger)
        with pytest.raises(storage.StorageUnavailable):storage.relative_source([root,other],str(root_path/'one'))
    for value in ['',str(root_path/'absent'),str(root_path)+'/../owned','relative']:
        monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',value)
        with storage.FDLedger(167) as ledger, pytest.raises(storage.StorageUnavailable):
            storage.open_storage_root('print_files',ledger)


def _depth_root(base,depth):
    count = len(base.parts)-1
    for index in range(depth-count):
        base = base/('d'+str(index)); base.mkdir(mode=0o700)
    return base


def test_actual_depth64_128_live_members_import_ceiling(root_path,monkeypatch):
    path = _depth_root(root_path,64)
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',str(path))
    with storage.FDLedger(200) as ledger:
        root = storage.open_storage_root('print_files',ledger)
        assert ledger.live_count == 65
        ledger.reserve_capacity(root.descriptor)
        # Real precreated owned files represent all retained storage resources;
        # this read/resource foundation grants no creation or checkpoint permit.
        resources = []
        for index in range(133):  # four markers/dirs +128 members +ZIP input
            file = path/str(index); file.write_bytes(b'owned fixture')
            resources.append(ledger.acquire(lambda file=file:os.open(file,os.O_RDONLY|os.O_NOFOLLOW)))
        assert ledger.live_count == 198
        temporary = [ledger.acquire(lambda:os.dup(ledger.fileno(root.descriptor))) for _ in range(2)]
        assert ledger.live_count == ledger.peak == 200
        with pytest.raises(storage.StorageUnavailable):ledger.acquire(lambda:pytest.fail('over-cap open'))
        for descriptor in reversed(temporary+resources):ledger.release(descriptor)
        assert ledger.count == 200 and ledger.live_count == 65
    assert ledger.count == 0
    too_deep = path/'extra';too_deep.mkdir()
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',str(too_deep))
    with storage.FDLedger(200) as ledger, pytest.raises(storage.StorageUnavailable):
        storage.open_storage_root('print_files',ledger)


def test_actual_two_depth64_roots_source32_export_ceiling(root_path,monkeypatch):
    a = _depth_root(root_path,64)
    second = root_path.parent/'second';second.mkdir(mode=0o700)
    b = _depth_root(second,64)
    monkeypatch.setenv('PRINT_FILE_STORAGE_ROOT',str(a));monkeypatch.setenv('EDUCATION_UPLOAD_ROOT',str(b))
    source = a
    for index in range(31):source=source/str(index);source.mkdir(mode=0o700)
    file=source/'one';file.write_bytes(b'actual')
    with storage.FDLedger(167) as ledger:
        roots=[storage.open_storage_root(kind,ledger) for kind in ('print_files','education_export')]
        assert ledger.live_count == 130
        ledger.reserve_capacity(roots[0].descriptor)
        extras=[ledger.acquire(lambda:os.dup(ledger.fileno(roots[0].descriptor))) for _ in range(5)]
        observed=[]
        result=storage.read_regular_source(roots[0],tuple(str(i) for i in range(31))+('one',),ledger.budget,lambda _:observed.append(ledger.live_count))
        assert result.byte_count == 6 and observed == [167] and ledger.peak == 167
        for descriptor in extras:ledger.release(descriptor)


def test_deadline_total_file_size_and_expected_hash(root_path):
    clock=[0.0];budget=storage.ReadBudget(clock=lambda:clock[0])
    budget.charge(storage.MAX_TOTAL_BYTES)
    with pytest.raises(storage.StorageUnavailable):budget.charge(1)
    clock[0]=60
    with pytest.raises(storage.StorageUnavailable):budget.check()
    file=root_path/'one';file.write_bytes(b'x')
    with storage.FDLedger(167) as ledger:
        root=storage.open_storage_root('print_files',ledger)
        for size,digest in [(2,None),(True,None),(1,'0'*64),(1,'A'*64)]:
            with pytest.raises(storage.StorageUnavailable):
                storage.read_regular_source(root,('one',),ledger.budget,lambda _:None,expected_size=size,expected_sha256=digest)
        with file.open('wb') as f:f.truncate(storage.MAX_FILE_BYTES+1)
        with pytest.raises(storage.StorageUnavailable):storage.read_regular_source(root,('one',),ledger.budget,lambda _:None)


def test_actual_512mib_shared_budget_refuses_next_byte(root_path):
    file=root_path/'sparse'
    with file.open('wb') as sink:sink.truncate(storage.MAX_FILE_BYTES)
    with storage.FDLedger(167) as ledger:
        root=storage.open_storage_root('print_files',ledger)
        budget=ledger.budget
        for _ in range(4):
            assert storage.read_regular_source(root,('sparse',),budget,lambda _:None).byte_count == storage.MAX_FILE_BYTES
        assert budget.total == storage.MAX_TOTAL_BYTES
        with pytest.raises(storage.StorageUnavailable):
            storage.read_regular_source(root,('sparse',),budget,lambda _:pytest.fail('over-budget sink'))


def test_uncertain_close_no_retry_or_open_and_stale_descriptor_refused(root_path,monkeypatch):
    ledger=storage.FDLedger(200)
    root=storage.open_storage_root('print_files',ledger)
    descriptor=ledger.acquire(lambda:os.dup(ledger.fileno(root.descriptor)))
    fd=ledger.fileno(descriptor)
    real_close=os.close;calls=[]
    def uncertain(value):
        calls.append(value)
        real_close(value)
        if value == fd:raise OSError(errno.EINTR,'uncertain close')
    monkeypatch.setattr(storage.os,'close',uncertain)
    with pytest.raises(storage.StorageUnavailable):ledger.release(descriptor)
    assert ledger.poisoned and descriptor.fd is None and descriptor.uncertain
    with pytest.raises(storage.StorageUnavailable):ledger.acquire(lambda:pytest.fail('poisoned open'))
    with pytest.raises(storage.StorageUnavailable):ledger.close()
    with pytest.raises(storage.StorageUnavailable):ledger.close()
    assert calls.count(fd) == 1 and len(calls) == len(set(calls))
    assert ledger.count == 1  # uncertain resource stays charged, no cleanup success.


def test_reserved_slot_never_revives_retired_handle(root_path):
    with storage.FDLedger(200) as ledger:
        root=storage.open_storage_root('print_files',ledger);ledger.reserve_capacity(root.descriptor)
        old=ledger.acquire(lambda:os.dup(ledger.fileno(root.descriptor)))
        ledger.release(old)
        new=ledger.acquire(lambda:os.dup(ledger.fileno(root.descriptor)))
        assert old is not new
        with pytest.raises(storage.StorageUnavailable):ledger.fileno(old)
        ledger.release(new)


@pytest.mark.parametrize('failure',['soft_limit','reserve_emfile','open_emfile','noninherit'])
def test_capacity_and_acquisition_failure_cleanups(root_path,monkeypatch,failure):
    ledger=storage.FDLedger(200)
    root=storage.open_storage_root('print_files',ledger)
    if failure == 'soft_limit':
        monkeypatch.setattr(storage.resource,'getrlimit',lambda _:(199,199))
        with pytest.raises(storage.StorageUnavailable):ledger.reserve_capacity(root.descriptor)
        assert not ledger._reserved
        ledger.close()
    elif failure == 'reserve_emfile':
        real_dup=os.dup;calls=[0]
        def fail(fd):
            calls[0]+=1
            if calls[0] == 10:raise OSError(errno.EMFILE,'capacity')
            return real_dup(fd)
        monkeypatch.setattr(storage.os,'dup',fail)
        with pytest.raises(storage.StorageUnavailable):ledger.reserve_capacity(root.descriptor)
        assert ledger.closed and ledger.poisoned and ledger.count == 0
    else:
        ledger.reserve_capacity(root.descriptor)
        if failure == 'noninherit':
            monkeypatch.setattr(storage.os,'set_inheritable',lambda *_:(_ for _ in ()).throw(OSError('flags')))
            factory=lambda:os.dup(ledger.fileno(root.descriptor))
        else:factory=lambda:(_ for _ in ()).throw(OSError(errno.EMFILE,'open'))
        with pytest.raises(storage.StorageUnavailable):ledger.acquire(factory)
        with pytest.raises(storage.StorageUnavailable):ledger.close()
        assert ledger.count == 0


def test_midread_deadline_sink_failure_and_namedstat_open_race(root_path,monkeypatch):
    path=root_path/'one';path.write_bytes(b'data')
    clock=[0.0];budget=storage.ReadBudget(clock=lambda:clock[0])
    with storage.FDLedger(167,budget=budget) as ledger:
        root=storage.open_storage_root('print_files',ledger)
        with pytest.raises(storage.StorageUnavailable):
            storage.read_regular_source(root,('one',),budget,lambda _:clock.__setitem__(0,60))
    with storage.FDLedger(167) as ledger:
        root=storage.open_storage_root('print_files',ledger)
        with pytest.raises(storage.StorageUnavailable):
            storage.read_regular_source(root,('one',),ledger.budget,lambda _:(_ for _ in ()).throw(RuntimeError('sink')))
        real_open=os.open
        def swapped(name,*args,**kwargs):
            if name == 'one':path.rename(root_path/'old');path.write_bytes(b'data')
            return real_open(name,*args,**kwargs)
        monkeypatch.setattr(storage.os,'open',swapped)
        with pytest.raises(storage.StorageUnavailable):storage.read_regular_source(root,('one',),ledger.budget,lambda _:pytest.fail('substituted bytes'))


def test_all_200_close_positions_retired_once(root_path,monkeypatch):
    real_close=os.close
    for failure_index in range(200):
        ledger=storage.FDLedger(200)
        root=storage.open_storage_root('print_files',ledger)
        ledger.reserve_capacity(root.descriptor)
        calls=[]
        failed=ledger._slots[failure_index].fd
        def close(fd):
            calls.append(fd);real_close(fd)
            if fd == failed:raise OSError(errno.EINTR,'uncertain')
        with monkeypatch.context() as patch:
            patch.setattr(storage.os,'close',close)
            with pytest.raises(storage.StorageUnavailable):ledger.close()
            with pytest.raises(storage.StorageUnavailable):ledger.close()
        assert len(calls) == len(set(calls)) == 200
        assert ledger.poisoned and ledger.count == 1


def test_acquired_fd_survives_failed_fstat_until_owned_cleanup(root_path,monkeypatch):
    ledger=storage.FDLedger(167)
    real_fstat=os.fstat;count=[0]
    def fail(fd):
        count[0]+=1
        if count[0] == 2:raise OSError('synthetic fstat')
        return real_fstat(fd)
    with monkeypatch.context() as patch:
        patch.setattr(storage.os,'fstat',fail)
        with pytest.raises(storage.StorageUnavailable):storage.open_storage_root('print_files',ledger)
    assert ledger.count == 2
    ledger.close()
    assert ledger.count == 0


def test_one_request_budget_includes_root_acquisition_and_no_file_reset(root_path,monkeypatch):
    clock=[0.0];budget=storage.ReadBudget(clock=lambda:clock[0])
    ledger=storage.FDLedger(167,budget=budget)
    real_open=os.open
    def slow(name,*args,**kwargs):
        fd=real_open(name,*args,**kwargs)
        clock[0]=60
        return fd
    with monkeypatch.context() as patch:
        patch.setattr(storage.os,'open',slow)
        with pytest.raises(storage.StorageUnavailable):storage.open_storage_root('print_files',ledger)
    assert ledger.count==1
    ledger.close();assert ledger.count==0
    (root_path/'one').write_bytes(b'owned')
    with storage.FDLedger(167) as ledger:
        root=storage.open_storage_root('print_files',ledger)
        with pytest.raises(storage.StorageUnavailable):
            storage.read_regular_source(root,('one',),storage.ReadBudget(),lambda _:pytest.fail('reset budget accepted'))
        with pytest.raises(AttributeError):ledger.budget=storage.ReadBudget()
