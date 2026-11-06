"""Class authority is the existing live Education cost-center roster."""
import hashlib
import json
import uuid
from sqlalchemy import text
from core.audit import log_audit
from core.errors import ErrorCode, OdinError
from modules.organizations.education_membership import retain_membership


def refused(message='Class not found', status=404):
    return OdinError(ErrorCode.not_found if status == 404 else ErrorCode.resource_conflict,
                     message, status=status)


def actor(db, principal, *, write=False):
    org = principal.get('group_id')
    if org is None:
        raise refused('Use an account in the school organization', 403)
    if write:
        retain_membership(db, principal['id'], org)
    row = db.execute(text('SELECT role FROM users WHERE id=:u AND group_id=:o AND is_active IS TRUE'),
                     {'u': principal['id'], 'o': org}).first()
    if not row:
        raise refused()
    return int(org), row.role == 'admin'


def center_access(db, principal, center_id, *, manage=False, write=False, require_active=True):
    org, admin = actor(db, principal, write=write)
    if write:
        db.execute(text('UPDATE education_cost_centers SET revision=revision WHERE id=:c AND org_id=:o'),
                   {'c': center_id, 'o': org})
    center = db.execute(text('SELECT * FROM education_cost_centers WHERE id=:c AND org_id=:o'),
                        {'c': center_id, 'o': org}).mappings().first()
    grants = set(db.execute(text("SELECT role FROM education_cost_center_grants WHERE org_id=:o "
        "AND cost_center_id=:c AND user_id=:u AND state='active'"),
        {'o': org, 'c': center_id, 'u': principal['id']}).scalars())
    if not center or not (admin or 'manager' in grants or (not manage and 'student' in grants)):
        raise refused()
    if write and require_active and center['state'] != 'active':
        raise refused('Class cost center is archived', 409)
    return dict(center), admin or 'manager' in grants, 'student' in grants


def class_access(db, principal, class_id, *, manage=False, write=False, require_active=True):
    org, _ = actor(db, principal)
    row = db.execute(text('SELECT * FROM education_classes WHERE id=:id AND org_id=:o'),
                     {'id': class_id, 'o': org}).mappings().first()
    if not row:
        raise refused()
    _, manager, student = center_access(db, principal, row['cost_center_id'], manage=manage, write=write, require_active=require_active)
    if write:
        db.execute(text('UPDATE education_classes SET revision=revision WHERE id=:id AND org_id=:o'),
                   {'id': class_id, 'o': org})
        row = db.execute(text('SELECT * FROM education_classes WHERE id=:id AND org_id=:o'),
                         {'id': class_id, 'o': org}).mappings().one()
    return {**row, 'can_manage': manager, 'can_submit': student}


def assignment_access(db, principal, assignment_id, *, manage=False, write=False, require_active=True):
    org, _ = actor(db, principal)
    row = db.execute(text('SELECT * FROM education_assignments WHERE id=:id AND org_id=:o'),
                     {'id': assignment_id, 'o': org}).mappings().first()
    if not row:
        raise refused('Assignment not found')
    cls = class_access(db, principal, row['class_id'], manage=manage, write=write, require_active=require_active)
    if write:
        db.execute(text('UPDATE education_assignments SET revision=revision WHERE id=:id AND org_id=:o'),
                   {'id': assignment_id, 'o': org})
        row = db.execute(text('SELECT * FROM education_assignments WHERE id=:id AND org_id=:o'),
                         {'id': assignment_id, 'o': org}).mappings().one()
    return dict(row), cls


def audit(db, principal, action, resource, resource_id, center_id, revision=1, *, automatic=False):
    """School audit contains no free text, grades, names, or cached personal responses."""
    org, _ = actor(db, principal)
    event = str(uuid.uuid4())
    receipt = json.dumps({'event': event, 'action': action, 'resource': str(resource_id),
                          'revision': revision}, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(receipt.encode()).hexdigest()
    db.execute(text('INSERT INTO education_audit_events '
        '(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,'
        'resource_id,cost_center_id,lifecycle_revision,details_json,result_json) '
        "VALUES(:e,:o,:kind,:u,:a,:e,:h,:r,:id,:c,:v,:details,'{}')"),
        {'e': event, 'o': org, 'u': str(principal['id']), 'a': action, 'h': digest,
         'r': resource, 'id': str(resource_id), 'c': center_id, 'v': revision,
         'kind': 'system' if automatic else 'user',
         'details': json.dumps({'automatic': True, 'authorized_by': principal['id']}) if automatic else '{}'})
    log_audit(db, action, 'education_class', details={'org_id': org, 'resource_id': str(resource_id)})


def list_classes(db, principal, *, offset=0, limit=50):
    org, admin = actor(db, principal)
    rows = db.execute(text('SELECT c.* FROM education_classes c WHERE c.org_id=:o AND '
        '(:admin=1 OR EXISTS (SELECT 1 FROM education_cost_center_grants g '
        "WHERE g.org_id=c.org_id AND g.cost_center_id=c.cost_center_id AND g.user_id=:u AND g.state='active')) "
        'ORDER BY c.created_at DESC,c.id LIMIT :limit OFFSET :offset'),
        {'o': org, 'admin': int(admin), 'u': principal['id'], 'limit': limit + 1, 'offset': offset}).mappings().all()
    items = []
    for row in rows[:limit]:
        _, manager, student = center_access(db, principal, row['cost_center_id'])
        items.append({**row, 'can_manage': manager, 'can_submit': student})
    return {'items': items, 'next_offset': offset + limit if len(rows) > limit else None}


def roster(db, principal, class_id):
    cls = class_access(db, principal, class_id)
    rows = db.execute(text('SELECT g.user_id,u.username,g.role FROM education_cost_center_grants g '
        'JOIN users u ON u.id=g.user_id AND u.group_id=g.org_id AND u.is_active IS TRUE '
        "WHERE g.org_id=:o AND g.cost_center_id=:c AND g.state='active' "
        "AND (:manager=1 OR g.role='manager' OR g.user_id=:u) ORDER BY g.role,u.username"),
        {'o': cls['org_id'], 'c': cls['cost_center_id'], 'u': principal['id'], 'manager': int(cls['can_manage'])}).mappings().all()
    return {'items': [dict(r) for r in rows]}


def create_class(db, principal, body):
    center, _, _ = center_access(db, principal, body.cost_center_id, manage=True, write=True)
    values = {**body.model_dump(), 'id': str(uuid.uuid4()), 'org': center['org_id'], 'actor': principal['id']}
    db.execute(text('INSERT INTO education_classes(id,org_id,cost_center_id,title,term,created_by) '
                    'VALUES(:id,:org,:cost_center_id,:title,:term,:actor)'), values)
    audit(db, principal, 'class.create', 'class', values['id'], center['id'])
    result = class_access(db, principal, values['id'])
    db.commit()
    return result


def update_class(db, principal, class_id, body):
    cls = class_access(db, principal, class_id, manage=True, write=True)
    values = {**cls, **body.model_dump(exclude_unset=True)}
    changed = db.execute(text('UPDATE education_classes SET title=:title,term=:term,state=:state,revision=revision+1 '
        'WHERE id=:id AND org_id=:o AND revision=:revision'), {**values, 'id': class_id, 'o': cls['org_id']})
    if changed.rowcount != 1:
        raise refused('Class changed; reload required', 409)
    audit(db, principal, 'class.update', 'class', class_id, cls['cost_center_id'], body.revision + 1)
    result = class_access(db, principal, class_id)
    db.commit()
    return result


def assignment_projection(row):
    result = dict(row)
    for name in ('allowed_materials', 'auto_printer_ids'):
        result[name] = json.loads(result[name])
    return result


def assignments(db, principal, class_id, *, offset=0, limit=50):
    cls = class_access(db, principal, class_id)
    rows = db.execute(text('SELECT * FROM education_assignments WHERE class_id=:c AND org_id=:o '
        'ORDER BY created_at DESC,id LIMIT :limit OFFSET :offset'),
        {'c': class_id, 'o': cls['org_id'], 'limit': limit + 1, 'offset': offset}).mappings().all()
    return {'items': [assignment_projection(r) for r in rows[:limit]],
            'next_offset': offset + limit if len(rows) > limit else None}


def save_assignment(db, principal, class_id, body, assignment_id=None):
    cls = class_access(db, principal, class_id, manage=True, write=True)
    if cls['state'] != 'active':
        raise refused('Class is archived', 409)
    if assignment_id:
        old, _ = assignment_access(db, principal, assignment_id, manage=True, write=True)
        if old['class_id'] != class_id or old['revision'] != body.revision:
            raise refused('Assignment changed; reload required', 409)
    from modules.organizations.education_classes_schemas import AssignmentCreate
    revision = body.revision if assignment_id else 0
    if assignment_id:
        merged = {key: value for key, value in assignment_projection(old).items() if key in ASSIGNMENT_FIELDS}
        merged.update(body.model_dump(exclude={'revision'}, exclude_unset=True))
        from pydantic import ValidationError
        from fastapi import HTTPException
        try:
            body = AssignmentCreate.model_validate(merged)
        except ValidationError:
            raise HTTPException(status_code=422, detail='Assignment changes must form a complete valid policy') from None
    for printer in body.auto_printer_ids:
        if not db.execute(text('SELECT 1 FROM education_cost_center_printers e JOIN printers p '
            "ON p.id=e.printer_id AND p.org_id=e.org_id WHERE e.org_id=:o AND e.cost_center_id=:c "
            "AND e.printer_id=:p AND e.state='active' AND p.is_active IS TRUE AND p.shared IS NOT TRUE"),
            {'o': cls['org_id'], 'c': cls['cost_center_id'], 'p': printer}).first():
            raise refused('Auto-approval printer not entitled to this class', 409)
    values = body.model_dump(exclude={'revision'})
    values['due_at'] = body.due_at.isoformat() if body.due_at else None
    for name in ('allowed_materials', 'auto_printer_ids'):
        values[name] = json.dumps(values[name])
    values.update(id=assignment_id or str(uuid.uuid4()), org=cls['org_id'], cls=class_id, actor=principal['id'])
    # Only schema-owned column identifiers are interpolated; request values are bound.
    fields = tuple(ASSIGNMENT_FIELDS)
    if assignment_id:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fields is the fixed ASSIGNMENT_FIELDS tuple; column and bind names never come from body keys; values are bound
        db.execute(text('UPDATE education_assignments SET ' + ','.join(f'{f}=:{f}' for f in fields) +
            ',authorized_by=:actor,revision=revision+1 WHERE id=:id AND org_id=:org'), values)
    else:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fields is the fixed ASSIGNMENT_FIELDS tuple; column and bind names never come from body keys; values are bound
        db.execute(text('INSERT INTO education_assignments(id,org_id,class_id,authorized_by,' + ','.join(fields) +
            ') VALUES(:id,:org,:cls,:actor,' + ','.join(':' + f for f in fields) + ')'), values)
    audit(db, principal, 'assignment.update' if assignment_id else 'assignment.create', 'assignment',
          values['id'], cls['cost_center_id'], revision + 1)
    row, _ = assignment_access(db, principal, values['id'])
    db.commit()
    return assignment_projection(row)


ASSIGNMENT_FIELDS = ('title', 'brief', 'due_at', 'max_print_seconds', 'max_material_grams',
                     'allowed_materials', 'submission_quota', 'auto_print_seconds',
                     'auto_material_grams', 'auto_printer_ids', 'state')
