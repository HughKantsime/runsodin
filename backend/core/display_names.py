"""Read-time human names. Raw dispatch and audit identity is never rewritten."""
import re
from sqlalchemy import bindparam, text

_TRANSPORT = re.compile(r'odin-dispatch-[0-9a-f-]+', re.IGNORECASE)


def _strip_extension(value):
    return re.sub(r'\.(?:gcode\.3mf|3mf|gcode|bgcode)$', '', (value or '').strip(), flags=re.IGNORECASE).strip()


def filename_stem(value):
    return _strip_extension((value or '').replace('\\', '/').rsplit('/', 1)[-1])


def human_name(stored=None, *, model_name=None, original_filename=None):
    # Model names made from filenames (e.g. "latch-v3.2-H2D-150-slot2-HF.gcode") keep no extension.
    for candidate in (_strip_extension(model_name), filename_stem(original_filename), _strip_extension(stored)):
        if candidate and candidate.strip() and not _TRANSPORT.search(candidate):
            return candidate.strip()
    return 'Unnamed print'


def job_name(stored, *, model_name=None, original_filename=None, project_name=None):
    """An operator's job name wins; a name derived from the transport, the 3MF title or the filename does not."""
    value = (stored or '').strip()
    derived = {name for name in (project_name, original_filename, filename_stem(original_filename)) if name}
    if value and not _TRANSPORT.search(value) and value not in derived and _strip_extension(value) == value:
        return value
    return human_name(stored, model_name=model_name, original_filename=original_filename)


_QUERIES = {
    'print_file': """SELECT f.id,f.filename AS raw_name,m.name AS model_name,
        m.org_id AS model_org,f.original_filename,f.org_id AS file_org
        FROM print_files f LEFT JOIN models m ON m.id=f.model_id WHERE f.id IN :ids""",
    'job': """SELECT j.id, j.item_name AS raw_name, m.name AS model_name,
        m.org_id AS model_org, f.original_filename, f.org_id AS file_org, f.project_name
        FROM jobs j LEFT JOIN models jm ON jm.id=j.model_id
        LEFT JOIN print_files f ON f.id=COALESCE(j.print_file_id,jm.print_file_id,
            (SELECT MIN(pf.id) FROM print_files pf WHERE pf.job_id=j.id HAVING COUNT(*)=1))
        LEFT JOIN models m ON m.id=COALESCE(j.model_id,f.model_id) WHERE j.id IN :ids""",
    'print_job': """SELECT p.id, COALESCE(p.job_name,p.filename) AS raw_name,
        m.name AS model_name,m.org_id AS model_org,f.original_filename,f.org_id AS file_org
        FROM print_jobs p LEFT JOIN jobs j ON j.id=p.scheduled_job_id
        LEFT JOIN models jm ON jm.id=j.model_id
        LEFT JOIN printers printer ON printer.id=p.printer_id
        LEFT JOIN print_files f ON f.id=COALESCE(j.print_file_id,jm.print_file_id,
            (SELECT MIN(pf.id) FROM print_files pf WHERE pf.job_id=j.id HAVING COUNT(*)=1),
            (SELECT MIN(pf.id) FROM print_files pf
             WHERE (pf.filename=p.filename OR pf.filename=p.job_name OR pf.original_filename=p.filename OR pf.original_filename=p.job_name)
             AND (pf.org_id=printer.org_id OR pf.org_id IS NULL) HAVING COUNT(*)=1))
        LEFT JOIN models m ON m.id=COALESCE(j.model_id,f.model_id) WHERE p.id IN :ids""",
    'archive': """SELECT a.id,a.print_name AS raw_name,m.name AS model_name,
        m.org_id AS model_org,f.original_filename,f.org_id AS file_org
        FROM print_archives a LEFT JOIN print_jobs p ON p.id=a.print_job_id
        LEFT JOIN jobs j ON j.id=COALESCE(p.scheduled_job_id,a.job_id)
        LEFT JOIN models jm ON jm.id=j.model_id
        LEFT JOIN print_files f ON f.id=COALESCE(j.print_file_id,a.print_file_id,jm.print_file_id,
            (SELECT MIN(pf.id) FROM print_files pf WHERE pf.job_id=j.id HAVING COUNT(*)=1))
        LEFT JOIN models m ON m.id=COALESCE(j.model_id,f.model_id) WHERE a.id IN :ids""",
}


def display_names(db, kind, ids, user=None):
    """One bounded batch query, with the same tenant visibility as source names."""
    from core.rbac import check_org_access
    ids = list(dict.fromkeys(ids))
    result = {}
    for offset in range(0, len(ids), 400):
        query = text(_QUERIES[kind]).bindparams(bindparam('ids', expanding=True))
        for row in db.execute(query, {'ids': ids[offset:offset+400]}).mappings():
            model = row['model_name'] if user is None or check_org_access(user, row['model_org']) else None
            filename = row['original_filename'] if user is None or check_org_access(user, row['file_org']) else None
            if kind == 'job':
                result[row['id']] = job_name(row['raw_name'], model_name=model, original_filename=filename,
                                             project_name=row['project_name'] if filename is not None else None)
            else:
                result[row['id']] = human_name(row['raw_name'], model_name=model, original_filename=filename)
    return result


def name_payloads(db, kind, rows, user=None):
    names = display_names(db, kind, [row['id'] for row in rows], user)
    return [dict(row, display_name=names.get(row['id'], human_name(row.get('item_name') or row.get('job_name') or row.get('print_name')))) for row in rows]


def name_jobs(db, jobs, user=None):
    names = display_names(db, 'job', [job.id for job in jobs], user)
    for job in jobs:
        job._read_display_name = names.get(job.id, human_name(job.item_name))
    return jobs


def name_printers(db, printers, user=None):
    """Batch the currently tracked print name for printer cards."""
    if not printers:
        return printers
    query = text("SELECT id,printer_id FROM print_jobs WHERE printer_id IN :ids AND status='running' ORDER BY id DESC").bindparams(bindparam('ids', expanding=True))
    rows = db.execute(query, {'ids':[printer.id for printer in printers]}).mappings().all()
    names = display_names(db, 'print_job', [row['id'] for row in rows], user)
    active = {}
    for row in rows:
        active.setdefault(row['printer_id'], names[row['id']])
    for printer in printers:
        printer.current_print_display_name = active.get(printer.id)
    return printers


def observed_item_name(executor, print_job_id):
    """Name a newly attached metrics job using the same graph as history reads."""
    from core.dispatch_observation_executor import TransactionView
    # The query and replaced predicate are constants; the attempt id stays bound.
    query = _QUERIES['print_job'].replace('p.id IN :ids', 'p.id=:id')
    row = TransactionView(executor).execute(text(query), {'id':print_job_id}).mappings().first()
    if row is None:
        return human_name()
    model = row['model_name'] if row['model_org'] in (None, row['file_org']) else None
    return human_name(model_name=model, original_filename=row['original_filename'] or row['raw_name'])


def rename_generated_job_names(db, old_names, new_name, *, org_id, model_id=None, print_file_id=None):
    """A rename carries jobs whose item_name is still the generated name; an operator's own name stays."""
    olds = sorted({name for name in old_names if name} - {new_name})
    if not olds or not new_name:
        return []
    owner = 'jobs.model_id=:owner' if model_id is not None else 'jobs.print_file_id=:owner AND jobs.model_id IS NULL'
    query = text(f"SELECT id, charged_to_org_id FROM jobs WHERE {owner} AND item_name IN :olds").bindparams(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment chosen from two literals; values are bound
        bindparam('olds', expanding=True))
    ids = [row.id for row in db.execute(query, {'owner': model_id if model_id is not None else print_file_id, 'olds': olds})
           if row.charged_to_org_id == org_id]
    if ids:
        db.execute(text('UPDATE jobs SET item_name=:name WHERE id IN :ids').bindparams(bindparam('ids', expanding=True)),
                   {'name': new_name, 'ids': ids})
    return ids
