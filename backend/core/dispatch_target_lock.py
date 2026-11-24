"""Owned configured-target exclusion, never sender-quiescence/recovery proof.

SQLite descriptors are private and must not be duplicated/unlocked externally.
Inherited PostgreSQL handles must not touch DBAPI; fork children must exec or
_exit without connection finalization. Both ownership scopes are PID pinned.
"""
from contextlib import ExitStack
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
import uuid


@dataclass(frozen=True)
class TargetLockResult:
    status: str
    handle: object = field(default=None,repr=False)
    reason: str | None = None
    message: str | None = None


@dataclass(frozen=True)
class TargetLockClose:
    released: bool
    scope: str = 'configured-target lock only; no sender or transport cleanup proof'


def _filesystem_type(fd):
    if os.name!='posix':return None
    libc=ctypes.CDLL(None,use_errno=True)
    info=(ctypes.c_longlong*512)()
    if sys.platform=='darwin':
        # SDK sys/mount.h __DARWIN_STRUCT_STATFS64: uint32 f_flags offset64.
        if platform.machine() not in ('arm64','x86_64') or ctypes.sizeof(ctypes.c_void_p)!=8:return None
        call=getattr(libc,'fstatfs64',None)
        if call is None:return None
    elif sys.platform.startswith('linux'):
        if ctypes.sizeof(ctypes.c_long)!=8:return None
        call=libc.fstatfs
    else:return None
    call.argtypes=(ctypes.c_int,ctypes.c_void_p);call.restype=ctypes.c_int
    if call(fd,ctypes.byref(info))!=0:return None
    if sys.platform=='darwin':
        flags=ctypes.c_uint32.from_buffer(info,64).value
        name=bytes(info)[72:88].split(b'\x00')[0]
        if flags&0x1000 and name in (b'apfs',b'hfs'):
            return 'local'
        return {b'nfs':'NFS', b'smbfs':'SMB'}.get(name)
    return ctypes.cast(ctypes.byref(info),ctypes.POINTER(ctypes.c_long))[0]&0xffffffff


_LOCAL_TYPES = ('local', 0xef53, 0x794c7630, 0x01021994, 0x58465342, 0x9123683e, 0x2fc12fc1)
_SHARED_TYPES = (0x65735546, 0x6a656a63)
_NETWORK_TYPES = {0x6969: 'NFS', 0xff534d42: 'CIFS', 0xfe534d42: 'SMB', 0x01021997: '9p'}


_SEPARATE_MOUNT = TargetLockResult('unsupported', reason='filesystem', message=(
    'Keep the ODIN database and its printer lock files together on the same local disk or Docker volume. No command was sent.'))


def _local_filesystem(fd):
    return _filesystem_type(fd) in _LOCAL_TYPES


def _mountinfo_type(folder, device):
    """Find the innermost mount containing this directory on the opened device."""
    dev = f'{os.major(device)}:{os.minor(device)}'
    depth, kind = 0, None
    try:
        lines = os.fsdecode(Path('/proc/self/mountinfo').read_bytes()).split('\n')
        for line in filter(None, lines):
            mount, filesystem = line.split(' - ', 1)
            fields = mount.split(' ')
            if fields[2] != dev:
                continue
            point = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4]))
            if not folder.is_relative_to(point):
                continue
            name = filesystem.split(' ', 1)[0]
            if len(point.parts) > depth:
                depth, kind = len(point.parts), name
            elif len(point.parts) == depth and kind != name:
                kind = None  # Ambiguous stacked mounts.
    except (OSError, ValueError, IndexError):
        return None
    return kind


_warned_storage_causes = {}


def warn_storage_once(logger, refusal):
    # setdefault elects one caller even when different pollers run concurrently.
    first = object()
    if _warned_storage_causes.setdefault((os.getpid(), refusal.reason), first) is first:
        logger.warning('%s', refusal.message)


def _storage_refusal(storage, folder='the data folder'):
    return TargetLockResult('unsupported', reason='filesystem', message=(
        f'ODIN cannot safely lock printers on this kind of storage ({folder} is on {storage}). '
        'Keep the data folder on a local disk or a Docker volume. No command was sent.'))


def _owner_refusal(item, *, folder=False, allow_root=False):
    owner = f'the ODIN user (UID {os.geteuid()})'
    if allow_root:
        owner += ' or root (UID 0)'
    return TargetLockResult('uncertain', reason='folder_owner' if folder else 'file_owner',
                            message=f'{item} must be owned by {owner}. No command was sent.')


_LOCK_FILE_PERMISSIONS = TargetLockResult('uncertain', reason='lock_permissions', message=(
    'Set the printer lock file inside the ODIN data folder to mode 600. No command was sent.'))


def _inaccessible_owner(path, parent_fd, label, *, folder=False, allow_root=False):
    """An owner-only directory or file may deny open before we can fstat it."""
    info = os.stat(path, dir_fd=parent_fd, follow_symlinks=False)
    if info.st_uid != os.geteuid() and not (allow_root and info.st_uid == 0):
        return _owner_refusal(label, folder=folder, allow_root=allow_root)
    if not folder and _private_file(info) and stat.S_IMODE(info.st_mode) != 0o600:
        return _LOCK_FILE_PERMISSIONS
    raise PermissionError


def _prove_exclusion(directory_fd):
    """Two independent opens on a fresh private inode, even during concurrent probes."""
    import fcntl
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
    exclusive = fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
        name = '.flock-test-' + uuid.uuid4().hex
        with ExitStack() as cleanup:
            first = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory_fd)
            cleanup.callback(os.unlink, name, dir_fd=directory_fd)
            cleanup.callback(os.close, first)
            second = os.open(name, flags, dir_fd=directory_fd)
            cleanup.callback(os.close, second)
            first_stat = os.fstat(first)
            if (not _private_file(first_stat, 0o600) or _identity(first_stat) != _identity(os.fstat(second))
                    or first_stat.st_dev != os.fstat(directory_fd).st_dev):
                return False
            fcntl.flock(first, exclusive)
            try:
                fcntl.flock(second, exclusive)
            except BlockingIOError:
                fcntl.flock(first, fcntl.LOCK_UN)
                fcntl.flock(second, exclusive)
                return True
            return False
    except Exception:
        return False


def _identity(value):return (value.st_dev,value.st_ino)


def _trusted_parent(value):
    return (stat.S_ISDIR(value.st_mode) and value.st_uid in (0,os.geteuid())
            and not stat.S_IMODE(value.st_mode)&0o022)


def _private_file(value,mode=None):
    return (stat.S_ISREG(value.st_mode) and value.st_uid==os.geteuid() and value.st_nlink==1
            and (mode is None or stat.S_IMODE(value.st_mode)==mode))


def _database_stat(parent_fd,name):
    """The database's identity, without ever opening it.

    On POSIX, closing any descriptor for a file drops every lock this process holds on that file,
    SQLite's included. Another process can then delete the WAL under a live connection, which
    corrupts the database on SQLite before 3.51. So nothing here may hold a descriptor on it.
    """
    return os.stat(name,dir_fd=parent_fd,follow_symlinks=False)


class SQLiteTargetLock:
    def __init__(self,database,parent,directory,lock_path,fds,database_identity):
        self._pid=os.getpid();self._lock=threading.RLock();self._receipt=None;self._lost=False
        self._database=database;self._parent=parent;self._directory=directory;self._lock_path=lock_path
        self._fds=fds;self._database_identity=database_identity
        self._identities=[_identity(os.fstat(fd)) for fd in fds]

    def _verify(self):
        if self._lost or self._receipt is not None:return False
        current=_database_stat(self._fds[0],self._database.name)
        if _identity(current)!=self._database_identity:raise RuntimeError('Lock path changed')
        if not _private_file(current):raise RuntimeError('Lock file ownership changed')
        paths=(self._parent,self._directory,self._lock_path)
        for index,(path,fd,identity) in enumerate(zip(paths,self._fds,self._identities)):
            current=os.stat(path,follow_symlinks=False);opened=os.fstat(fd)
            if _identity(current)!=identity or _identity(opened)!=identity:raise RuntimeError('Lock path changed')
            if index==0 and not _trusted_parent(current):raise RuntimeError('Database parent trust changed')
            if index==1 and (not stat.S_ISDIR(current.st_mode) or current.st_uid!=os.geteuid() or stat.S_IMODE(current.st_mode)!=0o700):raise RuntimeError('Lock directory ownership changed')
            if index==2 and not _private_file(current,0o600):raise RuntimeError('Lock file ownership changed')
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
    if not database or database==':memory:' or engine.url.query or not os.path.isabs(database):
        return TargetLockResult('unsupported', reason='filesystem', message=(
            'ODIN needs a database file in a local data folder or Docker volume to lock printers. No command was sent.'))
    if os.name!='posix' or not hasattr(os,'O_NOFOLLOW'):return _storage_refusal('unsupported storage')
    fds=[]
    try:
        import fcntl
        database=Path(database);parent=database.parent.resolve(strict=True);database=parent/database.name
        folder_label = f'The ODIN data folder ({ascii(parent.name or "/")})'
        dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            parent_fd = os.open(parent, dir_flags)
        except PermissionError:
            return _inaccessible_owner(parent, None, folder_label, folder=True, allow_root=True)
        fds.append(parent_fd)
        identity=_database_stat(parent_fd,database.name)
        if identity.st_uid != os.geteuid():
            return _owner_refusal(f'The database file {ascii(database.name)} in the ODIN data folder')
        if not _private_file(identity):raise RuntimeError('Unsafe database identity')
        # The parent stands in for the file here: a database mounted separately from its directory can't be checked.
        parent_stat = os.fstat(parent_fd)
        if identity.st_dev != parent_stat.st_dev:
            return _SEPARATE_MOUNT
        shared = None
        if not _local_filesystem(parent_fd):
            kind = _filesystem_type(parent_fd)
            if kind in _SHARED_TYPES:
                shared = _mountinfo_type(parent, parent_stat.st_dev)
                if shared != 'virtiofs':
                    return _storage_refusal(shared or 'unrecognized storage', ascii(parent.name or '/'))
            else:
                label = _NETWORK_TYPES.get(kind, kind)
                if isinstance(label, int):
                    label = f'storage type 0x{label:x}'
                return _storage_refusal(label or 'unrecognized storage', ascii(parent.name or '/'))
        if not _trusted_parent(parent_stat):
            if parent_stat.st_uid not in (0, os.geteuid()):
                return _owner_refusal(folder_label, folder=True, allow_root=True)
            mode = stat.S_IMODE(parent_stat.st_mode)
            return TargetLockResult('uncertain', reason='folder_permissions', message=(
                f'The ODIN data folder is writable by other users (mode {mode:o}). '
                'Set it to 755 (chmod go-w on the folder), then try again. No command was sent.'))
        directory_name=database.name+'.odin-sender-locks';directory=parent/directory_name
        lock_folder_label = f'The lock folder {ascii(directory_name)} inside the ODIN data folder'
        try:
            os.mkdir(directory_name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except PermissionError:
            return TargetLockResult('uncertain', reason='lock_permissions', message=(
                f'ODIN cannot create {ascii(directory_name)} inside the data folder {ascii(parent.name or "/")}. '
                f'Give the ODIN user (UID {os.geteuid()}) ownership and write access to the data folder. No command was sent.'))
        try:
            directory_fd = os.open(directory_name, dir_flags, dir_fd=parent_fd)
        except PermissionError:
            return _inaccessible_owner(directory_name, parent_fd, lock_folder_label, folder=True)
        fds.append(directory_fd)
        metadata=os.fstat(directory_fd)
        if metadata.st_dev != parent_stat.st_dev:
            return _SEPARATE_MOUNT
        if metadata.st_uid!=os.geteuid():return _owner_refusal(lock_folder_label, folder=True)
        if stat.S_IMODE(metadata.st_mode)!=0o700:
            return TargetLockResult('uncertain', reason='lock_permissions', message=(
                'Set the printer lock folder inside the ODIN data folder to mode 700. No command was sent.'))
        filename=key+'.lock';lock_path=directory/filename
        try:
            lock_fd = os.open(filename, os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
        except PermissionError:
            return _inaccessible_owner(filename, directory_fd, 'The printer lock file inside the ODIN data folder')
        fds.append(lock_fd)
        lock_stat = os.fstat(lock_fd)
        if lock_stat.st_dev != metadata.st_dev:
            return _SEPARATE_MOUNT
        if lock_stat.st_uid!=os.geteuid():return _owner_refusal('The printer lock file inside the ODIN data folder')
        if not _private_file(lock_stat,0o600):
            if _private_file(lock_stat):
                return _LOCK_FILE_PERMISSIONS
            raise RuntimeError('Unsafe lock file')
        if shared and not _prove_exclusion(directory_fd):
            return _storage_refusal(shared, ascii(parent.name or '/'))
        try:fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return TargetLockResult('conflict')
        handle=SQLiteTargetLock(database,parent,directory,lock_path,list(fds),_identity(identity))
        if not handle.held():raise RuntimeError('Lock identity unconfirmed')
        fds=[]
        return TargetLockResult('acquired',handle)
    except Exception:
        return TargetLockResult('uncertain', reason='other', message=(
            'ODIN could not verify its database and printer lock files. Check the files and permissions in the ODIN data folder. No command was sent.'))
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


def data_folder_lock_warning(engine):
    """Use one reserved key so repeated checks leave only one ordinary lock file."""
    try:
        if engine.dialect.name != 'sqlite':
            return None
        key = hashlib.sha256(b'ODIN data folder startup check').hexdigest()
        result = acquire_target_lock(engine, key)
        if result.handle is not None and not result.handle.close().released:
            return 'ODIN could not confirm that the printer lock file was released. Check the ODIN data folder. No command was sent.'
        return result.message
    except Exception:
        return 'ODIN could not check the printer lock files in its data folder. No command was sent.'


def acquire_target_lock(engine,physical_key):
    if type(physical_key) is not str or not re.fullmatch('[0-9a-f]{64}',physical_key):return TargetLockResult('unsupported')
    try:
        if engine.dialect.name=='sqlite':return _sqlite(engine,physical_key)
        if engine.dialect.name=='postgresql':return _postgres(engine,physical_key)
    except Exception:return TargetLockResult('uncertain')
    return TargetLockResult('unsupported')
