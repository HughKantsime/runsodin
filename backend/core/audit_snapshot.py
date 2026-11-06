"""Dedicated read snapshots: no chain/writer lock and no request-session upgrade."""
from contextlib import contextmanager


@contextmanager
def snapshot(engine):
    with engine.connect() as connection:
        if connection.dialect.name == 'postgresql':
            connection = connection.execution_options(isolation_level='REPEATABLE READ')
            with connection.begin():
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
                yield connection
        else:
            # sqlite3 legacy transaction mode does not BEGIN on SELECT.
            connection.exec_driver_sql('PRAGMA query_only=ON')
            connection.exec_driver_sql('BEGIN')
            try:
                yield connection
            finally:
                connection.rollback()
                connection.exec_driver_sql('PRAGMA query_only=OFF')
