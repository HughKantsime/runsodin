"""DB-closed verification with retained descriptors for fenced media syscalls."""
from contextlib import ExitStack
from dataclasses import dataclass,field
import hashlib,os,copy
from core.timelapse_manifest import _parts,_facts,_stable,_names,MAX_OBJECTS
from core.timelapse_digest import CHUNK_BYTES
from core.timelapse_rename import rename_noreplace
from core.timelapse_staging_fs import _refuse

_PROOF=object()

@dataclass(repr=False)
class VerifiedAction:
    fs:object
    row:dict
    snapshot:object
    stack:ExitStack=field(repr=False)
    parent:int=field(repr=False)
    name:str
    descriptor:int|None=field(repr=False)
    value:object
    source:bool
    action:str
    proof:object=field(repr=False)
    closed:bool=False
    def close(self):
        if not self.closed:
            self.closed=True
            self.stack.close()
    def __enter__(self):return self
    def __exit__(self,*exc):self.close()
    def __repr__(self):return '<verified media action>'


def _open_verified(fs,parent,name,row,stack,*,stage_source):
    value=fs.entry(parent,name,row['relative_path'])
    if value is None:return None,None
    if row['kind']=='file':
        fs.check_file(value,row,ignore_ctime=not stage_source,verified=None if stage_source else (fs.moves[row['object_index']]['verified_ctime_ns'] if fs.moves[row['object_index']]['phase']=='staged' else None))
    else:fs.stable_directory(value,fs.identity(row))
    fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|(os.O_DIRECTORY if row['kind']=='directory' else 0),dir_fd=parent)
    stack.callback(os.close,fd)
    if _facts(row['relative_path'],os.fstat(fd))!=value:_refuse()
    if row['kind']=='directory':
        if _names(fd,MAX_OBJECTS):_refuse()
        stamp=fs.moves[row['object_index']]['verified_ctime_ns']
        if not stage_source and stamp is not None and value.ctime_ns!=stamp:_refuse()
    else:
        if row['byte_count']>fs.remaining:_refuse('manifest_limit')
        digest=hashlib.sha256();left=row['byte_count']
        while left:
            fs.admit();block=os.read(fd,min(CHUNK_BYTES,left))
            if not block:_refuse()
            fs.remaining-=len(block);left-=len(block)
            if fs.remaining<0:_refuse('manifest_limit')
            digest.update(block)
        if os.read(fd,1) or digest.hexdigest()!=fs.moves[row['object_index']]['digest_sha256']:_refuse()
    fs.admit()
    if _facts(row['relative_path'],os.fstat(fd))!=value or fs.entry(parent,name,row['relative_path'])!=value:_refuse()
    return fd,value


def _pin_directory_chain(fs,path,stack,links,*,staging):
    """Retain every root-relative edge, including a parent's staged relocation."""
    if not path:return fs.root_fd
    parts=_parts(path);parent=_pin_directory_chain(fs,'/'.join(parts[:-1]),stack,links,staging=staging)
    expected=fs.parents.get(path)
    if expected is None:_refuse('phase_uncertain')
    row=fs.by_path.get(path) if staging else None
    selected_parent=parent;selected_name=parts[-1]
    if row is not None:
        source,destination=fs.locations(parent,parts[-1],row)
        if source is None:
            # Retain the original absent edge as well as the quarantine edge.
            copied=os.dup(parent);stack.callback(os.close,copied)
            links.append((copied,parts[-1],path,None,None))
            selected_parent=fs.quarantine_fd;selected_name=row['quarantine_name']
    value=fs.entry(selected_parent,selected_name,path)
    fs.stable_directory(value,expected)
    parent_copy=os.dup(selected_parent);stack.callback(os.close,parent_copy)
    descriptor=os.open(selected_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent_copy)
    stack.callback(os.close,descriptor)
    fs.stable_directory(_facts(path,os.fstat(descriptor)),expected)
    links.append((parent_copy,selected_name,path,value,descriptor))
    return descriptor


def _check_parent_links(token):
    """No hashes or delayed context-exit checks authorize a syscall."""
    fs=token.fs;fs.admit()
    for parent,name,path,expected,descriptor in token.parent_links:
        actual=fs.entry(parent,name,path)
        if expected is None:
            if actual is not None:_refuse('ownership_changed')
        elif actual is None or _stable(actual)!=_stable(expected) or _stable(_facts(path,os.fstat(descriptor)))!=_stable(expected):
            _refuse('ownership_changed')
    # The selected pinned chain must still reach the retained action-parent inode.
    if token.action=='rename' and _stable(_facts('',os.fstat(token.chain_parent)))!=_stable(_facts('',os.fstat(token.original_parent))):_refuse('ownership_changed')


def verify_action(fs,row,*,action,snapshot):
    """The caller must have closed all DB transactions before invoking this."""
    stack=ExitStack()
    try:
        fs.admit();source=False;links=[];chain_parent=None
        if action=='rename':
            if fs.moves[row['object_index']]['phase']!='rename_pending':_refuse('phase_uncertain')
            parts=_parts(row['relative_path'])
            chain_parent=_pin_directory_chain(fs,'/'.join(parts[:-1]),stack,links,staging=True)
            original_parent=stack.enter_context(fs.directory('/'.join(parts[:-1])))
            original_name=parts[-1]
            source_value,destination=fs.locations(original_parent,original_name,row)
            source=source_value is not None
            parent=original_parent if source else fs.quarantine_fd
            name=original_name if source else row['quarantine_name']
        elif action=='unlink':
            if fs.unlinks.get(row['object_index'],{}).get('phase')!='unlink_pending':_refuse('phase_uncertain')
            fs.originals_absent();original_parent=None;original_name=None
            for root in fs.original_roots:
                parts=_parts(root['relative_path'])
                pinned=_pin_directory_chain(fs,'/'.join(parts[:-1]),stack,links,staging=False)
                copied=os.dup(pinned);stack.callback(os.close,copied)
                if fs.entry(copied,parts[-1],root['relative_path']) is not None:_refuse()
                links.append((copied,parts[-1],root['relative_path'],None,None))
            parent=fs.quarantine_fd;name=row['quarantine_name']
        else:_refuse('phase_uncertain')
        parent_copy=os.dup(parent);stack.callback(os.close,parent_copy)
        fd,value=_open_verified(fs,parent_copy,name,row,stack,stage_source=source)
        if action=='rename' and value is None:_refuse('phase_uncertain')
        token=VerifiedAction(fs,row,copy.deepcopy(snapshot),stack,parent_copy,name,fd,value,source,action,_PROOF)
        token.parent_links=tuple(links);token.chain_parent=chain_parent
        token.parent_identity=_facts('',os.fstat(parent_copy))
        token.original_parent=original_parent;token.original_name=original_name
        token.root_copy=os.dup(fs.root_fd);stack.callback(os.close,token.root_copy)
        token.quarantine_copy=os.dup(fs.quarantine_fd);stack.callback(os.close,token.quarantine_copy)
        token.root_identity=_stable(_facts('',os.fstat(fs.root_fd)))
        token.quarantine_identity=_stable(_facts('.retirement',os.fstat(fs.quarantine_fd)))
        return token
    except BaseException:
        stack.close();raise


def apply_verified_action(token,*,snapshot):
    """No hashing. Caller holds exact current authority/inventory transaction."""
    if type(token) is not VerifiedAction or token.proof is not _PROOF or token.closed or token.snapshot!=snapshot:_refuse('phase_uncertain')
    fs=token.fs;row=token.row;fs.admit()
    if _stable(_facts('',os.fstat(token.root_copy)))!=token.root_identity or _stable(_facts('.retirement',os.fstat(token.quarantine_copy)))!=token.quarantine_identity or _stable(_facts('',os.fstat(fs.root_fd)))!=token.root_identity or _stable(_facts('.retirement',os.fstat(fs.quarantine_fd)))!=token.quarantine_identity or _stable(_facts('',os.fstat(token.parent)))!=_stable(token.parent_identity):_refuse('ownership_changed')
    if fs.entry(token.parent,token.name,row['relative_path'])!=token.value:_refuse()
    if token.descriptor is not None and _facts(row['relative_path'],os.fstat(token.descriptor))!=token.value:_refuse()
    if row['kind']=='directory' and token.descriptor is not None and _names(token.descriptor,MAX_OBJECTS):_refuse()
    if token.action=='rename':
        fs.locations(token.original_parent,token.original_name,row)
        _check_parent_links(token)
        if token.source:rename_noreplace(token.parent,token.name,fs.quarantine_fd,row['quarantine_name'])
        value=fs.entry(fs.quarantine_fd,row['quarantine_name'],row['relative_path'])
        opened=_facts(row['relative_path'],os.fstat(token.descriptor))
        if value!=opened or _stable(value)!=_stable(token.value):_refuse()
        if row['kind']=='file':fs.check_file(value,row,ignore_ctime=True)
        else:
            if _names(token.descriptor,MAX_OBJECTS):_refuse()
        if fs.entry(token.original_parent,token.original_name,row['relative_path']) is not None:_refuse()
        os.fsync(token.original_parent);os.fsync(fs.quarantine_fd)
        if fs.entry(fs.quarantine_fd,row['quarantine_name'],row['relative_path'])!=opened:_refuse()
        return opened.ctime_ns
    fs.originals_absent()
    _check_parent_links(token)
    if token.value is not None:
        if row['kind']=='file':os.unlink(token.name,dir_fd=token.parent)
        else:os.rmdir(token.name,dir_fd=token.parent)
    os.fsync(fs.quarantine_fd);fs.assert_absent(row)
    return 'absence_synced'
