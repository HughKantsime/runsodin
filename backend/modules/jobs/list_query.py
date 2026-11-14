"""GET /jobs query: filters, search and total.

Kept out of routes/jobs_crud.py (route-file size limit).

Search is one more AND on a query that already carries every visibility predicate (archived
Education work, controlled parts, org scope). A job the caller cannot see is therefore never a
candidate, and no match on its id or any of its names can reveal that it exists. Names are matched
only through the same joins and org checks that build the display name, and every value is bound.
"""
import re
from datetime import timezone

from sqlalchemy import and_, case, func, or_, text

from core.base import JobStatus
from core.printer_display_facts import may_read
from core.rbac import _get_org_filter, get_org_scope, should_filter_org
from modules.jobs.models import Job

MAX_QUERY_LENGTH = 200
_NUMBER = re.compile(r'^(#|job\s*#?)?\s*(\d{1,15})$', re.IGNORECASE)
_ARCHIVED_EDUCATION = ("NOT EXISTS (SELECT 1 FROM education_submissions es "
                       "WHERE es.job_id=jobs.id AND es.archived_at IS NOT NULL)")


def parse_job_number(q):
    """('218' | '#218' | 'job 218' | 'Job #218') -> (218, had_prefix); anything else -> (None, False)."""
    match = _NUMBER.match(q.strip())
    return (int(match.group(2)), bool(match.group(1))) if match else (None, False)


def like_pattern(value):
    """Lower-cased %value% with LIKE wildcards escaped (escape character '!')."""
    escaped = value.lower().replace('!', '!!').replace('%', '!%').replace('_', '!_')
    return '%' + escaped + '%'


def _superadmin(user):
    return bool(user) and user.get('role') == 'admin' and not user.get('group_id')


def _org_ok(user, column):
    """SQL twin of check_org_access for the org of a name source (model, file, printer, order)."""
    if not user:
        return '0=1'
    if _superadmin(user):
        return '1=1'
    return f'({column} IS NULL OR {column}=:q_org)'


def search_filter(user, q):
    """A WHERE clause matching q, or None when q is blank.

    "#218" and "job 218" match the job id only. A bare number matches the id or, as text, any
    name, printer or order number (order numbers are often plain digits).
    """
    q = ' '.join((q or '').split())[:MAX_QUERY_LENGTH]
    if not q:
        return None
    number, prefixed = parse_job_number(q)
    parts, params = [], {}
    if number is not None:
        parts.append('jobs.id=:q_id')
        params['q_id'] = number
        if prefixed:
            return text('(jobs.id=:q_id)').bindparams(**params)
    params['q_like'] = like_pattern(q)
    if user and not _superadmin(user):
        params['q_org'] = user.get('group_id')
    like = "LIKE :q_like ESCAPE '!'"
    parts.append(f"LOWER(COALESCE(jobs.item_name,'')) {like}")
    # The display-name graph (core.display_names, kind 'job'): job -> model -> file, org-checked.
    parts.append(
        "EXISTS (SELECT 1 FROM jobs qj LEFT JOIN models qjm ON qjm.id=qj.model_id "
        "LEFT JOIN print_files qf ON qf.id=COALESCE(qj.print_file_id,qjm.print_file_id,"
        "(SELECT MIN(qpf.id) FROM print_files qpf WHERE qpf.job_id=qj.id HAVING COUNT(*)=1)) "
        "LEFT JOIN models qm ON qm.id=COALESCE(qj.model_id,qf.model_id) WHERE qj.id=jobs.id AND ("
        f"(LOWER(COALESCE(qm.name,'')) {like} AND {_org_ok(user, 'qm.org_id')}) OR "
        f"(LOWER(COALESCE(qf.original_filename,'')) {like} AND {_org_ok(user, 'qf.org_id')}) OR "
        f"(LOWER(COALESCE(qf.project_name,'')) {like} AND {_org_ok(user, 'qf.org_id')})))")
    if may_read(user, 'printers'):
        parts.append(
            "EXISTS (SELECT 1 FROM printers qp WHERE qp.id=jobs.printer_id AND "
            f"(LOWER(COALESCE(qp.name,'')) {like} OR LOWER(COALESCE(qp.nickname,'')) {like}) AND "
            f"({_org_ok(user, 'qp.org_id')} OR qp.shared IS TRUE))")
    parts.append(
        "EXISTS (SELECT 1 FROM order_items qoi JOIN orders qo ON qo.id=qoi.order_id "
        f"WHERE qoi.id=jobs.order_item_id AND LOWER(COALESCE(qo.order_number,'')) {like} "
        f"AND {_org_ok(user, 'qo.org_id')})")
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- every fragment is a literal of this function (fixed columns, _org_ok's fixed predicates); the search text, job id and org are bound
    return text('(' + ' OR '.join(parts) + ')').bindparams(**params)


def _instant(db, column):
    """`column` in a form that orders and compares correctly as stored.

    On SQLite job times are text, in more than one shape: naive UTC 'YYYY-MM-DD HH:MM:SS[.ffffff]'
    and, in rows written by older versions, ISO 'YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00'. As text they
    do not sort together ('T' sorts after a space, so an older ISO row of the same day ranked
    above a newer naive one). SQLite's own strftime reads every shape, including a trailing Z
    or an offset (which it converts to UTC), and gives the second; the six fraction digits are
    taken from the stored text so sub-second order is kept. The result is always
    'YYYY-MM-DD HH:MM:SS.ffffff', or NULL for NULL or unreadable text. PostgreSQL stores real
    timestamps and is left alone.
    """
    if db.get_bind().dialect.name != 'sqlite':
        return column
    fraction = case((func.substr(column, 20, 1) == '.', func.substr(column, 21, 6)), else_='000000')
    return func.strftime('%Y-%m-%d %H:%M:%S', column).concat('.').concat(fraction)


def list_job_rows(db, current_user, *, statuses, printer_id, unassigned, held, finished_since,
                  order, org_id, q, limit, offset):
    """(rows, total): the requested page and the number of jobs matching every filter."""
    from core.controlled_parts import any_controlled_data, job_visibility
    query = db.query(Job).filter(text(_ARCHIVED_EDUCATION))
    if any_controlled_data(db):
        controlled, controlled_params = job_visibility(current_user)
        query = query.filter(text(controlled)).params(**controlled_params)
    if statuses:
        query = query.filter(Job.status == statuses[0]) if len(statuses) == 1 else query.filter(Job.status.in_(statuses))
    if printer_id:
        query = query.filter(Job.printer_id == printer_id)
    elif unassigned:
        query = query.filter(Job.printer_id.is_(None))
    if held is True:
        query = query.filter(Job.hold.is_(True))
    elif held is False:
        query = query.filter(Job.hold.isnot(True))
    if finished_since is not None:
        # Stored job times are naive UTC.
        since = finished_since.astimezone(timezone.utc).replace(tzinfo=None) if finished_since.tzinfo else finished_since
        # A job that ended without ever starting (cancelled or rejected while queued) has no
        # actual_end; its last update is when it ended.
        if db.get_bind().dialect.name == 'sqlite':
            since = since.strftime('%Y-%m-%d %H:%M:%S.%f')   # the text form _instant produces
        ended_unstarted = and_(Job.actual_end.is_(None), _instant(db, Job.updated_at) >= since,
                               Job.status.in_([JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.REJECTED]))
        query = query.filter(or_(and_(Job.actual_end.isnot(None), _instant(db, Job.actual_end) >= since), ended_unstarted))

    effective_org = _get_org_filter(current_user, org_id) if org_id is not None else get_org_scope(current_user)
    if should_filter_org(current_user, effective_org):
        query = query.filter((Job.charged_to_org_id == effective_org) | (Job.charged_to_org_id == None))  # noqa: E711

    # Search comes last, on top of every visibility predicate above.
    search = search_filter(current_user, q)
    if search is not None:
        query = query.filter(search)

    # Priority first; drag-and-drop queue_position orders jobs within a
    # priority (unplaced jobs last on SQLite and PostgreSQL alike); then age.
    if order == "recent":
        ordering = (_instant(db, func.coalesce(Job.actual_end, Job.updated_at, Job.created_at)).desc(), Job.id.desc())
    else:
        ordering = (Job.priority, Job.queue_position.is_(None), Job.queue_position, Job.created_at, Job.id)
    rows = query.order_by(*ordering).offset(offset).limit(limit).all()
    # A first page that is not full is the whole result; otherwise count the same filters.
    total = len(rows) if offset == 0 and len(rows) < limit else query.order_by(None).count()
    return rows, total
