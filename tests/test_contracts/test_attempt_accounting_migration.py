"""Additive attempt-accounting migration, legacy preservation and constraints."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text
from tests.test_contracts.test_education_review_workflow import review_db

MODULE='core.schema.migrations.007_attempt_accounting'

def test_fresh_bootstrap_has_durable_attempt_accounting(review_db):
 engine=review_db.get_bind()
 with engine.connect() as conn:
  inspector=inspect(conn)
  assert 'spool_snapshot' in {c['name'] for c in inspector.get_columns('print_jobs')}
  assert 'print_job_id' in {c['name'] for c in inspector.get_columns('spool_usage')}
  assert {'accounting_state','accounting_reason'} <= {c['name'] for c in inspector.get_columns('print_archives')}
  assert 'filament_delivery_outbox' in inspector.get_table_names()

def legacy_engine(tmp_path,wrong=False):
 engine=create_engine(f"sqlite:///{tmp_path/'legacy-attempt.db'}")
 with engine.begin() as conn:
  conn.execute(text('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY'+(',spool_snapshot INTEGER' if wrong else '')+')'))
  conn.execute(text('CREATE TABLE spool_usage(id INTEGER PRIMARY KEY,job_id INTEGER,spool_id INTEGER,weight_used_g REAL)'))
  conn.execute(text('CREATE TABLE print_archives(id INTEGER PRIMARY KEY,print_job_id INTEGER)'))
  conn.execute(text('INSERT INTO print_jobs(id) VALUES(7)'))
  conn.execute(text('INSERT INTO spool_usage(id,job_id,weight_used_g) VALUES(8,9,80)'))
  conn.execute(text('INSERT INTO print_archives(id,print_job_id) VALUES(10,7)'))
 return engine

def test_legacy_rows_preserved_without_invented_snapshot_or_attempt(tmp_path):
 migration=importlib.import_module(MODULE);engine=legacy_engine(tmp_path)
 with engine.begin() as conn:migration.apply(conn);migration.validate(conn)
 with engine.begin() as conn:migration.apply(conn);migration.validate(conn)
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT id,spool_snapshot FROM print_jobs')).one())==(7,None)
  assert tuple(conn.execute(text('SELECT id,job_id,weight_used_g,print_job_id FROM spool_usage')).one())==(8,9,80,None)
  assert tuple(conn.execute(text('SELECT id,print_job_id,accounting_state FROM print_archives')).one())==(10,7,None)

def test_wrong_adopted_shape_fails_before_mutation(tmp_path):
 migration=importlib.import_module(MODULE);engine=legacy_engine(tmp_path,wrong=True)
 with pytest.raises(Exception,match='shape mismatch'):
  with engine.begin() as conn:migration.apply(conn)
 with engine.connect() as conn:
  assert 'print_job_id' not in {c['name'] for c in inspect(conn).get_columns('spool_usage')}
  assert 'filament_delivery_outbox' not in inspect(conn).get_table_names()

def test_outbox_rejects_duplicate_delivery_invalid_state_and_negative_weight(review_db):
 from sqlalchemy.exc import IntegrityError
 engine=review_db.get_bind()
 statement=text("INSERT INTO filament_delivery_outbox(id,print_job_id,spool_id,spoolman_spool_id,grams_used,state) VALUES(:id,7,:spool,9,:grams,:state)")
 with engine.begin() as conn:conn.execute(statement,{'id':'synthetic-delivery','grams':80,'state':'pending','spool':8})
 for identifier,grams,state,spool in [('duplicate',80,'pending',8),('negative',-1,'pending',9),('bad-state',80,'pretend_sent',10)]:
  with pytest.raises(IntegrityError):
   with engine.begin() as conn:conn.execute(statement,{'id':identifier,'grams':grams,'state':state,'spool':spool})
 with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM filament_delivery_outbox')).scalar_one()==1

def test_attempt_usage_unique_preserves_unattributed_legacy_duplicates(review_db):
 from sqlalchemy.exc import IntegrityError
 engine=review_db.get_bind()
 with engine.begin() as conn:
  conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(7199,'Synthetic','Synthetic PLA')"))
  conn.execute(text('INSERT INTO spools(id,filament_id) VALUES(7199,7199)'))
  conn.execute(text('INSERT INTO spool_usage(spool_id,weight_used_g,print_job_id) VALUES(7199,10,NULL),(7199,10,NULL),(7199,10,7)'))
 with pytest.raises(IntegrityError):
  with engine.begin() as conn:conn.execute(text('INSERT INTO spool_usage(spool_id,weight_used_g,print_job_id) VALUES(7199,10,7)'))
 with engine.connect() as conn:assert conn.execute(text('SELECT COUNT(*) FROM spool_usage WHERE spool_id=7199')).scalar_one()==3

@pytest.mark.parametrize('definition',[
 'CREATE INDEX uq_spool_usage_attempt_spool ON spool_usage(print_job_id,spool_id) WHERE print_job_id IS NOT NULL',
 'CREATE UNIQUE INDEX uq_spool_usage_attempt_spool ON spool_usage(print_job_id,spool_id)',
 'CREATE UNIQUE INDEX uq_spool_usage_attempt_spool ON spool_usage(print_job_id) WHERE print_job_id IS NOT NULL',
])
def test_malformed_adopted_usage_index_rejected_before_mutation(tmp_path,definition):
 migration=importlib.import_module(MODULE);engine=legacy_engine(tmp_path)
 with engine.begin() as conn:
  conn.execute(text('ALTER TABLE spool_usage ADD COLUMN print_job_id INTEGER'))
  conn.execute(text(definition))
 with pytest.raises(Exception,match='index'):
  with engine.begin() as conn:migration.apply(conn)
 with engine.connect() as conn:assert 'spool_snapshot' not in {c['name'] for c in inspect(conn).get_columns('print_jobs')}

@pytest.mark.parametrize('expression',["grams_used >= 0","state IN ('pending','in_flight','sent','failed','outcome_unknown')"])
def test_named_weakened_outbox_check_rejected_before_mutation(tmp_path,expression):
 migration=importlib.import_module(MODULE);engine=legacy_engine(tmp_path)
 with engine.begin() as conn:
  migration.outbox.create(conn)
  ddl=conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='filament_delivery_outbox'")).scalar_one()
  assert expression in ddl
  conn.execute(text('DROP TABLE filament_delivery_outbox'))
  conn.execute(text(ddl.replace(expression,'1 = 1')))
 with pytest.raises(Exception,match='check'):
  with engine.begin() as conn:migration.apply(conn)
 with engine.connect() as conn:assert 'spool_snapshot' not in {c['name'] for c in inspect(conn).get_columns('print_jobs')}

def test_recorded_schema_index_drift_rejected(review_db):
 migration=importlib.import_module(MODULE);engine=review_db.get_bind()
 with engine.begin() as conn:
  conn.execute(text('DROP INDEX uq_spool_usage_attempt_spool'))
  conn.execute(text('CREATE INDEX uq_spool_usage_attempt_spool ON spool_usage(print_job_id,spool_id) WHERE print_job_id IS NOT NULL'))
 with pytest.raises(Exception,match='index'):
  with engine.connect() as conn:migration.validate(conn)

@pytest.mark.parametrize('grams',[float('inf'),float('-inf'),float('nan')])
def test_nonfinite_delivery_quantity_rejected(review_db,grams):
 from sqlalchemy.exc import IntegrityError
 with pytest.raises(IntegrityError):
  with review_db.get_bind().begin() as conn:conn.execute(text("INSERT INTO filament_delivery_outbox(id,print_job_id,spool_id,spoolman_spool_id,grams_used,state) VALUES('nonfinite',7,8,9,:grams,'pending')"),{'grams':grams})

@pytest.mark.parametrize('replacement',["'PENDING'", "'Pending'", "'pending '" ])
def test_state_literal_drift_rejected_before_mutation(tmp_path,replacement):
 migration=importlib.import_module(MODULE);engine=legacy_engine(tmp_path)
 with engine.begin() as conn:
  migration.outbox.create(conn)
  ddl=conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='filament_delivery_outbox'")).scalar_one()
  conn.execute(text('DROP TABLE filament_delivery_outbox'))
  conn.execute(text(ddl.replace("'pending'",replacement)))
 with pytest.raises(Exception,match='check'):
  with engine.begin() as conn:migration.apply(conn)
 with engine.connect() as conn:assert 'spool_snapshot' not in {c['name'] for c in inspect(conn).get_columns('print_jobs')}


def test_archive_ownership_columns_distinguish_unknown_from_explicit_global(review_db):
 """NULL legacy attribution must not grant global access by implication."""
 with review_db.get_bind().connect() as conn:
  columns={c['name']:c for c in inspect(conn).get_columns('print_archives')}
  assert {'org_id','ownership_state','ownership_source','education_submission_id'} <= columns.keys()
  for name in ('org_id','ownership_state','ownership_source','education_submission_id'):
   assert columns[name]['nullable']
   assert columns[name]['default'] is None


def test_legacy_archive_ownership_is_not_guessed_during_upgrade(tmp_path):
 migration=importlib.import_module(MODULE);engine=legacy_engine(tmp_path)
 with engine.begin() as conn:migration.apply(conn);migration.validate(conn)
 with engine.connect() as conn:
  assert tuple(conn.execute(text('SELECT id,org_id,ownership_state,ownership_source,education_submission_id FROM print_archives')).one())==(10,None,None,None,None)


@pytest.mark.parametrize('name,definition',[
 ('org_id','TEXT'),('ownership_state','INTEGER'),('ownership_source','INTEGER'),
 ('education_submission_id','TEXT'),
])
def test_malformed_archive_ownership_adoption_rejected_before_additive_mutation(tmp_path,name,definition):
 migration=importlib.import_module(MODULE);engine=legacy_engine(tmp_path)
 with engine.begin() as conn:conn.execute(text(f'ALTER TABLE print_archives ADD COLUMN {name} {definition}'))
 with pytest.raises(Exception,match='shape mismatch'):
  with engine.begin() as conn:migration.apply(conn)
 with engine.connect() as conn:
  assert 'spool_snapshot' not in {c['name'] for c in inspect(conn).get_columns('print_jobs')}
  assert 'filament_delivery_outbox' not in inspect(conn).get_table_names()
