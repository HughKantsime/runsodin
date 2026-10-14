"""FD-only birth leaf: actual platform support or explicit read-only refusal."""
import os
import stat
import struct
import sys
from types import SimpleNamespace
import pytest
from core import project_import_birth as birth


@pytest.mark.parametrize('descriptor',[True,False,-1,1.0,None,2**31])
def test_invalid_fd_refuses_without_fstat(monkeypatch,descriptor):
    monkeypatch.setattr(birth.os,'fstat',lambda fd:pytest.fail('Invalid FD reached OS'))
    with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(descriptor)


def test_actual_owned_fd_is_not_closed_even_when_platform_unsupported(tmp_path):
    path=tmp_path/'owned-marker';path.write_bytes(b'owned')
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        if sys.platform=='darwin' and not hasattr(os.fstat(fd),'st_birthtime_ns'):
            with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(fd)
        else:
            value=birth.read_fd_birth(fd)
            assert value.timestamp_ns>0 and value.inode==os.fstat(fd).st_ino
        assert os.read(fd,5)==b'owned'
    finally:os.close(fd)


def test_actual_closed_pipe_and_nofollow_symlink_fd_refuse(tmp_path):
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY);os.close(fd)
    with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(fd)
    read,write=os.pipe()
    try:
        with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(read)
        os.write(write,b'x');assert os.read(read,1)==b'x'
    finally:os.close(read);os.close(write)
    target=tmp_path/'target';target.write_bytes(b'owned');link=tmp_path/'link';link.symlink_to(target)
    with pytest.raises(OSError):os.open(link,os.O_RDONLY|os.O_NOFOLLOW)
    if hasattr(os,'O_PATH'):
        fd=os.open(link,os.O_PATH|os.O_NOFOLLOW)
        try:
            with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(fd)
        finally:os.close(fd)


def facts():return SimpleNamespace(st_dev=os.makedev(1,2),st_ino=7,st_mode=stat.S_IFREG|0o600)


def statx_result(mask=0x901,seconds=1700000000,nanos=123,ino=7,major=1):
    def call(fd,path,flags,requested,buffer):
        assert fd==11 and path==b'' and flags==0x1100 and requested==0xfff
        struct.pack_into('=I',buffer,0,mask);struct.pack_into('=Q',buffer,32,ino)
        struct.pack_into('=H',buffer,28,stat.S_IFREG|0o600)
        struct.pack_into('=II',buffer,136,major,2);struct.pack_into('=qI',buffer,80,seconds,nanos)
        return 0
    return call


@pytest.mark.parametrize('changes',[{'mask':0x101},{'seconds':0},{'seconds':-1},{'nanos':1000000000},{'seconds':2**63-1},{'ino':8},{'major':3}])
def test_linux_missing_or_mismatched_evidence_refuses(monkeypatch,changes):
    monkeypatch.setattr(birth.sys,'platform','linux');monkeypatch.setattr(birth.os,'fstat',lambda fd:facts())
    monkeypatch.setattr(birth,'_load_statx',lambda:statx_result(**changes))
    with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(11)


def test_linux_decodes_exact_tuple_and_rechecks_fd(monkeypatch):
    monkeypatch.setattr(birth.sys,'platform','linux');calls=[]
    monkeypatch.setattr(birth.os,'fstat',lambda fd:(calls.append(fd) or facts()))
    monkeypatch.setattr(birth,'_load_statx',lambda:statx_result())
    value=birth.read_fd_birth(11)
    assert (value.seconds,value.nanoseconds,value.facility)==(1700000000,123,'linux_statx')
    assert calls==[11,11]


def test_fd_substitution_during_facility_read_refuses(monkeypatch):
    monkeypatch.setattr(birth.sys,'platform','linux');rows=[facts(),SimpleNamespace(st_dev=os.makedev(1,2),st_ino=8,st_mode=stat.S_IFREG|0o600)]
    monkeypatch.setattr(birth.os,'fstat',lambda fd:rows.pop(0));monkeypatch.setattr(birth,'_load_statx',lambda:statx_result())
    with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(11)


@pytest.mark.parametrize('value',[None,True,0,1700000000.123])
def test_darwin_no_float_or_ctime_fallback(monkeypatch,value):
    monkeypatch.setattr(birth.sys,'platform','darwin');row=facts();row.st_birthtime_ns=value;row.st_birthtime=1700000000.;row.st_ctime_ns=1700000000000000000
    monkeypatch.setattr(birth.os,'fstat',lambda fd:row)
    with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(11)


def test_darwin_exact_ns_facility_contract_unit_only(monkeypatch):
    monkeypatch.setattr(birth.sys,'platform','darwin');row=facts();row.st_birthtime_ns=1700000000000000123
    monkeypatch.setattr(birth.os,'fstat',lambda fd:row)
    value=birth.read_fd_birth(11);assert value.timestamp_ns==row.st_birthtime_ns and value.facility=='macos_birthtime_ns'


def test_unknown_platform_and_unqualified_abi_refuse(monkeypatch):
    monkeypatch.setattr(birth.sys,'platform','unknown');monkeypatch.setattr(birth.os,'fstat',lambda fd:facts())
    with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(11)
    monkeypatch.setattr(birth.platform,'machine',lambda:'aarch64')
    with pytest.raises(birth.BirthUnavailable):birth._load_statx()


def test_missing_libc_symbol_and_syscall_failure_refuse(monkeypatch):
    monkeypatch.setattr(birth.platform,'machine',lambda:'x86_64')
    monkeypatch.setattr(birth.ctypes,'CDLL',lambda *a,**k:SimpleNamespace())
    with pytest.raises(birth.BirthUnavailable):birth._load_statx()
    monkeypatch.setattr(birth.sys,'platform','linux');monkeypatch.setattr(birth.os,'fstat',lambda fd:facts())
    monkeypatch.setattr(birth,'_load_statx',lambda:lambda *a:-1)
    with pytest.raises(birth.BirthUnavailable):birth.read_fd_birth(11)


@pytest.mark.parametrize('version',[b'2.27',b'3.0',b'unknown',b'2.41.1'])
def test_unqualified_glibc_contract_refuses_before_statx(monkeypatch,version):
    monkeypatch.setattr(birth.platform,'machine',lambda:'x86_64')
    def get_version():return version
    monkeypatch.setattr(birth.ctypes,'CDLL',lambda *a,**k:SimpleNamespace(gnu_get_libc_version=get_version))
    with pytest.raises(birth.BirthUnavailable):birth._load_statx()
