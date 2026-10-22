"""Original preparation evidence only, not physical retirement authority."""
import stat
import time
from core.timelapse_digest import capture_file_digest,MAX_BYTES
from core.timelapse_manifest import _parts,MAX_OBJECTS,MAX_DEPTH
from core.timelapse_recovery_schema import parents,moves

MAX_CAPTURE_SECONDS=60
STABLE=('device','inode','uid','gid','mode')


class EvidenceUnavailable(RuntimeError):
    def __init__(self,reason='phase_uncertain'):
        self.reason=reason
        super().__init__('Original media evidence could not be confirmed')


def _refuse(reason='phase_uncertain'):
    raise EvidenceUnavailable(reason) from None


def begin_capture_deadline():return time.monotonic()+MAX_CAPTURE_SECONDS


def check_deadline(deadline):
    if time.monotonic()>=deadline:_refuse('manifest_limit')


def _required(paths):
    required={''}
    for path in paths:
        parts=_parts(path)
        if len(parts)>MAX_DEPTH:_refuse()
        required.update('/'.join(parts[:index]) for index in range(1,len(parts)))
    return required


def capture_evidence(root,manifest,operation,deadline):
    """Bound total hashing work before opening any file; keep transactions closed."""
    check_deadline(deadline)
    file_objects=[value for value in manifest.objects if value.kind=='file']
    if any(type(value.byte_count) is not int or value.byte_count<0 for value in file_objects):_refuse()
    if sum(value.byte_count for value in file_objects)>MAX_BYTES:_refuse('manifest_limit')
    directory_rows=[{'operation_id':operation,'relative_path':value.relative_path,**{name:getattr(value,name) for name in STABLE}} for value in (manifest.root,*manifest.parents)]
    if len(directory_rows)>MAX_OBJECTS+MAX_DEPTH+1 or {row['relative_path'] for row in directory_rows}!=_required(value.relative_path for value in manifest.objects):_refuse()
    movement=[];remaining=MAX_BYTES
    for index,value in enumerate(manifest.objects):
        if value.kind=='absent':continue
        check_deadline(deadline)
        digest=None
        if value.kind=='file':
            seconds=min(deadline-time.monotonic(),MAX_CAPTURE_SECONDS)
            if seconds<=0:_refuse('manifest_limit')
            digest=capture_file_digest(root,manifest.root,value,manifest.parents,max_bytes=remaining,timeout_seconds=seconds)
            remaining-=value.byte_count
        movement.append({'operation_id':operation,'object_index':index,'kind':value.kind,'digest_sha256':digest,'phase':'planned'})
        check_deadline(deadline)
    return directory_rows,movement


def _directory(row,root):
    if any(type(row[name]) is not int or row[name]<0 for name in STABLE) or row['inode']==0:_refuse()
    if stat.S_IFMT(row['mode'])!=stat.S_IFDIR or row['mode']&0o022:_refuse()
    if (row['device'],row['uid'],row['gid'])!=(root.device,root.uid,root.gid):_refuse()


def validate_evidence(db,operation,plan,root):
    """Validate saved proof only. Never inspect, adopt or repair source files."""
    directory_rows=db.execute(parents.select().where(parents.c.operation_id==operation).limit(MAX_OBJECTS+MAX_DEPTH+2)).mappings().all()
    captured={row['relative_path']:row for row in directory_rows}
    if len(captured)!=len(directory_rows) or set(captured)!=_required(row['relative_path'] for row in plan):_refuse()
    for path,row in captured.items():
        if path:_parts(path)
        _directory(row,root)
    if tuple(captured[''][name] for name in STABLE)!=tuple(getattr(root,name) for name in STABLE):_refuse('ownership_changed')
    present={row['object_index']:row for row in plan if row['state']!='originally_absent'}
    for row in plan:
        if row['relative_path'] in captured:
            if row['state']=='originally_absent' or row['kind']!='directory' or tuple(row[name] for name in STABLE)!=tuple(captured[row['relative_path']][name] for name in STABLE):_refuse()
    movement=db.execute(moves.select().where(moves.c.operation_id==operation).limit(MAX_OBJECTS+1)).mappings().all()
    if len(movement)!=len(present) or {row['object_index'] for row in movement}!=set(present):_refuse()
    for row in movement:
        if row['kind']!=present[row['object_index']]['kind']:_refuse()
        digest=row['digest_sha256']
        if row['kind']=='file':
            if type(digest) is not str or len(digest)!=64 or any(value not in '0123456789abcdef' for value in digest):_refuse()
        elif row['kind']!='directory' or digest is not None:_refuse()
        if row['phase'] not in ('planned','rename_pending','staged'):_refuse()
        stamp=row['verified_ctime_ns']
        if row['phase']=='staged':
            if type(stamp) is not int or stamp<0:_refuse()
        elif stamp is not None:_refuse()
