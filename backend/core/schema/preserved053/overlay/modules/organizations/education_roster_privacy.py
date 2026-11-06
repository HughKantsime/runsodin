"""D4 personal data export/erasure, independent of the current license."""
from sqlalchemy import text


def export_roster(db, user_id):
    return {
        'identities': [dict(r) for r in db.execute(text(
            'SELECT connection_id,org_id,subject,user_id FROM education_roster_identities WHERE user_id=:u'), {'u': user_id}).mappings()],
        'grant_links': [dict(r) for r in db.execute(text(
            'SELECT connection_id,org_id,subject,user_id FROM education_roster_grant_links WHERE user_id=:u'), {'u': user_id}).mappings()],
        'configured_connections': [dict(r) for r in db.execute(text(
            'SELECT id,org_id,provider,enabled FROM education_roster_connections WHERE created_by=:u'), {'u': user_id}).mappings()],
        'connected_connections': [dict(r) for r in db.execute(text(
            'SELECT id,org_id,provider,enabled FROM education_roster_connections WHERE connected_by=:u'), {'u': user_id}).mappings()],
        'pending_actions': [dict(r) for r in db.execute(text(
            'SELECT connection_id,kind,expires_at FROM education_roster_receipts WHERE actor_id=:u'), {'u': user_id}).mappings()],
    }


def erase_roster(db, user_id):
    # Receipts can contain an entire provider roster. Drop the tenant's cached
    # previews/auth continuations even when this user was not their initiator.
    org = db.execute(text('SELECT group_id FROM users WHERE id=:u'), {'u': user_id}).scalar()
    db.execute(text('DELETE FROM education_roster_receipts WHERE actor_id=:u OR connection_id IN '
        '(SELECT id FROM education_roster_connections WHERE org_id=:o)'), {'u': user_id, 'o': org})
    from modules.organizations.education_roster_store import subject_digest
    for row in db.execute(text('SELECT connection_id,subject FROM education_roster_identities WHERE user_id=:u '
        'UNION SELECT connection_id,subject FROM education_roster_grant_links WHERE user_id=:u'), {'u': user_id}).mappings().all():
        db.execute(text('INSERT INTO education_roster_tombstones(connection_id,subject_hash) VALUES(:c,:s) '
            'ON CONFLICT(connection_id,subject_hash) DO NOTHING'),
            {'c': row['connection_id'], 's': subject_digest(row['connection_id'], row['subject'])})
    db.execute(text('DELETE FROM education_roster_grant_links WHERE user_id=:u'), {'u': user_id})
    db.execute(text('UPDATE education_roster_connections SET created_by=NULL WHERE created_by=:u'), {'u': user_id})
    db.execute(text("UPDATE education_roster_connections SET tokens_encrypted=NULL,connected_by=NULL,revision=revision+1 "
        "WHERE provider='google' AND connected_by=:u"), {'u': user_id})
    db.execute(text('DELETE FROM education_roster_identities WHERE user_id=:u'), {'u': user_id})
