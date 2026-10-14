"""Durable preparation only. No physical retirement or publisher quiescence claim."""
from dataclasses import dataclass
from datetime import datetime,timezone
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from fastapi import HTTPException
from core.api_token_policy import parse_token_scopes,token_scope_granted
from core.timelapse_media_lock import acquire_media_guard,MediaGuardUnavailable
from core.timelapse_manifest import capture_manifest,_root,_parts,MAX_OBJECTS
from core.timelapse_rename import _component
from core.timelapse_retirement_schema import intent,objects,facts
from core.timelapse_ownership_schema import anchors
from core.timelapse_recovery_schema import parents,moves
from modules.archives.timelapse_recovery_evidence import (capture_evidence,validate_evidence,
    begin_capture_deadline,check_deadline,EvidenceUnavailable)
from modules.archives.models import Timelapse
from modules.archives.timelapse_access import assert_timelapse_access

from core.timelapse_media_errors import RetirementUnavailable

@dataclass(frozen=True)
class PreparedRetirement:
    operation_id:str
    state:str
    created:bool


def _refuse(reason='ownership_changed'):
    raise RetirementUnavailable(reason) from None


def _true(value):return value is True or (type(value) is int and value==1)


def _floor(principal):
    if not isinstance(principal,dict) or principal.get('role')!='admin' or type(principal.get('id')) is not int or principal['id']<=0:_refuse('unknown_authority')
    group=principal.get('group_id')
    if group is not None and (type(group) is not int or group<=0):_refuse('unknown_authority')
    scopes=principal.get('_token_scopes',[])
    if type(scopes) is not list or any(type(value) is not str for value in scopes):_refuse('unknown_authority')
    if (scopes or principal.get('_auth_kind')=='user_api_token') and not token_scope_granted(scopes,'write:printers'):_refuse('unknown_authority')


def _actor(db,principal,*,lock=False):
    if lock:db.execute(text('UPDATE users SET id=id WHERE id=:id'),{'id':principal['id']})
    row=db.execute(text('SELECT role,group_id,is_active FROM users WHERE id=:id'),{'id':principal['id']}).first()
    if row is None or row[0]!='admin' or row[1]!=principal.get('group_id') or not _true(row[2]):_refuse('unknown_authority')
    if principal.get('_auth_kind')=='user_api_token':
        token=principal.get('_token_id')
        if type(token) is not int or token<=0:_refuse('unknown_authority')
        if lock:db.execute(text('UPDATE api_tokens SET id=id WHERE id=:id'),{'id':token})
        saved=db.execute(text('SELECT user_id,scopes,expires_at FROM api_tokens WHERE id=:id'),{'id':token}).first()
        scopes=parse_token_scopes(saved[1]) if saved else None
        if saved is None or saved[0]!=principal['id'] or scopes is None or not token_scope_granted(scopes,'write:printers'):_refuse('unknown_authority')
        if saved[2] is not None:
            expiry=datetime.fromisoformat(saved[2]) if isinstance(saved[2],str) else saved[2]
            if expiry.tzinfo is None:expiry=expiry.replace(tzinfo=timezone.utc)
            if expiry<=datetime.now(timezone.utc):_refuse('unknown_authority')


def _snapshot(media):
    return (media.id,media.printer_id,media.print_job_id,media.filename,media.status,
            media.retirement_operation_id,media.ownership_operation_id)


def _owned(db,tid,principal,*,lock=False):
    if lock:db.execute(text('UPDATE timelapses SET id=id WHERE id=:id'),{'id':tid})
    media=db.query(Timelapse).filter(Timelapse.id==tid).first()
    if media is None:_refuse('unknown_authority')
    assert_timelapse_access(db,media,principal,mutation=True)
    owner=db.execute(anchors.select().where(anchors.c.operation_id==media.ownership_operation_id)).mappings().one()
    duplicate=db.execute(text('SELECT id FROM timelapses WHERE filename=:filename AND id<>:id LIMIT 1'),{'filename':media.filename,'id':tid}).first()
    if duplicate is not None:_refuse('object_changed')
    if media.retirement_operation_id is not None:
        if media.status!='deleting' or media.retirement_operation_id!=media.ownership_operation_id:_refuse()
    elif media.status not in ('ready','failed'):_refuse('writer_active')
    return media,dict(owner)


def _quarantine(operation,index):
    name=operation+'_'+str(index)
    if len(name)>128:_refuse('unsafe_path')
    _component(name)
    return name


def _persisted(db,media,owner,root_identity):
    operation=media.retirement_operation_id
    saved=db.execute(intent.select().where(intent.c.operation_id==operation)).mappings().first()
    if saved is None or saved['state'] not in ('pending','in_flight','blocked') or (
        saved['timelapse_id'],saved['printer_id'],saved['filename'],saved['owner_state'],saved['owner_org_id'],saved['root_device'],saved['root_inode']
    )!=(media.id,media.printer_id,media.filename,owner['owner_state'],owner['owner_org_id'],root_identity.device,root_identity.inode):_refuse()
    plan=db.execute(objects.select().where(objects.c.operation_id==operation).order_by(objects.c.object_index)).mappings().all()
    if not 1<=len(plan)<=MAX_OBJECTS:_refuse('phase_uncertain')
    seen=set()
    video=media.filename;frames=str(media.id)
    for index,row in enumerate(plan):
        relative=row['relative_path'];_parts(relative)
        if row['object_index']!=index or relative in seen or not (relative==video or relative==frames or relative.startswith(frames+'/')):_refuse('phase_uncertain')
        seen.add(relative)
        if row['state']=='originally_absent':
            if relative not in (video,frames) or row['quarantine_name'] is not None or any(row[name] is not None for name in facts):_refuse('phase_uncertain')
        elif row['quarantine_name']!=_quarantine(operation,index):_refuse('phase_uncertain')
        if relative==video and row['kind']!='file':_refuse('phase_uncertain')
        if relative==frames and row['kind']!='directory':_refuse('phase_uncertain')
    if video not in seen or frames not in seen:_refuse('phase_uncertain')
    validate_evidence(db,operation,plan,root_identity)
    return PreparedRetirement(operation,saved['state'],False)


def _write_boundary(db):
    if db.get_bind().dialect.name=='postgresql':
        if db.connection().get_isolation_level()!='READ COMMITTED':_refuse('phase_uncertain')
        db.execute(text("SET LOCAL lock_timeout='5s'"))


def prepare_retirement(engine,root,timelapse_id,principal):
    """Commit an exact plan without moving/unlinking source media.

    Engine-only makes transaction ownership explicit. Never adopt another
    caller's staged writes or roll back its Session. API activation must wait
    for safe drain and every publisher to participate in the operation guard.
    """
    if not isinstance(engine,Engine) or type(timelapse_id) is not int or timelapse_id<=0:_refuse('unknown_authority')
    _floor(principal)
    try:
        with acquire_media_guard(root,timelapse_id) as guard:
            with Session(engine) as reader:
                _actor(reader,principal)
                media,owner=_owned(reader,timelapse_id,principal)
                before=_snapshot(media)
                retry=media.retirement_operation_id is not None
            # Reader is closed: no row/transaction locks span traversal.
            if retry:
                with Session(engine) as db:
                    _write_boundary(db);_actor(db,principal,lock=True)
                    media,owner=_owned(db,timelapse_id,principal,lock=True)
                    if _snapshot(media)!=before:_refuse()
                    guard.assert_owned()
                    with _root(root) as (_,root_identity):
                        guard.assert_root_identity(root_identity)
                        result=_persisted(db,media,owner,root_identity)
                    db.rollback()
                    return result
            deadline=begin_capture_deadline()
            manifest=capture_manifest(root,before[3],str(timelapse_id))
            operation=before[6]
            if type(operation) is not str or not 1<=len(operation)<=64:_refuse('unknown_authority')
            directory_rows,movement=capture_evidence(root,manifest,operation,deadline)
            payload=[]
            for index,value in enumerate(manifest.objects):
                absent=value.kind=='absent'
                if absent and value.relative_path not in (before[3],str(timelapse_id)):_refuse('phase_uncertain')
                payload.append({'operation_id':operation,'object_index':index,'relative_path':value.relative_path,
                    'kind':('file' if value.relative_path==before[3] else 'directory') if absent else value.kind,
                    **{name:getattr(value,name) for name in facts},
                    'state':'originally_absent' if absent else 'planned',
                    'quarantine_name':None if absent else _quarantine(operation,index)})
            check_deadline(deadline)
            with Session(engine) as db:
                _write_boundary(db);_actor(db,principal,lock=True)
                media,current_owner=_owned(db,timelapse_id,principal,lock=True)
                if _snapshot(media)!=before or current_owner!=owner:_refuse()
                try:guard.assert_root_identity(manifest.root)
                except MediaGuardUnavailable:_refuse()
                db.execute(intent.insert(),{'operation_id':operation,'timelapse_id':timelapse_id,'printer_id':media.printer_id,
                    'owner_state':owner['owner_state'],'owner_org_id':owner['owner_org_id'],'filename':media.filename,
                    'root_device':manifest.root.device,'root_inode':manifest.root.inode,'state':'pending','revision':1})
                db.execute(objects.insert(),payload)
                db.execute(parents.insert(),directory_rows)
                if movement:db.execute(moves.insert(),movement)
                media.status='deleting';media.retirement_operation_id=operation
                from core.audit import log_audit
                log_audit(db,'retirement_prepare','timelapse',timelapse_id,{'operation_id':operation,'actor_id':principal['id']})
                db.commit()
                return PreparedRetirement(operation,'pending',True)
    except RetirementUnavailable:raise
    except EvidenceUnavailable as error:_refuse(error.reason)
    except HTTPException:_refuse('unknown_authority')
    except Exception:_refuse('file_io')
