"""Empty publication journal; exact schema adoption, never infer legacy facts."""
import importlib,hashlib
from pathlib import Path
from sqlalchemy import inspect,text,CheckConstraint
from core.schema.migrator import MigrationError
from core.timelapse_publication_schema import TABLES

MIGRATION_ID='python:019-timelapse-publications'
SCHEMA_SOURCE_SHA256='a1c371d153ef288fedeb0666aa76351e53e4aec2772143adc51db5c8cb96dee2'
VALIDATION_SOURCE_SHA256='f1511d95894bf13e5e77318025350c140eb456cba7636b47b2ab6feb95ba2881'
RECIPE_SOURCE_SHA256='511a258474b92c9483b397d9cce67c92b0c14e77e61b3e6ba7e00a565b71aac0'
HELPER_SOURCE_SHA256='a31f9b87ac68b1009178fdeba9c9043cccd780f00c08be3113c40822f261f68e'
_contract=importlib.import_module('core.schema.migrations.015_timelapse_retirements')
_previous=importlib.import_module('core.schema.migrations.018_timelapse_unlinks')
_owner=importlib.import_module('core.schema.migrations.016_timelapse_ownership')


def _source():
    schema=Path(__file__).resolve().parents[2]/'timelapse_publication_schema.py'
    helper=Path(__file__).with_name('015_timelapse_retirements.py')
    validator=Path(__file__).resolve().parents[2]/'timelapse_publication_validation.py'
    recipe=Path(__file__).resolve().parents[2]/'timelapse_recipes.py'
    if hashlib.sha256(recipe.read_bytes()).hexdigest()!=RECIPE_SOURCE_SHA256:raise MigrationError('Publication recipe source contract changed')
    if hashlib.sha256(schema.read_bytes()).hexdigest()!=SCHEMA_SOURCE_SHA256 or hashlib.sha256(helper.read_bytes()).hexdigest()!=HELPER_SOURCE_SHA256 or hashlib.sha256(validator.read_bytes()).hexdigest()!=VALIDATION_SOURCE_SHA256:raise MigrationError('Publication source contract changed')


def _foreign(table):
    return [(tuple(value.column_keys),tuple(element.column.name for element in value.elements),next(iter(value.elements)).column.table.name) for value in table.foreign_key_constraints]


def _table(connection,table):
    inspector=inspect(connection);actual=inspector.get_columns(table.name)
    if {column['name'] for column in actual}!=set(table.columns.keys()):raise MigrationError('Publication columns differ')
    for column in actual:
        expected=table.columns[column['name']]
        default=str(expected.server_default.arg.compile(dialect=connection.dialect)) if expected.server_default is not None else None
        if str(column['type'].compile(dialect=connection.dialect)).upper()!=str(expected.type.compile(dialect=connection.dialect)).upper() or column['nullable']!=expected.nullable or (str(column.get('default')).upper() if column.get('default') is not None else None)!=(default.upper() if default is not None else None):raise MigrationError('Publication column shape differs')
    if inspector.get_pk_constraint(table.name)['constrained_columns']!=list(table.primary_key.columns.keys()):raise MigrationError('Publication primary key differs')
    expected_unique={value.name:tuple(column.name for column in value.columns) for value in table.constraints if value.__class__.__name__=='UniqueConstraint'}
    actual_unique={value['name']:tuple(value['column_names']) for value in inspector.get_unique_constraints(table.name)}
    if actual_unique!=expected_unique or any(value.get('duplicates_constraint') not in expected_unique for value in inspector.get_indexes(table.name)):raise MigrationError('Publication indexes differ')
    if connection.dialect.name=='postgresql':
        keys=connection.execute(text("""SELECT c.condeferrable,c.condeferred,c.convalidated,
            i.indimmediate,i.indisvalid,i.indisready,
            COALESCE((to_jsonb(i)->>'indnullsnotdistinct')::boolean,false)
            FROM pg_catalog.pg_constraint c JOIN pg_catalog.pg_index i ON i.indexrelid=c.conindid
            WHERE c.conrelid=pg_catalog.to_regclass(:table) AND c.contype IN ('p','u')"""),{'table':table.name}).all()
        if len(keys)!=1+len(expected_unique) or any(row!=(False,False,True,True,True,True,False) for row in keys):raise MigrationError('Publication key timing authority differs')
    expected_foreign=sorted(_foreign(table));actual_foreign=inspector.get_foreign_keys(table.name)
    shapes=[]
    for value in actual_foreign:
        options=value.get('options',{})
        if value.get('referred_schema') not in (None,inspector.default_schema_name) or options.get('ondelete')!='RESTRICT' or options.get('onupdate') not in (None,'NO ACTION') or options.get('deferrable'):raise MigrationError('Publication foreign authority differs')
        shapes.append((tuple(value['constrained_columns']),tuple(value['referred_columns']),value['referred_table']))
        if connection.dialect.name=='postgresql':
            flags=connection.execute(text("""SELECT f.convalidated,f.condeferrable,f.confrelid=parent.oid,f.confupdtype='a',f.confdeltype='r'
                FROM pg_constraint f JOIN pg_class child ON child.oid=f.conrelid
                JOIN pg_class parent ON parent.relname=:parent AND parent.relnamespace=child.relnamespace AND parent.relkind='r'
                WHERE f.conrelid=to_regclass(:table) AND f.contype='f' AND f.conname=:name"""),{'parent':value['referred_table'],'table':table.name,'name':value['name']}).all()
            if flags!=[(True,False,True,True,True)]:raise MigrationError('Publication native foreign authority differs')
    if sorted(shapes)!=expected_foreign:raise MigrationError('Publication foreign keys differ')
    checks={value['name']:_contract._normalize(value['sqltext']) for value in inspector.get_check_constraints(table.name)}
    if checks!=_contract._expected_checks(connection,table):raise MigrationError('Publication checks differ')
    if connection.dialect.name=='postgresql':
        valid=connection.execute(text("SELECT convalidated FROM pg_constraint WHERE conrelid=to_regclass(:table) AND contype='c'"),{'table':table.name}).scalars().all()
        if len(valid)!=len(checks) or any(value is not True for value in valid):raise MigrationError('Publication checks unvalidated')
    elif connection.exec_driver_sql("PRAGMA integrity_check('"+table.name+"')").scalars().all()!=['ok'] or connection.exec_driver_sql("PRAGMA foreign_key_check('"+table.name+"')").first() is not None:raise MigrationError('Publication retained integrity differs')


def _retained(connection):
    from core.timelapse_publication_validation import validate_retained
    validate_retained(connection)


def _sqlite_boundary(connection):
    if connection.dialect.name != 'sqlite':
        return
    if not connection.connection.driver_connection.in_transaction:
        connection.exec_driver_sql('BEGIN IMMEDIATE')
    # BEGIN on an existing caller may have been DEFERRED. A no-row write
    # acquires the reserved writer lock without changing any anchor facts.
    # Busy/stale-snapshot failures belong to the caller; never retry or commit.
    connection.exec_driver_sql('UPDATE main.timelapse_ownership_anchors SET operation_id=operation_id WHERE 0')


def _postgres_boundary(connection):
    if connection.dialect.name != 'postgresql':
        return
    if connection.exec_driver_sql('SHOW transaction_isolation').scalar_one() != 'read committed':
        raise MigrationError('Publication adoption requires READ COMMITTED')
    schema=connection.exec_driver_sql('SELECT current_schema()').scalar_one()
    if schema is None:
        raise MigrationError('Publication authority schema missing')
    prior=('timelapses','timelapse_retirements','timelapse_retirement_objects',
           'timelapse_ownership_anchors','timelapse_retirement_parents',
           'timelapse_retirement_quarantine','timelapse_retirement_moves',
           'timelapse_retirement_unlinks')
    inspector=inspect(connection)
    if any(not inspector.has_table(name,schema=schema) for name in prior):
        raise MigrationError('Publication prior authority table missing')
    names=set(prior)
    names.update(table.name for table in TABLES if inspector.has_table(table.name,schema=schema))
    quote=connection.dialect.identifier_preparer.quote_identifier
    qualified=[quote(schema)+'.'+quote(name) for name in sorted(names)]
    # Static table allowlist and quoted current schema. No filesystem work,
    # silent retry or caller commit/rollback inside the adoption boundary.
    connection.exec_driver_sql('LOCK TABLE '+', '.join(qualified)+' IN SHARE MODE NOWAIT')
    for name in sorted(names):
        identity=connection.execute(text("""SELECT visible.oid=locked.oid,locked.relkind
            FROM pg_catalog.pg_class visible,pg_catalog.pg_class locked
            WHERE visible.oid=pg_catalog.to_regclass(:visible)
            AND locked.oid=pg_catalog.to_regclass(:locked)"""),
            {'visible':name,'locked':quote(schema)+'.'+quote(name)}).all()
        if identity!=[(True,'r')]:
            raise MigrationError('Publication authority relation shadowed')


def _boundary(connection):
    _sqlite_boundary(connection)
    _postgres_boundary(connection)


def validate(connection):
    _source();_boundary(connection)
    _previous.validate(connection);_owner.validate(connection)
    for table in TABLES:
        if not inspect(connection).has_table(table.name):raise MigrationError('Publication table missing')
        _table(connection,table)
    _retained(connection)


def apply(connection):
    _source()
    _boundary(connection)
    _previous.validate(connection);_owner.validate(connection)
    for table in TABLES:
        if inspect(connection).has_table(table.name):_table(connection,table)
    _retained(connection)
    for table in TABLES:table.create(connection,checkfirst=True)
    validate(connection)
