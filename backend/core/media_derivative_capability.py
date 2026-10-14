"""Internal verified MP4-source capability, distinct from JPEG encode authority."""
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import sys
from types import MappingProxyType
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.media_process_capability import identity,IDENTITY_KEYS,KEYS,MediaCopyCleanupError

ROLES=('guard','source','output')
MARKERS=tuple('{ODIN_MEDIA_'+role.upper()+'}' for role in ROLES)

def validate(fd,role,expected=None):
    if type(fd) is not int or fd<0 or role not in ROLES:raise ValueError('Invalid derivative descriptor')
    actual=identity(fd)
    if expected is not None and (set(expected)!=set(IDENTITY_KEYS) or any(type(v) is not int for v in expected.values()) or actual!=dict(expected)):
        raise ValueError('Derivative descriptor identity differs')
    flags=fcntl.fcntl(fd,fcntl.F_GETFL)
    if flags&os.O_ACCMODE!=(os.O_RDONLY if role=='source' else os.O_RDWR) or flags&(os.O_APPEND|getattr(os,'O_PATH',0)):
        raise ValueError('Derivative descriptor access differs')
    if actual['mode']!=stat.S_IFREG|0o600 or actual['link_count']!=1:raise ValueError('Derivative file identity differs')
    if role=='source':
        if not 0<actual['byte_count']<=32*1024**3:raise ValueError('Derivative source limit')
    elif actual['byte_count']!=0:raise ValueError('Derivative creation differs')
    os.lseek(fd,0,os.SEEK_CUR)
    if role=='guard':fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return actual

@dataclass(frozen=True)
class MediaDerivativeCapability:
    descriptors:tuple
    facts:tuple
    guard:object
    def __post_init__(self):
        if type(self.descriptors) is not tuple or len(self.descriptors)!=3 or type(self.facts) is not tuple or len(self.facts)!=3:
            raise ValueError('Invalid derivative capability')
        object.__setattr__(self,'facts',tuple(MappingProxyType(dict(row)) for row in self.facts))
    @classmethod
    def from_receipts(cls,guard,source_fd,output_fd,source_facts,creation_facts):
        from core.timelapse_media_lock import MediaGuard
        if not isinstance(guard,MediaGuard):raise ValueError('Original media guard required')
        guard.assert_owned()
        if creation_facts.get('digest_sha256')!='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855':raise ValueError('Derivative creation digest differs')
        descriptors=(guard.fileno(),source_fd,output_fd)
        facts=(identity(descriptors[0]),{key:source_facts[key] for key in IDENTITY_KEYS},
               {key:creation_facts[key] for key in IDENTITY_KEYS})
        for fd,role,expected in zip(descriptors,ROLES,facts):validate(fd,role,expected)
        distinct(facts)
        return cls(descriptors,facts,guard)

def distinct(facts):
    if len({(row['device'],row['inode']) for row in facts})!=3 or any(tuple(row[k] for k in ('device','uid','gid'))!=tuple(facts[0][k] for k in ('device','uid','gid')) for row in facts):
        raise ValueError('Derivative identities overlap or owner differs')

def worker_command():
    return [sys.executable,str(Path(__file__).with_name('timelapse_derivative_worker.py')),'{ODIN_INPUT_FD}','{ODIN_RESULT_FD}',*MARKERS]

def prepare(capability,argv):
    if type(capability) is not MediaDerivativeCapability or list(argv)!=worker_command():raise ValueError('Fixed derivative worker required')
    capability.guard.assert_owned()
    if capability.descriptors[0]!=capability.guard.fileno():raise ValueError('Original derivative guard required')
    copies=[]
    try:
        for fd,role,facts in zip(capability.descriptors,ROLES,capability.facts):
            validate(fd,role,facts);copy=os.dup(fd);copies.append(copy);os.set_inheritable(copy,False);validate(copy,role,facts)
        return dict(zip(MARKERS,map(str,copies))),tuple(copies),{'version':1,'roles':list(ROLES),'facts':[dict(row) for row in capability.facts],'fds':copies}
    except Exception:
        pending=[]
        for fd in copies:
            try:os.close(fd)
            except OSError:pending.append(fd)
        if pending:raise MediaCopyCleanupError(pending) from None
        raise ValueError('Derivative capability unavailable') from None

def validate_envelope(record):
    if type(record) is not dict or set(record)!={'version','roles','facts','fds'} or type(record['version']) is not int or record['version']!=1 or record['roles']!=list(ROLES):raise ValueError('Invalid derivative envelope')
    if type(record['fds']) is not list or type(record['facts']) is not list or len(record['fds'])!=3 or len(record['facts'])!=3:raise ValueError('Invalid derivative roles')
    for fd,role,facts in zip(record['fds'],ROLES,record['facts']):validate(fd,role,facts)
    distinct(record['facts'])
    if len(set(record['fds']))!=3:raise ValueError('Derivative descriptors overlap')
    return tuple(record['fds'])

def complete_derivative_transport(group,manifest,*,input_writer,result_reader,source_fd,output_fd,deadline):
    import json,select,time
    from core.timelapse_derivative_worker import checked_manifest,file_facts
    from core.timelapse_derivative_timing import frame_plan
    checked_manifest(manifest)
    cap=group._media_capability
    if type(cap) is not MediaDerivativeCapability:raise ValueError('Derivative capability required')
    cap.guard.assert_owned()
    for fd,role,facts in zip(cap.descriptors,ROLES,cap.facts):validate(fd,role,facts)
    if source_fd!=cap.descriptors[1] or output_fd!=cap.descriptors[2] or file_facts(source_fd,deadline)!=manifest['source']:
        raise ValueError('Derivative transport source differs')
    encoded=(json.dumps(manifest,separators=(',',':'))+'\n').encode()
    if len(encoded)>8192:raise ValueError('Derivative manifest limit')
    received=bytearray();offset=0;result=None;completion=None
    try:
        if not group.arm().worker_started:raise ValueError('Derivative launch unconfirmed')
        os.set_blocking(input_writer,False);os.set_blocking(result_reader,False)
        while result is None or offset<len(encoded):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('Derivative deadline')
            if os.fstat(output_fd).st_size>32*1024**3:raise ValueError('Derivative output limit')
            readable,writable,_=select.select([result_reader],[input_writer] if offset<len(encoded) else [],[],min(remaining,.1))
            if writable:
                try:offset+=os.write(input_writer,encoded[offset:offset+65536])
                except BlockingIOError:pass
            if readable:
                try:block=os.read(result_reader,8193-len(received))
                except BlockingIOError:continue
                if not block:raise ValueError('Derivative result absent')
                received.extend(block)
                if len(received)>8192:raise ValueError('Derivative result limit')
                if received.endswith(b'\n'):
                    result=json.loads(received)
                    if offset!=len(encoded):raise ValueError('Premature derivative result')
        completion=group.wait_worker(deadline)
    finally:retirement=group.retire()
    if completion is None or not completion.worker_exited_normally or not retirement.group_empty or not retirement.channels_closed or not retirement.media_copies_closed:
        raise ValueError('Derivative completion unconfirmed')
    if type(result) is not dict or set(result)!={'operation_id','recipe','kind','frame_count','source_frame_count','output'}:
        raise ValueError('Derivative result shape')
    for key in ('operation_id','recipe','kind'):
        if result[key]!=manifest[key]:raise ValueError('Derivative result differs')
    recipe=checked_manifest(manifest)
    if type(result['frame_count']) is not int or result['frame_count']!=len(frame_plan(recipe,result['source_frame_count'])):
        raise ValueError('Derivative count differs')
    cap.guard.assert_owned()
    if file_facts(source_fd,deadline)!=manifest['source']:raise ValueError('Derivative source changed')
    actual=file_facts(output_fd,deadline)
    if actual!=result['output'] or not 0<actual['byte_count']<=32*1024**3 or any(actual[k]!=cap.facts[2][k] for k in KEYS+('link_count',)):
        raise ValueError('Derivative output differs')
    return result
