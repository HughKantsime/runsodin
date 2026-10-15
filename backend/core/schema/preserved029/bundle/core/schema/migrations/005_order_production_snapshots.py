"""Add empty immutable production records; never reconstruct historical BOMs."""
import re
from sqlalchemy import inspect, MetaData, Table, Column, Integer, Text, ForeignKey, ForeignKeyConstraint, CheckConstraint
from core.schema.migrator import validate_column_shape

MIGRATION_ID = 'python:005-order-production-snapshots'
TABLES = ('order_production_items','order_production_components','order_production_jobs')


# Historical migration shape is frozen independently of future ORM changes.
_METADATA = MetaData()
Table('orders', _METADATA, Column('id', Integer, primary_key=True))
Table('order_items', _METADATA, Column('id', Integer, primary_key=True))
Table('order_production_items', _METADATA,
    Column('order_item_id', Integer, ForeignKey('order_items.id', ondelete='RESTRICT'), primary_key=True, autoincrement=False),
    Column('order_id', Integer, ForeignKey('orders.id', ondelete='RESTRICT'), nullable=False),
    Column('product_id', Integer, nullable=False),
    Column('ordered_quantity', Integer, nullable=False),
    Column('mode', Text, nullable=False),
    Column('confirmed_quantity', Integer, nullable=False, server_default='0'),
    CheckConstraint('ordered_quantity > 0', name='opi_positive_quantity'),
    CheckConstraint('confirmed_quantity >= 0 AND confirmed_quantity <= ordered_quantity', name='opi_confirmation_bounds'),
    CheckConstraint("mode IN ('printed','mixed','consumable_only','historical')", name='opi_mode'))
Table('order_production_components', _METADATA,
    Column('order_item_id', Integer, ForeignKey('order_production_items.order_item_id', ondelete='RESTRICT'), primary_key=True, autoincrement=False),
    Column('model_id', Integer, primary_key=True, autoincrement=False),
    Column('pieces_per_unit', Integer, nullable=False),
    CheckConstraint('pieces_per_unit > 0', name='opc_positive_pieces'))
Table('order_production_jobs', _METADATA,
    Column('job_id', Integer, primary_key=True, autoincrement=False),
    Column('order_item_id', Integer, nullable=False),
    Column('model_id', Integer, nullable=False),
    Column('piece_count', Integer, nullable=False),
    ForeignKeyConstraint(['order_item_id', 'model_id'], ['order_production_components.order_item_id', 'order_production_components.model_id'], ondelete='RESTRICT'),
    CheckConstraint('piece_count > 0', name='opj_positive_pieces'))


def _normalize(value):
    value=re.sub(r'\s+', '', str(value)).replace('"','').lower().replace('::text','')
    value=value.replace('(','').replace(')','')
    return value.replace('=anyarray[','in').replace(']','')


def apply(connection):
    if connection.dialect.name=='sqlite' and not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    # Validate every pre-existing table before creating any missing table.
    existing=set(inspect(connection).get_table_names())
    for name in TABLES:
        if name in existing:_validate_table(connection,name)
    for name in TABLES:_METADATA.tables[name].create(connection,checkfirst=True)


def _validate_table(connection,name):
    inspector=inspect(connection);table=_METADATA.tables[name]
    actual={c['name']:c for c in inspector.get_columns(name)}
    if set(actual)!=set(table.columns.keys()):raise RuntimeError('Order production column shape mismatch')
    for column in table.columns:
        definition=str(column.type.compile(dialect=connection.dialect))
        if not column.nullable:definition+=' NOT NULL'
        if column.server_default is not None:definition+=' DEFAULT '+str(column.server_default.arg)
        validate_column_shape(connection,name,column.name,definition)
    if inspector.get_pk_constraint(name).get('constrained_columns')!=[c.name for c in table.primary_key]:
        raise RuntimeError('Order production primary key mismatch')
    expected_fks={(tuple(f.column_keys),tuple(e.column.name for e in f.elements),f.elements[0].column.table.name,f.ondelete) for f in table.foreign_key_constraints}
    actual_fks={(tuple(f['constrained_columns']),tuple(f['referred_columns']),f['referred_table'],f.get('options',{}).get('ondelete')) for f in inspector.get_foreign_keys(name)}
    if actual_fks!=expected_fks:raise RuntimeError('Order production foreign key mismatch')
    expected_checks={c.name:_normalize(c.sqltext) for c in table.constraints if c.__class__.__name__=='CheckConstraint'}
    actual_checks={c['name']:_normalize(c['sqltext']) for c in inspector.get_check_constraints(name)}
    if actual_checks!=expected_checks:raise RuntimeError('Order production check constraint mismatch')
    if inspector.get_unique_constraints(name):raise RuntimeError('Unexpected order production uniqueness')


def validate(connection):
    for name in TABLES:_validate_table(connection,name)
