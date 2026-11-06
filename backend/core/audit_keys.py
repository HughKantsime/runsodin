"""Installation audit HMAC keyring and last signed head, outside the database.

Retired keys remain available for old receipts. JWT rotation has no effect.
Atomic replacement and a file lock serialize initial creation across workers.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile


def directory():
    from license_manager import LICENSE_DIR
    return Path(os.environ.get('ODIN_AUDIT_KEY_DIR', LICENSE_DIR))


@contextmanager
def file_lock(name, *, blocking=True):
    root = directory()
    root.mkdir(parents=True, exist_ok=True)
    with open(root / name, 'a+b') as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.audit-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class AuditKeyMissing(ValueError):
    """Recover the original keyring; never misclassify missing secrets as tampering."""


def sealed_evidence(connection):
    from sqlalchemy import inspect, text
    tables = set(inspect(connection).get_table_names())
    def signed(value):
        if not isinstance(value, dict):
            return False
        receipts = value.get('receipts')
        return bool(value.get('key_id') and value.get('signature')) or (
            isinstance(receipts, list) and any(signed(receipt) for receipt in receipts))

    # Keys may be missing, so authentication cannot be checked here. Require a
    # signed envelope, never infer one merely from an action or table name.
    for table, column, predicate in (
        ('audit_redactions', 'evidence', ''),
        ('audit_retention_checkpoints', 'evidence', ''),
        ('audit_integrity_status', 'result', ''),
        ('audit_logs', 'details', "action IN ('audit.erasure','audit.retention',"
         "'audit.restore_baseline_accepted','restore_completed') AND "),
    ):
        if table not in tables:
            continue
        rows = connection.execute(text(
            'SELECT '+column+' FROM '+table+' WHERE '+predicate+
            "CAST("+column+" AS TEXT) LIKE '%\"key_id\"%' AND "
            "CAST("+column+" AS TEXT) LIKE '%\"signature\"%'")
            .execution_options(stream_results=True, max_row_buffer=256))
        for raw, in rows:
            try:
                if signed(json.loads(raw) if isinstance(raw, str) else raw):
                    return True
            except (ValueError, TypeError):
                continue
    return False


def missing_key(connection):
    return keyring() is None and (sealed_evidence(connection) or (directory() / '.audit-signed-head.json').exists())


def keyring(*, create=False, connection=None):
    path = directory() / '.audit-hmac-keys.json'
    if path.exists():
        return json.loads(path.read_text())
    if not create:
        return None
    # A caller must prove this database has no sealed evidence before first use.
    # A wholly lost directory cannot provide that proof on its own.
    if connection is None or sealed_evidence(connection) or (directory() / '.audit-signed-head.json').exists():
        raise AuditKeyMissing('audit_key_missing')
    with file_lock('.audit-key.lock'):
        if path.exists():
            return json.loads(path.read_text())
        value = new_keyring()
        atomic_json(path, value)
        return value


def new_keyring():
    key = secrets.token_hex(32)
    key_id = hashlib.sha256(bytes.fromhex(key)).hexdigest()[:16]
    return {'active': key_id, 'keys': {key_id: key}}


def rotate():
    if directory().stat().st_uid != os.geteuid():
        raise PermissionError('Audit key rotation requires the key directory owner')
    if keyring() is None:
        raise AuditKeyMissing('audit_key_missing')
    with file_lock('.audit-key.lock'):
        value = keyring()
        key = secrets.token_hex(32)
        key_id = hashlib.sha256(bytes.fromhex(key)).hexdigest()[:16]
        value['keys'][key_id] = key
        value['active'] = key_id
        atomic_json(directory() / '.audit-hmac-keys.json', value)
        return key_id


def saved_head():
    path = directory() / '.audit-signed-head.json'
    return json.loads(path.read_text()) if path.exists() else None


def save_head(value):
    from core.audit_head_history import persist
    persist(value)
