"""Live training authority shared by uploads, reviews, scheduling and dispatch.

Requirements span every class sharing a printer and optional school-wide scopes. They continue to
protect existing work if the training entitlement is removed; it cannot be used
as a downgrade bypass. No matching requirement preserves previous behavior.
"""
from datetime import datetime, timezone
import json
from uuid import uuid4
from sqlalchemy import text
from core.errors import ErrorCode, OdinError


def _adapter(db):
    from modules.organizations.education_policy import _ExecutionAdapter
    return _ExecutionAdapter(db)


def utc(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def record_status(training, record, now=None):
    now = now or datetime.now(timezone.utc)
    if not record:
        return 'required'
    if record['revoked_at']:
        return 'revoked'
    if record['training_revision'] != training['revision']:
        return 'outdated'
    if record['expires_at'] and utc(record['expires_at']) <= now:
        return 'expired'
    if record['completed_at']:
        return 'complete'
    if json.loads(training['quiz_json']) and not record['quiz_passed_at']:
        return 'quiz_required'
    return 'instructor_required'


def manager_or_admin(db, *, org_id, center_id, user_id):
    a = _adapter(db)
    user = a.fetchone('SELECT role,group_id,is_active FROM users WHERE id=:id', {'id': user_id})
    if not user or not user['is_active'] or user['group_id'] != org_id:
        return False
    return user['role'] == 'admin' or bool(a.fetchone(
        "SELECT 1 FROM education_cost_center_grants WHERE org_id=:org AND cost_center_id=:center "
        "AND user_id=:user AND role='manager' AND state='active'",
        {'org': org_id, 'center': center_id, 'user': user_id}))


def tenant_admin(db, *, org_id, user_id):
    user = _adapter(db).fetchone('SELECT role,group_id,is_active FROM users WHERE id=:id', {'id': user_id})
    return bool(user and user['is_active'] and user['group_id'] == org_id and user['role'] == 'admin')


def managed_students(db, *, org_id, user_id):
    """Current students of any active class managed by this live tenant user."""
    return {row['user_id'] for row in _adapter(db).fetchall(
        "SELECT DISTINCT s.user_id FROM education_cost_center_grants m "
        "JOIN users u ON u.id=m.user_id AND u.group_id=m.org_id AND u.is_active IS TRUE "
        "JOIN education_cost_centers c ON c.id=m.cost_center_id AND c.org_id=m.org_id AND c.state='active' "
        "JOIN education_cost_center_grants s ON s.cost_center_id=m.cost_center_id AND s.org_id=m.org_id "
        "JOIN users student ON student.id=s.user_id AND student.group_id=s.org_id "
        "WHERE m.org_id=:org AND m.user_id=:user AND m.role='manager' AND m.state='active' "
        "AND s.role='student' AND s.state='active'", {'org': org_id, 'user': user_id})}


def lock_center(db, center_id, org_id):
    # Bound waits BEFORE acquiring either authority lock. School serialization
    # also protects cross-class requirements and newly inserted definitions.
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session
    dialect = db.get_bind().dialect.name if isinstance(db, Session) else db.dialect.name if isinstance(db, Connection) else 'sqlite'
    a = _adapter(db)
    if dialect == 'postgresql':
        current = int(a.fetchone("SELECT setting FROM pg_settings WHERE name='lock_timeout'")['setting'])
        a.execute("SELECT set_config('lock_timeout',:bound,true)", {'bound': str(min(current, 1000) if current else 1000) + 'ms'})
    a.execute('UPDATE groups SET id=id WHERE id=:org', {'org': org_id})
    a.execute('UPDATE education_cost_centers SET revision=revision WHERE id=:id AND org_id=:org',
                         {'id': center_id, 'org': org_id})
    if dialect == 'postgresql':
        # Successful lock acquisition must not change unrelated work's timeout.
        # A failed PostgreSQL statement aborts the transaction; rollback restores it.
        a.execute("SELECT set_config('lock_timeout',:bound,true)", {'bound': str(current) + 'ms'})


def missing_training(db, *, org_id, center_id, user_id, printer_id, lock=False):
    if lock:
        lock_center(db, center_id, org_id)
    a = _adapter(db)
    if manager_or_admin(db, org_id=org_id, center_id=center_id, user_id=user_id):
        return []
    printer = a.fetchone('SELECT machine_type,model FROM printers WHERE id=:id AND org_id=:org',
                        {'id': printer_id, 'org': org_id})
    if not printer:
        return []  # Printer entitlement is enforced separately by existing policy.
    from modules.organizations.education_training_scope import applicable_trainings, scope_matches
    requirements = applicable_trainings(db, org_id, printer_id)
    missing = []
    for training in requirements:
        if not scope_matches(training, printer):
            continue
        record = a.fetchone('SELECT * FROM education_training_records WHERE training_id=:id AND user_id=:user AND org_id=:org',
                            {'id': training['id'], 'user': user_id, 'org': org_id})
        status = record_status(training, record)
        if status != 'complete':
            missing.append({'id': training['id'], 'name': training['name'], 'status': status})
    return missing


def refuse_training(missing):
    raise OdinError(ErrorCode.training_required,
                    'Training required: ' + ', '.join(t['name'] for t in missing),
                    status=403, extra={'required_trainings': missing})


def require_printer_training(db, *, org_id, center_id, user_id, printer_id, lock=True):
    missing = missing_training(db, org_id=org_id, center_id=center_id, user_id=user_id, printer_id=printer_id, lock=lock)
    if missing:
        refuse_training(missing)


def require_submission_training(db, *, principal, cost_center_id):
    """No selected printer yet: at least one authorized destination must be trained."""
    org = principal['group_id']
    lock_center(db, cost_center_id, org)
    a = _adapter(db)
    printers = a.fetchall(
        "SELECT p.id FROM printers p JOIN education_cost_center_printers e ON e.printer_id=p.id AND e.org_id=p.org_id "
        "WHERE e.cost_center_id=:center AND e.org_id=:org AND e.state='active' AND p.is_active IS TRUE AND p.shared IS NOT TRUE",
        {'center': cost_center_id, 'org': org})
    missing = {}
    for printer in printers:
        requirements = missing_training(db, org_id=org, center_id=cost_center_id, user_id=principal['id'], printer_id=printer['id'])
        if not requirements:
            return
        missing.update((t['id'], t) for t in requirements)
    if missing:
        refuse_training(list(missing.values()))


def job_missing_training(db, *, job_id, printer_id=None, lock=False):
    a = _adapter(db)
    row = a.fetchone('SELECT org_id,cost_center_id,submitted_by,approved_printer_id FROM education_submissions WHERE job_id=:id', {'id': job_id})
    if not row:
        return []
    return missing_training(db, org_id=row['org_id'], center_id=row['cost_center_id'], user_id=row['submitted_by'],
                                printer_id=printer_id or row['approved_printer_id'], lock=lock)


def job_training_allowed(db, **kwargs):
    return not job_missing_training(db, **kwargs)


def export_training_data(db, user_id):
    """Export numeric evidence, never quiz answer keys or other students' records."""
    a = _adapter(db)
    records = a.fetchall('SELECT r.*,t.name FROM education_training_records r JOIN education_trainings t ON t.id=r.training_id '
                         'WHERE r.user_id=:id ORDER BY r.training_id,r.user_id', {'id': user_id})
    # Filter subjects in SQL so exports do not materialize the school's audit.
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session
    dialect = db.get_bind().dialect.name if isinstance(db, Session) else db.dialect.name if isinstance(db, Connection) else 'sqlite'
    subject = "CAST(details_json AS jsonb)->>'user_id'" if dialect == 'postgresql' else "CAST(json_extract(details_json,'$.user_id') AS TEXT)"
    events = a.fetchall("SELECT event_id,org_id,actor_id,action,resource_id,details_json,result_json,created_at FROM education_audit_events "
                        "WHERE resource_type='training' AND (actor_id=:actor OR " + subject + "=:actor) ORDER BY created_at,event_id",  # constant dialect expressions; bound identity
                        {'actor': str(user_id)})
    return {'records': records, 'events': events}


def erase_training_data(db, user_id, actor_id):
    """Keep Education's immutable numeric audit history; revoke current proof."""
    rows = db.execute(text('SELECT training_id,org_id FROM education_training_records WHERE user_id=:id'), {'id': user_id}).all()
    db.execute(text('UPDATE education_training_records SET revoked_at=COALESCE(revoked_at,CURRENT_TIMESTAMP) WHERE user_id=:id'), {'id': user_id})
    from modules.organizations.education_admin_support import write_audit
    for row in rows:
        write_audit(db, org_id=row.org_id, actor_id=actor_id, action='training.erase', command_id=str(uuid4()),
                    request_digest='', resource_type='training', resource_id=row.training_id, cost_center_id=None,
                    lifecycle_revision=None, details={'user_id': user_id}, result={})
