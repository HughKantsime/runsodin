"""Host-wide advisory flock for runner aggregates and manual VM prechecks."""
from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

# Deliberately outside the checkout and independent of HOME/run ID. Do not
# unlink it on exit: replacing the inode would allow two simultaneous locks.
LOCK_PATH = Path('/tmp/odin-trusted-validation.lock')
LOCK_FD_ENV = 'ODIN_VALIDATION_LOCK_FD'
LOCK_WAIT_ENV = 'ODIN_VALIDATION_LOCK_WAIT_SECONDS'
# A precheck runs one aggregate (~2 h); Trusted Validation's job timeout is 360 min.
DEFAULT_LOCK_WAIT_SECONDS = 3 * 3600
LOCK_POLL_SECONDS = 10


def runner_job_active() -> bool:
    """Match the runner executable, not a shell's arguments; fail closed."""
    try:
        listing = subprocess.check_output(['ps', '-eo', 'args='], text=True)
    except (OSError, subprocess.CalledProcessError):
        return True
    return any(re.match(r'^(?:\S*/)?Runner\.Worker(?:\s|$)', line.strip())
               for line in listing.splitlines())


def _wait_for_lock(fd: int) -> None:
    """Trusted Validation waits for a manual precheck instead of failing on it."""
    try:
        limit = int(os.environ.get(LOCK_WAIT_ENV, DEFAULT_LOCK_WAIT_SECONDS))
    except ValueError:
        limit = DEFAULT_LOCK_WAIT_SECONDS
    deadline = time.monotonic() + max(0, limit)
    announced = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise OSError('validation host lock wait timed out') from None
            if not announced:
                print('waiting for the validation host lock (a precheck is running)', flush=True)
                announced = True
            time.sleep(LOCK_POLL_SECONDS)


@contextmanager
def validation_lock(*, precheck: bool = False):
    inherited = os.environ.get(LOCK_FD_ENV)
    owned = inherited is None
    fd = None
    try:
        try:
            if owned:
                fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            else:
                fd = int(inherited)
            info = os.fstat(fd)
            path_info = LOCK_PATH.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                    or stat.S_ISLNK(path_info.st_mode)):
                raise ValueError('invalid validation lock')
            if precheck:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                _wait_for_lock(fd)
        except (OSError, ValueError):
            raise SystemExit('refused: validation host lock is busy or unavailable') from None
        # Check after acquiring the lock: two prechecks cannot race this check.
        # Runner aggregates take the same lock but naturally have a Worker alive.
        if precheck and runner_job_active():
            raise SystemExit('refused: a runner job is active')
        yield fd
    finally:
        if owned and fd is not None:
            # close releases our lock, except while inherited by a live child.
            os.close(fd)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != '--precheck':
        raise SystemExit('usage: host_lock --precheck command [args ...]')
    with validation_lock(precheck=True) as fd:
        environment = dict(os.environ, **{LOCK_FD_ENV: str(fd)})
        return subprocess.call(sys.argv[2:], env=environment, pass_fds=(fd,))


if __name__ == '__main__':
    raise SystemExit(main())
