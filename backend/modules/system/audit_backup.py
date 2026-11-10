"""Private audit-key companions for ODIN database backups and bundled downloads.

The database remains a native .db/.dump for existing restore tools. Its companion
is copied with the backup, never imported automatically over a live installation.
"""
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import zipfile
from core.audit_keys import directory, file_lock

class AuditCompanionError(RuntimeError):
    """The native database backup succeeded but its audit companion did not."""


NAMES = ('.audit-hmac-keys.json', '.audit-signed-head.json')


def companion(backup):
    return Path(str(backup)+'.audit.zip')


def capture_material(backup, metadata):
    target = companion(backup)
    if not any((directory()/name).is_file() for name in NAMES):
        return
    # Daily checks and rotation use these locks in this order too. This copies
    # the complete history and keyring atomically, without logging either.
    with file_lock('.audit-daily.lock'), file_lock('.audit-key.lock'):
        files = [directory()/name for name in NAMES if (directory()/name).is_file()]
        if not files:
            return
        descriptor, temporary = tempfile.mkstemp(dir=backup.parent, prefix='.audit-backup-')
        os.close(descriptor)
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                for path in files:
                    archive.write(path, path.name)
                archive.writestr('manifest.json', json.dumps({
                    'database_sha256': metadata['sha256'],
                    'files': [path.name for path in files],
                    'missing': [name for name in NAMES if not (directory()/name).is_file()],
                }, sort_keys=True))
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def complete_backup(result):
    """Shared successful-backup hook: SQLite and PostgreSQL native archives."""
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], Path):
        backup, metadata = result
        try:
            capture_material(backup, metadata)
        except Exception:
            # A recoverable database is still valuable without its companion.
            # Never let capture or notification failure remove that snapshot.
            metadata['audit_companion'] = 'failed'
            try:
                companion(backup).unlink(missing_ok=True)
            except OSError:
                logging.getLogger(__name__).warning('Failed audit companion could not be removed')
            try:
                from modules.system.backup_failure_alerts import publish_backup_failure
                publish_backup_failure(AuditCompanionError())
            except Exception:
                logging.getLogger(__name__).warning('Audit companion failure alert could not be persisted')
    return result


def move_material(source, target):
    if companion(source).exists():
        os.replace(companion(source), companion(target))


def download_bundle(backup, *, database_export=None):
    from fastapi import HTTPException
    from modules.system.backup_api_export import ExportResponse
    sidecar = companion(backup)
    if not sidecar.exists():
        raise HTTPException(409, 'This backup has no audit-key companion; preserve the installation key directory separately')
    digest = hashlib.sha256()
    with backup.open('rb') as source:
        while chunk := source.read(1024*1024):
            digest.update(chunk)
    descriptor, temporary = tempfile.mkstemp(prefix='odin-backup-', suffix='.zip')
    os.close(descriptor)
    try:
        with zipfile.ZipFile(sidecar) as keys, zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as output:
            manifest = json.loads(keys.read('manifest.json'))
            if manifest['database_sha256'] != digest.hexdigest():
                raise HTTPException(409, 'Audit-key companion does not match this database backup')
            exported = database_export or backup
            if exported != backup:
                with exported.open('rb') as stream:
                    manifest['database_sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            output.write(exported, backup.name)
            for name in (*NAMES, 'manifest.json'):
                if name in keys.namelist():
                    output.writestr('audit-keys/'+name, json.dumps(manifest) if name == 'manifest.json' else keys.read(name))
        return ExportResponse(temporary, filename=backup.name+'.zip', media_type='application/zip',
                              temporary=Path(temporary))
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
