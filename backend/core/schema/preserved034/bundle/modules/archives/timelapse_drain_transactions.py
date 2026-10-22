"""Owned short drain CAS transactions. Never perform filesystem work here."""
import time
from datetime import datetime,timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.timelapse_retirement_schema import intent,objects
from core.timelapse_recovery_schema import parents,moves,quarantine
from core.timelapse_unlink_schema import unlinks
from modules.archives.timelapse_retirement import _actor,_owned,_snapshot,_write_boundary
from modules.archives.timelapse_stage_transactions import load_stage_plan


class DrainTransactionUnavailable(RuntimeError):
    def __init__(self,reason='phase_uncertain'):
        self.reason=reason
        super().__init__('Media retirement progress could not be confirmed')


def _refuse(reason='phase_uncertain'):
    raise DrainTransactionUnavailable(reason) from None


def load_drain_plan(engine,tid,principal,root):
    plan=load_stage_plan(engine,tid,principal,root)
    with Session(engine) as db:
        receipts={row['object_index']:dict(row) for row in db.execute(unlinks.select().where(unlinks.c.operation_id==plan.operation)).mappings()}
    return plan,receipts


def _deadline(deadline):
    if time.monotonic()>=deadline:_refuse('manifest_limit')


def _current(db,tid,principal,plan,deadline):
    _deadline(deadline);_write_boundary(db);_actor(db,principal,lock=True)
    media,owner=_owned(db,tid,principal,lock=True)
    if _snapshot(media)!=plan.snapshot or owner!=plan.owner:_refuse('ownership_changed')
    saved=db.execute(intent.select().where(intent.c.operation_id==plan.operation).with_for_update()).mappings().first()
    if saved is None or dict(saved)!=plan.record or saved['state']!='in_flight':_refuse()
    binding=db.execute(quarantine.select().where(quarantine.c.operation_id==plan.operation)).mappings().first()
    if binding is None or dict(binding)!=plan.quarantine_row:_refuse('ownership_changed')
    _deadline(deadline)
    return media


def _row(db,plan,row,receipts):
    index=row['object_index']
    if not any(value is row for value in plan.objects):_refuse()
    original=db.execute(objects.select().where(objects.c.operation_id==plan.operation,objects.c.object_index==index)).mappings().first()
    move=db.execute(moves.select().where(moves.c.operation_id==plan.operation,moves.c.object_index==index)).mappings().first()
    receipt=db.execute(unlinks.select().where(unlinks.c.operation_id==plan.operation,unlinks.c.object_index==index)).mappings().first()
    if original is None or dict(original)!=row or move is None or dict(move)!=plan.move_rows[index] or move['phase']!='staged':_refuse()
    if (dict(receipt) if receipt is not None else None)!=receipts.get(index):_refuse()
    parts=row['relative_path'].split('/');paths={''}|{'/'.join(parts[:index]) for index in range(1,len(parts))}
    saved={value['relative_path']:dict(value) for value in db.execute(parents.select().where(parents.c.operation_id==plan.operation,parents.c.relative_path.in_(paths))).mappings()}
    if saved!={path:plan.parent_rows[path] for path in paths}:_refuse('ownership_changed')


def commit_drain_step(engine,tid,principal,plan,receipts,row,phase,*,deadline):
    index=row['object_index'];previous=receipts.get(index)
    transitions={'unlink_confirmed':('unlink_pending','staged','unlink_confirmed'),
                 'fsync_confirmed':('unlink_confirmed','unlink_confirmed','fsync_confirmed'),
                 'retired':('fsync_confirmed','fsync_confirmed','retired')}
    with Session(engine) as db:
        _current(db,tid,principal,plan,deadline);_row(db,plan,row,receipts)
        first=False
        if phase=='unlink_pending':
            if row['state']!='staged' or (previous is not None and previous['phase']!='unlink_pending'):_refuse()
            if previous is None:
                first=db.execute(unlinks.select().where(unlinks.c.operation_id==plan.operation).limit(1)).first() is None
                db.execute(unlinks.insert(),{'operation_id':plan.operation,'object_index':index,'phase':phase})
            else:
                changed=db.execute(unlinks.update().where(unlinks.c.operation_id==plan.operation,unlinks.c.object_index==index,unlinks.c.phase=='unlink_pending').values(phase=phase))
                if changed.rowcount!=1:_refuse()
            next_state='staged'
        else:
            expected=transitions.get(phase)
            if expected is None or previous is None or previous['phase']!=expected[0] or row['state']!=expected[1]:_refuse()
            changed=db.execute(unlinks.update().where(unlinks.c.operation_id==plan.operation,unlinks.c.object_index==index,unlinks.c.phase==expected[0]).values(phase=phase))
            if changed.rowcount!=1:_refuse()
            next_state=expected[2]
            changed=db.execute(objects.update().where(objects.c.operation_id==plan.operation,objects.c.object_index==index,objects.c.state==expected[1]).values(state=next_state))
            if changed.rowcount!=1:_refuse()
        revision=plan.record['revision']+1
        changed=db.execute(intent.update().where(intent.c.operation_id==plan.operation,intent.c.revision==plan.record['revision'],intent.c.state=='in_flight').values(revision=revision,reason_code=None))
        if changed.rowcount!=1:_refuse()
        if first:
            from core.audit import log_audit
            log_audit(db,'retirement_drain','timelapse',tid,{'operation_id':plan.operation,'actor_id':principal['id']})
        _deadline(deadline);db.commit()
    row['state']=next_state
    receipts[index]={'operation_id':plan.operation,'object_index':index,'phase':phase}
    plan.record.update(revision=revision,reason_code=None)


def commit_completion(engine,tid,principal,plan,receipts,*,deadline):
    with Session(engine) as db:
        _current(db,tid,principal,plan,deadline)
        actual=[dict(row) for row in db.execute(objects.select().where(objects.c.operation_id==plan.operation).order_by(objects.c.object_index)).mappings()]
        saved_receipts={row['object_index']:dict(row) for row in db.execute(unlinks.select().where(unlinks.c.operation_id==plan.operation)).mappings()}
        saved_moves={row['object_index']:dict(row) for row in db.execute(moves.select().where(moves.c.operation_id==plan.operation)).mappings()}
        saved_parents={row['relative_path']:dict(row) for row in db.execute(parents.select().where(parents.c.operation_id==plan.operation)).mappings()}
        if actual!=plan.objects or saved_receipts!=receipts or saved_moves!=plan.move_rows or saved_parents!=plan.parent_rows:_refuse()
        for row in actual:
            if row['state']=='originally_absent':
                if row['object_index'] in receipts:_refuse()
            elif row['state']!='retired' or receipts.get(row['object_index'],{}).get('phase')!='retired':_refuse()
        changed=db.execute(text("DELETE FROM timelapses WHERE id=:tid AND status='deleting' AND ownership_operation_id=:op AND retirement_operation_id=:op"),{'tid':tid,'op':plan.operation})
        if changed.rowcount!=1:_refuse()
        stamp=datetime.now(timezone.utc);revision=plan.record['revision']+1
        changed=db.execute(intent.update().where(intent.c.operation_id==plan.operation,intent.c.state=='in_flight',intent.c.revision==plan.record['revision']).values(state='complete',completed_at=stamp,reason_code=None,revision=revision))
        if changed.rowcount!=1:_refuse()
        from core.audit import log_audit
        log_audit(db,'retirement_complete','timelapse',tid,{'operation_id':plan.operation,'actor_id':principal['id']})
        _deadline(deadline);db.commit()
    plan.record.update(state='complete',completed_at=stamp,reason_code=None,revision=revision)
