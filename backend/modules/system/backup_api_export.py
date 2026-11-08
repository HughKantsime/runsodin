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

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='backup-export')
_jobs = {}
_jobs_lock = threading.Lock()


def _response(source, bundle):
    from starlette.responses import FileResponse
    from starlette.background import BackgroundTask
    exported, temporary = prepare_export(source)
    try:
        if bundle:
            from modules.system.audit_backup import download_bundle
            return download_bundle(source, database_export=exported)
        response = FileResponse(exported, filename=source.name, media_type='application/octet-stream',
                                background=BackgroundTask(temporary.unlink, missing_ok=True) if temporary else None)
        temporary = None  # response owns cleanup
        return response
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _discard(future):
    if not future.cancelled() and future.exception() is None:
        response = future.result()
        if response.background:
            Path(response.path).unlink(missing_ok=True)


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
            if len(_jobs) >= 8:
                raise HTTPException(503, 'Backup export queue busy; retry later', headers={'Retry-After': '2'})
            job = _executor.submit(_response, source, bundle)
            # Remove abandoned artifacts even if the client never polls again.
            def expire():
                with _jobs_lock:
                    if _jobs.get(key, (None,))[0] is not job:
                        return
                    del _jobs[key]
                if not job.cancel():
                    job.add_done_callback(_discard)
            timer = threading.Timer(900, expire)
            timer.daemon = True
            _jobs[key] = (job, timer)
            timer.start()
    return JSONResponse({'status': 'preparing'}, status_code=202, headers={'Retry-After': '2'})
