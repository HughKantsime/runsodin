"""Durable ordered encode: completed frame receipts only; original media retained."""
import os
import re
import stat
import time
import uuid
from datetime import datetime,timezone
from sqlalchemy import select,update,text
from core.timelapse_publication_schema import publications,facts,bindings,versions,lifetimes,namespaces
from core.timelapse_ownership_schema import anchors
from core.timelapse_publication_validation import operation,bind,STABLE,FILE,EMPTY
from core.timelapse_media_lock import acquire_media_guard
from core.timelapse_manifest import _root,MAX_OBJECTS
from core.timelapse_recipes import make_recipe,recipe_from_row
from core.timelapse_rename import rename_noreplace
from core.media_process_capability import MediaEncodeCapability,worker_command,complete_media_transport
from core.sender_process_group import OwnedSenderGroup,OwnedGroupStartupError
from core.timelapse_media_worker import file_facts,FILE_KEYS
from core.audit import log_audit
from core.base import JobStatus

TERMINAL_JOB_STATES=frozenset(value.value for value in (JobStatus.COMPLETED,JobStatus.FAILED,JobStatus.CANCELLED))
from modules.archives.timelapse_frame_publication import FrameJournal,_directory,_file,_same
from modules.archives.timelapse_frame_capture import _capture_authority,_camera_authority


class EncodeUnavailable(ValueError):pass


def _refuse():raise EncodeUnavailable('Timelapse encode could not be confirmed') from None


def reopen_namespace(sessions,root_fd,root_identity,guard,nonce):
    """Reopen only recorded original binding; never create/adopt/replace."""
    with sessions() as db:
        binding=dict(db.execute(select(bindings).where(bindings.c.lifetime_nonce==nonce)).mappings().one())
        space=dict(bind(db,binding))
        lifetime=dict(db.execute(select(lifetimes).where(lifetimes.c.lifetime_nonce==nonce)).mappings().one())
    _same(_directory(os.fstat(root_fd)),{key:lifetime['root_'+key] for key in STABLE},STABLE)
    if space['phase']!='bound':_refuse()
    guard.assert_root_identity(root_identity)
    descriptor=os.open(space['relative_path'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
    try:
        _same(_directory(os.fstat(descriptor)),space,STABLE)
        _same(_directory(os.stat(space['relative_path'],dir_fd=root_fd,follow_symlinks=False)),space,STABLE)
    except BaseException:os.close(descriptor);raise
    return descriptor,space


class EncodeJournal(FrameJournal):
    def __init__(self,*,job_id,**kwargs):
        super().__init__(authority=self._authority,**kwargs)
        self.job_id=job_id;self.deadline=time.monotonic()+300

    def _authority(self,db,*,allow_ended=True,op=None,completed=False):
        for table,identifier in (('jobs',self.job_id),('printers',self.pid),('timelapses',self.tid)):
            if db.get_bind().dialect.name=='postgresql':
                if db.execute(text(f'SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT'),{'id':identifier}).first() is None:_refuse()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- lock helper receives literal table/column/predicate strings only; values are bound
            elif db.execute(text(f'UPDATE {table} SET id=id WHERE id=:id'),{'id':identifier}).rowcount!=1:_refuse()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- lock helper receives literal table/column/predicate strings only; values are bound
        row=dict(db.execute(text('SELECT printer_id,print_job_id,filename,status,ownership_operation_id,retirement_operation_id,frame_count,duration_seconds,file_size_mb,completed_at '
            'FROM timelapses WHERE id=:id'),{'id':self.tid}).mappings().one())
        owner=dict(db.execute(select(anchors).where(anchors.c.operation_id==self.nonce)).mappings().one())
        job=db.execute(text('SELECT printer_id,status FROM jobs WHERE id=:id'),{'id':self.job_id}).mappings().one()
        expected_nonce=op['result_nonce'] if completed else self.nonce
        expected_filename=op['destination_path'] if completed else owner['filename']
        expected_status='ready' if completed else ('encoding' if op is not None else 'capturing')
        if (row['printer_id']!=self.pid or row['print_job_id']!=self.job_id or row['ownership_operation_id']!=expected_nonce
            or row['filename']!=expected_filename or row['status']!=expected_status or row['retirement_operation_id'] is not None
            or owner['timelapse_id']!=self.tid or owner['printer_id']!=self.pid or owner['scheduled_job_id']!=self.job_id
            or owner['owner_state'] not in ('known','shared') or owner['ownership_source']!='first_capture_job_file'
            or job['printer_id']!=self.pid or type(job['status']) is not str or job['status'] not in TERMINAL_JOB_STATES):_refuse()
        _camera_authority(db,self.nonce,self.pid,self.job_id)
        if completed:
            saved=[dict(value) for value in db.execute(select(facts).where(facts.c.operation_id==op['operation_id'])).mappings()]
            count=sum(value['role']=='source' for value in saved);output=next(value for value in saved if value['role']=='output')
            if row['frame_count']!=count or row['duration_seconds']!=count/recipe_from_row(op).encode_fps or row['file_size_mb']!=output['byte_count']/(1024*1024) or row['completed_at'] is None:_refuse()
            import json
            receipts=db.execute(text("SELECT details FROM audit_logs WHERE action='timelapse_encode_complete' AND entity_type='timelapse' AND entity_id=:id"),{'id':self.tid}).scalars().all()
            expected={'operation_id':op['operation_id'],'frame_count':count}
            if sum((json.loads(value) if type(value) is str else value)==expected for value in receipts)!=1:_refuse()
        return row,owner

    def load(self,identifier):
        with self.sessions() as db:
            row=dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one())
            operation(db,row)
            saved=[dict(value) for value in db.execute(select(facts).where(facts.c.operation_id==identifier)).mappings()]
        if row['kind']!='encode' or row['source_nonce']!=self.nonce or row['source_timelapse_id']!=self.tid or row['source_printer_id']!=self.pid:_refuse()
        return row,saved

    def _namespace(self,row):
        descriptor,_=reopen_namespace(self.sessions,self.root_fd,self.root_identity,self.guard,self.nonce)
        return descriptor

    def _verify(self,directory,name,expected,*,published_ctime=None,rename_ctime=False):
        descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        try:
            before=os.fstat(descriptor);actual=dict(file_facts(descriptor,self.deadline),kind='file')
            _same(actual,_file(before,actual['digest_sha256']),FILE)
            _same(actual,expected,STABLE+('link_count','byte_count','mtime_ns','digest_sha256'))
            if not rename_ctime and actual['ctime_ns']!=(expected['ctime_ns'] if published_ctime is None else published_ctime):_refuse()
            _same(actual,_file(os.stat(name,dir_fd=directory,follow_symlinks=False),actual['digest_sha256']),FILE)
            return actual
        finally:os.close(descriptor)

    def sources(self,directory,space):
        """Copy complete durable receipts DB-closed before reading any JPEG bytes."""
        FrameJournal.inventory(self,directory)
        with self.sessions() as db:
            rows=[dict(row) for row in db.execute(select(publications).where(publications.c.kind=='frame',
                publications.c.source_nonce==self.nonce,publications.c.frame_namespace_id==space['namespace_id'],
                publications.c.phase=='complete').order_by(publications.c.frame_number).limit(MAX_OBJECTS+1)).mappings()]
            saved=[]
            for row in rows:
                operation(db,row)
                value=dict(db.execute(select(facts).where(facts.c.operation_id==row['operation_id'],facts.c.role=='output')).mappings().one())
                value['ctime_ns']=row['published_ctime_ns'];saved.append((row,value))
        if len(saved)>MAX_OBJECTS:_refuse()
        total=0;manifest=[];source_facts=[]
        for row,expected in saved:
            name='frame_'+str(row['frame_number']).zfill(6)+'.jpg'
            actual=self._verify(directory,name,expected);total+=actual['byte_count']
            if total>32*1024**3:_refuse()
            manifest.append({'number':row['frame_number'],'name':name,'facts':{key:actual[key] for key in FILE_KEYS}})
            source_facts.append(dict(actual,role='source',relative_path=space['relative_path']+'/'+name))
        return manifest,source_facts

    def zero_snapshot(self,db):
        current,_=self._authority(db)
        if type(current['frame_count']) is not int or current['frame_count']!=0:_refuse()
        if db.execute(select(bindings).where(bindings.c.lifetime_nonce==self.nonce)).first() is not None:_refuse()
        if db.execute(select(publications.c.operation_id).where(publications.c.kind=='frame',publications.c.source_nonce==self.nonce)).first() is not None:_refuse()
        records=[dict(value) for value in db.execute(select(namespaces).limit(MAX_OBJECTS+1)).mappings()]
        all_bindings=[dict(value) for value in db.execute(select(bindings).limit(MAX_OBJECTS+1)).mappings()]
        if len(records)>MAX_OBJECTS or len(all_bindings)>MAX_OBJECTS:_refuse()
        mapped={value['namespace_id']:value for value in all_bindings};expected={};saved=[]
        for value in records:
            if type(value['relative_path']) is not str or value['relative_path']!='frames_'+value['namespace_id']:_refuse()
            if value['phase']!='bound':continue
            binding=mapped.get(value['namespace_id'])
            if binding is None:_refuse()
            space=dict(bind(db,binding))
            lifetime=dict(db.execute(select(lifetimes).where(lifetimes.c.lifetime_nonce==space['lifetime_nonce'])).mappings().one())
            expected[space['relative_path']]=(space,{key:lifetime['root_'+key] for key in STABLE})
            saved.append((space,binding,lifetime))
        return records,all_bindings,saved,expected

    def verify_zero_inventory(self,snapshot):
        """No contents/adoption: any unrecorded random namespace blocks zero."""
        if not self._absent(self.root_fd,str(self.tid)):_refuse()
        if not self._absent(self.root_fd,'frames_'+self.nonce):_refuse()
        expected=snapshot[3];root=_directory(os.fstat(self.root_fd));count=0
        self.guard.assert_root_identity(self.root_identity)
        with os.scandir(self.root_fd) as entries:
            for entry in entries:
                count+=1
                if count>MAX_OBJECTS:_refuse()
                if not entry.name.startswith('frames_'):continue
                if entry.name not in expected:_refuse()
                space,saved_root=expected[entry.name]
                _same(root,saved_root,STABLE)
                _same(_directory(entry.stat(follow_symlinks=False)),space,STABLE)
        self.guard.assert_owned()

    def insufficient(self,count,*,zero_snapshot=None):
        """Existing failed metadata plus explicit audit reason; no phantom output."""
        try:
            with self.sessions() as db:
                row,_=self._authority(db)
                if zero_snapshot is not None:
                    if self.zero_snapshot(db)!=zero_snapshot:_refuse()
                    self.guard.assert_root_identity(self.root_identity)
                changed=db.execute(text("UPDATE timelapses SET status='failed',completed_at=:now,frame_count=:count,duration_seconds=NULL,file_size_mb=NULL "
                    "WHERE id=:id AND ownership_operation_id=:nonce AND filename=:filename AND status='capturing' AND retirement_operation_id IS NULL"),
                    {'now':datetime.now(timezone.utc),'count':count,'id':self.tid,'nonce':self.nonce,'filename':row['filename']})
                if changed.rowcount!=1:_refuse()
                log_audit(db,'timelapse_encode_failed','timelapse',self.tid,{'reason':'insufficient_frames','ownership_nonce':self.nonce,'frame_count':count})
                self.guard.assert_owned();db.commit()
        except Exception:
            with self.sessions() as db:
                row=db.execute(text('SELECT status,ownership_operation_id,filename,retirement_operation_id,printer_id,print_job_id,completed_at,duration_seconds,file_size_mb,frame_count FROM timelapses WHERE id=:id'),{'id':self.tid}).mappings().one()
                owner=db.execute(select(anchors).where(anchors.c.operation_id==self.nonce)).mappings().one()
                receipts=db.execute(text("SELECT details FROM audit_logs WHERE action='timelapse_encode_failed' AND entity_type='timelapse' AND entity_id=:id"),{'id':self.tid}).scalars().all()
                import json
                expected={'reason':'insufficient_frames','ownership_nonce':self.nonce,'frame_count':count}
                matching=[value for value in receipts if (json.loads(value) if type(value) is str else value)==expected]
                if row['frame_count']!=count or row['printer_id']!=self.pid or row['print_job_id']!=self.job_id or row['completed_at'] is None or row['duration_seconds'] is not None or row['file_size_mb'] is not None or row['status']!='failed' or row['ownership_operation_id']!=self.nonce or row['filename']!=owner['filename'] or row['retirement_operation_id'] is not None or len(matching)!=1:_refuse()
        return False

    def admit(self,manifest,source_facts,space):
        identifier=uuid.uuid4().hex;result_nonce=uuid.uuid4().hex;destination='encode_'+identifier+'.mp4'
        with self.sessions() as db:
            current,owner=self._authority(db)
            result_owner={key:value for key,value in owner.items() if key!='created_at'}
            result_owner.update(operation_id=result_nonce,filename=destination)
            db.execute(anchors.insert().values(**result_owner))
            from core import timelapse_capture_tenancy_schema as capture_tenancy
            capture_tenancy.copy(db.connection(),self.nonce,result_nonce)
            db.execute(versions.insert().values(version_nonce=result_nonce,lifetime_nonce=self.nonce))
            values=dict(operation_id=identifier,source_nonce=self.nonce,source_lifetime=self.nonce,kind='encode',
                source_timelapse_id=self.tid,source_printer_id=self.pid,source_filename=current['filename'],source_status='encoding',
                source_preclaim_status='capturing',actor_id=None,frame_namespace_id=None,frame_number=None,payload_byte_count=None,payload_digest_sha256=None,
                private_directory='publish_'+identifier,private_name='output.mp4',destination_path=destination,
                result_nonce=result_nonce,result_lifetime=self.nonce,result_tid=self.tid,phase='admitted',state='active',revision=1,
                reason_code=None,published_ctime_ns=None,completed_at=None,**make_recipe('encode').columns())
            db.execute(publications.insert().values(**values))
            additions=[dict(_directory(os.fstat(self.root_fd)),role='root',relative_path=''),
                dict(_directory(os.fstat(self.root_fd)),role='output_parent',relative_path='')]
            clean={key:space.get(key) for key in _directory(os.fstat(self.root_fd))};clean['kind']='directory'
            additions.append(dict(clean,role='source_parent',relative_path=space['relative_path']))
            for value in additions+source_facts:db.execute(facts.insert().values(operation_id=identifier,**value))
            changed=db.execute(text("UPDATE timelapses SET status='encoding' WHERE id=:id AND ownership_operation_id=:nonce AND filename=:filename "
                "AND status='capturing' AND retirement_operation_id IS NULL"),{'id':self.tid,'nonce':self.nonce,'filename':current['filename']})
            if changed.rowcount!=1:_refuse()
            operation(db,dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one()))
            self.guard.assert_owned()
            try:db.commit()
            except Exception:
                db.rollback();self.load(identifier)
        return identifier

    def _advance(self,identifier,expected,target,*,snapshot,added=(),ctime=None):
        original,original_facts=snapshot
        ordered=lambda values:sorted(values,key=lambda value:(value['role'],value['relative_path']))
        try:
            with self.sessions() as db:
                row=dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one())
                self._authority(db,op=row)
                present=[dict(value) for value in db.execute(select(facts).where(facts.c.operation_id==identifier)).mappings()]
                if row!=original or ordered(present)!=ordered(original_facts) or row['phase']!=expected or row['state']!='active':_refuse()
                for value in added:db.execute(facts.insert().values(operation_id=identifier,**value))
                changes=dict(phase=target,state='complete' if target=='complete' else 'active',reason_code=None,revision=row['revision']+1)
                if ctime is not None:changes['published_ctime_ns']=ctime
                if target=='complete':
                    changes['completed_at']=datetime.now(timezone.utc)
                    output=next(value for value in present if value['role']=='output')
                    count=sum(value['role']=='source' for value in present)
                    updated=db.execute(text("UPDATE timelapses SET status='ready',filename=:destination,ownership_operation_id=:result_nonce,"
                        "frame_count=:count,duration_seconds=:duration,file_size_mb=:size,completed_at=:now "
                        "WHERE id=:id AND ownership_operation_id=:source_nonce AND filename=:source_filename AND status='encoding' AND retirement_operation_id IS NULL"),
                        {'destination':row['destination_path'],'result_nonce':row['result_nonce'],'count':count,'duration':count/recipe_from_row(row).encode_fps,
                        'size':output['byte_count']/(1024*1024),'now':changes['completed_at'],'id':self.tid,'source_nonce':self.nonce,'source_filename':row['source_filename']})
                    if updated.rowcount!=1:_refuse()
                    log_audit(db,'timelapse_encode_complete','timelapse',self.tid,{'operation_id':identifier,'frame_count':count})
                changed=db.execute(update(publications).where(publications.c.operation_id==identifier,publications.c.phase==expected,
                    publications.c.state=='active',publications.c.revision==row['revision']).values(**changes))
                if changed.rowcount!=1:_refuse()
                operation(db,dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one()))
                self.guard.assert_owned();db.commit()
        except Exception:
            current,current_facts=self.load(identifier)
            mutable={'phase','state','reason_code','revision','published_ctime_ns','completed_at'}
            expected_facts=original_facts+[dict(value,operation_id=identifier) for value in added]
            if current['phase']!=target or current['state']!=('complete' if target=='complete' else 'active') or current['revision']!=original['revision']+1 or any(current[key]!=value for key,value in original.items() if key not in mutable) or ordered(current_facts)!=ordered(expected_facts):_refuse()
            if target=='complete':
                with self.sessions() as db:self._authority(db,op=current,completed=True)
        return self.load(identifier)

    def work(self,identifier):
        row,saved=self.load(identifier)
        if row['phase']!='admitted' or row['state']!='active':_refuse()
        namespace=self._namespace(row);private=None;output=None;group=None;channels=[]
        try:
            self.inventory(namespace)
            os.mkdir(row['private_directory'],0o700,dir_fd=self.root_fd)
            private=os.open(row['private_directory'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.root_fd)
            private_fact=_directory(os.fstat(private))
            output=os.open('output.mp4',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=private)
            creation=_file(os.fstat(output),EMPTY)
            os.fsync(output);os.fsync(private);os.fsync(self.root_fd)
            row,saved=self._advance(identifier,'admitted','private_created',snapshot=(row,saved),added=[
                dict(private_fact,role='private_root',relative_path=row['private_directory']),dict(creation,role='private_creation',relative_path='output.mp4')])
            cap=MediaEncodeCapability.from_receipts(self.guard,namespace,output,_directory(os.fstat(namespace)),creation)
            a,b=os.pipe();c,d=os.pipe();channels=[a,b,c,d]
            group=OwnedSenderGroup(worker_command(),defer_start=True,channels={'input':a,'result':d},media=cap)
            for fd in (a,d):channels.remove(fd);os.close(fd)
            row,saved=self._advance(identifier,'private_created','worker_admitted',snapshot=(row,saved))
            sources=sorted((value for value in saved if value['role']=='source'),key=lambda value:int(value['relative_path'].split('/frame_')[1][:-4]))
            manifest={'operation_id':identifier,**recipe_from_row(row).columns(),'frames':[
                {'number':int(value['relative_path'].split('/frame_')[1][:-4]),'name':value['relative_path'].split('/')[-1],
                    'facts':{key:value[key] for key in FILE_KEYS}} for value in sources]}
            manifest={key:manifest[key] for key in ('operation_id','recipe_version','encode_fps','frames')}
            actual=complete_media_transport(group,manifest,input_writer=b,result_reader=c,namespace_fd=namespace,output_fd=output,
                private_parent_fd=private,private_parent_facts=private_fact,deadline=self.deadline)
            # Close all owned transport FDs before recording output-ready.
            group.retire();group=None
            for fd in list(channels):channels.remove(fd);os.close(fd)
            descriptor,output=output,None;os.close(descriptor)
            descriptor,namespace=namespace,None;os.close(descriptor)
            descriptor,private=private,None;os.close(descriptor)
            final=dict(actual,kind='file',role='output',relative_path='output.mp4')
            self._advance(identifier,'worker_admitted','output_ready',snapshot=(row,saved),added=[final])
        finally:
            if group is not None:group.retire()
            for fd in channels:os.close(fd)
            for fd in (output,private,namespace):
                if fd is not None:os.close(fd)
        return self.resume(identifier)

    def resume(self,identifier):
        row,saved=self.load(identifier)
        if row['phase']=='complete':
            with self.sessions() as db:self._authority(db,op=row,completed=True)
            namespace=self._namespace(row)
            try:
                self.inventory(namespace)
                for value in saved:
                    if value['role']=='source':self._verify(namespace,value['relative_path'].split('/')[-1],value)
            finally:os.close(namespace)
            expected=next(value for value in saved if value['role']=='output')
            self._verify(self.root_fd,row['destination_path'],expected,published_ctime=row['published_ctime_ns'])
            return True
        if row['state']!='active':return False
        if row['phase'] in ('admitted','private_created','worker_admitted'):
            self._block(row,'phase_uncertain');return False
        expected=next(value for value in saved if value['role']=='output')
        private=self._private(row,saved);namespace=self._namespace(row)
        try:
            self.inventory(namespace)
            for value in saved:
                if value['role']=='source':self._verify(namespace,value['relative_path'].split('/')[-1],value)
            if row['phase']=='output_ready':
                self._verify(private,'output.mp4',expected)
                row,saved=self._advance(identifier,'output_ready','publish_pending',snapshot=(row,saved))
            if row['phase']=='publish_pending':
                source=not self._absent(private,'output.mp4');destination=not self._absent(self.root_fd,row['destination_path'])
                if source and not destination:
                    self._verify(private,'output.mp4',expected)
                    with self.sessions() as db:
                        self._authority(db,op=row);self.guard.assert_owned()
                        rename_noreplace(private,'output.mp4',self.root_fd,row['destination_path'])
                        _same(_file(os.stat(row['destination_path'],dir_fd=self.root_fd,follow_symlinks=False),expected['digest_sha256']),expected,STABLE+('link_count','byte_count','mtime_ns'))
                        db.rollback()
                elif not source and destination:self._verify(self.root_fd,row['destination_path'],expected,rename_ctime=True)
                else:_refuse()
                os.fsync(private);os.fsync(self.root_fd)
                actual=self._verify(self.root_fd,row['destination_path'],expected,rename_ctime=True)
                if not self._absent(private,'output.mp4'):_refuse()
                row,saved=self._advance(identifier,'publish_pending','published',snapshot=(row,saved),ctime=actual['ctime_ns'])
            if row['phase']=='published':
                self._verify(self.root_fd,row['destination_path'],expected,published_ctime=row['published_ctime_ns'])
                if not self._absent(private,'output.mp4'):_refuse()
                self._advance(identifier,'published','complete',snapshot=(row,saved));return True
            _refuse()
        finally:os.close(namespace);os.close(private)


def encode_owned_timelapse(*,sessions,root,timelapse_id,printer_id,nonce,job_id):
    if type(nonce) is not str or re.fullmatch('[0-9a-f]{32}',nonce) is None or any(type(value) is not int or value<=0 for value in (timelapse_id,printer_id,job_id)):return False
    try:
        with acquire_media_guard(root,timelapse_id) as guard,_root(root) as (root_fd,identity):
            journal=EncodeJournal(sessions=sessions,root_fd=root_fd,root_identity=identity,guard=guard,
                nonce=nonce,timelapse_id=timelapse_id,printer_id=printer_id,job_id=job_id)
            with sessions() as db:
                pending=db.execute(select(publications.c.operation_id).where(publications.c.kind=='encode',publications.c.source_nonce==nonce).limit(2)).scalars().all()
            if len(pending)>1:_refuse()
            if pending:return journal.resume(pending[0])
            def authority(db,*,allow_ended):return _capture_authority(db,nonce=nonce,timelapse_id=timelapse_id,printer_id=printer_id,job_id=job_id,allow_ended=allow_ended)
            frames=FrameJournal(sessions=sessions,root_fd=root_fd,root_identity=identity,guard=guard,authority=authority,
                nonce=nonce,timelapse_id=timelapse_id,printer_id=printer_id)
            frames.recover()
            with sessions() as db:
                journal._authority(db)
                binding=db.execute(select(bindings.c.namespace_id).where(bindings.c.lifetime_nonce==nonce)).scalar_one_or_none()
                zero_snapshot=journal.zero_snapshot(db) if binding is None else None
            if zero_snapshot is not None:
                journal.verify_zero_inventory(zero_snapshot)
                return journal.insufficient(0,zero_snapshot=zero_snapshot)
            directory,space=reopen_namespace(sessions,root_fd,identity,guard,nonce)
            try:manifest,saved=journal.sources(directory,space)
            finally:os.close(directory)
            if len(manifest)<2:return journal.insufficient(len(manifest))
            return journal.work(journal.admit(manifest,saved,space))
    except OwnedGroupStartupError as error:
        error.group.retire();return False
    except Exception:return False
