"""D3 aggregate reports: D2 assignment attribution and retained job ownership."""
from datetime import datetime, time, timedelta, timezone
import math
from sqlalchemy import text
from sqlalchemy.sql.elements import TextClause
from fastapi import HTTPException
from zoneinfo import ZoneInfo
from modules.organizations.education_program_policy import options, template_allowed
from core.finance_currency import read_currency
from core.report_job_ownership import proved_job_owner_sql, proved_education_job_sql
from modules.organizations.education_classes_service import actor, class_access, center_access

MAX_ROWS = 65536
MAX_CLASSES = 1000


class ReportRow(dict):
    """Keep cohort evidence in memory only; JSON/CSV/PDF never serialize IDs."""

    def __init__(self, values, students):
        super().__init__(values)
        self.students = frozenset(students)


def _bounded(db, query, params, limit=None):
    """Execute a prepared report statement; callers cannot pass raw SQL strings."""
    if not isinstance(query, TextClause):
        raise TypeError('A prepared report statement is required')
    limit = MAX_ROWS if limit is None else limit
    rows = db.execute(query, {**params, 'row_limit': limit + 1}).mappings().all()
    if len(rows) > limit:
        raise HTTPException(413, 'Program report exceeds row limit; select a narrower scope')
    return rows


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def _bucket():
    return dict(jobs=0, unassigned_jobs=0, completed_jobs=0, failed_jobs=0, print_hours=0.0,
        material_grams=0.0, cost=0.0, unknown_hours_jobs=0, unknown_material_jobs=0,
        unknown_cost_jobs=0, excluded_unproved_jobs=0, instructor_entered_teaching_hours=0.0,
        print_hours_approved_by_instructors=0.0, unknown_approved_hours_jobs=0,
        _jobs=set(), _approved_samples=0,
        _students=set(), _participants=set(), _concentrators=set(), _cost_samples=0,
        _grams_samples=0, _hours_samples=0)


def _job(bucket, row):
    if row['job_id'] in bucket['_jobs']:
        return
    bucket['_jobs'].add(row['job_id'])
    if not row['proved']:
        bucket['excluded_unproved_jobs'] += 1
        return
    bucket['jobs'] += 1
    bucket['unassigned_jobs'] += int(row['class_id'] is None)
    bucket['_students'].add(row['submitted_by'])
    completed = row['status'] == 'completed'
    failed = row['status'] == 'failed'
    bucket['completed_jobs'] += int(completed)
    bucket['failed_jobs'] += int(failed)
    if not (completed or failed):
        return
    seconds = _number(row['seconds'])
    hours = seconds / 3600 if seconds is not None else None
    if row['instructor_approved']:
        if hours is not None:
            bucket['print_hours_approved_by_instructors'] += hours
            bucket['_approved_samples'] += 1
        if hours is None or row['unmeasured_hours']:
            bucket['unknown_approved_hours_jobs'] += 1
    for key, value, unknown, sample in (
        ('print_hours', hours, 'unknown_hours_jobs', '_hours_samples'),
        ('material_grams', _number(row['grams']), 'unknown_material_jobs', '_grams_samples'),
        ('cost', _number(row['estimated_cost']), 'unknown_cost_jobs', '_cost_samples'),
    ):
        if value is None:
            bucket[unknown] += 1
        else:
            bucket[key] += value
            bucket[sample] += 1
    # A job with one known and one unknown archive attempt remains partial.
    if hours is not None and row['unmeasured_hours']:
        bucket['unknown_hours_jobs'] += 1
    if row['grams'] is not None and row['unmeasured']:
        bucket['unknown_material_jobs'] += 1


def _finish(bucket, **identity):
    b = dict(bucket)
    b.pop('_jobs')
    b['students_served'] = len(b.pop('_students'))
    b['participants'] = len(b.pop('_participants'))
    b['concentrators'] = len(b.pop('_concentrators'))
    terminal = b['completed_jobs'] + b['failed_jobs']
    b['success_rate'] = round(100 * b['completed_jobs'] / terminal, 2) if terminal else None
    for key, sample in (('cost', '_cost_samples'), ('material_grams', '_grams_samples'), ('print_hours', '_hours_samples'),
                        ('print_hours_approved_by_instructors', '_approved_samples')):
        b[key] = round(b[key], 2) if b.pop(sample) and math.isfinite(b[key]) else None
    b['instructor_entered_teaching_hours'] = round(b['instructor_entered_teaching_hours'], 2)
    return ReportRow({**identity, **b}, bucket['_students'])


def build_report(db, principal, filters, *, scheduled_org=None):
    """scheduled_org is only passed by the system report service after validating
    the live schedule creator as a platform admin. HTTP never accepts it.
    """
    if scheduled_org is None:
        org, admin = actor(db, principal)
        if filters.class_id:
            class_access(db, principal, filters.class_id, manage=True)
        if filters.cost_center_id:
            center_access(db, principal, filters.cost_center_id, manage=True)
    else:
        org, admin = scheduled_org, True
    config = options(db, org)
    template_allowed(db, org, filters.template)
    zone = ZoneInfo(config['timezone'])
    params = {'org': org, 'actor': principal['id'], 'admin': int(admin),
        'start': datetime.combine(filters.start_date, time.min, zone).astimezone(timezone.utc).replace(tzinfo=None),
        'end': datetime.combine(filters.end_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc).replace(tzinfo=None),
        'start_day': filters.start_date.isoformat(), 'end_day': filters.end_date.isoformat()}
    scope = 'p.org_id=:org AND (:admin=1 OR EXISTS (SELECT 1 FROM education_cost_center_grants g '
    scope += "WHERE g.org_id=p.org_id AND g.cost_center_id=p.id AND g.user_id=:actor AND g.role='manager' AND g.state='active'))"
    if filters.cost_center_id is not None:
        scope += ' AND p.id=:cost_center_id'
        params['cost_center_id'] = filters.cost_center_id
    class_scope = ''
    for field in ('class_id', 'term'):
        value = getattr(filters, field)
        if value is not None:
            column = 'id' if field == 'class_id' else field
            class_scope += ' AND c.' + column + '=:' + field
            params[field] = value
    cte = ('WITH centers AS (SELECT p.* FROM education_cost_centers p WHERE ' + scope + '), '
        'selected AS (SELECT c.* FROM education_classes c JOIN centers p '
        'ON p.id=c.cost_center_id AND p.org_id=c.org_id WHERE 1=1' + class_scope + ') ')
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- cte uses literal scope clauses and the fixed id/term columns; SELECT and ORDER BY are literals; filters and row_limit are bound
    classes = _bounded(db, text(cte + 'SELECT c.id,c.title,c.term,c.cost_center_id,p.display_name AS program '
        'FROM selected c JOIN centers p ON p.id=c.cost_center_id AND p.org_id=c.org_id ORDER BY c.term,c.title,c.id LIMIT :row_limit'), params, MAX_CLASSES)
    restricted = bool(filters.class_id or filters.term)
    program_scope = ' WHERE EXISTS (SELECT 1 FROM selected c WHERE c.cost_center_id=p.id)' if restricted else ''
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- cte uses fixed scope/id/term clauses; program_scope is empty or a literal EXISTS clause; filters and row_limit are bound
    programs = _bounded(db, text(cte + 'SELECT p.id,p.display_name AS program FROM centers p' + program_scope + ' ORDER BY p.id LIMIT :row_limit'), params, MAX_CLASSES)
    buckets = {r['id']: _bucket() for r in classes}
    program_buckets = {r['id']: _bucket() for r in programs}
    unassigned = {r['id']: _bucket() for r in programs} if not restricted else {}
    summary = _bucket()
    proof = '(' + proved_job_owner_sql() + ') AND (' + proved_education_job_sql() + ')'
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- cte and restricted suffix use fixed clauses; proof comes from no-argument source-owned ownership builders; filters and row_limit are bound
    rows = _bounded(db, text(cte + 'SELECT j.id AS job_id,c.id AS class_id,s.cost_center_id,s.submitted_by,j.status,j.estimated_cost,'
        'CASE WHEN ' + proof + ' THEN 1 ELSE 0 END AS proved,pa.grams,pa.unmeasured,pa.seconds,pa.unmeasured_hours, '
        "EXISTS (SELECT 1 FROM education_audit_events e WHERE e.org_id=s.org_id AND e.resource_type='submission' "
        "AND e.resource_id=CAST(s.id AS TEXT) AND e.action='submission.approve') AS instructor_approved "
        'FROM education_submissions s JOIN centers p ON p.id=s.cost_center_id AND p.org_id=s.org_id '
        'LEFT JOIN education_assignment_submissions x ON x.submission_id=s.id AND x.org_id=s.org_id '
        'LEFT JOIN education_assignments a ON a.id=x.assignment_id AND a.org_id=x.org_id '
        'LEFT JOIN selected c ON c.id=a.class_id AND c.org_id=a.org_id AND c.cost_center_id=s.cost_center_id '
        'JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id AND j.submitted_by=s.submitted_by '
        'LEFT JOIN (SELECT job_id,SUM(filament_used_grams) AS grams,'
        'SUM(CASE WHEN filament_used_grams IS NULL THEN 1 ELSE 0 END) AS unmeasured, '
        'SUM(actual_duration_seconds) AS seconds, '
        'SUM(CASE WHEN actual_duration_seconds IS NULL THEN 1 ELSE 0 END) AS unmeasured_hours '
        'FROM print_archives GROUP BY job_id) pa ON pa.job_id=j.id '
        'WHERE s.created_at>=:start AND s.created_at<:end' + (' AND c.id IS NOT NULL' if restricted else '') + ' ORDER BY s.id LIMIT :row_limit'), params)
    for row in rows:
        if row['class_id'] in buckets:
            _job(buckets[row['class_id']], row)
        elif row['class_id'] is None and row['cost_center_id'] in unassigned:
            _job(unassigned[row['cost_center_id']], row)
        if row['cost_center_id'] in program_buckets:
            _job(program_buckets[row['cost_center_id']], row)
        _job(summary, row)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- cte uses literal scope clauses and the fixed id/term columns; marks SELECT and ORDER BY are literals; filters and row_limit are bound
    marks = _bounded(db, text(cte + 'SELECT m.class_id,c.cost_center_id,m.user_id,m.participant,m.concentrator FROM education_program_marks m '
        'JOIN selected c ON c.id=m.class_id AND c.org_id=m.org_id ORDER BY m.class_id,m.user_id LIMIT :row_limit'), params)
    for row in marks:
        for bucket in (buckets[row['class_id']], program_buckets[row['cost_center_id']], summary):
            if row['participant']:
                bucket['_participants'].add(row['user_id'])
            if row['concentrator']:
                bucket['_concentrators'].add(row['user_id'])
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- cte uses literal scope clauses and only id/term columns from the fixed class_id/term loop; filter values and dates are bound
    hours = db.execute(text(cte + 'SELECT h.class_id,c.cost_center_id,SUM(h.hours) AS hours FROM education_instructor_hours h '
        'JOIN selected c ON c.id=h.class_id AND c.org_id=h.org_id '
        'WHERE h.activity_date>=:start_day AND h.activity_date<=:end_day GROUP BY h.class_id,c.cost_center_id'), params).mappings()
    for row in hours:
        buckets[row['class_id']]['instructor_entered_teaching_hours'] = float(row['hours'])
        program_buckets[row['cost_center_id']]['instructor_entered_teaching_hours'] += float(row['hours'])
        summary['instructor_entered_teaching_hours'] += float(row['hours'])
    return {'schema_version': 1, 'template': filters.template, 'filters': filters.model_dump(mode='json'),
        'generated_at': datetime.now(timezone.utc).isoformat(), 'org_id': org,
        'timezone': config['timezone'], 'perkins_enabled': config['perkins_enabled'],
        'metric_labels': {'instructor_entered_teaching_hours': 'instructor-entered teaching hours',
            'print_hours_approved_by_instructors': 'print hours approved by instructors',
            'participants': 'participants' if filters.term else 'participants (all-time marks)',
            'concentrators': 'concentrators' if filters.term else 'concentrators (all-time marks)'},
        'currency': read_currency(db, owner_scope='org', owner_key=org),
        'basis': {'program': 'Education cost center', 'jobs': 'Submissions created in the inclusive school-day window (' + config['timezone'] + '); class/term filters require D2 links, program totals include unassigned work',
            'students_served': 'Distinct submitters of proved jobs, all statuses',
            'print_hours': 'Sum of print_archives.actual_duration_seconds across all attempts of terminal jobs; missing attempts are unknown',
            'material_grams': 'Archive recorded measured or file-estimated grams, all attempts of terminal jobs',
            'cost': 'Job estimated cost in current organization currency; not actual expenditure',
            'success_rate': 'Completed / (completed + failed); no terminal jobs = unknown',
            'instructor_entered_teaching_hours': 'Instructor-entered teaching hours by activity date, entered and corrected by tenant admins; not print duration',
            'print_hours_approved_by_instructors': 'Archive attempt hours for jobs with retained submission.approve evidence (including instructor-authorized class rules); missing duration is unknown',
            'participants': ('Term marks' if filters.term else 'All-time marks') + '; explicit class marks independent of print date window; distinct people across classes'},
        'notice': 'DRAFT Perkins V planning worksheet; field definitions not yet confirmed; not an official filing.'
            if filters.template == 'perkins' else 'Generic funding and program report. Missing measurements are unknown, not zero.',
        'summary': _finish(summary),
        'programs': [_finish(program_buckets[r['id']], **r) for r in programs],
        'classes': [_finish(buckets[r['id']], **r) for r in classes] + [
            _finish(unassigned[r['id']], id=None, cost_center_id=r['id'], program=r['program'],
                    title='Unassigned', term='') for r in programs if r['id'] in unassigned]}
