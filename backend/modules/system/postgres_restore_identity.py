"""Keep issued job sequence values across an offline PostgreSQL restore."""
from sqlalchemy import text


def _sequence(connection):
    schema, name = connection.execute(text(
        "SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.oid=pg_get_serial_sequence('public.jobs','id')::regclass")).one()
    quote = connection.dialect.identifier_preparer.quote_identifier
    return quote(schema) + '.' + quote(name)


def job_identity_floor(connection):
    """Called under the restore's offline barrier, immediately before truncation."""
    value, called = connection.exec_driver_sql(
        'SELECT last_value,is_called FROM ' + _sequence(connection)).one()
    maximum = connection.execute(text('SELECT COALESCE(max(id),0) FROM public.jobs')).scalar_one()
    return max(value, maximum), bool(called or maximum >= value)


def preserve_job_identity(connection, previous):
    restored = job_identity_floor(connection)
    value = max(previous[0], restored[0])
    called = any(mark == value and used for mark, used in (previous, restored))
    connection.execute(text('SELECT setval(CAST(:sequence AS regclass),:value,:called)'),
                       {'sequence': _sequence(connection), 'value': value, 'called': called})
