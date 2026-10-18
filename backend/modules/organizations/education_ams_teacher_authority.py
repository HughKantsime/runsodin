"""Current teacher context, locked before printer/evidence transaction work."""
from dataclasses import dataclass
from sqlalchemy import text
from modules.organizations.education_review_service import _reviewable_submission,_assert_review_state


class EducationAMSAuthorityUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class TeacherAMSContext:
    submission_id:int
    job_id:int
    print_file_id:int
    org_id:int
    cost_center_id:int
    printer_id:int
    revision:int


# Closed set of (table, predicate, no-op column) row locks for teacher AMS review.
_TEACHER_LOCKS=frozenset({
    ('users','id=:user_id AND group_id=:org_id AND is_active IS TRUE','is_active'),
    ('education_cost_centers',"id=:center_id AND org_id=:org_id AND state='active'",'revision'),
    ('education_cost_center_grants',"org_id=:org_id AND cost_center_id=:center_id AND user_id=:user_id AND role='manager' AND state='active'",'state'),
    ('education_cost_center_printers',"org_id=:org_id AND cost_center_id=:center_id AND printer_id=:printer_id AND state='active'",'state'),
    ('education_submissions','id=:submission_id AND org_id=:org_id AND job_id=:job_id','updated_at'),
    ('jobs','id=:job_id AND charged_to_org_id=:org_id','status'),
})


def _lock_teacher_row(db,postgres,params,table,where,column):
    if (table,where,column) not in _TEACHER_LOCKS:raise ValueError('Unsupported teacher lock target')
    query=(f'SELECT 1 FROM {table} WHERE {where} FOR UPDATE NOWAIT' if postgres
           else f'UPDATE {table} SET {column}={column} WHERE {where} RETURNING 1')
    if db.execute(text(query),params).first() is None:raise ValueError()  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/predicate/column checked against the _TEACHER_LOCKS allowlist; values are bound


def lock_teacher_ams_context(db,*,principal,submission_id,printer_id,revision):
    """Caller owns rollback. Authenticate before locks, no network in transaction."""
    try:
        if (type(principal) is not dict or any(type(v) is not int or v<=0 for v in
            (principal.get('id'),principal.get('group_id'),submission_id,printer_id,revision))):raise ValueError()
        postgres=db.get_bind().dialect.name=='postgresql'
        if db.get_bind().dialect.name not in ('postgresql','sqlite'):raise ValueError()
        params={'user_id':principal['id'],'org_id':principal['group_id'],'submission_id':submission_id,
                'printer_id':printer_id}
        lock=lambda table,where,column:_lock_teacher_row(db,postgres,params,table,where,column)
        lock('users','id=:user_id AND group_id=:org_id AND is_active IS TRUE','is_active')
        user=db.execute(text('SELECT role FROM users WHERE id=:user_id AND group_id=:org_id'),params).mappings().one()
        current={**principal,'role':user['role']}
        row=_reviewable_submission(db,submission_id=submission_id,principal=current)
        _assert_review_state(row,revision)
        params.update(center_id=row.cost_center_id,job_id=row.job_id)
        lock('education_cost_centers',"id=:center_id AND org_id=:org_id AND state='active'",'revision')
        if current['role']!='admin':
            lock('education_cost_center_grants',"org_id=:org_id AND cost_center_id=:center_id AND user_id=:user_id AND role='manager' AND state='active'",'state')
        lock('education_cost_center_printers',"org_id=:org_id AND cost_center_id=:center_id AND printer_id=:printer_id AND state='active'",'state')
        lock('education_submissions','id=:submission_id AND org_id=:org_id AND job_id=:job_id','updated_at')
        lock('jobs','id=:job_id AND charged_to_org_id=:org_id','status')
        fresh=_reviewable_submission(db,submission_id=submission_id,principal=current)
        _assert_review_state(fresh,revision)
        if (fresh.job_id!=row.job_id or fresh.print_file_id!=row.print_file_id or fresh.cost_center_id!=row.cost_center_id):raise ValueError()
        printer=db.execute(text('SELECT 1 FROM printers WHERE id=:printer_id AND org_id=:org_id '
            'AND is_active IS TRUE AND shared IS NOT TRUE'),params).first()
        if printer is None:raise ValueError()
        return TeacherAMSContext(int(fresh.id),int(fresh.job_id),int(fresh.print_file_id),
            int(fresh.org_id),int(fresh.cost_center_id),printer_id,revision)
    except Exception:
        raise EducationAMSAuthorityUnavailable('Education read context unavailable; rollback required') from None
