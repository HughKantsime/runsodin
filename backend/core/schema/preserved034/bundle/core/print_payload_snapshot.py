"""Shared low-level private payload snapshots; no tenant or dispatch authority."""
from contextlib import contextmanager
from dataclasses import dataclass,field
import hashlib
import hmac
import logging
import os
import re
import stat
import tempfile

MAX_PAYLOAD_BYTES=100*1024*1024
log=logging.getLogger('odin.dispatch')


class PayloadUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PayloadSnapshot:
    path: str=field(repr=False)
    sha256: str


def _signature(info):
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns


@contextmanager
def _regular_source(path):
    descriptor=None
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=MAX_PAYLOAD_BYTES:
            raise PayloadUnavailable('Print payload must be a nonempty regular file within the 100 MiB upload limit')
        with os.fdopen(descriptor,'rb') as source:
            descriptor=None
            yield source,info
    except PayloadUnavailable:
        raise
    except (OSError,TypeError,ValueError):
        raise PayloadUnavailable('Print payload could not be safely read; no command was sent') from None
    finally:
        if descriptor is not None:os.close(descriptor)


def _copy_hash(source,initial,destination=None):
    digest=hashlib.sha256();size=0
    while True:
        chunk=source.read(min(1024*1024,MAX_PAYLOAD_BYTES-size+1))
        if not chunk:break
        size+=len(chunk)
        if size>MAX_PAYLOAD_BYTES:
            raise PayloadUnavailable('Print payload exceeds the 100 MiB upload limit')
        digest.update(chunk)
        if destination is not None:destination.write(chunk)
    if size!=initial.st_size or _signature(os.fstat(source.fileno()))!=_signature(initial):
        raise PayloadUnavailable('Print payload changed while being read; no command was sent')
    return digest.hexdigest()


def hash_regular_payload(path):
    with _regular_source(path) as (source,info):return _copy_hash(source,info)


@contextmanager
def stage_payload(path,expected_sha256):
    if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256):
        raise PayloadUnavailable('Print payload hash is invalid; no command was sent')
    suffix=os.path.splitext(path)[1].lower()
    if suffix not in ('.3mf','.gcode','.bgcode'):
        raise PayloadUnavailable('Print payload has an unsupported file extension; no command was sent')
    temporary=tempfile.TemporaryDirectory(prefix='odin-dispatch-payload-')
    try:
        descriptor,staged=tempfile.mkstemp(prefix='payload-',suffix=suffix,dir=temporary.name)
        with os.fdopen(descriptor,'wb') as destination:
            with _regular_source(path) as (source,info):digest=_copy_hash(source,info,destination)
        if not hmac.compare_digest(digest,expected_sha256):
            raise PayloadUnavailable('Print payload changed before staging; no command was sent')
        os.chmod(staged,0o400)
        yield PayloadSnapshot(staged,digest)
    finally:
        try:temporary.cleanup()
        except OSError:log.warning('Owned dispatch payload cleanup could not be confirmed')
