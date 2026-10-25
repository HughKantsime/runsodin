"""Owned configured-target exclusion, never sender-quiescence/recovery proof.

SQLite descriptors are private and must not be duplicated/unlocked externally.
Inherited PostgreSQL handles must not touch DBAPI; fork children must exec or
_exit without connection finalization. Both ownership scopes are PID pinned.
"""
from dataclasses import dataclass,field
import ctypes
import hashlib
import os
from pathlib import Path
import platform
import re
import stat
import sys
import threading


@dataclass(frozen=True)
class TargetLockResult:
    status: str
    handle: object = field(default=None,repr=False)


@dataclass(frozen=True)
class TargetLockClose:
    released: bool
    scope: str = 'configured-target lock only; no sender or transport cleanup proof'


def _local_filesystem(fd):
    if os.name!='posix':return False
    libc=ctypes.CDLL(None,use_errno=True)
    info=(ctypes.c_longlong*512)()
    if sys.platform=='darwin':
        # SDK sys/mount.h __DARWIN_STRUCT_STATFS64: uint32 f_flags offset64.
        if platform.machine() not in ('arm64','x86_64') or ctypes.sizeof(ctypes.c_void_p)!=8:return False
        call=getattr(libc,'fstatfs64',None)
        if call is None:return False
    elif sys.platform.startswith('linux'):
        if ctypes.sizeof(ctypes.c_long)!=8:return False
        call=libc.fstatfs
    else:return False
    call.argtypes=(ctypes.c_int,ctypes.c_void_p);call.restype=ctypes.c_int
    if call(fd,ctypes.byref(info))!=0:return False
    if sys.platform=='darwin':
        flags=ctypes.c_uint32.from_buffer(info,64).value
        name=bytes(info)[72:88].split(b'\x00')[0]
        return bool(flags&0x1000) and name in (b'apfs',b'hfs')
    magic=ctypes.cast(ctypes.byref(info),ctypes.POINTER(ctypes.c_long))[0]&0xffffffff
    # Local ext, overlay, tmpfs, XFS, Btrfs and ZFS. Network/unknown filesystems refuse.
    return magic in (0xef53,0x794c7630,0x01021994,0x58465342,0x9123683e,0x2fc12fc1)


def _identity(value):return (value.st_dev,value.st_ino)


def _trusted_parent(value):
    return (stat.S_ISDIR(value.st_mode) and value.st_uid in (0,os.geteuid())
            and not stat.S_IMODE(value.st_mode)&0o022)


def _private_file(value,mode=None):
    return (stat.S_ISREG(value.st_mode) and value.st_uid==os.geteuid() and value.st_nlink==1
            and (mode is None or stat.S_IMODE(value.st_mode)==mode))


class SQLiteTargetLock:
    def __init__(self,database,parent,directory,lock_path,fds):
        self._pid=os.getpid();self._lock=threading.RLock();self._receipt=None;self._lost=False
        self._database=database;self._parent=parent;self._directory=directory;self._lock_path=lock_path
        self._fds=fds
        self._identities=[_identity(os.fstat(fd)) for fd in fds]

    def _verify(self):
        if self._lost or self._receipt is not None:return False
        paths=(self._database,self._parent,self._directory,self._lock_path)
        for index,(path,fd,identity) in enumerate(zip(paths,self._fds,self._identities)):
            current=os.stat(path,follow_symlinks=False);opened=os.fstat(fd)
            if _identity(current)!=identity or _identity(opened)!=identity:raise RuntimeError('Lock path changed')
            if index in (0,3) and not _private_file(current,0o600 if index==3 else None):raise RuntimeError('Lock file ownership changed')
            if index==1 and not _trusted_parent(current):raise RuntimeError('Database parent trust changed')
            if index==2 and (not stat.S_ISDIR(current.st_mode) or current.st_uid!=os.geteuid() or stat.S_IMODE(current.st_mode)!=0o700):raise RuntimeError('Lock directory ownership changed')
        return True

    def held(self):
        if os.getpid()!=self._pid:return False
        with self._lock:
            try:return self._verify()
            except Exception:self._lost=True;return False

    def close(self):
        if os.getpid()!=self._pid:
            # Close inherited descriptors without LOCK_UN on shared description.
            for fd in self._fds:
                try:os.close(fd)
                except OSError:pass
            self._fds=[]
            return TargetLockClose(False)
        with self._lock:
            if self._receipt is not None:return self._receipt
            okay=self.held()
            import fcntl
            try:fcntl.flock(self._fds[-1],fcntl.LOCK_UN)
            except Exception:okay=False
            for fd in self._fds:
                try:os.close(fd)
                except Exception:okay=False
            self._fds=[];self._receipt=TargetLockClose(okay)
            return self._receipt


def _sqlite(engine,key):
    database=engine.url.database
    if not database or database==':memory:' or engine.url.query or not os.path.isabs(database):return TargetLockResult('unsupported')
    if os.name!='posix' or not hasattr(os,'O_NOFOLLOW'):return TargetLockResult('unsupported')
    fds=[]
    try:
        import fcntl
        database=Path(database);parent=database.parent.resolve(strict=True);database=parent/database.name
        fd=os.open(database,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);fds.append(fd)
        if not _private_file(os.fstat(fd)):raise RuntimeError('Unsafe database identity')
        if not _local_filesystem(fd):return TargetLockResult('unsupported')
        parent_fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC);fds.append(parent_fd)
        if not _trusted_parent(os.fstat(parent_fd)):raise RuntimeError('Database parent is writable or foreign')
        directory_name=database.name+'.odin-sender-locks';directory=parent/directory_name
        try:os.mkdir(directory_name,mode=0o700,dir_fd=parent_fd)
        except FileExistsError:pass
        directory_fd=os.open(directory_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent_fd);fds.append(directory_fd)
        metadata=os.fstat(directory_fd)
        if metadata.st_uid!=os.geteuid() or stat.S_IMODE(metadata.st_mode)!=0o700:raise RuntimeError('Unsafe lock directory')
        filename=key+'.lock';lock_path=directory/filename
        lock_fd=os.open(filename,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=directory_fd);fds.append(lock_fd)
        if not _private_file(os.fstat(lock_fd),0o600):raise RuntimeError('Unsafe lock file')
        try:fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return TargetLockResult('conflict')
        handle=SQLiteTargetLock(database,parent,directory,lock_path,list(fds))
        if not handle.held():raise RuntimeError('Lock identity unconfirmed')
        fds=[]
        return TargetLockResult('acquired',handle)
    except Exception:return TargetLockResult('uncertain')
    finally:
        for fd in fds:
            try:os.close(fd)
            except OSError:pass


def _advisory_key(key):
    value=hashlib.sha256(b'ODIN configured target exclusion v1\x00'+key.encode()).digest()[:8]
    return int.from_bytes(value,'big',signed=True)


class PostgreSQLTargetLock:
    def __init__(self,connection,pid,key,restore_required=False):
        self._connection=connection;self._backend_pid=pid;self._key=key
        self._restore_required=restore_required
        self._pid=os.getpid();self._lock=threading.RLock();self._lost=False;self._receipt=None

    def held(self):
        if os.getpid()!=self._pid:return False
        with self._lock:
            if self._lost or self._receipt is not None:return False
            try:
                from sqlalchemy import text
                from core.database_config import RESTORE_ADVISORY_LOCK
                from psycopg.pq import TransactionStatus
                if self._connection.connection.driver_connection.info.transaction_status != TransactionStatus.IDLE:
                    raise RuntimeError('Target session transaction is not idle')
                value=self._key&((1<<64)-1)
                barrier=RESTORE_ADVISORY_LOCK&((1<<64)-1)
                row=self._connection.execute(text('SELECT pg_backend_pid() AS pid, EXISTS('
                    "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() "
                    "AND classid=:upper AND objid=:lower AND objsubid=1 AND mode='ExclusiveLock' AND granted) AS held, EXISTS("
                    "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() "
                    "AND classid=:restore_upper AND objid=:restore_lower AND objsubid=1 AND mode='ShareLock' AND granted) AS restore_held"),
                    {'upper':value>>32,'lower':value&0xffffffff,
                     'restore_upper':barrier>>32,'restore_lower':barrier&0xffffffff}).one()
                if (row.pid!=self._backend_pid or row.held is not True
                        or self._restore_required and row.restore_held is not True):
                    raise RuntimeError('Owned session lock lost')
                return True
            except Exception:self._lost=True;return False

    def close(self):
        if os.getpid()!=self._pid:
            # Do not finalize inherited libpq/DBAPI connections. Caller must
            # exec/_exit, retaining this handle until then; no safe fork reuse.
            return TargetLockClose(False)
        with self._lock:
            if self._receipt is not None:return self._receipt
            okay=self.held()
            try:
                from sqlalchemy import text
                if okay:
                    okay=self._connection.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':self._key}).scalar_one() is True
            except Exception:okay=False
            try:self._connection.invalidate()
            except Exception:okay=False
            try:self._connection.close()
            except Exception:okay=False
            self._receipt=TargetLockClose(okay)
            return self._receipt


def _postgres(engine,key):
    from sqlalchemy import text
    connection=None
    try:
        from core.database_config import RESTORE_ADVISORY_LOCK
        from psycopg.pq import TransactionStatus
        advisory=_advisory_key(key)
        if advisory==RESTORE_ADVISORY_LOCK:raise RuntimeError('Target collides with restore barrier')
        connection=engine.connect().execution_options(isolation_level='AUTOCOMMIT')
        if connection.connection.driver_connection.info.transaction_status != TransactionStatus.IDLE:
            raise RuntimeError('Target session transaction is not idle')
        barrier=RESTORE_ADVISORY_LOCK&((1<<64)-1)
        row=connection.execute(text('SELECT pg_backend_pid() AS pid, EXISTS('
            "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() "
            "AND NOT (classid=:upper AND objid=:lower AND objsubid=1 AND mode='ShareLock' AND granted)) AS occupied, EXISTS("
            "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND pid=pg_backend_pid() "
            "AND classid=:upper AND objid=:lower AND objsubid=1 AND mode='ShareLock' AND granted) AS restore_held"),
            {'upper':barrier>>32,'lower':barrier&0xffffffff}).one()
        required=bool(getattr(engine,'_odin_restore_barrier_required',False) or row.restore_held)
        if row.occupied or required and row.restore_held is not True:raise RuntimeError('Session is not clean')
        acquired=connection.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':advisory}).scalar_one()
        if acquired is not True:return TargetLockResult('conflict')
        handle=PostgreSQLTargetLock(connection,row.pid,advisory,required)
        if not handle.held():raise RuntimeError('Owned session lock unconfirmed')
        connection=None
        return TargetLockResult('acquired',handle)
    except Exception:return TargetLockResult('uncertain')
    finally:
        if connection is not None:
            try:connection.invalidate()
            except Exception:pass
            try:connection.close()
            except Exception:pass


def acquire_target_lock(engine,physical_key):
    if type(physical_key) is not str or not re.fullmatch('[0-9a-f]{64}',physical_key):return TargetLockResult('unsupported')
    try:
        if engine.dialect.name=='sqlite':return _sqlite(engine,physical_key)
        if engine.dialect.name=='postgresql':return _postgres(engine,physical_key)
    except Exception:return TargetLockResult('uncertain')
    return TargetLockResult('unsupported')
