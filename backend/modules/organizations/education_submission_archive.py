"""Quiescent, revision-checked soft archives. Never delete Education history."""
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from core.errors import ErrorCode, OdinError
from modules.organizations.education_review_service import (
    _reviewable_submission, _decision_hash, _record_decision,
)
from modules.organizations.education_admin_support import claim_command

TERMINAL = {'completed', 'failed', 'rejected', 'cancelled'}


def archive_submission(db, *, submission_id, body, principal):
    action = 'submission.archive'
    try:
        if db.get_bind().dialect.name == 'sqlite':
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                connection.exec_driver_sql('UPDATE education_submissions SET updated_at=updated_at WHERE 1=0')
        postgres = db.get_bind().dialect.name == 'postgresql'
        actor_params = {'user_id': principal['id'], 'org_id': principal.get('group_id')}
        from modules.organizations.education_ams_teacher_authority import _lock_teacher_row
        _lock_teacher_row(db, postgres, actor_params, 'users',
                          'id=:user_id AND group_id=:org_id AND is_active IS TRUE', 'is_active')
        role = db.execute(text('SELECT role FROM users WHERE id=:user_id AND group_id=:org_id'), actor_params).scalar_one()
        principal = {**principal, 'role': role}
        row = _reviewable_submission(db, submission_id=submission_id, principal=principal)
        params = {'id': row.id, 'org': row.org_id, 'job': row.job_id}
        if role != 'admin':
            if row.center_state != 'active':
                raise OdinError(ErrorCode.not_found, 'Submission not found', status=404)
            _lock_teacher_row(db, postgres, {**actor_params, 'center_id': row.cost_center_id},
                'education_cost_center_grants',
                "org_id=:org_id AND cost_center_id=:center_id AND user_id=:user_id AND role='manager' AND state='active'", 'state')
        if db.get_bind().dialect.name == 'postgresql':
            db.execute(text('SELECT id FROM education_submissions WHERE id=:id AND org_id=:org FOR UPDATE NOWAIT'), params)
            db.execute(text('SELECT id FROM jobs WHERE id=:job AND charged_to_org_id=:org FOR UPDATE NOWAIT'), params)
        row = _reviewable_submission(db, submission_id=submission_id, principal=principal)
        digest = _decision_hash(submission_id, body)
        replay = claim_command(db, org_id=row.org_id, actor_id=principal['id'], action=action,
                               command_id=str(body.command_id), request_digest=digest)
        if replay is not None:
            db.commit()
            return replay
        if row.lifecycle_revision != body.revision or row.status not in TERMINAL or row.job_status not in TERMINAL:
            raise OdinError(ErrorCode.resource_conflict, 'Only terminal work at the current revision can be archived', status=409)
        if db.execute(text('SELECT 1 FROM education_monitor_claims WHERE submission_id=:id OR job_id=:job'), params).first() or db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:job'), params).first():
            raise OdinError(ErrorCode.resource_conflict, 'Reconcile the unresolved printer claim or command before archiving', status=409)
        result = {'id': row.id, 'job_id': row.job_id, 'status': row.status,
                  'lifecycle_revision': row.lifecycle_revision + 1, 'archived': True}
        changed = db.execute(text('UPDATE education_submissions SET archived_at=CURRENT_TIMESTAMP, '
            'archived_by=:actor,archive_reason=:reason,lifecycle_revision=lifecycle_revision+1 '
            'WHERE id=:id AND org_id=:org AND lifecycle_revision=:revision AND archived_at IS NULL'),
            {**params, 'actor': principal['id'], 'reason': body.reason or None, 'revision': body.revision})
        if changed.rowcount != 1:
            raise OdinError(ErrorCode.resource_conflict, 'Submission changed; reload required', status=409)
        _record_decision(db, row=row, principal=principal, action=action,
                         command_id=str(body.command_id), digest=digest, result=result,
                         details={'reason': body.reason, 'preserved_status': row.status})
        db.commit()
        return result
    except ValueError:
        db.rollback()
        raise OdinError(ErrorCode.not_found, 'Submission not found', status=404) from None
    except OperationalError:
        db.rollback()
        raise OdinError(ErrorCode.resource_conflict, 'Submission is busy; retry', status=409) from None
    except Exception:
        db.rollback()
        raise
