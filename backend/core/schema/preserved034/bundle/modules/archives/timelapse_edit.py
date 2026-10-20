"""Request-scoped immutable trim/speed publication; never edits source media."""
from contextlib import ExitStack
from datetime import datetime,timezone
import os,re,time,uuid,json
from sqlalchemy import select,update,text
from sqlalchemy.orm import Session
from fastapi import HTTPException
from core.timelapse_publication_schema import publications,facts,versions,lifetimes,retained
from core.timelapse_ownership_schema import anchors
from core.timelapse_publication_validation import operation,STABLE,FILE,EMPTY,version
from core.timelapse_recipes import recipe_from_row
from core.timelapse_derivative_timing import frame_plan
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root
from core.timelapse_media_worker import file_facts,FILE_KEYS
from core.media_probe_capability import probe_owned_media
from core.media_derivative_capability import MediaDerivativeCapability,worker_command,complete_derivative_transport
from core.sender_process_group import OwnedSenderGroup
from core.timelapse_rename import rename_noreplace
from core.audit import log_audit
from modules.archives.models import Timelapse
from core.timelapse_access import assert_timelapse_access
from modules.archives.timelapse_action_authority import fence_authorization,fresh_action_principal,lock_media_sources
from modules.archives.timelapse_read_lease import _snapshot
from modules.archives.timelapse_frame_publication import FrameJournal,_directory,_file,_same
from modules.archives.timelapse_encode import EncodeJournal
from modules.archives.timelapse_edit_source import open_proven_media

from core.timelapse_media_errors import EditUnavailable

def refuse():raise EditUnavailable('Timelapse edit could not be confirmed') from None

def fence(db,evidence):
    if evidence.purpose!='edit':refuse()
    if db.get_bind().dialect.name=='postgresql':
        current=int(db.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one())
        db.execute(text("SELECT set_config('lock_timeout',:bound,true)"),{'bound':str(min(current,2000) if current>0 else 2000)+'ms'})
    fence_authorization(db,preparation=True)
    return fresh_action_principal(db,evidence)

class EditJournal(FrameJournal):
    _verify=EncodeJournal._verify
    def __init__(self,*,evidence,deadline,**kwargs):
        super().__init__(authority=self._authorize,**kwargs);self.evidence=evidence;self.deadline=deadline
    def check(self):
        if time.monotonic()>=self.deadline:refuse()
        self.guard.assert_root_identity(self.root_identity)
    def load(self,identifier):
        with self.sessions() as db:
            row=db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one_or_none()
            if row is None:raise HTTPException(404,'Edit operation not found')
            row=dict(row);operation(db,row)
            saved=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==identifier)).mappings()]
        if row['kind'] not in ('trim','speed') or row['source_nonce']!=self.nonce or row['source_timelapse_id']!=self.tid:refuse()
        return row,saved
    def _authorize(self,db,op=None,*,completed=False):
        self.check();principal=fence(db,self.evidence)
        locked_snapshot=None
        if not completed:
            locked_snapshot=_snapshot(db,self.evidence,self.tid,lock=True)
        elif op is not None:
            saved=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==op['operation_id'])).mappings()]
            lock_media_sources(db,((publications,[op]),(facts,saved)))
        ids=sorted({self.tid}|({op['result_tid']} if op else set()))
        for tid in ids:
            if db.get_bind().dialect.name=='postgresql':
                if db.execute(text('SELECT id FROM timelapses WHERE id=:id FOR UPDATE NOWAIT'),{'id':tid}).first() is None:refuse()
            elif db.execute(text('UPDATE timelapses SET id=id WHERE id=:id'),{'id':tid}).rowcount!=1:refuse()
        source=db.get(Timelapse,self.tid);assert_timelapse_access(db,source,principal,mutation=True)
        current_version=db.execute(select(versions).where(versions.c.version_nonce==source.ownership_operation_id)).mappings().one_or_none()
        source_version=db.execute(select(versions).where(versions.c.version_nonce==self.nonce)).mappings().one_or_none()
        if current_version is None or source_version is None or current_version['lifetime_nonce']!=source_version['lifetime_nonce'] or source.retirement_operation_id is not None:refuse()
        if not completed:
            snapshot=_snapshot(db,self.evidence,self.tid)
            if snapshot!=locked_snapshot:refuse()
            if snapshot['media'][4]!=self.nonce:raise HTTPException(412,'Recording version changed')
            if op is not None and (op['actor_id']!=principal['id'] or op['source_filename']!=snapshot['media'][3]):refuse()
            return snapshot,principal
        if op['actor_id']!=principal['id']:refuse()
        operation(db,op)
        return source,principal
    def admit(self,identifier,recipe,snapshot,source_fact):
        nonce=uuid.uuid4().hex;destination=recipe.kind+'_'+identifier+'.mp4'
        with self.sessions() as db:
            fresh,principal=self.authority(db)
            if fresh!=snapshot:refuse()
            owner=dict(db.execute(select(anchors).where(anchors.c.operation_id==self.nonce)).mappings().one())
            result_tid=self.tid;result_lifetime=snapshot['version']['lifetime_nonce']
            if recipe.kind=='speed':
                media=Timelapse(printer_id=self.pid,print_job_id=snapshot['media'][2],filename=destination,status='encoding',frame_count=0,ownership_operation_id=nonce)
                db.add(media);db.flush();result_tid=media.id;result_lifetime=nonce
            copied={k:v for k,v in owner.items() if k!='created_at'}
            copied.update(operation_id=nonce,timelapse_id=result_tid,filename=destination)
            db.execute(anchors.insert().values(**copied))
            from core import timelapse_capture_tenancy_schema as capture_tenancy
            capture_tenancy.copy(db.connection(),self.nonce,nonce)
            if recipe.kind=='speed':
                life={k:v for k,v in snapshot['life'].items() if k!='created_at'}
                life.update(lifetime_nonce=nonce,original_timelapse_id=result_tid)
                db.execute(lifetimes.insert().values(**life))
            db.execute(versions.insert().values(version_nonce=nonce,lifetime_nonce=result_lifetime))
            row=dict(operation_id=identifier,source_nonce=self.nonce,source_lifetime=snapshot['version']['lifetime_nonce'],kind=recipe.kind,
                source_timelapse_id=self.tid,source_printer_id=self.pid,source_filename=snapshot['media'][3],source_status='ready',source_preclaim_status=None,
                actor_id=principal['id'],frame_namespace_id=None,frame_number=None,payload_byte_count=None,payload_digest_sha256=None,
                private_directory='publish_'+identifier,private_name='output.mp4',destination_path=destination,result_nonce=nonce,result_lifetime=result_lifetime,
                result_tid=result_tid,phase='admitted',state='active',revision=1,reason_code=None,published_ctime_ns=None,completed_at=None,**recipe.columns())
            db.execute(publications.insert().values(**row))
            additions=[dict(_directory(os.fstat(self.root_fd)),role=role,relative_path='') for role in ('root','output_parent')]
            additions.append(dict(source_fact,kind='file',role='source',relative_path=row['source_filename']))
            additions.extend(dict({k:v for k,v in value.items() if k!='operation_id'},role='source_parent') for value in snapshot['facts'] if value['role']=='output_parent' and value['relative_path'])
            for value in additions:db.execute(facts.insert().values(operation_id=identifier,**value))
            operation(db,row);self.check()
            try:db.commit()
            except Exception:
                db.rollback();saved,_=self.load(identifier)
                if any(saved[key]!=value for key,value in row.items()):raise
        return self.load(identifier)
    def advance(self,row,saved,target,*,added=(),ctime=None,metadata=None):
        expected=row['phase'];ordered=lambda values:sorted(values,key=lambda r:(r['role'],r['relative_path']))
        with self.sessions() as db:
            self.authority(db,row)
            actual=dict(db.execute(select(publications).where(publications.c.operation_id==row['operation_id'])).mappings().one())
            actual_facts=[dict(r) for r in db.execute(select(facts).where(facts.c.operation_id==row['operation_id'])).mappings()]
            if actual!=row or ordered(actual_facts)!=ordered(saved) or row['state']!='active':refuse()
            for value in added:db.execute(facts.insert().values(operation_id=row['operation_id'],**value))
            changes=dict(phase=target,state='complete' if target=='complete' else 'active',revision=row['revision']+1,reason_code=None)
            if ctime is not None:changes['published_ctime_ns']=ctime
            if target=='complete':
                if metadata is None:refuse()
                changes['completed_at']=datetime.now(timezone.utc)
                output=next(r for r in saved if r['role']=='output')
                if row['kind']=='trim':
                    db.execute(retained.insert().values(version_nonce=row['source_nonce'],lifetime_nonce=row['source_lifetime'],
                        retained_by_operation=row['operation_id'],filename=row['source_filename'],state='retained',retire_operation_id=None))
                result=db.get(Timelapse,row['result_tid'])
                expected_nonce=row['source_nonce'] if row['kind']=='trim' else row['result_nonce']
                if result.ownership_operation_id!=expected_nonce or result.retirement_operation_id is not None or result.status!=('ready' if row['kind']=='trim' else 'encoding'):refuse()
                result.filename=row['destination_path'];result.ownership_operation_id=row['result_nonce'];result.status='ready'
                result.frame_count=metadata['count'];result.duration_seconds=metadata['count']/30;result.file_size_mb=output['byte_count']/(1024*1024);result.completed_at=changes['completed_at']
                log_audit(db,'timelapse_edit_complete','timelapse',row['result_tid'],{'operation_id':row['operation_id'],'kind':row['kind'],'frame_count':metadata['count']})
                db.flush()
            changed=db.execute(update(publications).where(publications.c.operation_id==row['operation_id'],publications.c.phase==expected,
                publications.c.revision==row['revision'],publications.c.state=='active').values(**changes))
            if changed.rowcount!=1:refuse()
            candidate=dict(row,**changes);operation(db,candidate);self.check()
            try:db.commit()
            except Exception:
                db.rollback();current,current_facts=self.load(row['operation_id'])
                from modules.archives.timelapse_action_authority import _as_utc_datetime
                # SQLite DateTime drops timezone information. This field is
                # written here in UTC; compare that documented writer value,
                # without relaxing any phase, revision, recipe or file facts.
                if any((_as_utc_datetime(current[key])!=_as_utc_datetime(value) if key=='completed_at' else current[key]!=value)
                       for key,value in candidate.items()) or ordered(current_facts)!=ordered(saved+[dict(v,operation_id=row['operation_id']) for v in added]):raise
                with self.sessions() as check:self.authority(check,current,completed=target=='complete')
        return self.load(row['operation_id'])
    def open_file(self,row,saved,*,output=False,published=False):
        with self.sessions() as db:
            life=dict(db.execute(select(lifetimes).where(lifetimes.c.lifetime_nonce==row['source_lifetime'])).mappings().one())
        if not output:
            value=next(r for r in saved if r['role']=='source')
            path=row['source_filename'];parents=[r for r in saved if r['role']=='source_parent'];ctime=value['ctime_ns']
        else:
            value=next(r for r in saved if r['role']=='output')
            creation=next(r for r in saved if r['role']=='private_creation')
            # An unpublished output is admitted by its durable output_ready
            # bundle, never by a nonexistent complete live result version.
            if row['phase'] not in ('output_ready','publish_pending','published','complete') or any(value[k]!=creation[k] for k in STABLE):refuse()
            if published:
                path=row['destination_path'];parents=[r for r in saved if r['role']=='output_parent' and r['relative_path']]
                ctime=row['published_ctime_ns']
            else:
                private=next(r for r in saved if r['role']=='private_root')
                path=row['private_directory']+'/output.mp4';parents=[private];ctime=value['ctime_ns']
        expected={k:value[k] for k in FILE_KEYS}
        return open_proven_media(root_fd=self.root_fd,root_identity=self.root_identity,guard=self.guard,lifetime=life,
            path=path,expected=expected,parent_facts=parents,ctime=ctime,deadline=self.deadline)
    def work(self,row,saved,preflight):
        if row['phase']!='admitted' or row['state']!='active':refuse()
        with self.open_file(row,saved) as (source,source_facts):
            # Fresh readonly preflight completed before creation in execute_edit.
            frame_plan(recipe_from_row(row),preflight['count'])
            private=output=None;group=None;channels=[];cleanup_error=False
            try:
                self.check();os.mkdir(row['private_directory'],0o700,dir_fd=self.root_fd)
                private=os.open(row['private_directory'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.root_fd)
                private_fact=_directory(os.fstat(private))
                output=os.open('output.mp4',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=private)
                creation=_file(os.fstat(output),EMPTY)
                os.fsync(output);os.fsync(private);os.fsync(self.root_fd)
                row,saved=self.advance(row,saved,'private_created',added=[dict(private_fact,role='private_root',relative_path=row['private_directory']),
                    dict(creation,role='private_creation',relative_path='output.mp4')])
                cap=MediaDerivativeCapability.from_receipts(self.guard,source,output,source_facts,creation)
                a,b=os.pipe();c,d=os.pipe();channels=[a,b,c,d]
                self.check()
                group=OwnedSenderGroup(worker_command(),defer_start=True,channels={'input':a,'result':d},media=cap,startup_seconds=min(2,self.deadline-time.monotonic()))
                for fd in (a,d):channels.remove(fd);os.close(fd)
                row,saved=self.advance(row,saved,'worker_admitted')
                manifest={'operation_id':row['operation_id'],'kind':row['kind'],'recipe':recipe_from_row(row).columns(),
                    'source':source_facts,'deadline':self.deadline}
                result=complete_derivative_transport(group,manifest,input_writer=b,result_reader=c,source_fd=source,output_fd=output,deadline=self.deadline)
                if result['source_frame_count']!=preflight['count']:refuse()
                named=_file(os.stat('output.mp4',dir_fd=private,follow_symlinks=False),result['output']['digest_sha256'])
                _same(named,result['output'],FILE)
                _same(_directory(os.fstat(private)),private_fact,STABLE)
                group.retire();group=None
                for fd in list(channels):channels.remove(fd);os.close(fd)
                closing,output=output,None;os.close(closing)
                closing,private=private,None;os.close(closing)
                final=dict(result['output'],kind='file',role='output',relative_path='output.mp4')
                row,saved=self.advance(row,saved,'output_ready',added=[final])
            finally:
                if group is not None:
                    retired=group.retire()
                    if not retired.group_empty or not retired.channels_closed or not retired.media_copies_closed:cleanup_error=True
                for fd in channels+[fd for fd in (output,private) if fd is not None]:
                    try:os.close(fd)
                    except OSError:cleanup_error=True
                if cleanup_error:raise EditUnavailable('Timelapse edit cleanup unconfirmed') from None
        return self.resume(row,saved)
    def resume(self,row,saved):
        if row['phase']=='complete':return self.receipt(row,saved)
        if row['state']!='active' or row['phase'] in ('admitted','private_created','worker_admitted'):refuse()
        with self.sessions() as db:self.authority(db,row)
        with self.open_file(row,saved) as (source,source_facts):
            source_meta=probe_owned_media(self.guard,source,source_facts,self.deadline,row['operation_id'])
        plan=frame_plan(recipe_from_row(row),source_meta['count'])
        if row['phase']=='output_ready':
            with self.open_file(row,saved,output=True) as (output,output_facts):
                meta=probe_owned_media(self.guard,output,output_facts,self.deadline,row['operation_id'])
                if meta!=dict(source_meta,count=len(plan)):refuse()
            row,saved=self.advance(row,saved,'publish_pending')
        private=self._private(row,saved)
        try:
            expected=next(r for r in saved if r['role']=='output')
            if row['phase']=='publish_pending':
                source_present=not self._absent(private,'output.mp4');dest_present=not self._absent(self.root_fd,row['destination_path'])
                if source_present and not dest_present:
                    with self.open_file(row,saved,output=True) as (output,output_facts):
                        meta=probe_owned_media(self.guard,output,output_facts,self.deadline,row['operation_id'])
                        if meta!=dict(source_meta,count=len(plan)):refuse()
                    with self.sessions() as db:
                        self.authority(db,row);self.check()
                        rename_noreplace(private,'output.mp4',self.root_fd,row['destination_path'])
                        _same(_file(os.stat(row['destination_path'],dir_fd=self.root_fd,follow_symlinks=False),expected['digest_sha256']),expected,STABLE+('link_count','byte_count','mtime_ns'))
                        db.rollback()
                elif not source_present and dest_present:
                    self._verify(self.root_fd,row['destination_path'],expected,rename_ctime=True)
                else:refuse()
                os.fsync(private);os.fsync(self.root_fd)
                actual=self._verify(self.root_fd,row['destination_path'],expected,rename_ctime=True)
                if not self._absent(private,'output.mp4'):refuse()
                row,saved=self.advance(row,saved,'published',ctime=actual['ctime_ns'])
            if row['phase']=='published':
                with self.open_file(row,saved,output=True,published=True) as (output,output_facts):
                    meta=probe_owned_media(self.guard,output,output_facts,self.deadline,row['operation_id'])
                    if meta!=dict(source_meta,count=len(plan)):refuse()
                row,saved=self.advance(row,saved,'complete',metadata=meta)
            return self.receipt(row,saved)
        finally:os.close(private)
    def receipt(self,row,saved):
        with self.sessions() as db:
            source,principal=self.authority(db,row,completed=True)
            result=db.get(Timelapse,row['result_tid'])
            if result is None:refuse()
            assert_timelapse_access(db,result,principal,mutation=True)
            result_map=db.execute(select(versions).where(versions.c.version_nonce==result.ownership_operation_id)).mappings().one_or_none()
            if result_map is None or result_map['lifetime_nonce']!=row['result_lifetime'] or result.retirement_operation_id is not None:refuse()
            available=result.status=='ready' and result.ownership_operation_id==row['result_nonce'] and result.filename==row['destination_path']
        if row['phase']!='complete' or row['state']!='complete':refuse()
        with self.open_file(row,saved,output=True,published=True) as (output,output_facts):
            meta=probe_owned_media(self.guard,output,output_facts,self.deadline,row['operation_id'])
        return dict(operation_id=row['operation_id'],phase='complete',kind=row['kind'],recipe=recipe_from_row(row).columns(),
            source_nonce=row['source_nonce'],source_lifetime=row['source_lifetime'],result_nonce=row['result_nonce'],result_lifetime=row['result_lifetime'],
            result_tid=row['result_tid'],frame_count=meta['count'],duration_seconds=meta['count']/30,
            file_size_mb=output_facts['byte_count']/(1024*1024),completed_at=row['completed_at'].isoformat(),
            playback_available=available,playback_unavailable_reason=None if available else 'version_superseded')

def execute_edit(engine,root,tid,evidence,identifier,*,nonce=None,recipe=None,lookup=False):
    if not re.fullmatch('[0-9a-f]{32}',identifier or ''):raise HTTPException(400,'Invalid edit operation ID')
    deadline=time.monotonic()+300
    sessions=lambda:Session(engine)
    with acquire_media_guard(root,tid) as guard,_root(root) as (root_fd,identity):
        with sessions() as db:
            principal=fence(db,evidence)
            found=db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one_or_none()
            media=db.get(Timelapse,tid)
            if media is None:raise HTTPException(404,'Timelapse not found')
            assert_timelapse_access(db,media,principal,mutation=True)
            if found is not None:
                found=dict(found)
                if found['source_timelapse_id']!=tid or found['actor_id']!=principal['id'] or found['kind'] not in ('trim','speed'):raise HTTPException(404,'Edit operation not found')
                if not lookup and (found['source_nonce']!=nonce or recipe_from_row(found)!=recipe):raise HTTPException(409,'Edit operation identity differs')
                source_nonce=found['source_nonce']
            else:
                if lookup:raise HTTPException(404,'Edit operation not found')
                if media.ownership_operation_id!=nonce:raise HTTPException(412,'Recording version changed')
                snapshot=_snapshot(db,evidence,tid,lock=True);source_nonce=nonce
        journal=EditJournal(sessions=sessions,root_fd=root_fd,root_identity=identity,guard=guard,evidence=evidence,
            deadline=deadline,nonce=source_nonce,timelapse_id=tid,printer_id=media.printer_id)
        if found is not None:
            row,saved=journal.load(identifier)
            if row['phase']=='complete':return journal.receipt(row,saved)
            return journal.resume(row,saved)
        output=snapshot['output'];parents=[r for r in snapshot['facts'] if r['role']=='output_parent' and r['relative_path']]
        with open_proven_media(root_fd=root_fd,root_identity=identity,guard=guard,lifetime=snapshot['life'],path=snapshot['media'][3],
            expected=output,parent_facts=parents,ctime=snapshot['op']['published_ctime_ns'],deadline=deadline) as (fd,actual):
            meta=probe_owned_media(guard,fd,actual,deadline,identifier)
            frame_plan(recipe,meta['count'])
            row,saved=journal.admit(identifier,recipe,snapshot,actual)
        return journal.work(row,saved,meta)
