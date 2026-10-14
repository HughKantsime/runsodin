"""Internal typed media descriptors; no arbitrary subprocess descriptor API."""
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import sys
from types import MappingProxyType

ROLES=('guard','namespace','output')
MARKERS=tuple('{ODIN_MEDIA_'+role.upper()+'}' for role in ROLES)
KEYS=('device','inode','uid','gid','mode')
IDENTITY_KEYS=KEYS+('link_count','byte_count','mtime_ns','ctime_ns')


def identity(fd):
    value=os.fstat(fd)
    return dict(zip(KEYS,(value.st_dev,value.st_ino,value.st_uid,value.st_gid,value.st_mode)),
        link_count=value.st_nlink,byte_count=value.st_size,mtime_ns=value.st_mtime_ns,ctime_ns=value.st_ctime_ns)


def validate(fd,role,expected):
    if isinstance(expected,MappingProxyType):expected=dict(expected)
    if type(fd) is not int or fd<0 or role not in ROLES or type(expected) is not dict or set(expected)!=set(IDENTITY_KEYS):
        raise ValueError('Invalid media descriptor')
    if any(type(value) is not int or value<0 for value in expected.values()) or expected['inode']<=0:
        raise ValueError('Invalid media identity')
    actual=identity(fd)
    if actual!=expected:raise ValueError('Media identity differs')
    flags=fcntl.fcntl(fd,fcntl.F_GETFL)
    wanted=os.O_RDONLY if role=='namespace' else os.O_RDWR
    if flags & os.O_ACCMODE!=wanted or flags & (os.O_APPEND|getattr(os,'O_PATH',0)):
        raise ValueError('Media descriptor access differs')
    if role=='namespace':
        if actual['mode']!=stat.S_IFDIR|0o700:raise ValueError('Media directory differs')
    else:
        if actual['mode']!=stat.S_IFREG|0o600 or actual['link_count']!=1 or actual['byte_count']!=0:
            raise ValueError('Media creation differs')
        os.lseek(fd,0,os.SEEK_CUR)
        if role=='guard':fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return actual


@dataclass(frozen=True)
class MediaEncodeCapability:
    """Created from live guard and confirmed creation facts; never persisted FDs."""
    descriptors: tuple
    facts: tuple
    guard: object

    def __post_init__(self):
        if type(self.descriptors) is not tuple or len(self.descriptors)!=3 or type(self.facts) is not tuple or len(self.facts)!=3:
            raise ValueError('Invalid media capability')
        object.__setattr__(self,'facts',tuple(MappingProxyType(dict(row)) for row in self.facts))

    @classmethod
    def from_receipts(cls,guard,namespace_fd,output_fd,namespace_facts,creation_facts):
        from core.timelapse_media_lock import MediaGuard
        if not isinstance(guard,MediaGuard):raise ValueError('Media guard required')
        guard.assert_owned()
        descriptors=(guard.fileno(),namespace_fd,output_fd)
        namespace=identity(namespace_fd)
        if any(type(namespace_facts[key]) is not int or namespace_facts[key]<0 or namespace[key]!=namespace_facts[key] for key in KEYS):raise ValueError('Namespace receipt differs')
        output={key:creation_facts[key] for key in IDENTITY_KEYS}
        if creation_facts.get('digest_sha256', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')!='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855':raise ValueError('Output creation digest differs')
        facts=(identity(descriptors[0]),namespace,output)
        for fd,role,expected in zip(descriptors,ROLES,facts):validate(fd,role,expected)
        if len({(row['device'],row['inode']) for row in facts})!=3 or any(
            tuple(row[key] for key in ('device','uid','gid'))!=tuple(facts[0][key] for key in ('device','uid','gid')) for row in facts):
            raise ValueError('Media objects overlap or ownership differs')
        return cls(descriptors,tuple(facts),guard)


def worker_command():
    return [sys.executable,str(Path(__file__).with_name('timelapse_media_worker.py')),
        '{ODIN_INPUT_FD}','{ODIN_RESULT_FD}',*MARKERS]


def prepare(capability,argv):
    if type(capability) is not MediaEncodeCapability or list(argv)!=worker_command():
        raise ValueError('Fixed media worker required')
    from core.timelapse_media_lock import MediaGuard
    if not isinstance(capability.guard,MediaGuard) or capability.descriptors[0]!=capability.guard.fileno():raise ValueError('Original media guard required')
    capability.guard.assert_owned()
    copies=[]
    try:
        for fd,role,facts in zip(capability.descriptors,ROLES,capability.facts):
            validate(fd,role,facts)
            copy=os.dup(fd);copies.append(copy);os.set_inheritable(copy,False)
            validate(copy,role,facts)
        return dict(zip(MARKERS,map(str,copies))),tuple(copies),{
            'version':1,'roles':list(ROLES),'facts':[dict(row) for row in capability.facts],'fds':copies}
    except Exception:
        # Ownership of failed closes must remain with the caller.
        pending=[]
        for fd in copies:
            try:os.close(fd)
            except OSError:pending.append(fd)
        if pending:raise MediaCopyCleanupError(pending) from None
        raise ValueError('Media capability unavailable') from None


class MediaCopyCleanupError(ValueError):
    def __init__(self,pending):
        super().__init__('Media descriptor cleanup unconfirmed');self.pending=pending


def validate_envelope(record):
    if type(record) is not dict or set(record)!= {'version','roles','facts','fds'} or type(record['version']) is not int or record['version']!=1 or record['roles']!=list(ROLES):
        raise ValueError('Invalid media envelope')
    if type(record['fds']) is not list or type(record['facts']) is not list or len(record['fds'])!=3 or len(record['facts'])!=3:
        raise ValueError('Invalid media roles')
    for fd,role,facts in zip(record['fds'],ROLES,record['facts']):validate(fd,role,facts)
    if len(set(record['fds']))!=3 or len({(row['device'],row['inode']) for row in record['facts']})!=3:
        raise ValueError('Media identities overlap')
    if any(tuple(row[key] for key in ('device','uid','gid'))!=tuple(record['facts'][0][key] for key in ('device','uid','gid')) for row in record['facts']):
        raise ValueError('Media owners differ')
    return tuple(record['fds'])


def complete_media_transport(group,manifest,*,input_writer,result_reader,namespace_fd,output_fd,private_parent_fd,private_parent_facts,deadline):
    """Synchronous transport/verification receipt; caller owns DB phase and all FDs.

    No receipt is authority to publish. The caller must recheck current permission.
    Uncertain output remains untouched; this function never retries a worker.
    """
    import json,select,time
    from core.timelapse_media_worker import checked_manifest,read_frame,file_facts,MAX_MANIFEST_BYTES
    manifest=checked_manifest(manifest)
    encoded=(json.dumps(manifest,separators=(',',':'))+'\n').encode()
    if len(encoded)>MAX_MANIFEST_BYTES or type(deadline) not in (int,float) or not 0<deadline-time.monotonic()<=300:
        raise ValueError('Media transport limit')
    capability=group._media_capability
    if type(capability) is not MediaEncodeCapability:raise ValueError('Media capability required')
    capability.guard.assert_owned()
    validate(namespace_fd,'namespace',capability.facts[1])
    validate(output_fd,'output',capability.facts[2])
    parent=os.fstat(private_parent_fd)
    if any(getattr(parent,{'device':'st_dev','inode':'st_ino','uid':'st_uid','gid':'st_gid','mode':'st_mode'}[key])!=private_parent_facts[key] for key in KEYS) or parent.st_mode!=stat.S_IFDIR|0o700:
        raise ValueError('Private output parent differs')
    if any(getattr(parent,{'device':'st_dev','uid':'st_uid','gid':'st_gid'}[key])!=capability.facts[2][key] for key in ('device','uid','gid')):raise ValueError('Private output owner differs')
    named=os.stat('output.mp4',dir_fd=private_parent_fd,follow_symlinks=False)
    if (named.st_dev,named.st_ino)!=(capability.facts[2]['device'],capability.facts[2]['inode']):raise ValueError('Private output name differs')
    for fd,role,facts in zip(capability.descriptors,ROLES,capability.facts):validate(fd,role,facts)
    result=None;completion=None;received=bytearray();offset=0
    try:
        if not group.arm().worker_started:raise ValueError('Media launch unconfirmed')
        os.set_blocking(input_writer,False);os.set_blocking(result_reader,False)
        while result is None or offset<len(encoded):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('Media transport deadline')
            readable,writable,_=select.select([result_reader],[input_writer] if offset<len(encoded) else [],[],remaining)
            if writable:
                try:offset+=os.write(input_writer,encoded[offset:offset+65536])
                except BlockingIOError:pass
            if readable:
                try:block=os.read(result_reader,8193-len(received))
                except BlockingIOError:continue
                if not block:raise ValueError('Media result absent')
                received.extend(block)
                if len(received)>8192:raise ValueError('Media result limit')
                if received.endswith(b'\n'):
                    result=json.loads(received)
                    if offset!=len(encoded):raise ValueError('Premature media result')
        completion=group.wait_worker(deadline)
    finally:
        retirement=group.retire()
    if (completion is None or not completion.worker_exited_normally or not retirement.group_empty
            or not retirement.channels_closed or not retirement.media_copies_closed):raise ValueError('Media completion unconfirmed')
    if type(result) is not dict or set(result)!= {'operation_id','recipe_version','encode_fps','frame_count','output'}:
        raise ValueError('Media result differs')
    for key,expected in (('operation_id',manifest['operation_id']),('recipe_version',1),('encode_fps',30),('frame_count',len(manifest['frames']))):
        if type(result[key]) is not type(expected) or result[key]!=expected:raise ValueError('Media result differs')
    capability.guard.assert_owned()
    for frame in manifest['frames']:read_frame(namespace_fd,frame,capability.facts[0],deadline)
    actual=file_facts(output_fd,deadline)
    if type(result['output']) is not dict or any(type(result['output'].get(key)) is not int for key in KEYS+('link_count','byte_count','mtime_ns','ctime_ns')):raise ValueError('Media output facts differ')
    if actual!=result['output'] or actual['byte_count']<=0 or any(actual[key]!=capability.facts[2][key] for key in KEYS+('link_count',)):
        raise ValueError('Media output differs')
    current=os.fstat(private_parent_fd)
    if (current.st_dev,current.st_ino,current.st_uid,current.st_gid,current.st_mode)!=(parent.st_dev,parent.st_ino,parent.st_uid,parent.st_gid,parent.st_mode):raise ValueError('Private parent changed')
    named=os.stat('output.mp4',dir_fd=private_parent_fd,follow_symlinks=False)
    if (named.st_dev,named.st_ino,named.st_mode,named.st_nlink)!=(actual['device'],actual['inode'],actual['mode'],actual['link_count']):raise ValueError('Private output name changed')
    os.fsync(output_fd);os.fsync(private_parent_fd)
    if file_facts(output_fd,deadline)!=actual:raise ValueError('Media output changed')
    capability.guard.assert_owned()
    return actual
