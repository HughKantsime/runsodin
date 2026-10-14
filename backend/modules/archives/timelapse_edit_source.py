"""DB-closed opening of exact admitted current or durable journal MP4 facts."""
from contextlib import contextmanager,ExitStack
import os,time
from core.timelapse_manifest import _parts,_parent
from core.timelapse_publication_validation import STABLE,FILE
from core.timelapse_media_worker import file_facts
from modules.archives.timelapse_frame_publication import _directory,_file,_same

@contextmanager
def open_proven_media(*,root_fd,root_identity,guard,lifetime,path,expected,parent_facts,ctime,deadline):
    """No admission of raw paths: caller must hold current DB/journal authority.

    Current media: expected is prior complete operation output, ctime is its
    published receipt. Resuming private output: expected is exact output_ready
    output plus matching private_creation inode/private_root parent receipts;
    ctime is unchanged saved output ctime. Published resume uses journal's
    published ctime. A private file is never treated as a ready live version.
    """
    if time.monotonic()>=deadline:raise ValueError('Edit verification deadline')
    guard.assert_root_identity(root_identity)
    root={key:lifetime['root_'+key] for key in STABLE}
    _same(_directory(os.fstat(root_fd)),root,STABLE)
    parts=_parts(path)
    known={row['relative_path']:row for row in parent_facts}
    required={'/'.join(parts[:i]) for i in range(1,len(parts))}
    if set(known)!=required or len(known)!=len(parent_facts):raise ValueError('Edit parent receipt differs')
    descriptor=None
    try:
        with ExitStack() as stack:
            observed={};parent,name=stack.enter_context(_parent(root_fd,parts,root_identity,observed))
            for relative,value in observed.items():
                _same({key:getattr(value,key) for key in STABLE},known[relative],STABLE)
            descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
            os.set_inheritable(descriptor,False)
            def verify():
                guard.assert_root_identity(root_identity)
                actual=file_facts(descriptor,deadline)
                _same(actual,expected,STABLE+('link_count','byte_count','mtime_ns','digest_sha256'))
                if actual['ctime_ns']!=ctime:raise ValueError('Edit source ctime differs')
                _same(_file(os.stat(name,dir_fd=parent,follow_symlinks=False),actual['digest_sha256']),actual,FILE)
                with _parent(root_fd,parts,root_identity,dict(observed)) as (again,leaf):
                    _same(_file(os.stat(leaf,dir_fd=again,follow_symlinks=False),actual['digest_sha256']),actual,FILE)
                if time.monotonic()>=deadline:raise ValueError('Edit verification deadline')
                return actual
            actual=verify()
            yield descriptor,actual
            verify()
    finally:
        if descriptor is not None:
            closing,descriptor=descriptor,None
            # Unknown close cannot be retried or reported as successful cleanup.
            os.close(closing)
