"""Add soft archive metadata and an inventory repair journal; retain all history."""
from sqlalchemy import inspect, text

MIGRATION_ID = 'python:036-ctec-pilot'


def retry_spool_index(connection):
    """Defer enforcement while historical duplicates await explicit resolution."""
    if connection.dialect.name == 'postgresql':
        connection.execute(text('LOCK TABLE filament_slots IN SHARE ROW EXCLUSIVE MODE'))
    duplicate = connection.execute(text(
        'SELECT assigned_spool_id FROM filament_slots WHERE assigned_spool_id IS NOT NULL '
        'GROUP BY assigned_spool_id HAVING COUNT(*)>1 LIMIT 1')).first()
    if duplicate:
        return False
    connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_filament_slots_spool ON filament_slots(assigned_spool_id)'))
    return True


def apply(connection):
    if connection.dialect.name == 'sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    cols = {c['name'] for c in inspect(connection).get_columns('education_submissions')}
    if 'archived_at' not in cols:
        connection.execute(text('ALTER TABLE education_submissions ADD COLUMN archived_at TIMESTAMP NULL'))
    if 'archived_by' not in cols:
        connection.execute(text('ALTER TABLE education_submissions ADD COLUMN archived_by INTEGER NULL REFERENCES users(id) ON DELETE RESTRICT'))
    if 'archive_reason' not in cols:
        connection.execute(text('ALTER TABLE education_submissions ADD COLUMN archive_reason TEXT NULL'))
    if 'hidden_from_library' not in {c['name'] for c in inspect(connection).get_columns('models')}:
        connection.execute(text('ALTER TABLE models ADD COLUMN hidden_from_library BOOLEAN NOT NULL DEFAULT FALSE'))
    connection.execute(text('CREATE TABLE IF NOT EXISTS inventory_repairs ('
        'event_id VARCHAR(36) PRIMARY KEY, spool_id INTEGER NOT NULL, actor_id INTEGER NULL, '
        'action VARCHAR(40) NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL, '
        'created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)'))
    retry_spool_index(connection)
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    if not {'archived_at', 'archived_by', 'archive_reason'} <= {c['name'] for c in inspector.get_columns('education_submissions')}:
        raise RuntimeError('Education archive metadata missing')
    if not inspector.has_table('inventory_repairs'):
        raise RuntimeError('Inventory repair journal missing')
