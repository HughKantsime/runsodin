"""Add observed tray state and idempotent operator decisions; no balance rewrites."""
from sqlalchemy import inspect
from core.schema.migrator import MigrationError

MIGRATION_ID = 'python:046-tray-identity'
COLUMNS = {'state_id': 'VARCHAR(32)', 'slot_id': 'INTEGER', 'decision': 'VARCHAR(4)',
           'spool_id': 'INTEGER', 'previous_spool_id': 'INTEGER'}


def apply(connection):
    if connection.dialect.name not in ('sqlite', 'postgresql'):
        raise MigrationError('Unsupported tray identity dialect')
    if 'tray_observation' not in {c['name'] for c in inspect(connection).get_columns('filament_slots')}:
        connection.exec_driver_sql('ALTER TABLE filament_slots ADD COLUMN tray_observation TEXT')
    if 'tray_confirmations' not in inspect(connection).get_table_names():
        connection.exec_driver_sql('''CREATE TABLE tray_confirmations (
            state_id VARCHAR(32) PRIMARY KEY NOT NULL,
            slot_id INTEGER NOT NULL,
            decision VARCHAR(4) NOT NULL,
            spool_id INTEGER NOT NULL,
            previous_spool_id INTEGER,
            FOREIGN KEY(slot_id) REFERENCES filament_slots(id) ON DELETE CASCADE,
            FOREIGN KEY(spool_id) REFERENCES spools(id) ON DELETE RESTRICT)''')
    validate(connection)


def validate(connection):
    inspector = inspect(connection)
    column = next((c for c in inspector.get_columns('filament_slots') if c['name'] == 'tray_observation'), None)
    if not column or str(column['type']).upper() != 'TEXT' or not column['nullable'] or column.get('default') is not None:
        raise MigrationError('Tray observation shape differs')
    columns = {c['name']: c for c in inspector.get_columns('tray_confirmations')}
    if set(columns) != set(COLUMNS) or any(str(columns[n]['type']).upper() != t or
            columns[n]['nullable'] != (n == 'previous_spool_id') or columns[n].get('default') is not None for n, t in COLUMNS.items()):
        raise MigrationError('Tray confirmation shape differs')
    if inspector.get_pk_constraint('tray_confirmations')['constrained_columns'] != ['state_id']:
        raise MigrationError('Tray confirmation primary key differs')
    foreign = {(tuple(f['constrained_columns']), f['referred_table'], tuple(f['referred_columns']), f['options'].get('ondelete'))
               for f in inspector.get_foreign_keys('tray_confirmations')}
    if foreign != {(('slot_id',), 'filament_slots', ('id',), 'CASCADE'), (('spool_id',), 'spools', ('id',), 'RESTRICT')}:
        raise MigrationError('Tray confirmation foreign keys differ')
    if inspector.get_unique_constraints('tray_confirmations') or inspector.get_check_constraints('tray_confirmations') or inspector.get_indexes('tray_confirmations'):
        raise MigrationError('Tray confirmation constraints differ')
