"""Ready publication admission to a single owned readonly descriptor."""
import os,hashlib
from contextlib import ExitStack
from sqlalchemy import select,text
from sqlalchemy.orm import Session
from core.timelapse_manifest import _root,_parts,_parent,_facts
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_publication_schema import versions,lifetimes,publications,facts
from core.timelapse_access import assert_timelapse_access
from core.timelapse_request_budget import VerificationBudget
from core.timelapse_fd_response import AdmittedFDResponse
from modules.archives.models import Timelapse
from modules.archives.timelapse_retirement import _refuse
from modules.archives.timelapse_action_authority import fence_authorization,fresh_action_principal,lock_media_sources

def _snapshot(db,evidence,tid,*,lock=False):
    principal=fresh_action_principal(db,evidence)
    media=db.get(Timelapse,tid)
    if media is None:_refuse('unknown_authority')
    assert_timelapse_access(db,media,principal,mutation=evidence.purpose=='edit')
    if media.status!='ready' or media.retirement_operation_id is not None or not media.ownership_operation_id:_refuse('writer_active')
    nonce=media.ownership_operation_id
    anchor=db.execute(text("SELECT timelapse_id,printer_id,filename,scheduled_job_id FROM timelapse_ownership_anchors WHERE operation_id=:n"),{"n":nonce}).first()
    if anchor is None or tuple(anchor)!=(media.id,media.printer_id,media.filename,media.print_job_id):_refuse("ownership_changed")
    version=db.execute(select(versions).where(versions.c.version_nonce==nonce)).mappings().one()
    life=dict(db.execute(select(lifetimes).where(lifetimes.c.lifetime_nonce==version['lifetime_nonce'])).mappings().one())
    ops=db.execute(select(publications).where(publications.c.result_nonce==nonce,publications.c.kind!='frame').limit(2)).mappings().all()
    if len(ops)!=1:_refuse('phase_uncertain')
    op=dict(ops[0])
    if op['phase']!='complete' or op['state']!='complete' or op['result_tid']!=tid or op['destination_path']!=media.filename:_refuse('phase_uncertain')
    values=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==op['operation_id']).order_by(*facts.primary_key.columns).limit(16385)).mappings()]
    if len(values)>16384:_refuse('manifest_limit')
    output=[r for r in values if r['role']=='output' and r['relative_path']==op['private_name']]
    if len(output)!=1:_refuse('phase_uncertain')
    if lock:lock_media_sources(db,((lifetimes,[life]),(versions,[dict(version)]),(publications,[op]),(facts,values)))
    return {'media':(media.id,media.printer_id,media.print_job_id,media.filename,nonce),'life':life,'version':dict(version),'op':op,'facts':values,'output':output[0]}

def admitted_response(engine,root,tid,evidence,*,range_header=None,head=False,filename=None,expected_nonce=None):
    if evidence.purpose!='read':_refuse('unknown_authority')
    budget=VerificationBudget();fd=None
    try:
        with acquire_media_guard(root,tid) as guard,ExitStack() as stack:
            root_fd,identity=stack.enter_context(_root(root))
            with Session(engine) as db:
                fence_authorization(db);before=_snapshot(db,evidence,tid)
                if expected_nonce is not None and before['media'][4]!=expected_nonce:
                    from fastapi import HTTPException
                    raise HTTPException(412,'Recording version changed')
            if any(getattr(identity,k)!=before['life']['root_'+k] for k in ('device','inode','uid','gid','mode')):_refuse('object_changed')
            guard.assert_root_identity(identity)
            path=before['media'][3];parts=_parts(path)
            known={}
            for row in before['facts']:
                if row['role']=='output_parent':known[row['relative_path']]=row
            for i in range(1,len(parts)):
                if '/'.join(parts[:i]) not in known:_refuse('phase_uncertain')
            parents={};parent,name=stack.enter_context(_parent(root_fd,parts,identity,parents))
            for p,value in parents.items():
                if any(getattr(value,k)!=known[p][k] for k in ('device','inode','uid','gid','mode')):_refuse('object_changed')
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent);os.set_inheritable(fd,False)
            output=before['output'];value=_facts(path,os.fstat(fd))
            keys=('device','inode','uid','gid','mode','link_count','byte_count','mtime_ns')
            if any(getattr(value,k)!=output[k] for k in keys) or value.ctime_ns!=before['op']['published_ctime_ns']:_refuse('object_changed')
            budget.consume(value.byte_count);digest=hashlib.sha256();position=0
            while position<value.byte_count:
                budget.check();chunk=os.pread(fd,min(1<<20,value.byte_count-position),position)
                if not chunk:_refuse('object_changed')
                digest.update(chunk);position+=len(chunk)
            if digest.hexdigest()!=output['digest_sha256']:_refuse('object_changed')
            with Session(engine) as db:
                fence_authorization(db)
                if _snapshot(db,evidence,tid,lock=True)!=before:_refuse('ownership_changed')
                budget.check();guard.assert_root_identity(identity)
                if _facts(path,os.fstat(fd))!=value or _facts(path,os.stat(name,dir_fd=parent,follow_symlinks=False))!=value:_refuse('object_changed')
                # Rewalk the original chain while authorization fences are held.
                with _parent(root_fd,parts,identity,dict(parents)) as (check_parent,check_name):
                    if _facts(path,os.stat(check_name,dir_fd=check_parent,follow_symlinks=False))!=value:_refuse('object_changed')
                db.rollback()
        owned,fd=fd,None
        response=AdmittedFDResponse(owned,size=value.byte_count,range_header=range_header,head=head,filename=filename)
        return response
    finally:
        if fd is not None:os.close(fd)
