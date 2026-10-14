"""Affected actualPG fixed historical SQL/ledger contract; prior6phases separate."""
import os,secrets,tempfile,uuid,json,shutil
from pathlib import Path

def main():
 with tempfile.TemporaryDirectory(prefix='fixed25-history-owned-') as folder:
  root=Path(folder);root.chmod(0o700);password=root/'credential';password.write_text(secrets.token_urlsafe(48));password.chmod(0o600)
  admin_password=root/'maintenance';admin_password.write_text(secrets.token_urlsafe(48));admin_password.chmod(0o600)
  raw=os.environ['GLOBAL_ORIGIN_NATIVE_URL'];maintenance=raw.replace('postgresql+psycopg://','postgresql://');target=maintenance.rsplit('/',1)[0]+'/fixed_history_target'
  os.environ.update(DATABASE_URL=target,DATABASE_PASSWORD_FILE=str(password),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
  from sqlalchemy import create_engine,text
  admin=create_engine(raw,isolation_level='AUTOCOMMIT',hide_parameters=True);name='odin_validate_'+uuid.uuid4().hex[:20]
  with admin.connect() as c:c.exec_driver_sql('CREATE DATABASE '+name);c.exec_driver_sql('CREATE DATABASE fixed_history_target')
  from core.database_config import create_database_engine
  from core.schema import bootstrap_database
  from core.schema.pre026_restore_contract import owned_stage_database,bootstrap_stage,validate_postgres_history,EXPECTED_POSTGRES_LEDGER,SQL_SOURCE_PINS,validate_sql_inventory,check_sources
  from core.schema.migrator import MigrationError
  from modules.system import postgres_backup_service as b
  oracle=json.loads((Path(__file__).resolve().parents[2]/'artifacts/edu-workflow-check/pre026-independent-history-oracle.json').read_text())
  assert tuple(tuple(row) for row in oracle['expected_postgres_ledger'])==EXPECTED_POSTGRES_LEDGER and oracle['sql_source_sha256']==SQL_SOURCE_PINS
  source=maintenance.rsplit('/',1)[0]+'/'+name;old=create_database_engine(source,role='restore',password_file=str(password))
  bootstrap_stage(owned_stage_database(old,name))
  with old.connect() as c:
   actual=tuple(tuple(row) for row in c.execute(text('SELECT migration_id,checksum,dialect FROM odin_schema_migrations ORDER BY migration_id')))
   assert actual==tuple(tuple(row) for row in oracle['expected_postgres_ledger'])
   validate_postgres_history(c)
  print('fixed25-native-sourceindependent50ledger-24SQL-oracle-actualPG-stage-pass',flush=True)
  # SQL byte/path drift is tested in owned copies, never editing mounted source.
  copy=root/'sql-copy';backend=Path(__file__).resolve().parents[2]/'backend'
  for key in SQL_SOURCE_PINS:
   path=copy/key;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(backend/key,path)
  first=copy/next(iter(SQL_SOURCE_PINS));original=first.read_bytes()
  for mode in ('changed','new','missing'):
   if mode=='changed':first.write_bytes(original+b'\n-- unqualified\n')
   elif mode=='missing':first.unlink()
   else:(copy/'core/migrations/999_unqualified.sql').write_text('SELECT 1;')
   try:validate_sql_inventory(copy)
   except MigrationError:pass
   else:raise AssertionError('SQL drift silently redefined025')
   first.write_bytes(original);(copy/'core/migrations/999_unqualified.sql').unlink(missing_ok=True)
  validate_sql_inventory(copy)
  print('fixed25-native-changed-new-missingSQL-beforeexecution-refusal-pass',flush=True)
  engine=create_database_engine(target,role='restore',password_file=str(password));bootstrap_database(engine)
  b.postgres_paths(root).backups.mkdir(parents=True,exist_ok=True)
  archive=root/'fixed25.dump'
  def dump():b._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(archive),'--dbname',b._credentialless_cli_url(source,'odin-backup')],source,str(password),root)
  dump();sha=b._sha256(archive)
  args={'password_file':str(password),'maintenance_password_file':str(admin_password),'base_dir':root}
  good=b.validate_postgres_restore(archive,target,maintenance,**args)
  assert good['sha256']==sha and good['prepared_schema_version']==26 and good['restore_validated']
  print('fixed25-native-actual025archive-fixedhistory-import-before26-positive-originalhash-pass',flush=True)
  with old.begin() as c:c.execute(text("UPDATE odin_schema_migrations SET checksum=:bad WHERE migration_id='core/migrations/001_initial.sql'"),{'bad':'0'*64})
  dump()
  try:b.validate_postgres_restore(archive,target,maintenance,**args)
  except b.BackupValidationError as e:assert 'fixed packaged contract' in str(e)
  else:raise AssertionError('UploadedSQLhistory redefined trusted025')
  with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==0
  assert not list(b.postgres_paths(root).backups.glob('.odin-trusted025-*')) and not list(b.postgres_paths(root).backups.glob('.odin-upgraded026-*'))
  old.dispose();engine.dispose();check_sources()
  with admin.connect() as c:
   assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE 'odin_validate_%' AND datname<>:name"),{'name':name}).scalar_one()==0
   c.exec_driver_sql('DROP DATABASE '+name+' WITH (FORCE)');c.exec_driver_sql('DROP DATABASE fixed_history_target WITH (FORCE)')
  admin.dispose()
  print('fixed25-native-forgedhistoricalSQLchecksum-refusal-zero-targetwork-ownedcleanup-pass',flush=True)
 print('fixed25-native-pass',flush=True)
if __name__=='__main__':main()
