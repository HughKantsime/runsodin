"""Read-only, bounded media identity capture. This is not deletion authority.

Callers must hold the shared media-operation guard through capture and durable
intent creation. Checks detect observed change, not an atomic snapshot.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat

MAX_OBJECTS = 16384
MAX_DEPTH = 32
MAX_SIGNED = (1 << 63) - 1

class ManifestUnavailable(RuntimeError):
    """Fixed refusal without leaking file names or filesystem details."""

@dataclass(frozen=True)
class ObjectIdentity:
    relative_path: str
    kind: str
    device: int | None = None
    inode: int | None = None
    uid: int | None = None
    gid: int | None = None
    mode: int | None = None
    link_count: int | None = None
    byte_count: int | None = None
    mtime_ns: int | None = None
    ctime_ns: int | None = None

@dataclass(frozen=True)
class MediaManifest:
    root: ObjectIdentity
    parents: tuple[ObjectIdentity, ...]
    objects: tuple[ObjectIdentity, ...]


def _refuse():
    raise ManifestUnavailable('Media identity could not be confirmed')


def _facts(path, value):
    kind = 'file' if stat.S_ISREG(value.st_mode) else 'directory' if stat.S_ISDIR(value.st_mode) else None
    if kind is None:
        _refuse()
    fields = (value.st_dev,value.st_ino,value.st_uid,value.st_gid,value.st_mode,
              value.st_nlink,value.st_size,value.st_mtime_ns,value.st_ctime_ns)
    if any(type(number) is not int or not 0 <= number <= MAX_SIGNED for number in fields) or value.st_ino == 0 or value.st_nlink == 0:
        _refuse()
    return ObjectIdentity(path,kind,*fields)


def _stable(value):
    return (value.device,value.inode,value.uid,value.gid,value.mode)


def _parts(value):
    if type(value) is not str or not value or len(value) > 4096 or '\x00' in value or '\\' in value:
        _refuse()
    parts = value.split('/')
    if any(part in ('','.','..') for part in parts) or any(part in ('.retirement','.locks') for part in parts):
        _refuse()
    return parts


def _owned(value, root):
    if value.device != root.device or (value.uid,value.gid) != (root.uid,root.gid) or value.mode & (stat.S_IWGRP | stat.S_IWOTH) or (value.kind == 'file' and value.link_count != 1):
        _refuse()


@contextmanager
def _root(path):
    # Walk every ancestor without following symlinks. Do not canonicalize an
    # untrusted path with resolve(), which would silently adopt its target.
    absolute = Path(path).absolute()
    descriptors = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open('/', flags)
        descriptors.append(descriptor)
        ancestors = []
        for part in absolute.parts[1:]:
            if part in ('.','..'):
                _refuse()
            parent = descriptor
            before = _facts('',os.stat(part,dir_fd=parent,follow_symlinks=False))
            descriptor = os.open(part, flags, dir_fd=parent)
            descriptors.append(descriptor)
            identity = _stable(_facts('',os.fstat(descriptor)))
            if identity != _stable(before):_refuse()
            ancestors.append((parent,part,descriptor,identity))
        root = _facts('',os.fstat(descriptor))
        if root.uid != os.geteuid() or root.mode & (stat.S_IWGRP | stat.S_IWOTH):
            _refuse()
        yield descriptor,root
        if any(_stable(_facts('',os.fstat(fd))) != identity or _stable(_facts('',os.stat(name,dir_fd=parent,follow_symlinks=False))) != identity for parent,name,fd,identity in ancestors):
            _refuse()
    finally:
        failed = False
        for descriptor in reversed(descriptors):
            try:os.close(descriptor)
            except OSError:failed = True
        if failed:_refuse()


@contextmanager
def _parent(root_fd, parts, root, parents, max_depth=MAX_DEPTH):
    if type(max_depth) is not int or not 1 <= max_depth <= MAX_DEPTH or not 1 <= len(parts) <= max_depth or _parts('/'.join(parts)) != list(parts):
        _refuse()
    descriptor = root_fd
    descriptors = []
    snapshots = []
    try:
        for index,part in enumerate(parts[:-1]):
            relative = '/'.join(parts[:index+1])
            before = _facts(relative,os.stat(part,dir_fd=descriptor,follow_symlinks=False))
            _owned(before,root)
            if before.kind != 'directory':_refuse()
            child = os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=descriptor)
            descriptors.append(child)
            after = _facts(relative,os.fstat(child))
            if _stable(before) != _stable(after):_refuse()
            previous = parents.get(relative)
            if previous is not None and _stable(previous) != _stable(after):_refuse()
            parents[relative] = after
            snapshots.append((descriptor,part,child,_stable(after)))
            descriptor = child
        yield descriptor,parts[-1]
        for parent,name,child,identity in snapshots:
            if _stable(_facts('',os.fstat(child))) != identity or _stable(_facts('',os.stat(name,dir_fd=parent,follow_symlinks=False))) != identity:
                _refuse()
    finally:
        failed = False
        for descriptor in reversed(descriptors):
            try:os.close(descriptor)
            except OSError:failed = True
        if failed:_refuse()


def _names(descriptor, remaining):
    names = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if len(names) >= remaining:_refuse()
            names.append(entry.name)
    return sorted(names)


def capture_manifest(root, video_relative, frames_relative, *, max_objects=MAX_OBJECTS,max_depth=MAX_DEPTH):
    """Return immutable identities, or refuse without filesystem mutation."""
    try:
        if type(max_objects) is not int or not 1 <= max_objects <= MAX_OBJECTS or type(max_depth) is not int or not 1 <= max_depth <= MAX_DEPTH:
            _refuse()
        video,frames = _parts(video_relative),_parts(frames_relative)
        if len(video) > max_depth or len(frames) > max_depth:_refuse()
        if video == frames or video[:len(frames)] == frames or frames[:len(video)] == video:
            _refuse()
        results = []
        parents = {}
        def append(value):
            if len(results) >= max_objects:_refuse()
            results.append(value)
        with _root(root) as (root_fd,root_identity):
            def walk(parent,name,relative,depth,tree,expected_kind=None):
                _parts(relative)
                if depth > max_depth:_refuse()
                try:before = _facts(relative,os.stat(name,dir_fd=parent,follow_symlinks=False))
                except FileNotFoundError:
                    append(ObjectIdentity(relative,'absent'))
                    try:os.stat(name,dir_fd=parent,follow_symlinks=False)
                    except FileNotFoundError:return
                    _refuse()
                _owned(before,root_identity)
                if expected_kind is not None and before.kind != expected_kind:_refuse()
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if before.kind == 'directory':flags |= os.O_DIRECTORY
                descriptor = os.open(name,flags,dir_fd=parent)
                try:
                    opened = _facts(relative,os.fstat(descriptor))
                    if opened != before:_refuse()
                    append(opened)
                    if opened.kind == 'directory':
                        names = _names(descriptor,max_objects-len(results))
                        for child in names:walk(descriptor,child,relative+'/'+child,depth+1,True)
                        if names != _names(descriptor,max_objects):_refuse()
                    if _facts(relative,os.fstat(descriptor)) != opened or _facts(relative,os.stat(name,dir_fd=parent,follow_symlinks=False)) != opened:
                        _refuse()
                finally:os.close(descriptor)
            for parts,tree in [(video,False),(frames,True)]:
                with _parent(root_fd,parts,root_identity,parents,max_depth) as (parent,name):
                    walk(parent,name,'/'.join(parts),len(parts),tree,'directory' if tree else 'file')
            # Recheck the complete captured set after traversal. This detects
            # observed late changes; the caller guard is still mandatory.
            for value in results:
                with _parent(root_fd,_parts(value.relative_path),root_identity,parents,max_depth) as (parent,name):
                    try:current = _facts(value.relative_path,os.stat(name,dir_fd=parent,follow_symlinks=False))
                    except FileNotFoundError:
                        if value.kind != 'absent':_refuse()
                    else:
                        if value.kind == 'absent' or current != value:_refuse()
            result = MediaManifest(root_identity,tuple(parents[key] for key in sorted(parents)),tuple(sorted(results,key=lambda value:value.relative_path)))
        return result
    except ManifestUnavailable:
        raise
    except (OSError,ValueError,TypeError,AttributeError):
        raise ManifestUnavailable('Media identity could not be confirmed') from None
