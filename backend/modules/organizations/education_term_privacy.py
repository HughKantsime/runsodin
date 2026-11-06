"""Term data export and conservative graduation selection; existing GDPR owns erasure."""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from modules.organizations.education_term_retention import authority, policy, submissions, audit, parsed, now


def export_term_data(db, user_id):
    params = {'uid': user_id}
    return {
        'policies_created': [dict(r) for r in db.execute(text(
            'SELECT * FROM education_term_policies WHERE created_by=:uid'), params).mappings()],
        'content_cleanup': [dict(r) for r in db.execute(text(
            'SELECT c.* FROM education_term_cleanup c JOIN education_submissions s ON s.id=c.submission_id '
            'WHERE s.submitted_by=:uid'), params).mappings()],
    }


def erase_term_identity(db, user_id):
    db.execute(text('UPDATE education_term_policies SET created_by=NULL WHERE created_by=:uid'), {'uid': user_id})


def eligible_student(db, uid, org, *, lock=False):
    suffix = ' FOR UPDATE' if lock and db.get_bind().dialect.name == 'postgresql' else ''
    try:
        row = db.execute(text('SELECT role,is_active,group_id,username FROM users WHERE id=:uid' + suffix), {'uid': uid}).first()
    except DBAPIError as error:
        if suffix and (getattr(error.orig, 'sqlstate', None) or getattr(error.orig, 'pgcode', None)) == '55P03':
            db.rollback()
            raise HTTPException(409, 'student_busy_retry') from None
        raise
    if row and row.username == f'[deleted-{uid}]' and not db.execute(
        text('SELECT 1 FROM education_erasure_pending WHERE user_id=:uid'), {'uid': uid}).first():
        return False
    if not row or row.role != 'viewer' or row.is_active or row.group_id != org:
        return False
    if db.execute(text("SELECT 1 FROM education_cost_center_grants WHERE user_id=:uid AND state='active'"), {'uid': uid}).first():
        return False
    if db.execute(text("SELECT 1 FROM jobs WHERE (submitted_by=:uid OR charged_to_user_id=:uid) "
                       "AND status NOT IN ('completed','failed','cancelled','rejected')"), {'uid': uid}).first():
        return False
    return True


def term_student_ids(db, term):
    students = {r['submitted_by'] for r in submissions(db, term)}
    grants = db.execute(text("SELECT user_id,granted_at,revoked_at FROM education_cost_center_grants "
        "WHERE org_id=:org AND cost_center_id=:center AND role='student'"),
        {'org': term['org_id'], 'center': term['cost_center_id']})
    students.update(r.user_id for r in grants if parsed(r.granted_at) < parsed(term['ends_at']) and
                    (r.revoked_at is None or parsed(r.revoked_at) >= parsed(term['starts_at'])))
    return students


def pending_erasures(db, org):
    # Never expose the private original identifiers retained for recovery.
    rows = db.execute(text(
        'SELECT u.id AS user_id,u.username FROM education_erasure_pending p '
        'JOIN users u ON u.id=p.user_id WHERE u.group_id=:org ORDER BY u.id'), {'org': org}).mappings().all()
    return [dict(row) for row in rows if eligible_student(db, row['user_id'], org)]


def erase_student(db, uid, org, principal, term_id=None, *, resume=False):
    from modules.organizations.gdpr_erasure import erase_user_data_sync
    try:
        authority(db, principal, org, lock=True)
        if not eligible_student(db, uid, org, lock=True):
            raise HTTPException(409, 'student_eligibility_changed')
        if resume and not db.execute(text('SELECT 1 FROM education_erasure_pending WHERE user_id=:uid'), {'uid': uid}).first():
            raise HTTPException(409, 'no_pending_erasure')
        erase_user_data_sync(uid, current_user=principal, db=db)
        audit(db, 'student_anonymized', term_id, {'user_id': uid})
        db.commit()
        return {'user_id': uid, 'state': 'anonymized'}
    except Exception as error:
        db.rollback()
        if isinstance(error, HTTPException) and error.detail == 'student_busy_retry':
            raise
        pending = db.execute(text('SELECT 1 FROM education_erasure_pending WHERE user_id=:uid'), {'uid': uid}).first()
        reason = 'database_error' if isinstance(error, SQLAlchemyError) else 'erasure_failed'
        if isinstance(error, HTTPException):
            if error.detail in ('student_eligibility_changed', 'no_pending_erasure'):
                reason = error.detail
            elif error.status_code == 403:
                reason = 'administrator_authority_changed'
        result = {'user_id': uid, 'state': 'erasure_incomplete'} if pending else {
            'user_id': uid, 'state': 'blocked', 'reason': reason}
        try:
            audit(db, 'anonymization_incomplete' if pending else 'anonymization_blocked', term_id,
                  {'user_id': uid, 'reason': 'erasure_incomplete' if pending else reason})
            db.commit()
        except Exception:
            db.rollback()
        return result


def resume_erasure(db, uid, principal, org_id=None):
    org = authority(db, principal, org_id, lock=True)
    if not eligible_student(db, uid, org, lock=True) or not db.execute(
        text('SELECT 1 FROM education_erasure_pending WHERE user_id=:uid'), {'uid': uid}).first():
        raise HTTPException(409, 'No eligible pending erasure in this school')
    audit(db, 'erasure_resumed', None, {'user_id': uid, 'org_id': org})
    db.commit()
    return erase_student(db, uid, org, principal, resume=True)


def anonymize(db, term_id, user_ids, fingerprint, principal, org_id=None, *, after_id=0):
    from modules.organizations.education_term_retention import preview
    org = authority(db, principal, org_id, lock=True)
    term = policy(db, term_id, org)
    plan = preview(db, term, after_id=after_id)
    if fingerprint != plan['fingerprint']:
        raise HTTPException(409, 'Dry run changed; preview again')
    if now() < parsed(term['ends_at']):
        raise HTTPException(409, 'Term has not ended')
    term_students = term_student_ids(db, term)
    if not user_ids or len(set(user_ids)) != len(user_ids) or any(
        uid not in term_students or not eligible_student(db, uid, org, lock=True) for uid in sorted(user_ids)
    ):
        raise HTTPException(409, 'Select only inactive students from this term without active grants or jobs')
    audit(db, 'graduation_started', term_id, {'user_ids': user_ids})
    db.commit()
    return {'results': [erase_student(db, uid, org, principal, term_id) for uid in user_ids]}
