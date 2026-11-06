"""Atomic assignment admission and assessments; existing review owns print approval."""
import json
import math
import uuid
from datetime import datetime, timezone
from fastapi import HTTPException
from sqlalchemy import text
from core.errors import OdinError
from license_manager import require_feature
from modules.organizations.education_classes_service import assignment_access, audit, refused


def check_limits(assignment, file):
    """Unknown/nonfinite estimates never satisfy a configured bound."""
    for key, column, label in (('max_print_seconds', 'print_time_seconds', 'print time'),
                               ('max_material_grams', 'total_weight_grams', 'material weight')):
        bound = assignment[key]
        value = file[column]
        if bound is not None and (value is None or not math.isfinite(float(value)) or value <= 0 or value > bound):
            raise refused('Assignment ' + label + ' limit exceeded or estimate unavailable', 409)
    allowed = set(json.loads(assignment['allowed_materials']))
    if allowed:
        try:
            filaments = json.loads(file['filaments_json'] or '[]')
            materials = {str(f['type']).strip().upper() for f in filaments}
        except (ValueError, TypeError, KeyError):
            materials = set()
        if not materials or not materials <= allowed:
            raise refused('Assignment requires an allowed material for every filament', 409)


def validate_assigned_approval(db, row, *, print_file_id=None):
    """Called by ordinary and explicit-AMS review, including changed payloads."""
    assignment = db.execute(text('SELECT a.* FROM education_assignments a '
        'JOIN education_assignment_submissions x ON x.assignment_id=a.id AND x.org_id=a.org_id '
        'WHERE x.submission_id=:s AND x.org_id=:o'), {'s': row.id, 'o': row.org_id}).mappings().first()
    if assignment is None:
        return
    # Existing linked work remains reviewable after the classes entitlement lapses.
    # Same assignment lock as attachment/edit. Re-read after acquiring it.
    db.execute(text('UPDATE education_assignments SET revision=revision WHERE id=:a AND org_id=:o'),
               {'a': assignment['id'], 'o': row.org_id})
    assignment = db.execute(text('SELECT * FROM education_assignments WHERE id=:a AND org_id=:o'),
                            {'a': assignment['id'], 'o': row.org_id}).mappings().one()
    file = db.execute(text('SELECT * FROM print_files WHERE id=:f AND org_id=:o'),
                      {'f': print_file_id or row.print_file_id, 'o': row.org_id}).mappings().first()
    if not file:
        raise refused('Assignment file not found')
    check_limits(assignment, file)
    # Approval consumes quota exactly once, inside the existing review transaction
    # and assignment lock. Failed/rejected attempts before approval consume none.
    check_quota(db, assignment, row.submitted_by, exclude_submission=row.id)
    db.execute(text('UPDATE education_assignment_submissions '
        'SET first_approved_at=COALESCE(first_approved_at,CURRENT_TIMESTAMP) '
        'WHERE submission_id=:s AND org_id=:o'), {'s': row.id, 'o': row.org_id})


def check_quota(db, assignment, student_id, *, exclude_submission=-1):
    count = db.execute(text('SELECT COUNT(*) FROM education_assignment_submissions x '
        'JOIN education_submissions s ON s.id=x.submission_id AND s.org_id=x.org_id '
        'WHERE x.assignment_id=:a AND x.org_id=:o AND s.submitted_by=:u '
        'AND x.first_approved_at IS NOT NULL AND x.submission_id<>:exclude'),
        {'a': assignment['id'], 'o': assignment['org_id'], 'u': student_id,
         'exclude': exclude_submission}).scalar_one()
    if assignment['submission_quota'] is not None and count >= assignment['submission_quota']:
        raise refused('Assignment submission quota reached', 409)


def link_submission(db, principal, assignment_id, body):
    a, cls = assignment_access(db, principal, assignment_id, write=True)
    row = db.execute(text('SELECT s.*,f.print_time_seconds,f.total_weight_grams,f.filaments_json '
        'FROM education_submissions s JOIN print_files f ON f.id=s.print_file_id AND f.org_id=s.org_id '
        'WHERE s.id=:s AND s.org_id=:o AND s.submitted_by=:u AND s.cost_center_id=:c'),
        {'s': body.submission_id, 'o': cls['org_id'], 'u': principal['id'], 'c': cls['cost_center_id']}).mappings().first()
    if not row:
        raise refused('Submission not found')
    existing = db.execute(text('SELECT * FROM education_assignment_submissions WHERE submission_id=:s'),
                          {'s': row['id']}).mappings().first()
    if existing:
        if existing['assignment_id'] != assignment_id:
            raise refused('Submission already belongs to another assignment', 409)
        db.commit()
        return {'submission_id': row['id'], 'assignment_id': assignment_id, 'linked': True,
                'auto_approval': row['status']}
    if not cls['can_submit']:
        raise refused('Active student grant required', 403)
    if cls['state'] != 'active' or a['state'] != 'active':
        raise refused('Assignment is closed', 409)
    if a['due_at'] and datetime.fromisoformat(a['due_at']) < datetime.now(timezone.utc):
        raise refused('Assignment due date has passed', 409)
    if row['status'] != 'submitted' or row['lifecycle_revision'] != body.revision:
        raise refused('Only a current pending submission can be linked; reload required', 409)
    check_limits(a, row)
    check_quota(db, a, principal['id'])
    changed = db.execute(text("UPDATE education_submissions SET lifecycle_revision=lifecycle_revision+1 "
        "WHERE id=:s AND org_id=:o AND status='submitted' AND lifecycle_revision=:r "
        "AND EXISTS (SELECT 1 FROM jobs j WHERE j.id=education_submissions.job_id AND j.status='submitted')"),
        {'s': row['id'], 'o': cls['org_id'], 'r': body.revision})
    if changed.rowcount != 1:
        raise refused('Submission changed; reload required', 409)
    db.execute(text('INSERT INTO education_assignment_submissions(submission_id,org_id,assignment_id) '
                    'VALUES(:s,:o,:a)'), {'s': row['id'], 'o': cls['org_id'], 'a': assignment_id})
    audit(db, principal, 'assignment.link', 'submission', row['id'], cls['cost_center_id'])
    db.commit()
    return {'submission_id': row['id'], 'assignment_id': assignment_id, 'linked': True,
            'auto_approval': try_auto_approve(db, principal, assignment_id, row['id'])}


def try_auto_approve(db, student, assignment_id, submission_id):
    """Try each configured printer in order, with fresh authority after a refusal."""
    try:
        a, _ = assignment_access(db, student, assignment_id)
        printers = json.loads(a['auto_printer_ids'])
        db.rollback()
    except (HTTPException, OdinError):
        db.rollback()
        return 'manual_review'
    for printer in printers:
        result = _try_printer(db, student, assignment_id, submission_id, printer)
        if result != 'manual_review':
            return result
    return 'manual_review'


def _try_printer(db, student, assignment_id, submission_id, printer):
    """Retryable after link commit. Never turn an approval refusal into a lost submission."""
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval
    try:
        require_feature('classes')
        a, cls = assignment_access(db, student, assignment_id)
        if not cls['can_submit'] or not json.loads(a['auto_printer_ids']) or a['state'] != 'active' or cls['state'] != 'active':
            db.rollback()
            return 'manual_review'
        teacher = db.execute(text('SELECT id,username,role,group_id,is_active FROM users '
            'WHERE id=:u AND group_id=:o AND is_active IS TRUE'),
            {'u': a['authorized_by'], 'o': cls['org_id']}).mappings().first()
        if teacher is None:
            db.rollback()
            return 'manual_review'
        teacher = dict(teacher)
        # Lock/recheck the configuring instructor, center, class, assignment and rule.
        a, cls = assignment_access(db, teacher, assignment_id, manage=True, write=True)
        _, student_class = assignment_access(db, student, assignment_id)
        if not student_class['can_submit']:
            db.rollback()
            return 'manual_review'
        row = db.execute(text('SELECT s.lifecycle_revision,s.status,f.print_time_seconds,f.total_weight_grams '
            'FROM education_submissions s JOIN print_files f ON f.id=s.print_file_id AND f.org_id=s.org_id '
            'WHERE s.id=:s AND s.org_id=:o AND s.submitted_by=:u AND s.cost_center_id=:c '
            'AND EXISTS (SELECT 1 FROM education_assignment_submissions x WHERE x.submission_id=s.id '
            'AND x.org_id=s.org_id AND x.assignment_id=:a)'),
            {'s': submission_id, 'o': cls['org_id'], 'u': student['id'], 'c': cls['cost_center_id'], 'a': assignment_id}).mappings().one()
        if row['status'] != 'submitted':
            db.rollback()
            return 'already_reviewed'
        if a['authorized_by'] != teacher['id'] or a['state'] != 'active' or cls['state'] != 'active':
            db.rollback()
            return 'manual_review'
        for value, bound in ((row['print_time_seconds'], a['auto_print_seconds']),
                             (row['total_weight_grams'], a['auto_material_grams'])):
            if value is None or bound is None or not math.isfinite(float(value)) or value <= 0 or value > bound:
                db.rollback()
                return 'manual_review'
        # Recheck the current rule for each printer; stale candidates grant no authority.
        if printer not in json.loads(a['auto_printer_ids']) or (a['due_at'] and datetime.fromisoformat(a['due_at']) < datetime.now(timezone.utc)):
            db.rollback()
            return 'manual_review'
        body = SubmissionApproval(revision=row['lifecycle_revision'], printer_id=printer,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f'odin-class:{assignment_id}:{submission_id}:{a["revision"]}:{printer}'))
        audit(db, teacher, 'assignment.auto_approve', 'submission', submission_id, cls['cost_center_id'], automatic=True)
        approve_submission(db, submission_id=submission_id, body=body, principal={**teacher, '_automatic_class_approval': True})
        return 'approved'
    except (HTTPException, OdinError):
        db.rollback()
        return 'manual_review'
    except Exception:
        # The link is durable even if review storage becomes unavailable. Do not leak DB/path details.
        db.rollback()
        return 'review_unavailable'


# Compatibility import for the existing route/service callers.
from modules.organizations.education_assessments import assess  # noqa: E402,F401
