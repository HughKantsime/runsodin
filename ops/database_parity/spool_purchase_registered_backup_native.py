"""Affected025 registered app backup/data-only restore; owned tmpfs DB only."""
import os,secrets,tempfile
from pathlib import Path

def main():
 with tempfile.TemporaryDirectory(prefix='purchase-registered-owned-') as folder:
  root=Path(folder);root.chmod(0o700);credential=root/'credential';credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
  maintenance_credential=root/'maintenance-credential';maintenance_credential.write_text(secrets.token_urlsafe(48));maintenance_credential.chmod(0o600)
  connection_url=os.environ['SPOOL_PURCHASE_NATIVE_URL']
  maintenance=connection_url.replace('postgresql+psycopg://','postgresql://')
  source=maintenance.rsplit('/',1)[0]+'/purchase_registered'
  os.environ.update(DATABASE_URL=source,DATABASE_PASSWORD_FILE=str(credential),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
  from sqlalchemy import text,create_engine
  admin=create_engine(connection_url,isolation_level="AUTOCOMMIT",hide_parameters=True)
  with admin.connect() as c:c.exec_driver_sql("CREATE DATABASE purchase_registered")
  from sqlalchemy.orm import Session
  from core.database_config import create_database_engine
  from core.schema import bootstrap_database
  from core.economic_source_creation import _create_spool
  from core.spool_purchase_evidence import write_purchase
  from modules.system import postgres_backup_service as backup
  from modules.system.backup_service import BackupValidationError
  from modules.system.postgres_canonical_executables import validate_executables,canonical_functions,canonical_triggers
  from core.postgres_restore_gate import RestoreConsumerBlocked
  engine=create_database_engine(source,role='restore',password_file=str(credential))
  first=bootstrap_database(engine);assert 'python:025-spool-purchase-evidence' in first['applied']
  assert bootstrap_database(engine)['applied']==[]
  assert (len(canonical_functions()),len(canonical_triggers()))==(17,24)
  recovery_only=os.environ.get('SPOOL_PURCHASE_RECOVERY_ONLY')=='1'
  actor={'id':3,'role':'admin','group_id':1,'_auth_kind':'bearer_jwt'}
  with Session(engine) as db:
   db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Fixture','PLA')"))
   db.execute(text("INSERT INTO system_config(key,value) VALUES('purchase_marker','\"original\"')"))
   spool=_create_spool(db,{'filament_id':80,'org_id':1,'initial_weight_g':1000,'price':10,'status':'active'},owner_org_id=1)
   write_purchase(db,spool.id,actor,{'currency':'USD','price':'0','net_weight_g':'1000.125'},0)
   write_purchase(db,spool.id,actor,{'currency':'EUR','price':'12.34','net_weight_g':'950'},1)
   write_purchase(db,spool.id,actor,None,2)
   active=_create_spool(db,{'filament_id':80,'org_id':1,'status':'active'},owner_org_id=1)
   write_purchase(db,active.id,actor,{'currency':'JPY','price':'0','net_weight_g':'0.001'},0)
   db.commit()
  def snapshot():
   with engine.connect() as c:
    return {name:c.execute(text('SELECT row_to_json(t)::text FROM '+name+' t ORDER BY '+order)).scalars().all()
     for name,order in [('spool_purchase_evidence','evidence_nonce'),('spools','id'),('cost_source_lifetimes','birth_nonce'),('cost_identities','identity_uid')]}
  def marker():
   with engine.connect() as c:return c.execute(text("SELECT value FROM system_config WHERE key='purchase_marker'")).scalar_one()
  original=snapshot()
  with engine.connect() as c:validate_executables(c)
  negative=["ALTER FUNCTION public.spool_purchase_evidence_guard() SECURITY DEFINER",
   "ALTER FUNCTION public.spools_purchase_guard() SET search_path TO public",
   "CREATE FUNCTION public.spool_purchase_evidence_guard(integer) RETURNS integer LANGUAGE sql AS 'SELECT 1'",
   "ALTER TABLE public.spool_purchase_evidence DISABLE TRIGGER spool_purchase_evidence_no_truncate",
   "DROP TRIGGER spool_purchase_evidence_no_truncate ON public.spool_purchase_evidence",
   "DROP TRIGGER spool_purchase_evidence_no_truncate ON public.spool_purchase_evidence; CREATE TRIGGER spool_purchase_evidence_no_truncate BEFORE TRUNCATE ON public.spool_purchase_evidence FOR EACH STATEMENT EXECUTE FUNCTION public.spools_purchase_guard()"]
  for sql in ([] if recovery_only else negative):
   with engine.connect() as c:
    for part in sql.split(';'):c.exec_driver_sql(part)
    try:validate_executables(c)
    except BackupValidationError:pass
    else:raise AssertionError('Altered025 executable accepted')
    c.rollback()
  print('spool-purchase-registered-canonical17-24-baseline-pass' if recovery_only else 'spool-purchase-registered-canonical17-24-negatives-pass',flush=True)
  args={'password_file':str(credential),'maintenance_password_file':str(maintenance_credential),'base_dir':root}
  candidate,metadata=backup.create_postgres_backup(source,maintenance_url=maintenance,**args)
  assert metadata['restore_validated'] is True and snapshot()==original
  if not recovery_only:
   with engine.begin() as c:c.execute(text("UPDATE system_config SET value='\"changed\"' WHERE key='purchase_marker'"))
   backup.stage_postgres_restore(candidate,source,maintenance,**args);engine.dispose()
   result=backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],**args)
   assert result['status']=='restored' and marker()=='original' and snapshot()==original
   with engine.connect() as c:validate_executables(c)
   print('spool-purchase-registered-app-backup-dataonly-history-counter-restore-pass',flush=True)
   with engine.begin() as c:c.execute(text("UPDATE system_config SET value='\"rollback-original\"' WHERE key='purchase_marker'"))
   backup.stage_postgres_restore(candidate,source,maintenance,**args);engine.dispose()
   try:backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],fail_after_restore=True,**args)
   except RuntimeError as exc:assert 'Injected post-restore failure' in str(exc)
   else:raise AssertionError('Expected injected failure')
   assert marker()=='rollback-original' and snapshot()==original and not backup.postgres_paths(root).manifest.exists()
   with engine.connect() as c:validate_executables(c)
   print('spool-purchase-registered-actual-rollback-pass',flush=True)
   # Same-name malicious archive function is evidence only; never executed.
   with engine.begin() as c:c.exec_driver_sql("CREATE OR REPLACE FUNCTION public.spool_purchase_evidence_guard() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog,public AS $$ BEGIN UPDATE public.system_config SET value='\"EXECUTED\"' WHERE key='purchase_marker'; RETURN NEW; END; $$")
   malicious=root/'malicious.dump'
   backup._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(malicious),'--dbname',backup._credentialless_cli_url(source,'odin-backup')],source,str(credential),root)
   try:backup.validate_postgres_restore(malicious,source,maintenance,**args)
   except BackupValidationError as exc:assert 'schema/executables differ' in str(exc)
   else:raise AssertionError('Altered archive accepted')
   assert marker()=='rollback-original'
   with engine.begin() as c:
    from core.spool_purchase_schema import pg_functions
    c.exec_driver_sql('CREATE OR REPLACE FUNCTION public.spool_purchase_evidence_guard() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog,public AS $$'+pg_functions()['spool_purchase_evidence_guard']+'$$')
   print('spool-purchase-registered-malicious-marker-zero-execution-pass',flush=True)
  # Failed recovery leaves durable marker and exclusive barrier; session death
  # releases advisory lock but must not admit ordinary consumers.
  backup.stage_postgres_restore(candidate,source,maintenance,**args);engine.dispose()
  restore=backup._restore_postgres_archive;calls=[]
  def fail_recovery(*a,**kw):
   if a[1]==source:
    calls.append(True)
    if len(calls)==2:raise RuntimeError('Owned recovery failure')
   return restore(*a,**kw)
  backup._restore_postgres_archive=fail_recovery
  try:
   try:backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],fail_after_restore=True,**args)
   except BackupValidationError as exc:assert 'recovery incomplete' in str(exc)
   else:raise AssertionError('Expected recovery failure')
  finally:backup._restore_postgres_archive=restore
  barrier,barrier_engine=backup._FAILED_RESTORE_BARRIERS.pop(source);barrier.close();barrier_engine.dispose()
  manifest=backup._read_pending_manifest(backup.postgres_paths(root));assert manifest['state']=='restore_in_progress' and manifest['recovery_incomplete']
  blocked=None
  try:
   blocked=create_database_engine(source,role='api',password_file=str(credential))
   with blocked.connect():pass
  except RestoreConsumerBlocked:pass
  else:raise AssertionError('Incomplete durable restore admitted consumer')
  finally:
   if blocked is not None:blocked.dispose()
  engine.dispose()
  print('spool-purchase-registered-failedrecovery-sessiondeath-durablebarrier-pass',flush=True)
  with admin.connect() as c:c.exec_driver_sql('DROP DATABASE purchase_registered WITH (FORCE)')
  admin.dispose()
 print('spool-purchase-registered-recovery-only-pass' if recovery_only else 'spool-purchase-registered-backup-pass',flush=True)
if __name__=='__main__':main()
