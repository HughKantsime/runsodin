"""Stable root-wide media exclusion; retained FDs never cache verification."""
import fcntl
import os
import stat
from pathlib import Path
from core.timelapse_manifest import _root,_facts,_stable,ObjectIdentity

MAX_GUARD_COMPONENTS=64
MAX_GUARD_PATH=4096

class MediaGuardUnavailable(RuntimeError):pass

def _refuse():raise MediaGuardUnavailable('Media operation ownership could not be confirmed') from None

def _fingerprint(value):
    return (value.st_dev,value.st_ino,value.st_mode,value.st_uid,value.st_gid,value.st_nlink,value.st_size if stat.S_ISREG(value.st_mode) else None)

def _close_once(descriptors):
    """Caller has already retired this vector. No retries after uncertainty."""
    failed=False
    for descriptor in descriptors:
        try:os.close(descriptor)
        except OSError:failed=True
    if failed:_refuse()

class _RootChain:
    def __init__(self,descriptors,nodes,slash):
        self.descriptors=descriptors;self.nodes=tuple(nodes);self.slash=slash
        self.root_fd=descriptors[-1]
    def check(self):
        if not self.descriptors:_refuse()
        if _stable(_facts('',os.fstat(self.descriptors[0])))!=self.slash or _stable(_facts('',os.stat('/',follow_symlinks=False)))!=self.slash:_refuse()
        for parent,name,child,expected in self.nodes:
            if _stable(_facts('',os.fstat(child)))!=expected or _stable(_facts('',os.stat(name,dir_fd=parent,follow_symlinks=False)))!=expected:_refuse()
        root=_facts('',os.fstat(self.root_fd))
        if root.kind!='directory' or root.uid!=os.geteuid() or root.mode & (stat.S_IWGRP|stat.S_IWOTH):_refuse()
        return root
    def retire(self):
        descriptors,self.descriptors=self.descriptors,[];self.nodes=();self.root_fd=None
        return list(reversed(descriptors))

def _open_chain(root):
    parts=root.parts[1:]
    if len(str(root))>MAX_GUARD_PATH or len(parts)>MAX_GUARD_COMPONENTS or any(p in ('.','..') or '\x00' in p for p in parts):_refuse()
    descriptors=[];nodes=[]
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,'O_CLOEXEC',0)
    try:
        fd=os.open('/',flags);descriptors.append(fd);os.set_inheritable(fd,False)
        slash=_stable(_facts('',os.fstat(fd)))
        for name in parts:
            parent=fd;before=_facts('',os.stat(name,dir_fd=parent,follow_symlinks=False))
            fd=os.open(name,flags,dir_fd=parent);descriptors.append(fd);os.set_inheritable(fd,False)
            after=_facts('',os.fstat(fd))
            if after.kind!='directory' or _stable(after)!=_stable(before):_refuse()
            nodes.append((parent,name,fd,_stable(after)))
        chain=_RootChain(descriptors,nodes,slash);chain.check();descriptors=[]
        return chain
    finally:
        owned,descriptors=descriptors,[]
        _close_once(reversed(owned))

class MediaGuard:
    def __init__(self,descriptor,root,root_identity,folder_identity,file_identity,chain,folder_fd):
        self._descriptor=descriptor;self._root=root;self._root_identity=root_identity
        self._folder_identity=folder_identity;self._file_identity=file_identity
        self._chain=chain;self._folder_fd=folder_fd;self._uncertain=None;self._lost=False
    def _locks_match(self):
        if _fingerprint(os.fstat(self._folder_fd))!=self._folder_identity or _fingerprint(os.stat('.locks',dir_fd=self._chain.root_fd,follow_symlinks=False))!=self._folder_identity:_refuse()
        if _fingerprint(os.fstat(self._descriptor))!=self._file_identity or _fingerprint(os.stat('media.lock',dir_fd=self._folder_fd,follow_symlinks=False))!=self._file_identity:_refuse()
    def assert_owned(self):
        if self._descriptor is None or self._lost or self._uncertain is not None:_refuse()
        try:
            if _fingerprint(os.fstat(self._descriptor))!=self._file_identity:_refuse()
            before=self._chain.check()
            if (before.device,before.inode,before.mode,before.uid,before.gid)!=self._root_identity:_refuse()
            self._locks_match()
            after=self._chain.check()
            if (after.device,after.inode,after.mode,after.uid,after.gid)!=self._root_identity:_refuse()
            self._locks_match()
        except Exception:
            self._lost=True;_refuse()
    def assert_root_identity(self,identity):
        self.assert_owned()
        if not isinstance(identity,ObjectIdentity) or identity.kind!='directory' or identity.relative_path!='' or (identity.device,identity.inode,identity.mode,identity.uid,identity.gid)!=self._root_identity:
            self._lost=True;_refuse()
    def fileno(self):self.assert_owned();return self._descriptor
    def close(self):
        if self._uncertain is not None:_refuse()
        descriptor,self._descriptor=self._descriptor,None
        folder,self._folder_fd=self._folder_fd,None
        chain,self._chain=self._chain,None
        owned=([descriptor] if descriptor is not None else [])+([folder] if folder is not None else [])+(chain.retire() if chain is not None else [])
        try:_close_once(owned)
        except MediaGuardUnavailable:
            self._uncertain=True;self._lost=True;raise
    def __enter__(self):self.fileno();return self
    def __exit__(self,*args):self.close()

def acquire_media_guard(root,timelapse_id):
    """Private retained directories; only lock FD may be passed to a child."""
    if type(timelapse_id) is not int or not 1<=timelapse_id<=(1<<63)-1:_refuse()
    descriptor=None;directory=None;chain=None
    try:
        root=Path(root).absolute();chain=_open_chain(root);root_identity=chain.check();root_fd=chain.root_fd
        try:os.mkdir('.locks',0o700,dir_fd=root_fd)
        except FileExistsError:pass
        directory=os.open('.locks',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|getattr(os,'O_CLOEXEC',0),dir_fd=root_fd);os.set_inheritable(directory,False)
        folder=os.fstat(directory)
        if folder.st_uid!=root_identity.uid or folder.st_gid!=root_identity.gid or stat.S_IMODE(folder.st_mode)!=0o700 or folder.st_dev!=root_identity.device:_refuse()
        descriptor=os.open('media.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|getattr(os,'O_CLOEXEC',0),0o600,dir_fd=directory);os.set_inheritable(descriptor,False)
        identity=os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_nlink!=1 or identity.st_size!=0 or identity.st_uid!=root_identity.uid or identity.st_gid!=root_identity.gid or stat.S_IMODE(identity.st_mode)!=0o600 or identity.st_dev!=root_identity.device:_refuse()
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        current=os.stat('media.lock',dir_fd=directory,follow_symlinks=False);current_folder=os.stat('.locks',dir_fd=root_fd,follow_symlinks=False)
        after_folder=os.fstat(directory)
        if _fingerprint(current)!=_fingerprint(identity) or _fingerprint(current_folder)!=_fingerprint(after_folder) or _fingerprint(after_folder)[:5]!=_fingerprint(folder)[:5]:_refuse()
        guard=MediaGuard(descriptor,root,(root_identity.device,root_identity.inode,root_identity.mode,root_identity.uid,root_identity.gid),_fingerprint(current_folder),_fingerprint(identity),chain,directory)
        guard.assert_owned();descriptor=None;directory=None;chain=None
        return guard
    except Exception:_refuse()
    finally:
        owned=([descriptor] if descriptor is not None else [])+([directory] if directory is not None else [])+(chain.retire() if chain is not None else [])
        descriptor=None;directory=None;chain=None
        _close_once(owned)
