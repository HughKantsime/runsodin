"""Assessment saves append a revision; GDPR is the only history text mutation."""
import uuid
from sqlalchemy import text
from modules.organizations.education_classes_service import assignment_access, audit, refused

def assess(db, principal, assignment_id, submission_id, body):
    a, cls = assignment_access(db, principal, assignment_id, manage=True, write=True, require_active=False)
    old = db.execute(text('SELECT * FROM education_assignment_submissions '
        'WHERE submission_id=:s AND assignment_id=:a AND org_id=:o'),
        {'s': submission_id, 'a': assignment_id, 'o': cls['org_id']}).mappings().first()
    if old is None:
        raise refused('Assessment not found', 404)
    values = {**old, **body.model_dump(exclude_unset=True)}
    changed = db.execute(text('UPDATE education_assignment_submissions SET feedback=:feedback,grade=:grade,'
        'learning_status=:learning_status,reviewed_by=:u,revision=revision+1,updated_at=CURRENT_TIMESTAMP '
        'WHERE submission_id=:s AND assignment_id=:a AND org_id=:o AND revision=:revision '
        'AND EXISTS (SELECT 1 FROM education_submissions s JOIN users u ON u.id=s.submitted_by '
        'WHERE s.id=education_assignment_submissions.submission_id AND u.is_active IS TRUE)'),
        {**values, 'u': principal['id'], 's': submission_id, 'a': assignment_id, 'o': cls['org_id']})
    if changed.rowcount != 1:
        raise refused('Assessment not found or changed; reload required', 409)
    # No assessment edit overwrites a previous revision. The only exception to
    # retained text is the existing GDPR erasure path (including reviewer scrub).
    db.execute(text('INSERT INTO education_assessment_revisions '
        '(id,submission_id,org_id,revision,reviewed_by,old_grade,new_grade,old_feedback,new_feedback,'
        'old_learning_status,new_learning_status) VALUES(:id,:s,:o,:revision,:u,:old_grade,:new_grade,'
        ':old_feedback,:new_feedback,:old_status,:new_status)'),
        {'id': str(uuid.uuid4()), 's': submission_id, 'o': cls['org_id'], 'revision': body.revision + 1,
         'u': principal['id'], 'old_grade': old['grade'], 'new_grade': values['grade'],
         'old_feedback': old['feedback'], 'new_feedback': values['feedback'],
         'old_status': old['learning_status'], 'new_status': values['learning_status']})
    audit(db, principal, 'assignment.assess', 'submission', submission_id, cls['cost_center_id'], body.revision + 1)
    result = dict(db.execute(text('SELECT * FROM education_assignment_submissions WHERE submission_id=:s'),
                             {'s': submission_id}).mappings().one())
    db.commit()
    return result
