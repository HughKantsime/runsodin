"""Read-only durable fence for cooperating ODIN PostgreSQL consumers."""
from __future__ import annotations
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path

MARKER='restore-pending-postgres.json'
MAX_MARKER_BYTES=8192
BASE_KEYS={'schema_version','dialect','state','sha256','size_bytes','actor_id','staged_at'}
PROGRESS_KEYS={'rollback_backup','rollback_sha256','rollback_size_bytes','apply_started_at'}


class RestoreConsumerBlocked(RuntimeError):
    """Recovery or storage verification must finish before application DB work."""


def _refuse():
    raise RestoreConsumerBlocked('PostgreSQL restore state is unavailable or requires recovery') from None


def _identity(value):
    return (value.st_dev,value.st_ino,value.st_uid,value.st_gid,value.st_mode)


def _file_identity(value):
    return _identity(value)+(value.st_nlink,value.st_size,value.st_mtime_ns,value.st_ctime_ns)


def _object(pairs):
    value={}
    for key,item in pairs:
        if key in value:_refuse()
        value[key]=item
    return value


def _timestamp(value):
    if type(value) is not str or not 1<=len(value)<=64:_refuse()
    try:parsed=datetime.fromisoformat(value)
    except ValueError:_refuse()
    if parsed.tzinfo is None or parsed.utcoffset() is None:_refuse()


def _checksum(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:_refuse()


def _size(value):
    if type(value) is not int or not 100<=value<=104857600:_refuse()


def _manifest(value):
    if type(value) is not dict or type(value.get('schema_version')) is not int or value['schema_version']!=1 or value.get('dialect')!='postgresql':_refuse()
    state=value.get('state')
    if state not in ('staged','restore_in_progress'):_refuse()
    required=BASE_KEYS if state=='staged' else BASE_KEYS|PROGRESS_KEYS
    allowed=required if state=='staged' else required|{'recovery_incomplete','recovery_error'}
    if not required<=value.keys() or not value.keys()<=allowed:_refuse()
    _checksum(value['sha256']);_size(value['size_bytes']);_timestamp(value['staged_at'])
    if value['actor_id'] is not None and (type(value['actor_id']) is not int or value['actor_id']<=0):_refuse()
    if state!='staged':_refuse()


def assert_postgres_restore_ready(data_directory=None):
    """No DB, writes, lockfile, archive access, path adoption or FD-number retry."""
    configured=data_directory if data_directory is not None else os.getenv('ODIN_DATA_DIR','/data')
    if not isinstance(configured,(str,Path)) or not str(configured) or not Path(configured).is_absolute():_refuse()
    path=Path(configured)
    if any(part in ('.','..') for part in str(configured).split('/')):_refuse()
    descriptors=[];ancestors=[]
    try:
        descriptor=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(descriptor)
        for part in path.parts[1:]:
            parent=descriptor;before=os.stat(part,dir_fd=parent,follow_symlinks=False)
            descriptor=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);descriptors.append(descriptor)
            identity=_identity(os.fstat(descriptor))
            if identity!=_identity(before):_refuse()
            ancestors.append((parent,part,descriptor,identity))
        root=os.fstat(descriptor)
        if root.st_uid not in (0,os.geteuid()) or root.st_mode&0o022:_refuse()
        try:before=os.stat(MARKER,dir_fd=descriptor,follow_symlinks=False)
        except FileNotFoundError:before=None
        if before is not None:
            if not stat.S_ISREG(before.st_mode) or before.st_nlink!=1 or before.st_uid not in (0,os.geteuid()) or stat.S_IMODE(before.st_mode)!=0o600 or not 1<=before.st_size<=MAX_MARKER_BYTES:_refuse()
            marker=os.open(MARKER,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=descriptor);descriptors.append(marker)
            if _file_identity(os.fstat(marker))!=_file_identity(before):_refuse()
            data=bytearray()
            while len(data)<=MAX_MARKER_BYTES:
                chunk=os.read(marker,min(4096,MAX_MARKER_BYTES+1-len(data)))
                if not chunk:break
                data.extend(chunk)
            if len(data)>MAX_MARKER_BYTES or len(data)!=before.st_size:_refuse()
            if _file_identity(os.fstat(marker))!=_file_identity(before) or _file_identity(os.stat(MARKER,dir_fd=descriptor,follow_symlinks=False))!=_file_identity(before):_refuse()
            value=json.loads(data.decode('utf-8'),object_pairs_hook=_object,parse_constant=lambda _: _refuse())
            _manifest(value)
        # A successful absent/staged decision belongs to this original directory.
        for parent,name,opened,identity in ancestors:
            if _identity(os.fstat(opened))!=identity or _identity(os.stat(name,dir_fd=parent,follow_symlinks=False))!=identity:_refuse()
        if before is None:
            try:os.stat(MARKER,dir_fd=descriptor,follow_symlinks=False)
            except FileNotFoundError:pass
            else:_refuse()
        elif _file_identity(os.stat(MARKER,dir_fd=descriptor,follow_symlinks=False))!=_file_identity(before):_refuse()
    except RestoreConsumerBlocked:raise
    except (OSError,ValueError,TypeError,RecursionError):_refuse()
    finally:
        uncertain=False
        while descriptors:
            owned=descriptors.pop()
            try:os.close(owned)
            except OSError:uncertain=True
        if uncertain:_refuse()
