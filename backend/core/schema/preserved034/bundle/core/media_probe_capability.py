"""Internal readonly media preflight: live guard and one proven source FD only."""
from dataclasses import dataclass
import os,sys,json,select,time
from pathlib import Path
from types import MappingProxyType
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core.media_derivative_capability import validate
from core.media_process_capability import identity,IDENTITY_KEYS,MediaCopyCleanupError

ROLES=('guard','source')
MARKERS=('{ODIN_MEDIA_GUARD}','{ODIN_MEDIA_SOURCE}')

@dataclass(frozen=True)
class MediaProbeCapability:
    descriptors:tuple
    facts:tuple
    guard:object
    def __post_init__(self):
        if type(self.descriptors) is not tuple or len(self.descriptors)!=2 or type(self.facts) is not tuple or len(self.facts)!=2:raise ValueError('Invalid readonly probe capability')
        object.__setattr__(self,'facts',tuple(MappingProxyType(dict(row)) for row in self.facts))
    @classmethod
    def from_receipts(cls,guard,source_fd,source_facts):
        from core.timelapse_media_lock import MediaGuard
        if not isinstance(guard,MediaGuard):raise ValueError('Original media guard required')
        guard.assert_owned()
        fds=(guard.fileno(),source_fd);facts=(identity(fds[0]),{key:source_facts[key] for key in IDENTITY_KEYS})
        for fd,role,expected in zip(fds,ROLES,facts):validate(fd,role,expected)
        separate(facts)
        return cls(fds,facts,guard)

def separate(facts):
    if len({(row['device'],row['inode']) for row in facts})!=2 or any(tuple(row[k] for k in ('device','uid','gid'))!=tuple(facts[0][k] for k in ('device','uid','gid')) for row in facts):raise ValueError('Readonly probe identities differ')

def worker_command():
    return [sys.executable,str(Path(__file__).with_name('timelapse_probe_worker.py')),'{ODIN_INPUT_FD}','{ODIN_RESULT_FD}',*MARKERS]

def prepare(capability,argv):
    if type(capability) is not MediaProbeCapability or list(argv)!=worker_command():raise ValueError('Fixed readonly probe worker required')
    capability.guard.assert_owned()
    if capability.descriptors[0]!=capability.guard.fileno():raise ValueError('Original readonly guard required')
    copies=[]
    try:
        for fd,role,facts in zip(capability.descriptors,ROLES,capability.facts):
            validate(fd,role,facts);copy=os.dup(fd);copies.append(copy);os.set_inheritable(copy,False);validate(copy,role,facts)
        return dict(zip(MARKERS,map(str,copies))),tuple(copies),{'version':1,'roles':list(ROLES),'facts':[dict(r) for r in capability.facts],'fds':copies}
    except Exception:
        pending=[]
        for fd in copies:
            try:os.close(fd)
            except OSError:pending.append(fd)
        if pending:raise MediaCopyCleanupError(pending) from None
        raise ValueError('Readonly probe capability unavailable') from None

def validate_envelope(record):
    if type(record) is not dict or set(record)!={'version','roles','facts','fds'} or type(record['version']) is not int or record['version']!=1 or record['roles']!=list(ROLES):raise ValueError('Invalid readonly probe envelope')
    if type(record['fds']) is not list or type(record['facts']) is not list or len(record['fds'])!=2 or len(record['facts'])!=2:raise ValueError('Invalid readonly probe descriptors')
    for fd,role,facts in zip(record['fds'],ROLES,record['facts']):validate(fd,role,facts)
    separate(record['facts'])
    if len(set(record['fds']))!=2:raise ValueError('Readonly probe descriptors overlap')
    return tuple(record['fds'])

def probe_owned_media(guard,source_fd,source_facts,deadline,identifier):
    """Caller proves DB lifetime/authority/name/parents before and after this call."""
    from core.sender_process_group import OwnedSenderGroup
    from core.timelapse_media_worker import file_facts
    from core.timelapse_probe_worker import checked_manifest
    cap=MediaProbeCapability.from_receipts(guard,source_fd,source_facts)
    manifest={'operation_id':identifier,'source':source_facts,'deadline':deadline};checked_manifest(manifest)
    encoded=(json.dumps(manifest,separators=(',',':'))+'\n').encode()
    channels=[];group=None;result=None;completion=None
    cleanup_error=False
    try:
        a,b=os.pipe();c,d=os.pipe();channels=[a,b,c,d]
        remaining=deadline-time.monotonic()
        if remaining<=0:raise ValueError('Readonly probe deadline')
        group=OwnedSenderGroup(worker_command(),defer_start=True,channels={'input':a,'result':d},media=cap,startup_seconds=min(2,remaining))
        for fd in (a,d):channels.remove(fd);os.close(fd)
        if not group.arm().worker_started:raise ValueError('Readonly probe start unconfirmed')
        os.set_blocking(b,False);os.set_blocking(c,False);offset=0;received=bytearray()
        while result is None or offset<len(encoded):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ValueError('Readonly probe deadline')
            readable,writable,_=select.select([c],[b] if offset<len(encoded) else [],[],remaining)
            if writable:
                try:offset+=os.write(b,encoded[offset:])
                except BlockingIOError:pass
            if readable:
                try:block=os.read(c,8193-len(received))
                except BlockingIOError:continue
                if not block:raise ValueError('Readonly probe result absent')
                received.extend(block)
                if len(received)>8192:raise ValueError('Readonly probe result limit')
                if received.endswith(b'\n'):
                    result=json.loads(received)
                    if offset!=len(encoded):raise ValueError('Premature readonly probe result')
        completion=group.wait_worker(deadline)
    finally:
        if group is not None:
            retired=group.retire()
            if not retired.group_empty or not retired.channels_closed or not retired.media_copies_closed:cleanup_error=True
        for fd in channels:
            try:os.close(fd)
            except OSError:cleanup_error=True
        if cleanup_error:raise ValueError('Readonly probe cleanup unconfirmed') from None
    if completion is None or not completion.worker_exited_normally:raise ValueError('Readonly probe exit unconfirmed')
    if type(result) is not dict or set(result)!={'operation_id','source','metadata'} or result['operation_id']!=identifier or result['source']!=source_facts:
        raise ValueError('Readonly probe result differs')
    metadata=result['metadata']
    if type(metadata) is not dict or set(metadata)!={'count','width','height'} or type(metadata['count']) is not int or not 1<=metadata['count']<=32768 or any(type(metadata[k]) is not int or not 2<=metadata[k]<=4096 or metadata[k]%2 for k in ('width','height')):
        raise ValueError('Readonly probe metadata differs')
    guard.assert_owned()
    if file_facts(source_fd,deadline)!=source_facts:raise ValueError('Readonly probe source changed')
    return metadata
