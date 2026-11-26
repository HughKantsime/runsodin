"""Recovery code operations. The caller owns the user lock and transaction."""
import base64
import hmac
import os
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text

from core.db_compat import as_utc_datetime

ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
MFA_ENCRYPTION_UNAVAILABLE = "Encryption is not configured on this server. MFA setup requires ENCRYPTION_KEY to be set."


def recovery_key():
    """Derive a recovery-only MAC key from the installation's Fernet key."""
    try:
        master = base64.urlsafe_b64decode(os.environ.get('ENCRYPTION_KEY', ''))
        if len(master) != 32:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=503, detail=MFA_ENCRYPTION_UNAVAILABLE) from None
    return hmac.digest(master, b'ODIN/mfa-recovery-codes/v1', 'sha256')


def _hash(key, salt, code):
    return hmac.digest(key, bytes.fromhex(salt) + code.encode('ascii'), 'sha256').hex()


def delete_codes(db, user_id):
    db.execute(text('DELETE FROM mfa_recovery_codes WHERE user_id=:uid'), {'uid': user_id})


def issue_codes(db, user_id, *, key=None):
    if key is None:
        key = recovery_key()
    codes = []
    rows = []
    created = datetime.now(timezone.utc)
    while len(codes) < 10:
        code = ''.join(secrets.choice(ALPHABET) for _ in range(10))
        formatted = code[:5] + '-' + code[5:]
        if formatted in codes:
            continue
        salt = secrets.token_hex(16)
        rows.append({'uid': user_id, 'salt': salt, 'digest': _hash(key, salt, code), 'created': created})
        codes.append(formatted)
    delete_codes(db, user_id)
    db.execute(text('INSERT INTO mfa_recovery_codes(user_id,code_salt,code_hash,created_at) '
                    'VALUES(:uid,:salt,:digest,:created)'), rows)
    return codes


def code_status(db, user_id):
    row = db.execute(text('SELECT COUNT(*) - COUNT(used_at) AS remaining, MAX(created_at) AS generated_at '
                          'FROM mfa_recovery_codes WHERE user_id=:uid'), {'uid': user_id}).mappings().one()
    return {"remaining": row["remaining"],
            "generated_at": as_utc_datetime(row["generated_at"]) if row["generated_at"] is not None else None}


def export_codes(db, user_id):
    rows = db.execute(text('SELECT created_at,used_at FROM mfa_recovery_codes '
                           'WHERE user_id=:uid ORDER BY used_at'), {'uid': user_id}).all()
    used = [as_utc_datetime(row.used_at) for row in rows if row.used_at is not None]
    return {'remaining': len(rows) - len(used), 'generated_at': as_utc_datetime(rows[0].created_at) if rows else None,
            'used_at': used}


def consume_code(db, user_id, value):
    if not isinstance(value, str) or not value.isascii():
        return False
    code = ''.join(value.split()).replace('-', '').upper()
    if len(code) != 10 or any(char not in ALPHABET for char in code):
        return False
    key = recovery_key()
    rows = db.execute(text('SELECT id,code_salt,code_hash FROM mfa_recovery_codes '
                           'WHERE user_id=:uid AND used_at IS NULL'), {'uid': user_id})
    match = None
    for row in rows:
        if secrets.compare_digest(_hash(key, row.code_salt, code), row.code_hash):
            match = row.id
    if match is None:
        return False
    updated = db.execute(text('UPDATE mfa_recovery_codes SET used_at=:now '
                              'WHERE id=:id AND user_id=:uid AND used_at IS NULL'),
                         {'now': datetime.now(timezone.utc), 'id': match, 'uid': user_id})
    return updated.rowcount == 1
