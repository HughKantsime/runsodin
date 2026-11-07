"""Authenticated redaction/checkpoint evidence, keyed outside the database.

A dedicated installation keyring retains old keys across audit-key rotation.
Receipts never contain original plaintext or the destroyed per-row salt.
"""
import hashlib
import hmac
import json


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def signature(value):
    from core.audit_keys import keyring
    ring = keyring()
    key = ring['keys'].get(value.get('key_id')) if ring else None
    if not key:
        raise ValueError('Audit signing key unavailable')
    return hmac.new(bytes.fromhex(key), b'odin-audit-receipt-v1\0'+encoded(value), hashlib.sha256).hexdigest()


def seal(value, connection=None):
    from core.audit_keys import keyring
    data = {**value, 'key_id': keyring(create=True, connection=connection)['active']}
    return {**data, 'signature': signature(data)}


def deletion_key(connection):
    """A missing key must not veto legally required deletion."""
    from core.audit_keys import AuditKeyMissing, keyring
    try:
        keyring(create=True, connection=connection)
    except AuditKeyMissing:
        return False
    return True


def seal_deletion(value, connection):
    from core.audit_keys import AuditKeyMissing
    try:
        return seal(value, connection)
    except AuditKeyMissing:
        return {**value, 'unsealed': True, 'reason': 'audit_key_missing'}


def authentic(value):
    if not isinstance(value, dict):
        return False
    if value.get('head_signature_version') == 2 and 'history' in value:
        from core.audit_head_history import history_digest
        if history_digest(value['history']) != value.get('history_sha256'):
            return False
    data = {k: v for k, v in value.items()
            if k != 'signature' and not (k == 'history' and value.get('head_signature_version') == 2)}
    try:
        return hmac.compare_digest(str(value.get('signature', '')), signature(data))
    except (ValueError, KeyError, TypeError, OSError):
        return False


def redaction(before, after):
    """Only NULL actor/IP and structural [deleted] substitutions are allowed."""
    for name in ('actor_user_id', 'actor_username', 'actor_auth_kind', 'ip_address'):
        if before[name] != after[name] and after[name] is not None:
            return False
    if before['details'] == after['details']:
        return True
    try:
        new = json.loads(after['details'])
    except (TypeError, ValueError):
        return False
    if new == {'anonymized_user_id': before['entity_id']} and before['entity_type'] == 'user':
        return True
    try:
        old = json.loads(before['details'])
    except (TypeError, ValueError):
        return False

    if before['action'] == 'controlled_part.changed' and isinstance(old, dict):
        from core.controlled_privacy import erased_details
        authors = {before.get('actor_user_id')}
        authors.update(value.get('changed_by') for value in (old.get('before'), old.get('after')) if isinstance(value, dict))
        if any(new == erased_details(old, uid, actor_id=before.get('actor_user_id'))
               for uid in authors if uid is not None):
            return True

    if before['action'] in ('controlled_audience.saved', 'controlled_audience.deleted') and isinstance(old, dict):
        from core.controlled_privacy import erased_audience_details
        members = {uid for side in ('before', 'after') if isinstance(old.get(side), list)
                   for uid in old[side] if type(uid) is int}
        if any(new == erased_audience_details(old, uid) for uid in members):
            return True

    def scrubbed(a, b, key=None):
        from core.identity_scrub import PROTECTED_KEYS, STRUCTURAL_KEYS
        if key in PROTECTED_KEYS:
            return a == b
        if a == b:
            return True
        if isinstance(a, dict) and isinstance(b, dict):
            mapped = {k if k in b else '[deleted-user]': k for k in a}
            if set(mapped) != set(b):
                return False
            return all((k == original or original not in STRUCTURAL_KEYS) and
                       scrubbed(a[original], b[k], original) for k, original in mapped.items())
        if isinstance(a, list) and isinstance(b, list):
            return len(a) == len(b) and all(scrubbed(x, y) for x, y in zip(a, b))
        if isinstance(a, str) and isinstance(b, str) and ('[deleted]' in b or '[deleted-user]' in b):
            import re
            pattern = '.*'.join(re.escape(part) for part in re.split(r'\[deleted(?:-user)?\]', b))
            return re.fullmatch(pattern, a, flags=re.DOTALL) is not None
        return False
    # Password history removes the IP suffix together with the identity.
    if before['action'] in ('login_success', 'login_failed'):
        prefix = 'Login' if before['action'] == 'login_success' else 'Failed login'
        if new == prefix+': [deleted]' and isinstance(old, str) and old.startswith(prefix+': '):
            return True
    return scrubbed(old, new)
