"""Read the volume-backed deploy window with a bounded fail-closed lifetime."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Lock
import time

MESSAGE = 'ODIN is updating; try again in a few minutes'
_cache = None
_lock = Lock()


def _utc(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.utcoffset() != timedelta(0):
        raise ValueError('Deploy timestamps must be UTC')
    return stamp


def _read(path, mtime):
    until = datetime.fromtimestamp(mtime, timezone.utc) + timedelta(minutes=30)
    version = None
    try:
        with path.open() as source:
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError('Deploy window is too large')
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get('version'), str) or not data['version']:
            raise ValueError('Invalid deploy window')
        _utc(data['opened_at'])
        requested_until = _utc(data['until'])
        version = data['version']
        until = min(until, requested_until)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        pass
    return version, until


def active() -> dict | None:
    """Stat on every call; cache parsing for at most two seconds, never expiry."""
    global _cache
    path = Path(os.environ.get('ODIN_DEPLOY_FREEZE_PATH', '/data/deploy-freeze.json'))
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        # Unknown state must refuse dispatch, even when a parsed window is cached.
        return {'release': None, 'until': None}
    signature = (str(path), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    with _lock:
        now = time.monotonic()
        if _cache is None or _cache[0] != signature or now - _cache[1] >= 2:
            _cache = (signature, now, _read(path, stat.st_mtime))
        version, until = _cache[2]
    if datetime.now(timezone.utc) >= until:
        return None
    # 'release', not 'version': release tooling greps /health for "version":"<new>".
    return {'release': version, 'until': until.isoformat()}
