"""Owned short staging transactions; no filesystem work inside row locks."""
from dataclasses import dataclass
import time
from sqlalchemy.orm import Session
from core.timelapse_retirement_schema import intent,objects
from core.timelapse_recovery_schema import parents,moves,quarantine
from core.timelapse_manifest import ObjectIdentity
from modules.archives.timelapse_retirement import _actor,_owned,_persisted,_snapshot,_write_boundary


class StageTransactionUnavailable(RuntimeError):
    def __init__(self,reason='phase_uncertain'):
        self.reason=reason
        super().__init__('Staging progress could not be confirmed')


def _refuse(reason='phase_uncertain'):
    raise StageTransactionUnavailable(reason) from None


@dataclass
class StagePlan:
    operation:str
    snapshot:tuple
    owner:dict
    record:dict
    objects:list
    parent_rows:dict
    move_rows:dict
    quarantine_row:dict


def load_stage_plan(engine,tid,principal,root):
    with Session(engine) as db:
        _actor(db,principal);media,owner=_owned(db,tid,principal)
        if media.retirement_operation_id is None:_refuse()
        _persisted(db,media,owner,root);operation=media.retirement_operation_id
        record=dict(db.execute(intent.select().where(intent.c.operation_id==operation)).mappings().one())
        planned=[dict(row) for row in db.execute(objects.select().where(objects.c.operation_id==operation).order_by(objects.c.object_index)).mappings()]
        parent_rows={row['relative_path']:dict(row) for row in db.execute(parents.select().where(parents.c.operation_id==operation)).mappings()}
        movement={row['object_index']:dict(row) for row in db.execute(moves.select().where(moves.c.operation_id==operation)).mappings()}
        binding=db.execute(quarantine.select().where(quarantine.c.operation_id==operation)).mappings().first()
        if binding is None:_refuse()
        return StagePlan(operation,_snapshot(media),owner,record,planned,parent_rows,movement,dict(binding))


def directory_identity(path,row):
    return ObjectIdentity(path,'directory',**{name:row[name] for name in ('device','inode','uid','gid','mode')})


def commit_stage_step(engine,tid,principal,plan,row=None,*,phase=None,stamp=None,deadline):
    """CAS admission/progress; caller updates memory only after acknowledged commit."""
    with Session(engine) as db:
        if time.monotonic()>=deadline:_refuse('manifest_limit')
        _write_boundary(db);_actor(db,principal,lock=True)
        media,owner=_owned(db,tid,principal,lock=True)
        if _snapshot(media)!=plan.snapshot or owner!=plan.owner:_refuse('ownership_changed')
        current=db.execute(intent.select().where(intent.c.operation_id==plan.operation).with_for_update()).mappings().first()
        if current is None or dict(current)!=plan.record:_refuse()
        binding=db.execute(quarantine.select().where(quarantine.c.operation_id==plan.operation)).mappings().first()
        if binding is None or dict(binding)!=plan.quarantine_row:_refuse('ownership_changed')
        paths={''}
        if row is not None:
            parts=row['relative_path'].split('/')
            paths.update('/'.join(parts[:index]) for index in range(1,len(parts)))
        current_parents={value['relative_path']:dict(value) for value in db.execute(parents.select().where(parents.c.operation_id==plan.operation,parents.c.relative_path.in_(paths))).mappings()}
        if current_parents!={path:plan.parent_rows[path] for path in paths}:_refuse('ownership_changed')
        if time.monotonic()>=deadline:_refuse('manifest_limit')
        next_revision=plan.record['revision']+1
        if row is not None:
            index=row['object_index'];original=plan.move_rows[index]
            actual=db.execute(objects.select().where(objects.c.operation_id==plan.operation,objects.c.object_index==index)).mappings().first()
            actual_move=db.execute(moves.select().where(moves.c.operation_id==plan.operation,moves.c.object_index==index)).mappings().first()
            if actual is None or dict(actual)!=row or actual_move is None or dict(actual_move)!=original:_refuse()
            if phase=='rename_pending':
                if original['phase'] not in ('planned','rename_pending') or row['state']!='planned' or stamp is not None:_refuse()
            elif phase=='staged':
                if original['phase']!='rename_pending' or row['state']!='planned' or type(stamp) is not int or stamp<0:_refuse()
            else:_refuse()
            if row['kind']=='directory':
                children=[value for value in plan.objects if value['relative_path'].rpartition('/')[0]==row['relative_path'] and value['state']!='originally_absent']
                if any(plan.move_rows[value['object_index']]['phase']!='staged' or value['state']!='staged' for value in children):_refuse()
                indices=[value['object_index'] for value in children]
                if indices:
                    actual_children={value['object_index']:dict(value) for value in db.execute(objects.select().where(objects.c.operation_id==plan.operation,objects.c.object_index.in_(indices))).mappings()}
                    actual_moves={value['object_index']:dict(value) for value in db.execute(moves.select().where(moves.c.operation_id==plan.operation,moves.c.object_index.in_(indices))).mappings()}
                    if actual_children!={value['object_index']:value for value in children} or actual_moves!={index:plan.move_rows[index] for index in indices}:_refuse()
            changed=db.execute(moves.update().where(moves.c.operation_id==plan.operation,moves.c.object_index==index,moves.c.phase==original['phase']).values(phase=phase,verified_ctime_ns=stamp))
            if changed.rowcount!=1:_refuse()
            if phase=='staged':
                changed=db.execute(objects.update().where(objects.c.operation_id==plan.operation,objects.c.object_index==index,objects.c.state=='planned').values(state='staged'))
                if changed.rowcount!=1:_refuse()
        changed=db.execute(intent.update().where(intent.c.operation_id==plan.operation,intent.c.revision==plan.record['revision'],intent.c.state==plan.record['state']).values(state='in_flight',revision=next_revision,reason_code=None))
        if changed.rowcount!=1:_refuse()
        if row is None and plan.record['state']=='pending':
            from core.audit import log_audit
            log_audit(db,'retirement_stage','timelapse',tid,{'operation_id':plan.operation,'actor_id':principal['id']})
        if time.monotonic()>=deadline:_refuse('manifest_limit')
        db.commit()
    plan.record.update(state='in_flight',revision=next_revision,reason_code=None)
    if row is not None:
        plan.move_rows[row['object_index']].update(phase=phase,verified_ctime_ns=stamp)
        if phase=='staged':row['state']='staged'
