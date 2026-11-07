"""Hash outside database write transactions; pin and revalidate the exact upload."""
import hashlib
import os
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from fastapi import HTTPException


@contextmanager
def prepare_upload(row, *, allow_absent=False, deadline=None):
    """Never traverse symlinks, trust a client path, or remove changed/hardlinked bytes."""
    root = Path(os.environ.get('EDUCATION_UPLOAD_ROOT', '/data/education_uploads'))
    path = Path(row['stored_path'] or '')
    if not root.is_absolute() or not path.is_absolute():
        raise HTTPException(409, 'Upload storage ownership unconfirmed')
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise HTTPException(409, 'Upload is outside Education storage') from None
    if len(parts) != 2 or parts[0] != str(row['org_id']) or any(p in ('..', '.') for p in path.parts):
        raise HTTPException(409, 'Upload storage ownership unconfirmed')
    descriptors = []
    try:
        # Pin each parent from /, refusing symlinked roots and intermediate directories.
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(fd)
        for part in (*root.parts[1:], parts[0]):
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        try:
            payload = os.open(parts[1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        except FileNotFoundError:
            if allow_absent:
                yield None
                return
            raise HTTPException(409, 'Upload is missing before cleanup intent') from None
        descriptors.append(payload)
        before = os.fstat(payload)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise HTTPException(409, 'Upload is not an exclusively owned regular file')
        digest = hashlib.sha256()
        while chunk := os.read(payload, 1024 * 1024):
            if deadline is not None and time.monotonic() >= deadline:
                raise HTTPException(409, 'request_budget_exhausted')
            digest.update(chunk)
        after = os.fstat(payload)
        named = os.stat(parts[1], dir_fd=fd, follow_symlinks=False)
        if digest.hexdigest() != row['expected_hash'] or (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or (
            named.st_dev, named.st_ino, named.st_nlink
        ) != (before.st_dev, before.st_ino, 1):
            raise HTTPException(409, 'Upload bytes changed; cleanup refused')
        yield (fd, parts[1], before)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)



def unlink_upload(row, *, prepared):
    if prepared is None:
        return  # A durable pending intent proved ownership before the prior unlink.
    fd, name, before = prepared
    named = os.stat(name, dir_fd=fd, follow_symlinks=False)
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
    if any(getattr(before, key) != getattr(named, key) for key in fields):
        raise HTTPException(409, 'upload_changed')
    os.unlink(name, dir_fd=fd)
    os.fsync(fd)
