"""Preview account links explicitly; grant-only links never authorize sign-in."""
import secrets
from sqlalchemy import text
from modules.organizations import education_roster_store as store
from modules.organizations import education_roster_providers as providers


def without_erased(db, connection, roster, clear_handles=None):
    hashes = set(db.execute(text('SELECT subject_hash FROM education_roster_tombstones WHERE connection_id=:c'),
                            {'c': connection['id']}).scalars())
    members, skipped = [], []
    def erased(subject_hash):
        entry = {'reason': 'roster_erased_subject'}
        if clear_handles is not None:
            handle = secrets.token_urlsafe(32)
            clear_handles[handle] = subject_hash
            entry['clear_handle'] = handle
        return entry

    for entry in roster.get('skipped', []):
        subject_hash = store.subject_digest(connection['id'], entry.get('subject', ''))
        skipped.append(erased(subject_hash) if subject_hash in hashes else entry)
    for member in roster['members']:
        subject_hash = store.subject_digest(connection['id'], member['subject'])
        if subject_hash in hashes:
            # Do not put erased subject/email data back in the cached preview.
            skipped.append(erased(subject_hash))
        else:
            members.append(member)
    return {**roster, 'members': members, 'skipped': skipped}


def resolved_users(db, connection, roster):
    resolved = {}
    for member in roster['members']:
        subject = member['subject']
        if subject in resolved:
            continue
        params = {'c': connection['id'], 's': subject}
        linked = db.execute(text("SELECT user_id,'identity' AS mode FROM education_roster_identities "
            "WHERE connection_id=:c AND subject=:s UNION ALL SELECT user_id,'grant_only' AS mode "
            "FROM education_roster_grant_links WHERE connection_id=:c AND subject=:s"), params).mappings().all()
        if linked:
            if len(linked) != 1:
                providers.refuse('roster_identity_conflict')
            row = db.execute(text('SELECT * FROM users WHERE id=:u'), {'u': linked[0]['user_id']}).mappings().one()
            if row['group_id'] != connection['org_id'] or not row['is_active']:
                providers.refuse('roster_identity_conflict')
            resolved[subject] = {**linked[0], 'confirm': False}
            continue
        matches = db.execute(text('SELECT * FROM users WHERE LOWER(email)=:e'), {'e': member['email']}).mappings().all()
        if not matches:
            resolved[subject] = {'user_id': None, 'mode': 'identity', 'confirm': False}
            continue
        row = matches[0]
        if len(matches) != 1 or row['group_id'] != connection['org_id'] or not row['is_active']:
            providers.refuse('roster_identity_conflict')
        others = db.execute(text('SELECT connection_id FROM education_roster_identities WHERE user_id=:u '
            'UNION ALL SELECT connection_id FROM education_roster_grant_links WHERE user_id=:u'), {'u': row['id']}).scalars().all()
        if connection['id'] in others:
            providers.refuse('roster_identity_conflict')
        # Canvas email is never identity proof. Google may adopt an uncredentialed
        # viewer only after the administrator confirms this individual account.
        mode = 'identity' if (not others and connection['provider'] == 'google' and row['role'] == 'viewer'
            and not any(row[k] for k in ('password_hash', 'mfa_enabled', 'oidc_subject', 'oidc_issuer'))) else 'grant_only'
        resolved[subject] = {'user_id': row['id'], 'username': row['username'], 'mode': mode, 'confirm': True}
    return resolved


def account_links(resolved, roster):
    return [{'subject': subject, 'user_id': row['user_id'], 'mode': row['mode'],
             'username': row['username'],
             'email': next(m['email'] for m in roster['members'] if m['subject'] == subject),
             'roles': sorted({m['role'] for m in roster['members'] if m['subject'] == subject}),
             'label': f"Link existing account #{row['user_id']}" + (' (grant-only; no new sign-in identity)' if row['mode'] == 'grant_only' else '')}
            for subject, row in resolved.items() if row['confirm']]


def confirm_links(resolved, confirmations):
    expected = {(subject, r['user_id'], r['mode']) for subject, r in resolved.items() if r['confirm']}
    actual = {(r['subject'], r['user_id'], r['mode']) for r in confirmations}
    if (actual != expected or len(actual) != len(confirmations)
        or any(r.get('decision', 'confirm') not in ('confirm', 'decline') for r in confirmations)):
        providers.refuse('roster_account_confirmation_required', 422)
    return {r['subject'] for r in confirmations if r.get('decision') == 'decline'}
