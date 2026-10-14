"""Internal owned drain; endpoints await fencing of every media publisher."""
from dataclasses import dataclass
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from core.timelapse_manifest import _root,_parts,MAX_OBJECTS,ObjectIdentity
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_quarantine import quarantine_directory
from core.timelapse_drain_fs import DrainFilesystem
from core.timelapse_retirement_schema import intent,objects
from core.timelapse_recovery_schema import parents,moves,quarantine
from core.timelapse_unlink_schema import unlinks
from core.timelapse_ownership_schema import anchors
from modules.archives.timelapse_retirement import _floor,_actor,_quarantine
from modules.archives.timelapse_recovery_evidence import validate_evidence
from modules.archives.timelapse_stage_transactions import directory_identity
from modules.archives.timelapse_drain_transactions import load_drain_plan,commit_drain_step,commit_completion
from modules.archives.timelapse_access import immutable_timelapse_policy,_PROJECTION


class DrainUnavailable(RuntimeError):
    def __init__(self,reason='file_io'):
        self.reason=reason
        super().__init__('Media retirement could not be confirmed')


@dataclass(frozen=True)
class RetiredMedia:
    operation_id:str
    state:str='complete'


def _input(engine):
    if not isinstance(engine,Engine):raise DrainUnavailable('unknown_authority')


def _completed_evidence(db,record):
    operation=record['operation_id']
    planned=[dict(row) for row in db.execute(objects.select().where(objects.c.operation_id==operation).order_by(objects.c.object_index).limit(MAX_OBJECTS+1)).mappings()]
    if not 1<=len(planned)<=MAX_OBJECTS:raise DrainUnavailable('phase_uncertain')
    root=db.execute(parents.select().where(parents.c.operation_id==operation,parents.c.relative_path=='')).mappings().first()
    if root is None or (root['device'],root['inode'])!=(record['root_device'],record['root_inode']):raise DrainUnavailable('phase_uncertain')
    binding=db.execute(quarantine.select().where(quarantine.c.operation_id==operation)).mappings().first()
    if binding is None or type(binding['inode']) is not int or binding['inode']<=0 or binding['mode']!=16832 or any(binding[key]!=root[key] for key in ('device','uid','gid')):raise DrainUnavailable('phase_uncertain')
    identity=ObjectIdentity('','directory',**{key:root[key] for key in ('device','inode','uid','gid','mode')})
    validate_evidence(db,operation,planned,identity)
    receipt_rows={row['object_index']:dict(row) for row in db.execute(unlinks.select().where(unlinks.c.operation_id==operation)).mappings()}
    movement={row['object_index']:dict(row) for row in db.execute(moves.select().where(moves.c.operation_id==operation)).mappings()}
    present=set();seen=set();video=record['filename'];frames=str(record['timelapse_id'])
    for index,row in enumerate(planned):
        path=row['relative_path'];_parts(path)
        if row['object_index']!=index or path in seen or not (path==video or path==frames or path.startswith(frames+'/')):raise DrainUnavailable('phase_uncertain')
        seen.add(path)
        if (path==video and row['kind']!='file') or (path==frames and row['kind']!='directory'):raise DrainUnavailable('phase_uncertain')
        if row['state']=='originally_absent':
            if path not in (video,frames) or index in receipt_rows:raise DrainUnavailable('phase_uncertain')
        else:
            present.add(index)
            if row['state']!='retired' or row['quarantine_name']!=_quarantine(operation,index) or receipt_rows.get(index,{}).get('phase')!='retired' or movement[index]['phase']!='staged':raise DrainUnavailable('phase_uncertain')
    if video not in seen or frames not in seen or set(receipt_rows)!=present:raise DrainUnavailable('phase_uncertain')


def retirement_receipt(engine,operation_id,principal):
    _input(engine)
    if type(operation_id) is not str or not 1<=len(operation_id)<=64:raise DrainUnavailable('unknown_authority')
    try:
        _floor(principal)
        with Session(engine) as db:
            _actor(db,principal)
            record=db.execute(intent.select().where(intent.c.operation_id==operation_id)).mappings().first()
            owner=db.execute(anchors.select().where(anchors.c.operation_id==operation_id)).mappings().first()
            keys=('timelapse_id','printer_id','filename','owner_state','owner_org_id')
            if record is None or owner is None or record['state']!='complete' or record['completed_at'] is None or tuple(record[k] for k in keys)!=tuple(owner[k] for k in keys):raise DrainUnavailable('unknown_authority')
            policy,parameters=immutable_timelapse_policy(principal,mutation=True)
            found=db.execute(text('SELECT a.operation_id FROM '+_PROJECTION+' WHERE a.operation_id=:_operation AND '+policy),{**parameters,'_operation':operation_id}).first()
            if found is None:raise DrainUnavailable('unknown_authority')
            _completed_evidence(db,record)
        return RetiredMedia(operation_id)
    except DrainUnavailable:raise
    except Exception as error:raise DrainUnavailable(getattr(error,'reason','unknown_authority')) from None


def drain_retirement(engine,root,tid,principal):
    _input(engine)
    if type(tid) is not int or tid<=0:raise DrainUnavailable('unknown_authority')
    try:
        _floor(principal)
        with acquire_media_guard(root,tid) as guard:
            with _root(root) as (root_fd,identity):
                guard.assert_root_identity(identity)
                plan,receipts=load_drain_plan(engine,tid,principal,identity)
                protected=directory_identity('.retirement',plan.quarantine_row)
                parent_identities={path:directory_identity(path,row) for path,row in plan.parent_rows.items()}
                with quarantine_directory(root,identity,protected) as (fd,_):
                    fs=DrainFilesystem(root_fd,fd,identity,protected,plan.objects,parent_identities,plan.move_rows,receipts,guard)
                    fs.preflight()
                    ordered=sorted((row for row in plan.objects if row['state']!='originally_absent'),key=lambda row:(row['kind']=='directory',-row['relative_path'].count('/'),row['object_index']))
                    for row in ordered:
                        fs.admit();receipt=receipts.get(row['object_index'])
                        if receipt is None or receipt['phase']=='unlink_pending':
                            commit_drain_step(engine,tid,principal,plan,receipts,row,'unlink_pending',deadline=fs.deadline)
                            fs.admit();fs.unlink_and_sync(row);fs.admit()
                            commit_drain_step(engine,tid,principal,plan,receipts,row,'unlink_confirmed',deadline=fs.deadline)
                        fs.assert_absent(row)
                        if receipts[row['object_index']]['phase']=='unlink_confirmed':
                            commit_drain_step(engine,tid,principal,plan,receipts,row,'fsync_confirmed',deadline=fs.deadline)
                        fs.assert_absent(row)
                        if receipts[row['object_index']]['phase']=='fsync_confirmed':
                            commit_drain_step(engine,tid,principal,plan,receipts,row,'retired',deadline=fs.deadline)
                    fs.preflight();fs.admit()
                    commit_completion(engine,tid,principal,plan,receipts,deadline=fs.deadline)
                    return RetiredMedia(plan.operation)
    except DrainUnavailable:raise
    except Exception as error:raise DrainUnavailable(getattr(error,'reason','file_io')) from None
