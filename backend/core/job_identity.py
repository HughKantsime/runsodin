"""SQLite job identities survive deletion; PostgreSQL keeps its native sequence."""
from sqlalchemy import text

# The UPDATE is the first allocator statement: acquire the SQLite writer lock
# before reading MAX(jobs.id). Allocation and insertion share one transaction.
ALLOCATE = ('UPDATE job_id_high_water SET high_water = '
            'max(high_water, COALESCE((SELECT max(id) FROM jobs), 0)) + 1 WHERE singleton=1')
READ = 'SELECT high_water FROM job_id_high_water WHERE singleton=1'


def allocate_sqlite_job_id(execute):
    execute(ALLOCATE)
    row = execute(READ).fetchone()
    if row is None:
        raise RuntimeError('Job identity high-water row missing')
    return row[0]


def before_job_insert(mapper, connection, job):
    if connection.dialect.name == 'sqlite' and job.id is None:
        job.id = allocate_sqlite_job_id(lambda sql: connection.execute(text(sql)))


def insert_job(cursor, statement, values, *, postgres=False):
    """Raw qmark creation paths use the same allocator as ORM flushes."""
    if not postgres:
        if statement.count('INSERT INTO jobs (') != 1 or statement.count('VALUES (') != 1:
            raise ValueError('Expected a single explicit-column job INSERT')
        identifier = allocate_sqlite_job_id(cursor.execute)
        statement = statement.replace('INSERT INTO jobs (', 'INSERT INTO jobs (id, ', 1)
        statement = statement.replace('VALUES (', 'VALUES (?, ', 1)
        values = (identifier, *values)
    return cursor.execute(statement, values)
