#!/bin/bash
# Run on the Docker host with a local python:3.11-slim image and a shared checkout.
set -eu -o pipefail
repo=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
bind=$(mktemp -d "$HOME/.odin-lock-check.XXXXXX")
name="odin-lock-check-${bind##*.}"
volume=
cleanup() {
    status=$?
    trap - EXIT
    docker rm -f "$name-holder" "$name-contender" >/dev/null 2>&1 || true
    if [[ -n "$volume" ]]; then docker volume rm "$volume" >/dev/null || status=1; fi
    # An interrupted holder can leave private files owned by the container UID.
    if ! rm -rf -- "$bind" 2>/dev/null; then
        docker run --rm --pull=never --network none --mount "type=bind,src=$bind,dst=/data" \
            python:3.11-slim chown -R "$(id -u):$(id -g)" /data || status=1
        rm -rf -- "$bind" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
volume=$(docker volume create)

probe() {
    docker run --rm -i --pull=never --network none --name "$name-$3" \
        --mount "type=$1,src=$2,dst=/data" \
        --mount "type=bind,src=$repo/backend/core/dispatch_target_lock.py,dst=/probe/dispatch_target_lock.py,readonly" \
        -w /probe -e PYTHONDONTWRITEBYTECODE=1 python:3.11-slim python - "$1" "$3" <<'PY'
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from dispatch_target_lock import acquire_target_lock, _mountinfo_type

mount, role = sys.argv[1:]
data = Path('/data')
db = data / 'probe.db'
done = data / '.contender-finished'
kind = _mountinfo_type(data, data.stat().st_dev) or 'unrecognized'
print(f'{mount} {role}: /data mountinfo type {kind}', file=sys.stderr, flush=True)
if role == 'holder':
    os.chown(data, 10001, 10001)
    data.chmod(0o755)
os.setgroups([])
os.setgid(10001)
os.setuid(10001)
if role == 'holder':
    db.touch(mode=0o600)
engine = SimpleNamespace(dialect=SimpleNamespace(name='sqlite'),
                         url=SimpleNamespace(database=str(db), query={}))
lock = acquire_target_lock(engine, 'a' * 64)
result = f'{mount} {role}: {lock.status}'
print(f'{result} {lock.message}' if lock.message else result, flush=True)
if role == 'contender':
    if lock.handle:
        lock.handle.close()
    done.touch()
    sys.exit(0 if lock.status == 'conflict' else 1)
if lock.status != 'acquired':
    sys.exit(1)
try:
    deadline = time.monotonic() + 30
    while not done.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    ok = done.exists() and lock.handle.held()
finally:
    released = lock.handle.close().released
sys.exit(0 if ok and released else 1)
PY
}

check_mount() {
    probe "$1" "$2" holder | {
        IFS= read -r result || return 1
        echo "$result"
        [[ "$result" == *" holder: acquired" ]] || return 1
        probe "$1" "$2" contender
    }
}

status=0
check_mount bind "$bind" || status=1
check_mount volume "$volume" || status=1
exit "$status"
