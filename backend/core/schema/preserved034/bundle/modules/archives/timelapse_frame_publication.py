"""Durable frame phases. No legacy adoption, filesystem deletion or frame replay."""
import hashlib
import os
import stat
import uuid
from datetime import datetime,timezone
from sqlalchemy import select,update,text
from core.timelapse_publication_schema import publications,facts,bindings
from core.timelapse_publication_validation import operation,bind,STABLE,FILE,EMPTY
from core.timelapse_manifest import MAX_OBJECTS
from core.timelapse_recipes import make_recipe
from core.timelapse_rename import rename_noreplace


class FramePublicationUnavailable(ValueError):
    pass


def _refuse():
    raise FramePublicationUnavailable('Frame publication could not be confirmed') from None


def _directory(value):
    if not stat.S_ISDIR(value.st_mode):_refuse()
    return dict(kind='directory',device=value.st_dev,inode=value.st_ino,uid=value.st_uid,gid=value.st_gid,mode=value.st_mode,
                link_count=None,byte_count=None,mtime_ns=None,ctime_ns=None,digest_sha256=None)


def _file(value,digest):
    if not stat.S_ISREG(value.st_mode) or value.st_nlink!=1:_refuse()
    return dict(kind='file',device=value.st_dev,inode=value.st_ino,uid=value.st_uid,gid=value.st_gid,mode=value.st_mode,
                link_count=value.st_nlink,byte_count=value.st_size,mtime_ns=value.st_mtime_ns,ctime_ns=value.st_ctime_ns,digest_sha256=digest)


def _same(actual,expected,keys):
    if any(actual[key]!=expected[key] for key in keys):_refuse()


class FrameJournal:
    def __init__(self,*,sessions,root_fd,root_identity,guard,authority,nonce,timelapse_id,printer_id):
        self.sessions=sessions;self.root_fd=root_fd;self.root_identity=root_identity;self.guard=guard
        self.authority=authority;self.nonce=nonce;self.tid=timelapse_id;self.pid=printer_id

    def load(self,identifier):
        with self.sessions() as db:
            row=dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one())
            operation(db,row)
            saved=[dict(f) for f in db.execute(select(facts).where(facts.c.operation_id==identifier)).mappings()]
        if row['source_nonce']!=self.nonce or row['source_timelapse_id']!=self.tid or row['source_printer_id']!=self.pid or row['kind']!='frame':_refuse()
        return row,saved

    def _advance(self,identifier,expected,target,*,snapshot,added=(),ctime=None):
        """Commit one immutable phase; unknown ACK is resolved only by exact ID."""
        original,original_facts=snapshot
        try:
            with self.sessions() as db:
                self.authority(db,allow_ended=expected in ('output_ready','publish_pending','published'))
                row=dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one())
                present=[dict(value) for value in db.execute(select(facts).where(facts.c.operation_id==identifier)).mappings()]
                ordered=lambda values:sorted(values,key=lambda value:(value['role'],value['relative_path']))
                if row!=original or ordered(present)!=ordered(original_facts):_refuse()
                if row['phase']!=expected or row['state'] not in ('active','blocked') or (row['state']=='blocked' and row['reason_code'] not in ('writer_active','file_io')):_refuse()
                for value in added:db.execute(facts.insert().values(operation_id=identifier,**value))
                changes=dict(phase=target,state='complete' if target=='complete' else 'active',reason_code=None,revision=row['revision']+1)
                if ctime is not None:changes['published_ctime_ns']=ctime
                if target=='complete':changes['completed_at']=datetime.now(timezone.utc)
                result=db.execute(update(publications).where(publications.c.operation_id==identifier,publications.c.phase==expected,
                    publications.c.state==row['state'],publications.c.revision==row['revision']).values(**changes))
                if result.rowcount!=1:_refuse()
                if target=='complete':
                    counted=db.execute(text("UPDATE timelapses SET frame_count=frame_count+1 WHERE id=:id AND ownership_operation_id=:nonce "
                        "AND filename=:filename AND status='capturing' AND retirement_operation_id IS NULL"),
                        {'id':self.tid,'nonce':self.nonce,'filename':row['source_filename']})
                    if counted.rowcount!=1:_refuse()
                operation(db,dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one()))
                self.guard.assert_owned();db.commit()
        except Exception:
            # A commit may have succeeded before its acknowledgment failed.
            current,current_facts=self.load(identifier)
            mutable={'phase','state','reason_code','revision','published_ctime_ns','completed_at'}
            if current['phase']!=target or current['revision']!=original['revision']+1 or any(current[key]!=value for key,value in original.items() if key not in mutable):_refuse()
            expected_facts=original_facts+[dict(value,operation_id=identifier) for value in added]
            if sorted(current_facts,key=lambda value:(value['role'],value['relative_path']))!=sorted(expected_facts,key=lambda value:(value['role'],value['relative_path'])):_refuse()
        return self.load(identifier)

    def _block(self,row,reason):
        with self.sessions() as db:
            changed=db.execute(update(publications).where(publications.c.operation_id==row['operation_id'],
                publications.c.phase==row['phase'],publications.c.state==row['state'],publications.c.revision==row['revision']).values(
                    state='blocked',reason_code=reason,revision=row['revision']+1))
            if changed.rowcount!=1:_refuse()
            db.commit()

    def _namespace(self,row):
        with self.sessions() as db:
            binding=dict(db.execute(select(bindings).where(bindings.c.lifetime_nonce==self.nonce)).mappings().one())
            space=bind(db,binding)
        if space['namespace_id']!=row['frame_namespace_id'] or space['phase']!='bound':_refuse()
        self.guard.assert_root_identity(self.root_identity)
        descriptor=os.open(space['relative_path'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.root_fd)
        try:
            _same(_directory(os.fstat(descriptor)),space,STABLE)
            _same(_directory(os.stat(space['relative_path'],dir_fd=self.root_fd,follow_symlinks=False)),space,STABLE)
        except BaseException:os.close(descriptor);raise
        return descriptor

    def _private(self,row,saved):
        root=next(value for value in saved if value['role']=='root')
        _same(_directory(os.fstat(self.root_fd)),root,STABLE)
        self.guard.assert_owned()
        expected=next(value for value in saved if value['role']=='private_root')
        descriptor=os.open(row['private_directory'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.root_fd)
        try:
            _same(_directory(os.fstat(descriptor)),expected,STABLE)
            _same(_directory(os.stat(row['private_directory'],dir_fd=self.root_fd,follow_symlinks=False)),expected,STABLE)
        except BaseException:os.close(descriptor);raise
        return descriptor

    def _verify(self,directory,name,expected,*,published_ctime=None,rename_ctime=False):
        descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        try:
            before=os.fstat(descriptor);digest=hashlib.sha256();total=0
            while True:
                part=os.read(descriptor,65536)
                if not part:break
                total+=len(part)
                if total>16777216:_refuse()
                digest.update(part)
            actual=_file(os.fstat(descriptor),digest.hexdigest())
            _same(actual,_file(before,digest.hexdigest()),FILE)
            _same(actual,expected,STABLE+('link_count','byte_count','mtime_ns','digest_sha256'))
            if not rename_ctime and actual['ctime_ns']!=(expected['ctime_ns'] if published_ctime is None else published_ctime):_refuse()
            _same(actual,_file(os.stat(name,dir_fd=directory,follow_symlinks=False),actual['digest_sha256']),FILE)
            return actual
        finally:os.close(descriptor)

    def _absent(self,directory,name):
        try:os.stat(name,dir_fd=directory,follow_symlinks=False)
        except FileNotFoundError:return True
        return False

    def inventory(self,directory):
        """Refuse unknown namespace entries; never mint receipts from disk."""
        with self.sessions() as db:
            rows=[dict(row) for row in db.execute(select(publications).where(publications.c.kind=='frame',
                publications.c.source_nonce==self.nonce,publications.c.phase=='complete').limit(MAX_OBJECTS+1)).mappings()]
            if len(rows)>MAX_OBJECTS:_refuse()
            expected={}
            for row in rows:
                operation(db,row)
                saved=dict(db.execute(select(facts).where(facts.c.operation_id==row['operation_id'],facts.c.role=='output')).mappings().one())
                expected[row['destination_path'].split('/')[-1]]=(saved,row['published_ctime_ns'])
        count=0
        with os.scandir(directory) as entries:
            for entry in entries:
                count+=1
                if count>MAX_OBJECTS or entry.name not in expected:_refuse()
                saved,ctime=expected[entry.name]
                actual=_file(entry.stat(follow_symlinks=False),saved['digest_sha256'])
                _same(actual,saved,STABLE+('link_count','byte_count','mtime_ns'))
                if actual['ctime_ns']!=ctime:_refuse()
        if count!=len(expected):_refuse()

    def admit(self,payload):
        identifier=uuid.uuid4().hex
        payload_digest=hashlib.sha256(payload).hexdigest()
        with self.sessions() as db:
            current=self.authority(db,allow_ended=False)
            binding=dict(db.execute(select(bindings).where(bindings.c.lifetime_nonce==self.nonce)).mappings().one())
            space=bind(db,binding);number=binding['next_number']
            if type(number) is not int or not 1<=number<(1<<63)-1:_refuse()
            changed=db.execute(update(bindings).where(bindings.c.lifetime_nonce==self.nonce,bindings.c.next_number==number).values(next_number=number+1))
            if changed.rowcount!=1:_refuse()
            values=dict(operation_id=identifier,source_nonce=self.nonce,source_lifetime=self.nonce,kind='frame',
                source_timelapse_id=self.tid,source_printer_id=self.pid,source_filename=current['filename'],source_status='capturing',
                source_preclaim_status=None,actor_id=None,frame_namespace_id=space['namespace_id'],frame_number=number,
                payload_byte_count=len(payload),payload_digest_sha256=payload_digest,
                private_directory='publish_'+identifier,private_name='output.jpg',destination_path=space['relative_path']+f'/frame_{number:06d}.jpg',
                result_nonce=self.nonce,result_lifetime=self.nonce,result_tid=self.tid,phase='admitted',state='active',revision=1,
                reason_code=None,published_ctime_ns=None,completed_at=None,**make_recipe('frame').columns())
            db.execute(publications.insert().values(**values))
            for role,path,value in (('root','',_directory(os.fstat(self.root_fd))),('source_parent',space['relative_path'],dict(space)),('output_parent',space['relative_path'],dict(space))):
                clean={key:value.get(key) for key in _directory(os.fstat(self.root_fd))};clean['kind']='directory'
                db.execute(facts.insert().values(operation_id=identifier,role=role,relative_path=path,**clean))
            operation(db,dict(db.execute(select(publications).where(publications.c.operation_id==identifier)).mappings().one()))
            self.guard.assert_owned()
            try:db.commit()
            except Exception:
                db.rollback()
                self.load(identifier)  # Refuse absent/unknown admission; never allocate another ID.
        return identifier

    def write(self,identifier,payload):
        row,saved=self.load(identifier)
        if row['phase']!='admitted' or row['state']!='active' or len(payload)!=row['payload_byte_count'] or hashlib.sha256(payload).hexdigest()!=row['payload_digest_sha256']:_refuse()
        namespace=self._namespace(row)
        os.close(namespace)
        self.guard.assert_owned()
        os.mkdir(row['private_directory'],0o700,dir_fd=self.root_fd)
        private=os.open(row['private_directory'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.root_fd)
        output=None
        try:
            private_fact=_directory(os.fstat(private))
            if private_fact['mode']!=stat.S_IFDIR|0o700 or private_fact['inode']==self.root_identity.inode:_refuse()
            output=os.open('output.jpg',os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=private)
            creation=_file(os.fstat(output),EMPTY)
            os.fsync(output);os.fsync(private);os.fsync(self.root_fd)
            row,saved=self._advance(identifier,'admitted','private_created',snapshot=(row,saved),added=[dict(private_fact,role='private_root',relative_path=row['private_directory']),dict(creation,role='private_creation',relative_path='output.jpg')])
            view=memoryview(payload)
            while view:
                count=os.write(output,view)
                if count<=0:_refuse()
                view=view[count:]
            os.fsync(output);os.fsync(private)
            final=_file(os.fstat(output),row['payload_digest_sha256'])
            self._verify(private,'output.jpg',final)
            self._advance(identifier,'private_created','output_ready',snapshot=(row,saved),added=[dict(final,role='output',relative_path='output.jpg')])
        finally:
            if output is not None:os.close(output)
            os.close(private)
        return self.resume(identifier)

    def resume(self,identifier):
        row,saved=self.load(identifier)
        if row['phase']=='complete':return True
        if row['phase'] in ('admitted','private_created'):
            self._block(row,'phase_uncertain');return False
        if row['state']=='blocked' and row['reason_code'] not in ('writer_active','file_io'):return False
        expected=next(value for value in saved if value['role']=='output')
        private=self._private(row,saved);namespace=self._namespace(row)
        try:
            name=row['destination_path'].split('/')[-1]
            if row['phase']=='output_ready':
                self._verify(private,'output.jpg',expected)
                row,saved=self._advance(identifier,'output_ready','publish_pending',snapshot=(row,saved))
            if row['phase']=='publish_pending':
                source_present=not self._absent(private,'output.jpg');destination_present=not self._absent(namespace,name)
                if source_present and not destination_present:
                    self._verify(private,'output.jpg',expected)
                    with self.sessions() as db:
                        self.authority(db,allow_ended=True)
                        self.guard.assert_owned()
                        rename_noreplace(private,'output.jpg',namespace,name)
                        _same(_file(os.stat(name,dir_fd=namespace,follow_symlinks=False),expected['digest_sha256']),expected,STABLE+('link_count','byte_count','mtime_ns'))
                        db.rollback()  # Release authority locks before directory synchronization.
                elif not source_present and destination_present:
                    self._verify(namespace,name,expected,rename_ctime=True)
                else:_refuse()
                os.fsync(private);os.fsync(namespace)
                actual=self._verify(namespace,name,expected,rename_ctime=True)
                if not self._absent(private,'output.jpg'):_refuse()
                row,saved=self._advance(identifier,'publish_pending','published',snapshot=(row,saved),ctime=actual['ctime_ns'])
            if row['phase']=='published':
                self._verify(namespace,name,expected,published_ctime=row['published_ctime_ns'])
                if not self._absent(private,'output.jpg'):_refuse()
                self._advance(identifier,'published','complete',snapshot=(row,saved))
                return True
            _refuse()
        finally:os.close(namespace);os.close(private)

    def recover(self):
        with self.sessions() as db:
            pending=[dict(row) for row in db.execute(select(publications).where(publications.c.kind=='frame',
                publications.c.source_nonce==self.nonce,publications.c.phase!='complete').order_by(publications.c.frame_number).limit(MAX_OBJECTS+1)).mappings()]
        if len(pending)>MAX_OBJECTS:_refuse()
        recovered=False
        for row in pending:
            if row['state']=='blocked' and row['reason_code'] not in ('writer_active','file_io'):continue
            if not self.resume(row['operation_id']) and row['phase'] not in ('admitted','private_created'):_refuse()
            recovered |= row['phase'] not in ('admitted','private_created')
        return recovered
