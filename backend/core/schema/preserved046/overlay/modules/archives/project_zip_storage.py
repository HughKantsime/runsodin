"""Bounded rooted reads and request-owned FD accounting for project ZIPs.

No filesystem writes, journal permits, tenant authority or route activation.
All consumers must stage bytes privately until this helper returns successfully.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import resource
import stat
import time
from dataclasses import dataclass

IMPORT_FD_LIMIT = 200
EXPORT_FD_LIMIT = 167
MAX_ROOT_DEPTH = 64
MAX_SOURCE_DEPTH = 32
MAX_PATH_BYTES = 4096
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
READ_CHUNK = 1024 * 1024
MAX_INTEGER = 2**63 - 1
_RESERVED = {'.project-imports', '.operation-birth', '.root-generation'}


class StorageUnavailable(RuntimeError):
    """Sanitized refusal; unpublished bytes must be discarded."""


def _refuse():
    raise StorageUnavailable('Project ZIP storage unavailable') from None


@dataclass(eq=False)
class _Descriptor:
    fd: int | None
    placeholder: bool = False
    uncertain: bool = False


class FDLedger:
    """One shared request pool, with real capacity reservation before writes.

    It is resource ownership, never a filesystem or DB authority capability.
    Retire each descriptor before its one close attempt; an uncertain close
    poisons the pool and is never retried, even if that numeric FD is reused.
    """
    def __init__(self, limit, *, budget=None):
        if type(limit) is not int or limit not in (IMPORT_FD_LIMIT, EXPORT_FD_LIMIT):
            _refuse()
        self._budget = ReadBudget() if budget is None else budget
        if type(self._budget) is not ReadBudget:
            _refuse()
        self.limit = limit
        self.poisoned = False
        self.closed = False
        self.peak = 0
        self._slots = []
        self._anchor = None
        self._reserved = False

    @property
    def budget(self):
        return self._budget

    def check(self):
        if self.closed or self.poisoned:
            _refuse()
        self.budget.check()

    @property
    def count(self):
        return sum(s.fd is not None or s.uncertain for s in self._slots)

    @property
    def live_count(self):
        return sum(s.fd is not None and not s.placeholder for s in self._slots)

    def fileno(self, descriptor):
        self.check()
        if not any(s is descriptor for s in self._slots) or descriptor.fd is None or descriptor.placeholder:
            _refuse()
        return descriptor.fd

    def _track(self, slot, fd, placeholder=False):
        # Ownership is recorded before any syscall on the acquired FD can fail.
        if type(fd) is not int or fd < 0:
            _refuse()
        slot.fd = fd
        slot.placeholder = placeholder
        self.peak = max(self.peak, self.count)
        os.set_inheritable(fd, False)
        if os.get_inheritable(fd):
            _refuse()

    def _retire_close(self, slot):
        fd, slot.fd = slot.fd, None
        slot.placeholder = False
        if fd is None:
            return
        try:
            os.close(fd)
        except Exception:
            slot.uncertain = True
            self.poisoned = True
            _refuse()

    def acquire(self, factory):
        self.check()
        slot = next((s for s in self._slots if s.fd is not None and s.placeholder), None)
        if slot is not None:
            self._retire_close(slot)
        else:
            slot = next((s for s in self._slots if s.fd is None and not s.uncertain), None)
            if slot is None:
                if len(self._slots) >= self.limit:
                    _refuse()
                slot = _Descriptor(None)
                self._slots.append(slot)
        # A retired descriptor object is never revived for a later numeric FD.
        index = self._slots.index(slot)
        slot = _Descriptor(None)
        self._slots[index] = slot
        try:
            self._track(slot, factory())
            return slot
        except Exception:
            self.poisoned = True
            # If factory acquired an FD, _track retained it for final cleanup.
            _refuse()

    def reserve_capacity(self, anchor):
        self.check()
        if self._reserved:
            _refuse()
        anchor_fd = self.fileno(anchor)
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != resource.RLIM_INFINITY and soft < self.limit:
            _refuse()
        self._anchor = anchor
        try:
            for index, slot in enumerate(self._slots):
                self.check()
                if slot.fd is None:
                    slot = _Descriptor(None)
                    self._slots[index] = slot
                    self._track(slot, os.dup(anchor_fd), True)
            while len(self._slots) < self.limit:
                self.check()
                slot = _Descriptor(None)
                self._slots.append(slot)
                self._track(slot, os.dup(anchor_fd), True)
            self._reserved = True
        except Exception:
            self.poisoned = True
            self.close()
            _refuse()

    def release(self, descriptor):
        # Closing owned resources remains required after deadline expiry.
        if self.closed or self.poisoned:
            _refuse()
        if not any(s is descriptor for s in self._slots) or descriptor.fd is None or descriptor.placeholder:
            _refuse()
        if self._reserved and descriptor is self._anchor:
            _refuse()  # Capacity anchor remains owned until ledger shutdown.
        self._retire_close(descriptor)
        if self._reserved:
            # Keep capacity owned during all phases, not only at preflight.
            try:
                index = self._slots.index(descriptor)
                placeholder = _Descriptor(None)
                self._slots[index] = placeholder
                self._track(placeholder, os.dup(self.fileno(self._anchor)), True)
            except Exception:
                self.poisoned = True
                _refuse()

    def close(self):
        if self.closed:
            if self.poisoned:
                _refuse()
            return
        self.closed = True
        uncertain = self.poisoned
        for slot in reversed(self._slots):
            if slot.fd is not None:
                try:
                    self._retire_close(slot)
                except StorageUnavailable:
                    uncertain = True
        if uncertain:
            self.poisoned = True
            _refuse()

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *_):
        self.close()


class ReadBudget:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._last = self._now()
        self._deadline = self._last + 60
        self.total = 0

    def _now(self):
        value = self._clock()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            _refuse()
        return value

    def check(self):
        now = self._now()
        if now < self._last or now >= self._deadline:
            _refuse()
        self._last = now

    def charge(self, size):
        self.check()
        if type(size) is not int or size < 0 or self.total + size > MAX_TOTAL_BYTES:
            _refuse()
        self.total += size


@dataclass(frozen=True)
class FileFacts:
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    link_count: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @property
    def stable(self):
        return self.device, self.inode, self.uid, self.gid, self.mode


def _facts(value):
    values = (value.st_dev, value.st_ino, value.st_uid, value.st_gid, value.st_mode,
              value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if any(type(v) is not int or not 0 <= v <= MAX_INTEGER for v in values) or values[1] == 0 or values[5] == 0:
        _refuse()
    return FileFacts(*values)


def _parts(path, *, depth):
    if type(path) is not str or not path.startswith('/') or '\\' in path or '\0' in path:
        _refuse()
    try:
        if len(path.encode('utf-8')) > MAX_PATH_BYTES:
            _refuse()
        parts = tuple(path[1:].split('/'))
        if len(parts) > depth or any(p in ('', '.', '..') or len(p.encode('utf-8')) > 255 for p in parts):
            _refuse()
        return parts
    except UnicodeError:
        _refuse()


def _flags(directory=False):
    if not hasattr(os, 'O_NOFOLLOW') or not hasattr(os, 'O_DIRECTORY'):
        _refuse()
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0) | (os.O_DIRECTORY if directory else 0)


@dataclass(frozen=True)
class _Edge:
    parent: _Descriptor | None
    name: str
    descriptor: _Descriptor
    facts: FileFacts


class RootHandle:
    def __init__(self, ledger, path, edges):
        self.ledger = ledger
        self.path = path
        self.parts = _parts(path, depth=MAX_ROOT_DEPTH)
        self._edges = tuple(edges)
        self._closed = False

    @property
    def descriptor(self):
        return self._edges[-1].descriptor

    @property
    def facts(self):
        return self._edges[-1].facts

    def check(self):
        try:
            self.ledger.check()
            if self._closed:
                _refuse()
            for edge in self._edges:
                fd = self.ledger.fileno(edge.descriptor)
                parent = None if edge.parent is None else self.ledger.fileno(edge.parent)
                named = _facts(os.stat(edge.name, dir_fd=parent, follow_symlinks=False))
                current = _facts(os.fstat(fd))
                if not stat.S_ISDIR(current.mode) or named.stable != edge.facts.stable or current.stable != edge.facts.stable:
                    _refuse()
            root = _facts(os.fstat(self.ledger.fileno(self.descriptor)))
            if root.uid != os.geteuid() or root.mode & 0o022:
                _refuse()
        except Exception:
            _refuse()

    def close(self):
        if self._closed:
            return
        self._closed = True
        for edge in reversed(self._edges):
            self.ledger.release(edge.descriptor)


def open_storage_root(kind, ledger):
    config = {'print_files': ('PRINT_FILE_STORAGE_ROOT', '/data/print_files'),
              'education_export': ('EDUCATION_UPLOAD_ROOT', '/data/education_uploads')}
    ledger.check()
    if type(kind) is not str or kind not in config:
        _refuse()
    variable, default = config[kind]
    path = os.environ.get(variable, default)
    parts = _parts(path, depth=MAX_ROOT_DEPTH)
    edges = []
    try:
        for name in ('/', *parts):
            parent = edges[-1].descriptor if edges else None
            parent_fd = ledger.fileno(parent) if parent is not None else None
            before = _facts(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
            if not stat.S_ISDIR(before.mode):
                _refuse()
            descriptor = ledger.acquire(lambda: os.open(name, _flags(True), dir_fd=parent_fd))
            after = _facts(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
            opened = _facts(os.fstat(ledger.fileno(descriptor)))
            edges.append(_Edge(parent,name,descriptor,before))
            if before.stable != after.stable or before.stable != opened.stable:
                _refuse()
        result = RootHandle(ledger,path,edges)
        result.check()
        return result
    except Exception:
        # The ledger owns every successful acquisition, including failed fstat.
        _refuse()


def relative_source(roots, path):
    """Select exactly one configured root; no resolve or favorable fallback."""
    _parts(path, depth=MAX_ROOT_DEPTH + MAX_SOURCE_DEPTH)
    matches = []
    for root in roots:
        root.check()
        if path.startswith(root.path + '/'):
            relative = path[len(root.path):]
            parts = _parts(relative, depth=MAX_SOURCE_DEPTH)
            if _RESERVED.intersection(parts):
                _refuse()
            matches.append((root,parts))
    if len(matches) != 1:
        _refuse()
    return matches[0]


@dataclass(frozen=True)
class VerifiedRead:
    facts: FileFacts
    sha256: str
    byte_count: int


def read_regular_source(root, relative_parts, budget, consume, *, expected_size=None, expected_sha256=None):
    """Pinned incremental read into a private sink; caller discards on failure."""
    if budget is not root.ledger.budget:
        _refuse()
    if type(relative_parts) is not tuple or not relative_parts or any(type(p) is not str for p in relative_parts):
        _refuse()
    _parts(root.path + '/' + '/'.join(relative_parts), depth=MAX_ROOT_DEPTH + MAX_SOURCE_DEPTH)
    parts = _parts('/'+'/'.join(relative_parts), depth=MAX_SOURCE_DEPTH)
    if parts != relative_parts or _RESERVED.intersection(parts) or not callable(consume):
        _refuse()
    if expected_size is not None and (type(expected_size) is not int or not 0 <= expected_size <= MAX_FILE_BYTES):
        _refuse()
    if expected_sha256 is not None and (type(expected_sha256) is not str or not re.fullmatch('[0-9a-f]{64}',expected_sha256)):
        _refuse()
    ledger = root.ledger
    edges = []
    def check():
        budget.check()
        root.check()
        for edge in edges:
            parent = ledger.fileno(edge.parent)
            named = _facts(os.stat(edge.name,dir_fd=parent,follow_symlinks=False))
            current = _facts(os.fstat(ledger.fileno(edge.descriptor)))
            if stat.S_ISDIR(edge.facts.mode):
                same = named.stable == current.stable == edge.facts.stable
            else:
                same = named == current == edge.facts
            if not same or current.device != root.facts.device or current.uid != root.facts.uid or current.gid != root.facts.gid or current.mode & 0o022:
                _refuse()
    try:
        root.check()
        budget.check()
        parent = root.descriptor
        for index,name in enumerate(parts):
            check()
            parent_fd = ledger.fileno(parent)
            before = _facts(os.stat(name,dir_fd=parent_fd,follow_symlinks=False))
            directory = index < len(parts)-1
            if (directory and not stat.S_ISDIR(before.mode)) or (not directory and not stat.S_ISREG(before.mode)):
                _refuse()
            if before.device != root.facts.device or before.uid != root.facts.uid or before.gid != root.facts.gid or before.mode & 0o022:
                _refuse()
            if not directory and (before.link_count != 1 or before.size > MAX_FILE_BYTES):
                _refuse()
            descriptor = ledger.acquire(lambda: os.open(name,_flags(directory),dir_fd=parent_fd))
            edges.append(_Edge(parent,name,descriptor,before))
            check()
            parent = descriptor
        facts = edges[-1].facts
        if expected_size is not None and expected_size != facts.size:
            _refuse()
        total = 0
        digest = hashlib.sha256()
        while True:
            check()
            chunk = os.read(ledger.fileno(parent), min(READ_CHUNK,facts.size-total+1))
            check()
            if not chunk:
                break
            if total + len(chunk) > facts.size:
                _refuse()
            budget.charge(len(chunk))
            total += len(chunk)
            digest.update(chunk)
            consume(chunk)
            check()
        value = digest.hexdigest()
        if total != facts.size or (expected_sha256 is not None and expected_sha256 != value):
            _refuse()
        check()
        return VerifiedRead(facts,value,total)
    except Exception:
        _refuse()
    finally:
        for edge in reversed(edges):
            ledger.release(edge.descriptor)
