"""Keep private license authority out of database artifacts sent over HTTP.

With fleet locking enabled, on-host recovery backups retain the authority and
SQLite exports are sanitized copies. Native PostgreSQL archives with private
policy require on-host recovery; other historical formats pass through.
"""
from pathlib import Path
import os
import shutil
from contextlib import closing
import sqlite3
import subprocess
import tempfile

from fastapi import HTTPException


def private_config_key(key):
    return key.startswith('license_lock_') or key == 'license_active_selection'


def prepare_export(source):
    source = Path(source)
    from core.license_lock import enabled
    if not enabled():
        return source, None
    with source.open('rb') as stream:
        magic = stream.read(16)
    if magic.startswith(b'PGDMP'):
        try:
            result = subprocess.run(['pg_restore', '--data-only', '--table=system_config', str(source)],
                                    capture_output=True, check=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            raise HTTPException(503, 'Backup privacy inspection temporarily unavailable') from None
        # Search the decoded table data, not compressed archive bytes. Fail
        # closed for native archives rather than breaking their restore format.
        if b'license_lock_' in result.stdout or b'license_active_selection' in result.stdout:
            raise HTTPException(409, 'This backup contains private license authority and requires on-host recovery')
        return source, None
    if magic != b'SQLite format 3\x00':
        return source, None  # opaque historical formats retain their existing behavior
    # Always rebuild the exported copy: even a database with no current private
    # rows may retain deleted envelopes in free pages.
    descriptor, name = tempfile.mkstemp(prefix='odin-backup-export-', suffix='.db')
    os.close(descriptor)
    target = Path(name)
    try:
        shutil.copyfile(source, target)
        with closing(sqlite3.connect(target)) as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute("DELETE FROM system_config WHERE substr(key,1,13)='license_lock_' OR key='license_active_selection'")
            db.commit()
            db.execute('VACUUM')
        return target, target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


# Bounded background preparation: HTTP requests only enqueue/poll. No database
# copy, VACUUM, pg_restore or ZIP build runs synchronously on the request path.
from concurrent.futures import ThreadPoolExecutor
import threading
from starlette.responses import FileResponse
from starlette.background import BackgroundTask

_MAX_LIVE_JOBS = 8
_MAX_READY_JOBS = 8
_JOB_TTL = 15 * 60
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='backup-export')
_jobs = {}
_live_jobs = set()
# A completed Future invokes add_done_callback immediately in the caller.
_jobs_lock = threading.RLock()


class ExportResponse(FileResponse):
    """Own temporary artifacts even if a download fails or is cancelled."""

    def __init__(self, path, *, temporary=None, **kwargs):
        self.temporary = temporary
        super().__init__(path, background=BackgroundTask(self.discard) if temporary else None, **kwargs)

    def discard(self):
        if self.temporary:
            self.temporary.unlink(missing_ok=True)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.discard()


def _response(source, bundle):
    exported, temporary = prepare_export(source)
    try:
        if bundle:
            from modules.system.audit_backup import download_bundle
            response = download_bundle(source, database_export=exported)
            return ExportResponse(response.path, temporary=Path(response.path),
                                  filename=source.name + '.zip', media_type='application/zip')
        response = ExportResponse(exported, filename=source.name, media_type='application/octet-stream',
                                  temporary=temporary)
        temporary = None  # response owns cleanup
        return response
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _discard(future):
    if not future.cancelled() and future.exception() is None:
        future.result().discard()


def _expire(key, job):
    with _jobs_lock:
        if _jobs.get(key, (None,))[0] is not job:
            return
        del _jobs[key]
        # Running jobs still occupy the live cap until their worker exits.
        # Their eventual artifacts must be removed even without another poll.
        if not job.cancel():
            job.add_done_callback(_discard)


def _completed(job):
    with _jobs_lock:
        _live_jobs.discard(job)
        # Completed artifacts never block admission. Bound retained files too:
        # evict the oldest ready entries instead of refusing a new export.
        ready = [key for key, (future, _) in _jobs.items() if future.done()]
        for key in ready[:-_MAX_READY_JOBS]:
            future, timer = _jobs.pop(key)
            timer.cancel()
            _discard(future)


def download_response(source, bundle=False):
    from core.license_lock import enabled
    from starlette.responses import JSONResponse
    source = Path(source)
    if not enabled():
        return _response(source, bundle)
    stamp = source.stat()
    key = (str(source), stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, bundle)
    with _jobs_lock:
        job, timer = _jobs.get(key, (None, None))
        if job is not None:
            if job.done():
                del _jobs[key]
                timer.cancel()
                return job.result()
        else:
            if sum(not future.done() for future in _live_jobs) >= _MAX_LIVE_JOBS:
                raise HTTPException(503, 'Backup export queue busy; retry later', headers={'Retry-After': '2'})
            job = _executor.submit(_response, source, bundle)
            _live_jobs.add(job)
            # Expiry runs independently of polling, including queued/running jobs.
            timer = threading.Timer(_JOB_TTL, _expire, args=(key, job))
            timer.daemon = True
            _jobs[key] = (job, timer)
            job.add_done_callback(_completed)
            timer.start()
    return JSONResponse({'status': 'preparing'}, status_code=202, headers={'Retry-After': '2'})
