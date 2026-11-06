"""Transactional training administration, assessment and student projections."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import text
from core.errors import ErrorCode, OdinError
from modules.organizations.education_admin_support import mutation_context, complete_command, write_audit, reload_required
from modules.organizations.education_membership import retain_membership
from modules.organizations.education_training_policy import lock_center, managed_students, manager_or_admin, record_status, tenant_admin, utc
from modules.organizations.education_training_scope import matched_printers, scope_printers, student_in_scope


def not_found():
    return OdinError(ErrorCode.not_found, 'Training or class not found', status=404)


def center_access(db, principal, center_id, *, manage=False, lock=False):
    org = principal.get('group_id')
    if org is None:
        raise not_found()
    if lock:
        lock_center(db, center_id, org)
    center = db.execute(text("SELECT id FROM education_cost_centers WHERE id=:id AND org_id=:org AND state='active'"),
                        {'id': center_id, 'org': org}).first()
    user = db.execute(text('SELECT group_id,is_active FROM users WHERE id=:id'), {'id': principal['id']}).first()
    if not center or not user or user.group_id != org or not user.is_active:
        raise not_found()
    manager = manager_or_admin(db, org_id=org, center_id=center_id, user_id=principal['id'])
    student = db.execute(text("SELECT 1 FROM education_cost_center_grants WHERE cost_center_id=:center AND org_id=:org "
                              "AND user_id=:user AND role='student' AND state='active'"),
                         {'center': center_id, 'org': org, 'user': principal['id']}).first()
    if not manager and (manage or not student):
        raise not_found()
    return org, manager


def training_access(db, principal, training_id, *, manage=False, lock=False, assessment=False, student_id=None):
    row = db.execute(text('SELECT * FROM education_trainings WHERE id=:id AND org_id=:org'),
                     {'id': str(training_id), 'org': principal.get('group_id')}).mappings().first()
    if not row:
        raise not_found()
    org = row['org_id']
    if lock:
        lock_center(db, row['cost_center_id'], org)
        # Decide authority using the definition protected by the school lock.
        row = db.execute(text('SELECT * FROM education_trainings WHERE id=:id AND org_id=:org'),
                         {'id': str(training_id), 'org': org}).mappings().one()
    if assessment and row['school_wide']:
        if not tenant_admin(db, org_id=org, user_id=principal['id']):
            students = managed_students(db, org_id=org, user_id=principal['id'])
            if not students or (student_id is not None and student_id not in students):
                raise not_found()
        return dict(row), True
    manager = manager_or_admin(db, org_id=org, center_id=row['cost_center_id'], user_id=principal['id'])
    if manage or manager:
        center_access(db, principal, row['cost_center_id'], manage=manage)
    elif not student_in_scope(db, row, principal['id']):
        raise not_found()
    # Re-read after the center lock: another writer may have revised the quiz.
    row = db.execute(text('SELECT * FROM education_trainings WHERE id=:id AND org_id=:org'),
                     {'id': str(training_id), 'org': org}).mappings().one()
    return dict(row), manager


def projection(db, training, user_id, *, manager=False):
    result = dict(training)
    quiz = json.loads(result.pop('quiz_json'))
    result['quiz'] = quiz if manager else [{k: v for k, v in q.items() if k != 'correct'} for q in quiz]
    record = db.execute(text('SELECT * FROM education_training_records WHERE training_id=:id AND user_id=:user AND org_id=:org'),
                        {'id': training['id'], 'user': user_id, 'org': training['org_id']}).mappings().first()
    result['status'] = record_status(training, record)
    result['record'] = dict(record) if record else None
    result['can_manage'] = manager
    result['matched_printers'] = [{'id': p['id'], 'name': p['name']} for p in matched_printers(db, training)]
    return result


def audit(db, principal, training, action, *, command_id=None, digest='', details=None, result=None):
    write_audit(db, org_id=training['org_id'], actor_id=principal['id'], action=action,
                command_id=command_id or str(uuid4()), request_digest=digest, resource_type='training',
                resource_id=training['id'], cost_center_id=training['cost_center_id'],
                lifecycle_revision=training['revision'], details=details or {}, result=result or {})


def finish(db, principal, training, action, command_id, digest, result, details=None):
    audit(db, principal, training, action, command_id=command_id, digest=digest, details=details, result=result)
    complete_command(db, org_id=training['org_id'], actor_id=principal['id'], action=action,
                     command_id=command_id, result=result)
    db.commit()
    from fastapi.encoders import jsonable_encoder
    return jsonable_encoder(result)


def list_trainings(db, principal, center_id):
    org, manager = center_access(db, principal, center_id)
    rows = db.execute(text('SELECT * FROM education_trainings WHERE org_id=:org ORDER BY name,id'), {'org': org}).mappings().all()
    class_scope = {'org_id': org, 'cost_center_id': center_id, 'school_wide': False}
    printers = scope_printers(db, class_scope)
    printer_ids = {p['id'] for p in printers}
    items = []
    for row in rows:
        owns = manager and row['cost_center_id'] == center_id
        if not row['active'] and not owns:
            continue
        if row['cost_center_id'] != center_id and not row['school_wide'] and not printer_ids.intersection(p['id'] for p in matched_printers(db, row)):
            continue
        can_manage = manager_or_admin(db, org_id=org, center_id=row['cost_center_id'], user_id=principal['id'])
        if row['school_wide']:
            can_manage = manager
        item = projection(db, row, principal['id'], manager=can_manage)
        item['can_edit'] = can_manage and (not row['school_wide'] or tenant_admin(db, org_id=org, user_id=principal['id']))
        items.append(item)
    return {'items': items, 'can_manage': manager, 'can_manage_school_wide': tenant_admin(db, org_id=org, user_id=principal['id']), 'printers': printers,
            'school_printers': scope_printers(db, {**class_scope, 'school_wide': True}) if manager else []}


def save_training(db, principal, body, *, center_id=None, training_id=None):
    if training_id:
        old, _ = training_access(db, principal, training_id, manage=True, lock=True)
        center_id = old['cost_center_id']
        org = old['org_id']
    else:
        org, _ = center_access(db, principal, center_id, manage=True, lock=True)
        old = None
    if (body.school_wide or (old and old['school_wide'])) and not tenant_admin(db, org_id=org, user_id=principal['id']):
        raise OdinError(ErrorCode.permission_denied, 'Only a tenant admin may change school-wide training definitions', status=403)
    action = 'training.update' if old else 'training.create'
    # Bind path identity as well as payload to the idempotency digest.
    action += ':' + (str(training_id) if old else str(center_id))
    command_id, digest, replay = mutation_context(db, body, principal, org, action)
    if replay is not None:
        return replay
    if old and (old['revision'] != body.revision or old['edit_version'] != body.edit_version):
        raise reload_required()
    retain_membership(db, principal['id'], org)
    training = {'id': str(training_id or uuid4()), 'org_id': org, 'cost_center_id': center_id,
                'school_wide': body.school_wide, 'name': body.name, 'description': body.description, 'scope_kind': body.scope_kind,
                'scope_value': body.scope_value, 'quiz_json': json.dumps([q.model_dump() for q in body.quiz]),
                'pass_mark': body.pass_mark, 'instructor_required': body.instructor_required,
                'expiry_days': body.expiry_days, 'active': body.active, 'revision': old['revision'] if old else 1,
                'edit_version': old['edit_version'] + 1 if old else 1, 'created_by': principal['id']}
    if not matched_printers(db, training) and body.active:
        raise OdinError(ErrorCode.validation_failed, 'Training scope must match at least one authorized printer', status=422)
    requirement_fields = ('school_wide', 'scope_kind', 'scope_value', 'pass_mark', 'instructor_required')
    if old and (any(old[key] != training[key] for key in requirement_fields) or json.loads(old['quiz_json']) != json.loads(training['quiz_json'])):
        training['revision'] += 1
    if old:
        db.execute(text('UPDATE education_trainings SET school_wide=:school_wide,name=:name,description=:description,scope_kind=:scope_kind,scope_value=:scope_value,'
                        'quiz_json=:quiz_json,pass_mark=:pass_mark,instructor_required=:instructor_required,expiry_days=:expiry_days,'
                        'active=:active,revision=:revision,edit_version=:edit_version WHERE id=:id AND org_id=:org_id'), training)
    else:
        db.execute(text('INSERT INTO education_trainings(id,org_id,cost_center_id,school_wide,name,description,scope_kind,scope_value,quiz_json,'
                        'pass_mark,instructor_required,expiry_days,active,revision,edit_version,created_by) VALUES '
                        '(:id,:org_id,:cost_center_id,:school_wide,:name,:description,:scope_kind,:scope_value,:quiz_json,:pass_mark,'
                        ':instructor_required,:expiry_days,:active,:revision,:edit_version,:created_by)'), training)
    # Audit snapshots deliberately contain no editable prose or answer keys.
    return finish(db, principal, training, action, command_id, digest, {'id': training['id'], 'revision': training['revision'], 'edit_version': training['edit_version']})


def _student(db, training, user_id):
    if not student_in_scope(db, training, user_id):
        raise not_found()


def assess(db, principal, training_id, body, *, kind, user_id=None):
    training, _ = training_access(db, principal, training_id, manage=kind != 'quiz', lock=True,
                                  assessment=kind != 'quiz', student_id=user_id)
    target = principal['id'] if kind == 'quiz' else user_id
    if kind != 'revoke':
        _student(db, training, target)
    action = f'training.{kind}:{training_id}:{target}'
    command_id, digest, replay = mutation_context(db, body, principal, training['org_id'], action)
    if replay is not None:
        return replay
    if body.revision != training['revision']:
        raise reload_required()
    if not training['active']:
        raise OdinError(ErrorCode.invalid_state_transition, 'Training is archived', status=409)
    if kind != 'revoke':
        retain_membership(db, target, training['org_id'])
    retain_membership(db, principal['id'], training['org_id'])
    row = db.execute(text('SELECT * FROM education_training_records WHERE training_id=:id AND user_id=:user'),
                     {'id': training['id'], 'user': target}).mappings().first()
    if kind == 'revoke' and not row:
        raise not_found()
    now = datetime.now(timezone.utc)
    status = record_status(training, row, now)
    record = dict(row) if row and (kind == 'revoke' or status not in ('expired', 'outdated', 'revoked')) else {
        'training_id': training['id'], 'org_id': training['org_id'], 'user_id': target,
        'training_revision': training['revision'], 'quiz_attempts': row['quiz_attempts'] if row else 0,
        'failed_attempts': 0, 'cooldown_until': None, 'quiz_score': None, 'quiz_passed_at': None,
        'instructor_id': None, 'instructor_at': None, 'completed_at': None, 'expires_at': None, 'revoked_at': None}
    quiz = json.loads(training['quiz_json'])
    if kind == 'revoke':
        record['revoked_at'] = record['revoked_at'] or now
    elif status == 'complete':
        raise OdinError(ErrorCode.invalid_state_transition, 'Training is already complete; revoke before reassessing', status=409)
    elif kind == 'quiz':
        if record['quiz_passed_at']:
            raise OdinError(ErrorCode.invalid_state_transition, 'Quiz already passed; awaiting instructor sign-off', status=409)
        if record['cooldown_until'] and utc(record['cooldown_until']) > now:
            raise OdinError(ErrorCode.rate_limited, 'Too many failed attempts. Try again after the cooldown.', status=429,
                            extra={'cooldown_until': utc(record['cooldown_until']).isoformat()})
        if record['cooldown_until']:
            record['failed_attempts'] = 0
            record['cooldown_until'] = None
        if not quiz or len(body.answers) != len(quiz) or any(type(a) is not int or not 0 <= a < len(q['choices']) for a, q in zip(body.answers, quiz)):
            raise OdinError(ErrorCode.validation_failed, 'Answer every quiz question with a valid choice', status=422)
        score = sum(a == q['correct'] for a, q in zip(body.answers, quiz)) * 100 // len(quiz)
        record['quiz_attempts'] += 1
        record['failed_attempts'] = record['failed_attempts'] + 1 if score < training['pass_mark'] else 0
        if record['failed_attempts'] >= 3:
            record['cooldown_until'] = now + timedelta(minutes=15)
        record['quiz_score'] = score
        record['quiz_passed_at'] = now if score >= training['pass_mark'] else None
    elif kind == 'signoff':
        if not training['instructor_required']:
            raise OdinError(ErrorCode.invalid_state_transition, 'This training requires a quiz only', status=409)
        record['instructor_id'], record['instructor_at'] = principal['id'], now
    else:
        raise ValueError('Unknown training assessment')
    if kind != 'revoke' and (not quiz or record['quiz_passed_at']) and (not training['instructor_required'] or record['instructor_at']):
        record['completed_at'] = now
        record['expires_at'] = now + timedelta(days=training['expiry_days']) if training['expiry_days'] else None
    db.execute(text('INSERT INTO education_training_records(training_id,org_id,user_id,training_revision,quiz_attempts,failed_attempts,cooldown_until,quiz_score,quiz_passed_at,'
                    'instructor_id,instructor_at,completed_at,expires_at,revoked_at) VALUES '
                    '(:training_id,:org_id,:user_id,:training_revision,:quiz_attempts,:failed_attempts,:cooldown_until,:quiz_score,:quiz_passed_at,:instructor_id,:instructor_at,:completed_at,:expires_at,:revoked_at) '
                    'ON CONFLICT(training_id,user_id) DO UPDATE SET training_revision=excluded.training_revision,quiz_score=excluded.quiz_score,'
                    'quiz_attempts=excluded.quiz_attempts,failed_attempts=excluded.failed_attempts,cooldown_until=excluded.cooldown_until,'
                    'quiz_passed_at=excluded.quiz_passed_at,instructor_id=excluded.instructor_id,instructor_at=excluded.instructor_at,'
                    'completed_at=excluded.completed_at,expires_at=excluded.expires_at,revoked_at=excluded.revoked_at'), record)
    result = {'training_id': training['id'], 'user_id': target, 'status': record_status(training, record, now),
              'quiz_score': record['quiz_score'], 'expires_at': record['expires_at'],
              'quiz_attempts': record['quiz_attempts'], 'cooldown_until': record['cooldown_until']}
    return finish(db, principal, training, action, command_id, digest, result,
                  {'user_id': target, 'reason': getattr(body, 'reason', 'quiz')})


def roster(db, principal, training_id):
    training, _ = training_access(db, principal, training_id, manage=True, assessment=True)
    from modules.organizations.education_training_roster import roster_items
    items = roster_items(db, training)
    if training['school_wide'] and not tenant_admin(db, org_id=training['org_id'], user_id=principal['id']):
        students = managed_students(db, org_id=training['org_id'], user_id=principal['id'])
        items = [item for item in items if item['user_id'] in students]
    return {'items': items}
