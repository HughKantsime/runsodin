"""Add durable physical-attempt attribution without guessing legacy history."""
import re
from decimal import Decimal, localcontext
from sqlalchemy import Column, DateTime, Float, Integer, MetaData, String, Table, Text, UniqueConstraint, CheckConstraint, inspect, text, func
from core.schema.migrator import validate_column_shape, MigrationError

MIGRATION_ID='python:007-attempt-accounting'
COLUMNS={
 'print_jobs':{'spool_snapshot':'TEXT'},
 'spool_usage':{'print_job_id':'INTEGER'},
 'print_archives':{'accounting_state':'TEXT','accounting_reason':'TEXT',
  'org_id':'INTEGER','ownership_state':'TEXT','ownership_source':'TEXT',
  'education_submission_id':'INTEGER'},
}
metadata=MetaData()
outbox=Table('filament_delivery_outbox',metadata,
 Column('id',String(64),primary_key=True),
 Column('print_job_id',Integer,nullable=False),Column('spool_id',Integer,nullable=False),
 Column('spoolman_spool_id',Integer,nullable=False),Column('grams_used',Float,nullable=False),
 Column('state',String(20),nullable=False),Column('attempt_started_at',DateTime(timezone=True)),
 Column('outcome_reason',Text),Column('created_at',DateTime(timezone=True),server_default=text('CURRENT_TIMESTAMP')),
 Column('updated_at',DateTime(timezone=True),server_default=text('CURRENT_TIMESTAMP')),
 UniqueConstraint('print_job_id','spool_id',name='uq_filament_delivery_attempt_spool'),
 CheckConstraint('grams_used >= 0 AND grams_used <= 1.7976931348623157e308',name='ck_filament_delivery_nonnegative'),
 CheckConstraint("state IN ('pending','in_flight','sent','failed','outcome_unknown')",name='ck_filament_delivery_state'))

def _normal_check(expression):
 # PostgreSQL expands scientific literals and adds casts. Normalize syntax,
 # but preserve string values: case and whitespace change allowed states.
 number=r'[+-]?[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?'
 def canonical(match):
  raw=match.group(0)
  with localcontext() as context:
   context.prec=max(32,len(raw))
   return str(Decimal(raw).normalize()).lower().replace('e+','e')
 pieces=[]
 for index,segment in enumerate(re.split(r"('(?:''|[^'])*')",str(expression))):
  if index % 2:
   inner=segment[1:-1]
   pieces.append(canonical(re.fullmatch(number,inner)) if re.fullmatch(number,inner) else segment)
  else:
   segment=segment.lower()
   segment=re.sub(r'::(?:character varying|double precision|text(?:\[\])?|real|numeric)','',segment)
   segment=re.sub(r'\s+','',segment).replace('(','').replace(')','').replace('"','')
   segment=re.sub(r'(?<![a-z_0-9])'+number+r'(?![a-z_0-9])',canonical,segment)
   pieces.append(segment)
 return ''.join(pieces)


def _validate_usage_index(connection,required=True):
 index=next((item for item in inspect(connection).get_indexes('spool_usage') if item['name']=='uq_spool_usage_attempt_spool'),None)
 if index is None:
  if required:raise MigrationError('Attempt usage index missing')
  return
 options=index.get('dialect_options',{})
 predicate=options.get(connection.dialect.name+'_where')
 if not index.get('unique') or index.get('column_names')!=['print_job_id','spool_id'] or _normal_check(predicate)!='print_job_idisnotnull':
  raise MigrationError('Attempt usage index shape mismatch')


def _validate_outbox(connection):
 clock_type='TIMESTAMP WITH TIME ZONE' if connection.dialect.name=='postgresql' else 'DATETIME'
 for name,definition in {'id':'VARCHAR(64) NOT NULL','print_job_id':'INTEGER NOT NULL','spool_id':'INTEGER NOT NULL','spoolman_spool_id':'INTEGER NOT NULL','grams_used':'FLOAT NOT NULL','state':'VARCHAR(20) NOT NULL','attempt_started_at':clock_type,'outcome_reason':'TEXT','created_at':clock_type+' DEFAULT CURRENT_TIMESTAMP','updated_at':clock_type+' DEFAULT CURRENT_TIMESTAMP'}.items():
  validate_column_shape(connection,'filament_delivery_outbox',name,definition)
 inspector=inspect(connection)
 if not any(set(item['column_names'])=={'print_job_id','spool_id'} for item in inspector.get_unique_constraints('filament_delivery_outbox')):
  raise MigrationError('Attempt delivery unique constraint missing')
 if not any(item['constrained_columns']==['id'] for item in [inspector.get_pk_constraint('filament_delivery_outbox')]):
  raise MigrationError('Attempt delivery primary key missing')
 checks={item['name']:_normal_check(item['sqltext']) for item in inspector.get_check_constraints('filament_delivery_outbox')}
 states="'pending','in_flight','sent','failed','outcome_unknown'"
 expected_state="state=anyarray["+states+"]" if connection.dialect.name=='postgresql' else "statein"+states
 expected_quantity=_normal_check('grams_used >= 0 AND grams_used <= 1.7976931348623157e308')
 if checks.get('ck_filament_delivery_state')!=expected_state or checks.get('ck_filament_delivery_nonnegative')!=expected_quantity:
  raise MigrationError('Attempt delivery check shape mismatch')

def apply(connection):
 if connection.dialect.name=='sqlite' and not connection.connection.driver_connection.in_transaction:
  connection.exec_driver_sql('BEGIN IMMEDIATE')
 inspector=inspect(connection)
 # Validate all adopted shapes before any additive mutation.
 for table,columns in COLUMNS.items():
  actual={item['name'] for item in inspector.get_columns(table)}
  for name,definition in columns.items():
   if name in actual:validate_column_shape(connection,table,name,definition)
 if inspector.has_table(outbox.name):_validate_outbox(connection)
 _validate_usage_index(connection,required=False)
 for table,columns in COLUMNS.items():
  actual={item['name'] for item in inspect(connection).get_columns(table)}
  for name,definition in columns.items():
   if name not in actual:connection.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {definition}'))  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- DDL/validation SQL built from module constants only; no runtime input
 outbox.create(connection,checkfirst=True)
 # Legacy NULL attempt IDs remain untouched, including existing duplicates.
 connection.execute(text('CREATE UNIQUE INDEX IF NOT EXISTS uq_spool_usage_attempt_spool ON spool_usage(print_job_id,spool_id) WHERE print_job_id IS NOT NULL'))

def validate(connection):
 for table,columns in COLUMNS.items():
  for name,definition in columns.items():validate_column_shape(connection,table,name,definition)
 _validate_outbox(connection)
 _validate_usage_index(connection)
