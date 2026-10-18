"""Atomic content history rooted in current profile authorization."""
import difflib
import logging
from fastapi import HTTPException
from sqlalchemy import text
from core.rbac import check_org_access
from core.db_compat import execute_insert_returning_id
from core.dependencies import log_audit

log = logging.getLogger('odin.api')
MAX_CONTENT_BYTES = 50 * 1024 * 1024


def _bounded_text(value, limit):
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode('utf-8')) <= limit
    except UnicodeError:
        return False


def accessible_profile(db, profile_id, actor, *, mutate=False):
    row = db.execute(text('SELECT * FROM printer_profiles WHERE id=:id'), {'id': profile_id}).first()
    if not row or not check_org_access(actor, row.org_id):
        raise HTTPException(404, 'Profile not found')
    if mutate and actor.get('role') != 'admin' and row.created_by != actor.get('id'):
        raise HTTPException(403, 'Can only edit your own profiles')
    return row


def revisions(db, profile_id, actor):
    accessible_profile(db, profile_id, actor)
    return [dict(row._mapping) for row in db.execute(text('SELECT id,profile_id,revision_number,file_format,slicer,category,changelog,created_by,created_at FROM printer_profile_revisions WHERE profile_id=:id ORDER BY revision_number DESC'), {'id': profile_id}).fetchall()]


def revision(db, profile_id, number, actor):
    accessible_profile(db, profile_id, actor)
    row = db.execute(text('SELECT * FROM printer_profile_revisions WHERE profile_id=:id AND revision_number=:number'), {'id': profile_id, 'number': number}).first()
    if row is None:
        raise HTTPException(404, 'Profile revision not found')
    return dict(row._mapping)


def content_diff(db, profile_id, before, after, actor):
    left = revision(db, profile_id, before, actor)['raw_content']
    right = revision(db, profile_id, after, actor)['raw_content']
    # Refuse oversized comparison rather than allocating an unbounded diff.
    if len(left.encode('utf-8')) + len(right.encode('utf-8')) > 2 * 1024 * 1024:
        raise HTTPException(413, 'Profile content is too large to compare')
    lines = difflib.unified_diff(left.splitlines(keepends=True), right.splitlines(keepends=True), fromfile=f'revision-{before}', tofile=f'revision-{after}')
    output, size = [], 0
    for line in lines:
        size += len(line.encode('utf-8'))
        if size > 1024 * 1024:
            raise HTTPException(413, 'Profile diff is too large')
        output.append(line)
    return {'from_revision': before, 'to_revision': after, 'diff': ''.join(output)}


def _snapshot(db, row, number, content, changelog, actor_id, *, metadata=None):
    values = {'profile': row.id, 'number': number, 'content': content, 'format': row.file_format,
              'slicer': row.slicer, 'category': row.category, 'changelog': changelog, 'actor': actor_id}
    if metadata:
        values.update(format=metadata['file_format'], slicer=metadata['slicer'], category=metadata['category'])
    return execute_insert_returning_id(db, 'INSERT INTO printer_profile_revisions(profile_id,revision_number,raw_content,file_format,slicer,category,changelog,created_by) VALUES(:profile,:number,:content,:format,:slicer,:category,:changelog,:actor)', values)


def write_revision(db, profile_id, actor, *, content=None, changelog=None, revert_to=None, updates=None):
    if revert_to is None and (not _bounded_text(content, MAX_CONTENT_BYTES)):
        raise HTTPException(400, 'Profile raw_content must be text within 50 MB')
    if changelog is not None and (not _bounded_text(changelog, 16000) or len(changelog) > 4000):
        raise HTTPException(400, 'Profile changelog must be text within 4000 characters')
    try:
        # Portable row write lock serializes numbering and rechecks current ownership.
        db.execute(text('UPDATE printer_profiles SET id=id WHERE id=:id'), {'id': profile_id})
        row = accessible_profile(db, profile_id, actor, mutate=True)
        restored = revision(db, profile_id, revert_to, actor) if revert_to is not None else None
        if restored:
            content = restored['raw_content']
            changelog = f'Restored revision {revert_to}'
        if not _bounded_text(row.raw_content, MAX_CONTENT_BYTES):
            raise HTTPException(400, 'Existing profile content must be text within 50 MB before versioning')
        if not _bounded_text(content, MAX_CONTENT_BYTES):
            raise HTTPException(400, 'Restored profile content must be text within 50 MB')
        latest = db.execute(text('SELECT MAX(revision_number) FROM printer_profile_revisions WHERE profile_id=:id'), {'id': profile_id}).scalar()
        if latest is None:
            _snapshot(db, row, 1, row.raw_content, 'Original content before first revision', row.created_by)
            latest = 1
        number = latest + 1
        identifier = _snapshot(db, row, number, content, changelog, actor.get('id'), metadata=restored)
        fields = {'raw_content': content}
        if restored:
            fields.update({key: restored[key] for key in ('file_format', 'slicer', 'category')})
        allowed = {'name', 'description', 'tags', 'is_shared', 'printer_id', 'filament_type'}
        fields.update({key: value for key, value in (updates or {}).items() if key in allowed})
        sets = ', '.join(f'{key}=:{key}' for key in fields)
        fields['id'] = profile_id
        db.execute(text(f'UPDATE printer_profiles SET {sets},updated_at=CURRENT_TIMESTAMP WHERE id=:id'), fields)  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- column names pass a fixed allowlist; values are bound
        log_audit(db, 'update', 'profile', profile_id, {'revision_number': number, 'reverted_from': revert_to})
        db.flush()
        db.commit()
        return {'id': identifier, 'profile_id': profile_id, 'revision_number': number}
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        log.error('Profile revision transaction failed for profile %s (%s)', profile_id, type(exc).__name__)
        raise HTTPException(500, f'Profile revision could not be saved. Reference profile {profile_id}') from None
