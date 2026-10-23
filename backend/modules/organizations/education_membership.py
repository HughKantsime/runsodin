"""Retention is historical identity, never an authorization decision."""
from sqlalchemy import text
from core.errors import ErrorCode, OdinError


def retain_membership(db, user_id, org_id):
    # Serialize with restore and revalidate current membership before first write.
    if db.get_bind().dialect.name == 'sqlite':
        db.execute(text('UPDATE users SET id=id WHERE id=:id'), {'id': user_id})
    suffix = ' FOR UPDATE' if db.get_bind().dialect.name == 'postgresql' else ''
    user = db.execute(text('SELECT group_id,is_active FROM users WHERE id=:id' + suffix), {'id': user_id}).first()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — suffix is a fixed dialect-selected FOR UPDATE literal; values are bound
    if not user or user.group_id != org_id or not user.is_active:
        raise OdinError(ErrorCode.permission_denied, 'Active tenant membership required', status=403)
    db.execute(text('INSERT INTO education_memberships(user_id,org_id) VALUES(:id,:org) ON CONFLICT(user_id,org_id) DO UPDATE SET ended_at=NULL'), {'id': user_id, 'org': org_id})
