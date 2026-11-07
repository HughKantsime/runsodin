"""Lock the complete source alias graph before changing a controlled policy."""
from sqlalchemy import text, bindparam
from core.controlled_parts import selected_file_sql, refuse


def related(db, kind, identifier):
    source = identifier if kind == 'file' else db.execute(text(selected_file_sql(':id')), {'id': identifier}).scalar()
    files = db.execute(text('SELECT id FROM print_files WHERE id=:id OR (stored_path IS NOT NULL '
                            'AND stored_path=(SELECT stored_path FROM print_files WHERE id=:id)) ORDER BY id'), {'id': source}).scalars().all()
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- job-selection SQL and lock table names are source-owned constants; file and job IDs are bound
    query = text('SELECT j.id FROM jobs j WHERE (' + selected_file_sql('j.id') + ') IN :files '
                 'OR j.id=:job ORDER BY j.id').bindparams(bindparam('files', expanding=True))
    jobs = db.execute(query, {'files': files, 'job': identifier if kind == 'job' else None}).scalars().all()
    return files, jobs


def lock_related(db, kind, identifier):
    # SQLite acquired the writer at resource(); PG locks deterministic rows.
    files, jobs = related(db, kind, identifier)
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text("SET LOCAL lock_timeout = '1s'"))
        for table, ids in (('jobs', jobs), ('print_files', files)):
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- job-selection SQL and lock table names are source-owned constants; file and job IDs are bound
            query = text(f'SELECT id FROM {table} WHERE id IN :ids ORDER BY id FOR UPDATE').bindparams(bindparam('ids', expanding=True))
            db.execute(query, {'ids': ids}).all()
        if related(db, kind, identifier) != (files, jobs):
            db.rollback()
            refuse(db, 'controlled_part_source_changed', kind, identifier)


def assert_not_education(db, kind, identifier):
    files, jobs = related(db, kind, identifier)
    query = text('SELECT 1 FROM education_submissions WHERE job_id IN :jobs OR print_file_id IN :files LIMIT 1').bindparams(
        bindparam('jobs', expanding=True), bindparam('files', expanding=True))
    linked = db.execute(query, {'jobs': jobs, 'files': files}).first()
    owner_query = text('SELECT 1 FROM print_files f WHERE f.id IN :files AND (EXISTS '
                      '(SELECT 1 FROM education_cost_centers e WHERE e.org_id=f.org_id) OR EXISTS '
                      '(SELECT 1 FROM education_upload_operations o WHERE o.final_path=f.stored_path)) LIMIT 1').bindparams(bindparam('files', expanding=True))
    if linked or db.execute(owner_query, {'files': files}).first():
        db.rollback()
        refuse(db, 'controlled_part_education_owned', kind, identifier)
