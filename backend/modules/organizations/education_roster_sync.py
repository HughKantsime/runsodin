"""Explicit preview then atomic roster import into D2's classes and live grants."""
import json
import uuid
from sqlalchemy import text
from core.db_compat import execute_insert_returning_id
from modules.organizations import education_roster_store as store
from modules.organizations import education_roster_providers as providers
from modules.organizations.education_membership import retain_membership
from modules.organizations.education_roster_resolution import (
    resolved_users, without_erased, account_links, confirm_links)
from modules.organizations.education_classes_service import actor, audit as school_audit


def admin_org(db, principal):
    org, admin = actor(db, principal)
    if not admin:
        providers.refuse('roster_admin_required', 403)
    return org


async def courses(db, connection):
    if connection['provider'] == 'google':
        if not connection['tokens'].get('refresh_token'):
            providers.refuse('roster_reconnect_required')
        return await providers.google_roster(connection['config'], connection['tokens'], None)
    return [{'id': c['id'], 'title': c['title']} for c in connection['tokens'].get('contexts', {}).values()]


async def source(db, connection, course_id):
    if connection['provider'] == 'google':
        if not connection['tokens'].get('refresh_token'):
            providers.refuse('roster_reconnect_required')
        data = await providers.google_roster(connection['config'], connection['tokens'], course_id)
    else:
        context = connection['tokens'].get('contexts', {}).get(course_id)
        if not context:
            providers.refuse('roster_launch_required')
        data = await providers.canvas_roster(connection['config'], context)
    return normalize(data, connection['config'])


def normalize(data, config):
    if not isinstance(data.get('id'), str) or not 1 <= len(data['id']) <= 255:
        providers.refuse('roster_course_invalid', 422)
    members, identities, emails, skipped = {}, {}, {}, list(data.get('skipped', []))
    if len(data['members']) > 10000:
        providers.refuse('roster_limit_exceeded', 422)
    for item in data['members']:
        subject, email, role = item.get('subject'), item.get('email'), item.get('role')
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255 or role not in ('manager', 'student'):
            providers.refuse('roster_member_invalid', 422)
        if not isinstance(email, str) or not email.strip() or '@' not in email or len(email) > 254:
            skipped.append({'subject': subject, 'reason': 'roster_email_missing_or_invalid'})
            continue
        email = email.strip().lower()
        if email.rsplit('@', 1)[-1] not in config['allowed_domains']:
            skipped.append({'subject': subject, 'reason': 'roster_domain_refused'})
            continue
        if identities.get(subject, email) != email or emails.get(email, subject) != subject:
            providers.refuse('roster_identity_conflict')
        identities[subject], emails[email] = email, subject
        members[(subject, role)] = {'subject': subject, 'email': email, 'role': role}
    return {'id': data['id'], 'title': str(data['title'])[:200], 'term': str(data.get('term', ''))[:200],
            'members': [members[k] for k in sorted(members)], 'skipped': skipped}


def local_state(db, connection):
    params = {'o': connection['org_id'], 'c': connection['id']}
    statements = [
        'SELECT id,username,group_id,is_active,role,email,password_hash,mfa_enabled,oidc_subject,oidc_issuer FROM users WHERE group_id=:o ORDER BY id',
        'SELECT * FROM education_roster_identities WHERE org_id=:o ORDER BY connection_id,subject',
        'SELECT * FROM education_roster_grant_links WHERE org_id=:o ORDER BY connection_id,subject',
        'SELECT * FROM education_roster_tombstones WHERE connection_id=:c ORDER BY subject_hash',
        'SELECT c.* FROM education_classes c JOIN education_roster_courses r ON r.class_id=c.id WHERE r.connection_id=:c ORDER BY c.id',
        'SELECT g.* FROM education_cost_center_grants g JOIN education_classes c ON c.cost_center_id=g.cost_center_id '
        'JOIN education_roster_courses r ON r.class_id=c.id WHERE r.connection_id=:c ORDER BY g.id',
        'SELECT e.* FROM education_cost_centers e JOIN education_classes c ON c.cost_center_id=e.id '
        'JOIN education_roster_courses r ON r.class_id=c.id WHERE r.connection_id=:c ORDER BY e.id',
    ]
    values = [[dict(r) for r in db.execute(text(s), params).mappings()] for s in statements]
    return store.digest(json.dumps(values, sort_keys=True, default=str))


def mapping(db, connection, course_id):
    return db.execute(text('SELECT c.* FROM education_classes c JOIN education_roster_courses r '
        'ON r.class_id=c.id AND r.org_id=c.org_id WHERE r.connection_id=:c AND r.course_id=:course'),
        {'c': connection['id'], 'course': course_id}).mappings().first()


def preview(db, principal, connection, roster):
    org = admin_org(db, principal)
    if org != connection['org_id']:
        providers.refuse('roster_connection_not_found', 404)
    # Serialize preview with config saves/imports, after remote I/O.
    current = store.load(db, connection['id'], org, lock=True)
    if current['revision'] != connection['revision']:
        providers.refuse('roster_configuration_changed')
    clear_handles = {}
    roster = without_erased(db, connection, roster, clear_handles)
    resolved = resolved_users(db, connection, roster)
    cls = mapping(db, connection, roster['id'])
    before = set()
    if cls:
        if cls['state'] != 'active':
            providers.refuse('roster_class_archived')
        before = set(db.execute(text("SELECT user_id,role FROM education_cost_center_grants WHERE cost_center_id=:c AND state='active'"),
                                {'c': cls['cost_center_id']}).all())
    desired = {(resolved[m['subject']]['user_id'], m['role']) for m in roster['members'] if resolved[m['subject']]['user_id']}
    added = [m for m in roster['members'] if (resolved[m['subject']]['user_id'], m['role']) not in before]
    removed = sorted(before - desired)
    token = store.receipt(db, connection['id'], 'preview', {
        'revision': current['revision'], 'roster': roster, 'local': local_state(db, connection),
        'clear_handles': clear_handles}, principal['id'])
    store.audit(db, 'preview', org, resource=connection['id'])
    db.commit()
    return {'preview_token': token, 'expires_in': 600, 'course': roster, 'class_id': cls['id'] if cls else None,
            'create_users': sum(u['user_id'] is None for u in resolved.values()), 'add_memberships': added,
            'account_links': account_links(resolved, roster), 'skipped': roster.get('skipped', []),
            'revoke_memberships': [{'user_id': u, 'role': role} for u, role in removed]}


def apply(db, principal, connection_id, token, confirmations=()):
    org = admin_org(db, principal)
    retain_membership(db, principal['id'], org)
    connection = store.load(db, connection_id, org, lock=True)
    plan = store.consume(db, token, connection_id, 'preview', principal['id'])
    if plan['revision'] != connection['revision'] or plan['local'] != local_state(db, connection):
        providers.refuse('roster_preview_stale')
    roster = plan['roster']
    roster = without_erased(db, connection, roster)
    resolved = resolved_users(db, connection, roster)
    # Keep a confirmed placeholder from acquiring credentials between the preview
    # check and the binding write. Retain membership locks the live users row.
    for uid in sorted({r['user_id'] for r in resolved.values() if r['user_id'] is not None}):
        retain_membership(db, uid, org)
    if plan['local'] != local_state(db, connection):
        providers.refuse('roster_preview_stale')
    declined = confirm_links(resolved, confirmations)
    roster = {**roster, 'members': [m for m in roster['members'] if m['subject'] not in declined],
              'skipped': roster.get('skipped', []) + [
                  {'subject': s, 'reason': 'roster_link_declined'} for s in sorted(declined)]}
    cls = mapping(db, connection, roster['id'])
    if cls:
        center_id, class_id = cls['cost_center_id'], cls['id']
        db.execute(text('UPDATE education_cost_centers SET revision=revision WHERE id=:c'), {'c': center_id})
        if plan['local'] != local_state(db, connection):
            providers.refuse('roster_preview_stale')
        active = db.execute(text('SELECT state FROM education_cost_centers WHERE id=:c'), {'c': center_id}).scalar()
        if cls['state'] != 'active' or active != 'active':
            providers.refuse('roster_class_archived')
        # A manually attached second D2 class would share the grants. Refuse changing it.
        if db.execute(text('SELECT COUNT(*) FROM education_classes WHERE cost_center_id=:c'), {'c': center_id}).scalar() != 1:
            providers.refuse('roster_shared_center_refused')
    else:
        class_id = str(uuid.uuid4())
        code = 'roster-' + class_id
        center_id = execute_insert_returning_id(db,
            'INSERT INTO education_cost_centers(org_id,name_key,code_key,display_name,code,created_by) '
            'VALUES(:o,:k,:k,:title,:k,:a)', {'o': org, 'k': code, 'title': roster['title'], 'a': principal['id']})
        db.execute(text('INSERT INTO education_classes(id,org_id,cost_center_id,title,term,created_by) '
            'VALUES(:id,:o,:c,:title,:term,:a)'), {'id': class_id, 'o': org, 'c': center_id,
            'title': roster['title'], 'term': roster['term'], 'a': principal['id']})
        db.execute(text('INSERT INTO education_roster_courses(connection_id,org_id,course_id,class_id) VALUES(:c,:o,:course,:cls)'),
            {'c': connection_id, 'o': org, 'course': roster['id'], 'cls': class_id})
    # Preserve the original grant evidence for every unchanged membership.
    desired = {(resolved[m['subject']]['user_id'], m['role']) for m in roster['members']}
    declined_users = {resolved[s]['user_id'] for s in declined}
    for grant in db.execute(text("SELECT id,user_id,role FROM education_cost_center_grants WHERE cost_center_id=:c AND state='active'"),
                            {'c': center_id}).mappings().all():
        # Declining a proposed link skips that account; it does not authorize
        # removing manually assigned grants that were absent from the revocation preview.
        if grant['user_id'] not in declined_users and (grant['user_id'], grant['role']) not in desired:
            db.execute(text("UPDATE education_cost_center_grants SET state='revoked',revoked_by=:a,revoked_at=CURRENT_TIMESTAMP WHERE id=:id"),
                       {'a': principal['id'], 'id': grant['id']})
    for member in roster['members']:
        subject = member['subject']
        resolution = resolved[subject]
        uid = resolution['user_id']
        if uid is None:
            from license_manager import check_user_limit
            check_user_limit(db.execute(text('SELECT COUNT(*) FROM users')).scalar())
            uid = execute_insert_returning_id(db,
                "INSERT INTO users(username,email,password_hash,role,is_active,group_id) VALUES(:name,:email,'','viewer',TRUE,:o)",
                {'name': 'classroom-' + uuid.uuid4().hex, 'email': member['email'], 'o': org})
            resolution['user_id'] = uid
        retain_membership(db, uid, org)
        statement = (
            'INSERT INTO education_roster_identities(connection_id,org_id,subject,user_id) '
            'VALUES(:c,:o,:s,:u) ON CONFLICT(connection_id,subject) DO NOTHING'
            if resolution['mode'] == 'identity' else
            'INSERT INTO education_roster_grant_links(connection_id,org_id,subject,user_id) '
            'VALUES(:c,:o,:s,:u) ON CONFLICT(connection_id,subject) DO NOTHING')
        db.execute(text(statement),
            {'c': connection_id, 'o': org, 's': subject, 'u': uid})
        db.execute(text('INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) '
            "VALUES(:o,:c,:u,:r,'active',:a) ON CONFLICT(cost_center_id,user_id,role) DO UPDATE SET "
            "state='active',granted_by=:a,granted_at=CURRENT_TIMESTAMP,revoked_at=NULL,revoked_by=NULL "
            "WHERE education_cost_center_grants.state<>'active'"),
            {'o': org, 'c': center_id, 'u': uid, 'r': member['role'], 'a': principal['id']})
    db.execute(text('UPDATE education_classes SET title=:title,term=:term,revision=revision+1 WHERE id=:id'),
               {'title': roster['title'], 'term': roster['term'], 'id': class_id})
    db.execute(text('UPDATE education_cost_centers SET revision=revision+1,display_name=:title WHERE id=:c'),
               {'c': center_id, 'title': roster['title']})
    school_audit(db, principal, 'roster.import', 'class', class_id, center_id)
    db.commit()
    return {'class_id': class_id, 'cost_center_id': center_id, 'memberships': len(roster['members']),
            'skipped': roster.get('skipped', [])}
