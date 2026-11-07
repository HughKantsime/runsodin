"""Class marks and admin-approved instructor effort, with live D2 roster authority."""
import uuid
import json
from core.identity_scrub import scrub_text
from sqlalchemy import text
from modules.organizations.education_classes_service import actor, class_access, refused
from modules.organizations.education_program_audit import audit
from modules.organizations.education_membership import retain_membership


def records(db, principal, class_id, activity_date=None):
    cls = class_access(db, principal, class_id, manage=True)
    params = {'c': class_id, 'o': cls['org_id']}
    marks = db.execute(text('SELECT user_id,participant,concentrator,revision FROM education_program_marks '
        'WHERE class_id=:c AND org_id=:o ORDER BY user_id'), params).mappings().all()
    day_filter = '' if activity_date is None else ' AND activity_date=:day'
    params['day'] = activity_date.isoformat() if activity_date else None
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- day_filter is empty or the literal AND activity_date=:day; date, class and org are bound
    hours = db.execute(text('SELECT id,instructor_id,activity_date,hours,revision FROM education_instructor_hours '
        'WHERE class_id=:c AND org_id=:o' + day_filter + ' ORDER BY activity_date DESC,instructor_id LIMIT 100'), params).mappings().all()
    return {'marks': [{**r, 'participant': bool(r['participant']), 'concentrator': bool(r['concentrator'])} for r in marks], 'hours': [dict(r) for r in hours],
            'can_approve_hours': actor(db, principal)[1]}


def _member(db, cls, user_id, role):
    if not db.execute(text('SELECT 1 FROM education_cost_center_grants g JOIN users u '
        'ON u.id=g.user_id AND u.group_id=g.org_id AND u.is_active IS TRUE '
        "WHERE g.cost_center_id=:c AND g.org_id=:o AND g.user_id=:u AND g.role=:r AND g.state='active'"),
        {'c': cls['cost_center_id'], 'o': cls['org_id'], 'u': user_id, 'r': role}).first():
        raise refused('Active class ' + role + ' required', 409)
    retain_membership(db, user_id, cls['org_id'])


def mark(db, principal, class_id, user_id, body):
    cls = class_access(db, principal, class_id, manage=True, write=True, require_active=False)
    if body.participant or body.concentrator:
        _member(db, cls, user_id, 'student')
    params = dict(c=class_id, o=cls['org_id'], u=user_id, a=principal['id'], **body.model_dump())
    existing = db.execute(text('SELECT revision,participant,concentrator FROM education_program_marks WHERE class_id=:c AND user_id=:u'), params).mappings().first()
    if (existing['revision'] if existing else 0) != body.revision:
        raise refused('Reporting mark changed; reload required', 409)
    if existing is None and not (body.participant or body.concentrator):
        raise refused('Reporting mark not found')
    if existing is None:
        db.execute(text('INSERT INTO education_program_marks(class_id,org_id,user_id,participant,concentrator,marked_by) '
            'VALUES(:c,:o,:u,:participant,:concentrator,:a)'), params)
    else:
        db.execute(text('UPDATE education_program_marks SET participant=:participant,concentrator=:concentrator,'
            'marked_by=:a,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE class_id=:c AND user_id=:u'), params)
    audit(db, principal, 'program.mark', 'program_mark', class_id + ':' + str(user_id), cls['cost_center_id'], body.revision + 1,
        details={'before': {k: bool(existing[k]) for k in ('participant', 'concentrator')} if existing else None,
                 'after': {'participant': body.participant, 'concentrator': body.concentrator}})
    db.commit()
    return {'revision': body.revision + 1}


def approve_hours(db, principal, class_id, body):
    if not actor(db, principal)[1]:
        raise refused('Tenant admin approval required for instructor hours', 403)
    cls = class_access(db, principal, class_id, manage=True, write=True, require_active=False)
    _member(db, cls, body.instructor_id, 'manager')
    params = dict(c=class_id, o=cls['org_id'], a=principal['id'], **body.model_dump(mode='json'))
    existing = db.execute(text('SELECT id,revision,hours FROM education_instructor_hours WHERE class_id=:c '
        'AND instructor_id=:instructor_id AND activity_date=:activity_date'), params).first()
    if (existing.revision if existing else 0) != body.revision:
        raise refused('Approved hours changed; reload required', 409)
    if existing is None:
        params['id'] = str(uuid.uuid4())
        db.execute(text('INSERT INTO education_instructor_hours(id,class_id,org_id,instructor_id,activity_date,hours,approved_by) '
            'VALUES(:id,:c,:o,:instructor_id,:activity_date,:hours,:a)'), params)
    else:
        params['id'] = existing.id
        db.execute(text('UPDATE education_instructor_hours SET hours=:hours,approved_by=:a,revision=revision+1,'
            'updated_at=CURRENT_TIMESTAMP WHERE id=:id'), params)
    audit(db, principal, 'program.hours_approved', 'instructor_hours', params['id'], cls['cost_center_id'], body.revision + 1,
        details={'before_hours': existing.hours if existing else None, 'after_hours': body.hours,
                 'activity_date': body.activity_date.isoformat()})
    db.commit()
    return {'id': params['id'], 'revision': body.revision + 1}


def export_program_records(db, user_id):
    params = {'u': user_id}
    def rows(sql):
        return [dict(r) for r in db.execute(text(sql), params).mappings()]
    email = db.execute(text('SELECT email FROM users WHERE id=:u'), params).scalar()
    receiving = []
    if email and email != '[deleted]':
        for schedule in db.execute(text("SELECT id,frequency,is_active,recipients FROM report_schedules WHERE report_type='program_report'")).mappings():
            if email.casefold() in {address.casefold() for address in json.loads(schedule['recipients'] or '[]')}:
                receiving.append({key: schedule[key] for key in ('id', 'frequency', 'is_active')})
    return {
        'recipient_schedules': receiving,
        'schedules_created': rows("SELECT id,name,frequency,is_active,filters FROM report_schedules WHERE report_type='program_report' AND created_by=:u"),
        'marks': rows('SELECT * FROM education_program_marks WHERE user_id=:u'),
        'instructor_hours': rows('SELECT * FROM education_instructor_hours WHERE instructor_id=:u'),
        # Approvers get identifiers and role, never another person's marks/hours.
        'marks_authored': rows('SELECT class_id,org_id,marked_by,updated_at FROM education_program_marks WHERE marked_by=:u'),
        'hours_approved': rows('SELECT id,class_id,org_id,approved_by,updated_at FROM education_instructor_hours WHERE approved_by=:u'),
        'actions': rows("SELECT event_id,org_id,action,resource_type,resource_id,lifecycle_revision,created_at FROM education_audit_events WHERE actor_kind='user' AND actor_id=CAST(:u AS TEXT) AND action LIKE 'program.%'"),
    }


def erase_program_records(db, user_id, identifiers=()):
    # Retain numeric school accounting and classification under the existing
    # erased user placeholder. Remove reviewer identity, as D2 does.
    db.execute(text('UPDATE education_program_marks SET marked_by=NULL,revision=revision+1 WHERE marked_by=:u'), {'u': user_id})
    db.execute(text('UPDATE education_instructor_hours SET approved_by=NULL,revision=revision+1 WHERE approved_by=:u'), {'u': user_id})

    # Schedule configuration is deletable, unlike retained school accounting.
    from modules.organizations.education_program_policy import deactivate_schedules
    deactivate_schedules(db, user_id)
    identities = {v.casefold() for v in identifiers if isinstance(v, str) and v}
    for row in db.execute(text("SELECT id,name,recipients,filters,is_active FROM report_schedules WHERE report_type='program_report'")).mappings().all():
        recipients = json.loads(row['recipients'] or '[]')
        kept = [v for v in recipients if v.casefold() not in identities]
        filters = json.loads(row['filters'] or '{}')
        if isinstance(filters.get('term'), str):
            filters['term'] = scrub_text(filters['term'], identifiers)
        db.execute(text('UPDATE report_schedules SET name=:name,recipients=:recipients,filters=:filters,is_active=:active WHERE id=:id'),
            {'id': row['id'], 'name': scrub_text(row['name'], identifiers), 'recipients': json.dumps(kept),
             'filters': json.dumps(filters), 'active': bool(row['is_active'] and kept)})
