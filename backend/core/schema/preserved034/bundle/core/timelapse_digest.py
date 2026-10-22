"""Bounded read-only content evidence. Caller-held media guard is mandatory.

This is neither retirement authority nor a deadline for blocking filesystem
syscalls. Digests prove content equality, not universal inode generations.
"""
import hashlib
import math
import os
import time
from core.timelapse_manifest import ObjectIdentity,_root,_parent,_parts,_facts,_stable,_owned,MAX_DEPTH,MAX_OBJECTS

MAX_BYTES=32*1024**3
MAX_SECONDS=60
CHUNK_BYTES=1024**2


class DigestUnavailable(RuntimeError):
    pass


def _refuse():
    raise DigestUnavailable('Media digest could not be confirmed') from None


def capture_file_digest(root,expected_root,expected_file,expected_parents,*,max_bytes=MAX_BYTES,timeout_seconds=30):
    """Hash only the exact captured file through verified directory descriptors."""
    try:
        if type(max_bytes) is not int or not 0<=max_bytes<=MAX_BYTES:_refuse()
        if type(timeout_seconds) not in (int,float) or not math.isfinite(timeout_seconds) or not 0<timeout_seconds<=MAX_SECONDS:_refuse()
        deadline=time.monotonic()+timeout_seconds
        def timely():
            if time.monotonic()>=deadline:_refuse()
        if not isinstance(expected_root,ObjectIdentity) or expected_root.kind!='directory' or expected_root.relative_path!='':_refuse()
        if not isinstance(expected_file,ObjectIdentity) or expected_file.kind!='file':_refuse()
        if type(expected_file.byte_count) is not int or not 0<=expected_file.byte_count<=max_bytes:_refuse()
        parts=_parts(expected_file.relative_path)
        if len(parts)>MAX_DEPTH or type(expected_parents) is not tuple or len(expected_parents)>MAX_OBJECTS:_refuse()
        saved={}
        for value in expected_parents:
            if not isinstance(value,ObjectIdentity) or value.kind!='directory' or value.relative_path in saved:_refuse()
            if len(_parts(value.relative_path))>MAX_DEPTH:_refuse()
            saved[value.relative_path]=value
        required={'/'.join(parts[:index]) for index in range(1,len(parts))}
        if not required.issubset(saved):_refuse()
        timely()
        with _root(root) as (root_fd,root_identity):
            if _stable(root_identity)!=_stable(expected_root):_refuse()
            _owned(expected_file,root_identity)
            for path in required:_owned(saved[path],root_identity)
            parents={path:saved[path] for path in required}
            with _parent(root_fd,parts,root_identity,parents) as (parent,name):
                timely()
                if _facts(expected_file.relative_path,os.stat(name,dir_fd=parent,follow_symlinks=False))!=expected_file:_refuse()
                descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                try:
                    if _facts(expected_file.relative_path,os.fstat(descriptor))!=expected_file:_refuse()
                    digest=hashlib.sha256();remaining=expected_file.byte_count
                    while remaining:
                        timely()
                        block=os.read(descriptor,min(CHUNK_BYTES,remaining))
                        if not block:_refuse()
                        digest.update(block);remaining-=len(block)
                    timely()
                    if os.read(descriptor,1):_refuse()
                    timely()
                    if _facts(expected_file.relative_path,os.fstat(descriptor))!=expected_file:_refuse()
                    if _facts(expected_file.relative_path,os.stat(name,dir_fd=parent,follow_symlinks=False))!=expected_file:_refuse()
                    result=digest.hexdigest()
                finally:os.close(descriptor)
        timely()
        return result
    except DigestUnavailable:raise
    except Exception:_refuse()
