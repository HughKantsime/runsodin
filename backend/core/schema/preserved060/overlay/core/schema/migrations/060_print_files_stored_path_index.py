"""Index shared print-file paths without changing stored rows."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:060-print-files-stored-path-index'
INDEX = 'ix_print_files_stored_path'


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported print file index dialect')
    connection.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS {INDEX} ON print_files (stored_path)')
    validate(connection)


def validate(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported print file index dialect')
    inspector = inspect(connection)
    if not inspector.has_table('print_files'):
        raise MigrationError('Print file table missing')
    index = next((i for i in inspector.get_indexes('print_files') if i['name'] == INDEX), None)
    options = (index or {}).get('dialect_options', {})
    if (not index or index['unique'] or index['column_names'] != ['stored_path']
            or index.get('column_sorting') or index.get('include_columns')
            or options.get(connection.dialect.name + '_where') is not None
            or options.get('postgresql_include') or options.get('postgresql_ops')
            or options.get('postgresql_using', 'btree') != 'btree'):
        raise MigrationError('Print file stored path index shape differs')
