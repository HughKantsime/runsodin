"""Owned no-overwrite staging/reconciliation, never unlink or final authority."""
from contextlib import contextmanager
import hashlib
import os
import time
from core.timelapse_manifest import ObjectIdentity,_facts,_stable,_owned,_parts,_names,MAX_OBJECTS
from core.timelapse_digest import MAX_BYTES,CHUNK_BYTES
from core.timelapse_rename import rename_noreplace

FACTS=('device','inode','uid','gid','mode','link_count','byte_count','mtime_ns','ctime_ns')


class StagingFilesystemUnavailable(RuntimeError):
    def __init__(self,reason='object_changed'):
        self.reason=reason
        super().__init__('Media staging could not be confirmed')


def _refuse(reason='object_changed'):
    raise StagingFilesystemUnavailable(reason) from None


class StageFilesystem:
    """Root/quarantine descriptors remain caller-owned under its stable guard."""
    def __init__(self,root_fd,quarantine_fd,root_identity,quarantine_identity,plan,parents,moves,guard):
        self.root_fd=root_fd;self.quarantine_fd=quarantine_fd
        self.root=root_identity;self.quarantine=quarantine_identity;self.guard=guard
        self.plan=plan;self.parents=parents;self.moves=moves
        self.by_path={row['relative_path']:row for row in plan}
        self.deadline=time.monotonic()+60;self.remaining=3*MAX_BYTES
        present=[row for row in plan if row['state']!='originally_absent']
        if sum(row['byte_count'] for row in present if row['kind']=='file')>MAX_BYTES:_refuse('manifest_limit')
        self.children={}
        for row in present:
            parent=row['relative_path'].rpartition('/')[0]
            self.children.setdefault(parent,[]).append(row)
            move=moves[row['object_index']]
            if (move['phase'] in ('planned','rename_pending') and row['state']!='planned') or (move['phase']=='staged' and row['state']!='staged'):_refuse('phase_uncertain')
        for row in present:
            if row['kind']=='directory' and moves[row['object_index']]['phase'] in ('rename_pending','staged'):
                if any(moves[child['object_index']]['phase']!='staged' for child in self.children.get(row['relative_path'],[])):_refuse('phase_uncertain')

    def admit(self):
        if time.monotonic()>=self.deadline:_refuse('manifest_limit')
        self.guard.assert_root_identity(self.root)
        if _stable(_facts('',os.fstat(self.root_fd)))!=_stable(self.root) or _stable(_facts('.retirement',os.fstat(self.quarantine_fd)))!=_stable(self.quarantine) or _stable(_facts('.retirement',os.stat('.retirement',dir_fd=self.root_fd,follow_symlinks=False)))!=_stable(self.quarantine):_refuse('ownership_changed')

    def entry(self,fd,name,path):
        self.admit()
        try:return _facts(path,os.stat(name,dir_fd=fd,follow_symlinks=False))
        except FileNotFoundError:return None

    def identity(self,row):
        return ObjectIdentity(row['relative_path'],row['kind'],**{name:row[name] for name in FACTS})

    def stable_directory(self,value,expected):
        if value is None or value.kind!='directory' or _stable(value)!=_stable(expected):_refuse()
        _owned(value,self.root)

    def locations(self,parent,name,row):
        source=self.entry(parent,name,row['relative_path'])
        destination=self.entry(self.quarantine_fd,row['quarantine_name'],row['relative_path'])
        phase=self.moves[row['object_index']]['phase']
        if phase=='planned':
            if source is None or destination is not None:_refuse()
        elif phase=='rename_pending':
            if (source is None)==(destination is None):_refuse('phase_uncertain')
        elif phase=='staged':
            if source is not None or destination is None:_refuse()
        else:_refuse('phase_uncertain')
        return source,destination

    @contextmanager
    def directory(self,path):
        if not path:
            self.admit();yield self.root_fd;self.admit();return
        parts=_parts(path);parent_path='/'.join(parts[:-1]);name=parts[-1]
        with self.directory(parent_path) as parent:
            expected=self.parents.get(path)
            if expected is None:_refuse('phase_uncertain')
            row=self.by_path.get(path)
            destination=None
            if row is not None:
                if row['state']=='originally_absent' or row['kind']!='directory':_refuse('phase_uncertain')
                source,destination=self.locations(parent,name,row)
                chosen=source if source is not None else destination
                chosen_parent=parent if source is not None else self.quarantine_fd
                chosen_name=name if source is not None else row['quarantine_name']
            else:
                chosen=self.entry(parent,name,path);chosen_parent=parent;chosen_name=name
            self.stable_directory(chosen,expected)
            descriptor=os.open(chosen_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=chosen_parent)
            try:
                self.stable_directory(_facts(path,os.fstat(descriptor)),expected)
                if destination is not None:
                    if _names(descriptor,MAX_OBJECTS):_refuse()
                    move=self.moves[row['object_index']]
                    if move['phase']=='staged' and chosen.ctime_ns!=move['verified_ctime_ns']:_refuse()
                yield descriptor
                self.stable_directory(_facts(path,os.fstat(descriptor)),expected)
                self.stable_directory(self.entry(chosen_parent,chosen_name,path),expected)
                if row is not None:self.locations(parent,name,row)
            finally:os.close(descriptor)

    def check_file(self,value,row,*,ignore_ctime=False,verified=None):
        if value is None or value.kind!='file':_refuse()
        _owned(value,self.root)
        for name in FACTS:
            if name=='ctime_ns' and ignore_ctime:continue
            if getattr(value,name)!=row[name]:_refuse()
        if verified is not None and value.ctime_ns!=verified:_refuse()

    def hash_file(self,fd,name,row,*,ignore_ctime=False,verified=None):
        if row['byte_count']>self.remaining:_refuse('manifest_limit')
        value=self.entry(fd,name,row['relative_path'])
        self.check_file(value,row,ignore_ctime=ignore_ctime,verified=verified)
        descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        try:
            if _facts(row['relative_path'],os.fstat(descriptor))!=value:_refuse()
            digest=hashlib.sha256();left=row['byte_count']
            while left:
                self.admit()
                block=os.read(descriptor,min(CHUNK_BYTES,left))
                if not block or len(block)>self.remaining:_refuse('manifest_limit')
                self.remaining-=len(block);left-=len(block);digest.update(block)
            self.admit()
            extra=os.read(descriptor,1)
            if extra:
                self.remaining-=len(extra)
                if self.remaining<0:_refuse('manifest_limit')
                _refuse()
            if _facts(row['relative_path'],os.fstat(descriptor))!=value or self.entry(fd,name,row['relative_path'])!=value:_refuse()
            if digest.hexdigest()!=self.moves[row['object_index']]['digest_sha256']:_refuse()
            return value
        finally:os.close(descriptor)

    def check_directory(self,fd,name,row,*,verified=None):
        value=self.entry(fd,name,row['relative_path'])
        self.stable_directory(value,self.identity(row))
        descriptor=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            if _stable(_facts(row['relative_path'],os.fstat(descriptor)))!=_stable(value) or _names(descriptor,MAX_OBJECTS):_refuse()
            if verified is not None and value.ctime_ns!=verified:_refuse()
            if _stable(self.entry(fd,name,row['relative_path']))!=_stable(value):_refuse()
            return value
        finally:os.close(descriptor)

    def verify(self,row):
        parts=_parts(row['relative_path'])
        with self.directory('/'.join(parts[:-1])) as parent:
            if row['state']=='originally_absent':
                if self.entry(parent,parts[-1],row['relative_path']) is not None:_refuse()
                return None
            source,destination=self.locations(parent,parts[-1],row)
            if self.moves[row['object_index']]['phase']!='staged' or source is not None:_refuse('phase_uncertain')
            stamp=self.moves[row['object_index']]['verified_ctime_ns']
            if row['kind']=='file':return self.hash_file(self.quarantine_fd,row['quarantine_name'],row,ignore_ctime=True,verified=stamp)
            return self.check_directory(self.quarantine_fd,row['quarantine_name'],row,verified=stamp)

    def stage(self,row):
        parts=_parts(row['relative_path']);move=self.moves[row['object_index']]
        if move['phase']!='rename_pending':_refuse('phase_uncertain')
        if row['kind']=='directory' and any(self.moves[child['object_index']]['phase']!='staged' for child in self.children.get(row['relative_path'],[])):_refuse('phase_uncertain')
        with self.directory('/'.join(parts[:-1])) as parent:
            source,destination=self.locations(parent,parts[-1],row)
            if source is not None:
                if row['kind']=='file':self.hash_file(parent,parts[-1],row)
                else:self.check_directory(parent,parts[-1],row)
                self.admit()
                rename_noreplace(parent,parts[-1],self.quarantine_fd,row['quarantine_name'])
            if row['kind']=='file':value=self.hash_file(self.quarantine_fd,row['quarantine_name'],row,ignore_ctime=True)
            else:value=self.check_directory(self.quarantine_fd,row['quarantine_name'],row)
            self.admit();os.fsync(parent);self.admit();os.fsync(self.quarantine_fd);self.admit()
            if self.entry(self.quarantine_fd,row['quarantine_name'],row['relative_path'])!=value:_refuse()
            if self.entry(parent,parts[-1],row['relative_path']) is not None:_refuse()
            return value.ctime_ns
