"""Keep private license authority out of database artifacts sent over HTTP.

On-host recovery backups retain the authority. SQLite exports are sanitized
copies. Native PostgreSQL archives with private policy require on-host recovery;
we never rewrite an opaque pg_dump archive or serve it without inspecting it.
"""
from pathlib import Path
import os
import shutil
import sqlite3
import subprocess
import tempfile

from fastapi import HTTPException


def private_config_key(key):
    return key.startswith('license_lock_') or key == 'license_active_selection'


def prepare_export(source):
    source = Path(source)
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
        raise HTTPException(503, 'Backup privacy inspection unavailable for this format')
    # Always rebuild the exported copy: even a database with no current private
    # rows may retain deleted envelopes in free pages.
    descriptor, name = tempfile.mkstemp(prefix='odin-backup-export-', suffix='.db')
    os.close(descriptor)
    target = Path(name)
    try:
        shutil.copyfile(source, target)
        with sqlite3.connect(target) as db:
            db.execute('PRAGMA secure_delete=ON')
            db.execute("DELETE FROM system_config WHERE substr(key,1,13)='license_lock_' OR key='license_active_selection'")
            db.commit()
            db.execute('VACUUM')
        return target, target
    except BaseException:
        target.unlink(missing_ok=True)
        raise
