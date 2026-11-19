#!/bin/sh
# Run on the Docker host with a local python:3.11-slim image and a shared checkout.
set -eu
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
bind=$(mktemp -d "$repo/.odin-lock-check.XXXXXX")
trap 'rmdir "$bind"' EXIT
volume=$(docker volume create)
trap 'docker volume rm "$volume" >/dev/null; rmdir "$bind"' EXIT

probe() {
    docker run --rm -i --pull=never --network none \
        --mount "type=$1,src=$2,dst=/data" \
        --mount "type=bind,src=$repo/backend/core/dispatch_target_lock.py,dst=/probe/dispatch_target_lock.py,readonly" \
        -w /probe -e PYTHONDONTWRITEBYTECODE=1 python:3.11-slim python - "$1" <<'PY'
import os
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
from dispatch_target_lock import acquire_target_lock

data = Path('/data')
original = data.stat()
parent = os.getpid()
db = data / 'probe.db'
locks = data / 'probe.db.odin-sender-locks'
try:
    os.chown(data, 10001, 10001)
    data.chmod(0o755)
    pid = os.fork()
    if pid == 0:
        os.setgroups([])
        os.setgid(10001)
        os.setuid(10001)
        db.touch(mode=0o600)
        engine = SimpleNamespace(dialect=SimpleNamespace(name='sqlite'),
                                 url=SimpleNamespace(database=str(db), query={}))
        lock = acquire_target_lock(engine, 'a' * 64)
        print(f'{sys.argv[1]}: {lock.status}', lock.message or '', flush=True)
        ok = lock.handle is not None and lock.handle.close().released
        os._exit(0 if ok else 1)
    _, status = os.waitpid(pid, 0)
finally:
    if os.getpid() == parent:
        if locks.exists():
            shutil.rmtree(locks)
        db.unlink(missing_ok=True)
        os.chown(data, original.st_uid, original.st_gid)
        data.chmod(original.st_mode & 0o7777)
sys.exit(os.waitstatus_to_exitcode(status))
PY
}

status=0
probe bind "$bind" || status=1
probe volume "$volume" || status=1
exit "$status"
