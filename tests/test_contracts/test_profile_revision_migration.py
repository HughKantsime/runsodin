"""Additive profile content history keeps legacy text and rejects malformed adoption."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text
from tests.test_contracts.test_education_review_workflow import review_db

MODULE='core.schema.migrations.008_profile_revisions'
TABLE='printer_profile_revisions'


def test_fresh_bootstrap_has_owned_profile_revision_storage_without_fake_history(review_db):
 engine=review_db.get_bind()
 with engine.connect() as connection:
  inspector=inspect(connection)
  assert TABLE in inspector.get_table_names()
  assert {'id','profile_id','revision_number','raw_content','file_format','slicer','category','changelog','created_by','created_at'} <= {column['name'] for column in inspector.get_columns(TABLE)}
  assert connection.execute(text(f'SELECT COUNT(*) FROM {TABLE}')).scalar_one()==0


def legacy_profiles(tmp_path):
 engine=create_engine(f"sqlite:///{tmp_path/'legacy-profile.db'}")
 with engine.begin() as connection:
  connection.execute(text('CREATE TABLE printer_profiles(id INTEGER PRIMARY KEY,created_by INTEGER,org_id INTEGER,raw_content TEXT,file_format TEXT,slicer TEXT,category TEXT)'))
  connection.execute(text("INSERT INTO printer_profiles VALUES(7,3,1,:content,'json','orca','process')"),{'content':'{\r\n  "layer_height": "0.2"\r\n}\r\n'})
 return engine


def test_repeated_profile_migration_preserves_exact_legacy_content_and_no_invented_revisions(tmp_path):
 migration=importlib.import_module(MODULE);engine=legacy_profiles(tmp_path)
 try:
  for _ in range(2):
   with engine.begin() as connection:migration.apply(connection);migration.validate(connection)
  with engine.connect() as connection:
   assert connection.execute(text('SELECT raw_content FROM printer_profiles WHERE id=7')).scalar_one()=='{\r\n  "layer_height": "0.2"\r\n}\r\n'
   assert connection.execute(text(f'SELECT COUNT(*) FROM {TABLE}')).scalar_one()==0
 finally:engine.dispose()


def test_malformed_adopted_profile_revision_table_is_rejected_without_altering_original(tmp_path):
 migration=importlib.import_module(MODULE);engine=legacy_profiles(tmp_path)
 try:
  with engine.begin() as connection:connection.execute(text(f'CREATE TABLE {TABLE}(id TEXT PRIMARY KEY,profile_id INTEGER,revision_number INTEGER,raw_content TEXT)'))
  with pytest.raises(Exception,match='shape mismatch'):
   with engine.begin() as connection:migration.apply(connection)
  with engine.connect() as connection:
   assert connection.execute(text('SELECT raw_content FROM printer_profiles WHERE id=7')).scalar_one()=='{\r\n  "layer_height": "0.2"\r\n}\r\n'
   assert {column['name'] for column in inspect(connection).get_columns(TABLE)}=={'id','profile_id','revision_number','raw_content'}
 finally:engine.dispose()

@pytest.mark.parametrize('change', ['weak_check','missing_unique','reversed_unique','wrong_cascade'])
def test_adopted_profile_history_rejects_weakened_constraints(tmp_path,change):
 migration=importlib.import_module(MODULE);engine=legacy_profiles(tmp_path)
 clauses={'weak_check':'CHECK(revision_number >= 0), UNIQUE(profile_id,revision_number)',
 'missing_unique':'CHECK(revision_number > 0)',
 'reversed_unique':'CHECK(revision_number > 0), UNIQUE(revision_number,profile_id)',
 'wrong_cascade':'CHECK(revision_number > 0), UNIQUE(profile_id,revision_number)'}
 action='RESTRICT' if change=='wrong_cascade' else 'CASCADE'
 try:
  with engine.begin() as connection:
   connection.execute(text(f'CREATE TABLE {TABLE}(id INTEGER PRIMARY KEY NOT NULL,profile_id INTEGER NOT NULL REFERENCES printer_profiles(id) ON DELETE {action},revision_number INTEGER NOT NULL,raw_content TEXT NOT NULL,file_format TEXT NOT NULL,slicer TEXT NOT NULL,category TEXT NOT NULL,changelog TEXT,created_by INTEGER,created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,{clauses[change]})'))
  with pytest.raises(Exception,match='shape mismatch'):
   with engine.begin() as connection:migration.apply(connection)
  with engine.connect() as connection:
   assert connection.execute(text(f'SELECT COUNT(*) FROM {TABLE}')).scalar_one()==0
   assert connection.execute(text('SELECT raw_content FROM printer_profiles WHERE id=7')).scalar_one()=='{\r\n  "layer_height": "0.2"\r\n}\r\n'
 finally:engine.dispose()


def test_recorded_profile_migration_revalidates_later_constraint_drift(tmp_path):
 from core.schema.migrator import apply_python_migration
 from sqlalchemy.schema import CreateTable
 migration=importlib.import_module(MODULE);engine=legacy_profiles(tmp_path)
 try:
  with engine.begin() as connection:assert apply_python_migration(connection,MODULE)
  with engine.begin() as connection:
   connection.execute(text(f'DROP TABLE {TABLE}'))
   weakened=str(CreateTable(migration._TABLE).compile(dialect=connection.dialect)).replace('revision_number > 0','revision_number >= 0')
   connection.execute(text(weakened))
  with pytest.raises(Exception,match='check shape mismatch'):
   with engine.begin() as connection:apply_python_migration(connection,MODULE)
  with engine.connect() as connection:
   assert connection.execute(text('SELECT raw_content FROM printer_profiles WHERE id=7')).scalar_one()=='{\r\n  "layer_height": "0.2"\r\n}\r\n'
 finally:engine.dispose()
