"""Affected026 registered app backup/data-only restore; owned tmpfs DB only."""
import os,secrets,tempfile
from pathlib import Path

def main():
 with tempfile.TemporaryDirectory(prefix='origin-registered-owned-') as folder:
  root=Path(folder);root.chmod(0o700);credential=root/'credential';credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
  maintenance_credential=root/'maintenance-credential';maintenance_credential.write_text(secrets.token_urlsafe(48));maintenance_credential.chmod(0o600)
  connection_url=os.environ['GLOBAL_ORIGIN_NATIVE_URL']
  maintenance=connection_url.replace('postgresql+psycopg://','postgresql://')
  source=maintenance.rsplit('/',1)[0]+'/global_origin_registered'
  os.environ.update(DATABASE_URL=source,DATABASE_PASSWORD_FILE=str(credential),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
  from sqlalchemy import text,create_engine
  admin=create_engine(connection_url,isolation_level="AUTOCOMMIT",hide_parameters=True)
  with admin.connect() as c:c.exec_driver_sql("CREATE DATABASE global_origin_registered")
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
  # Exact owned pre026 fixture, not a production compatibility switch.
  import core.schema.bootstrap as schema_bootstrap
  import importlib
  from uuid import uuid4
  from core.economic_identity import _add_created
  from modules.orders.models import Order
  G=importlib.import_module('core.schema.migrations.026_global_order_origins').schema
  real_apply=schema_bootstrap.apply_python_migration;raw_tables=schema_bootstrap.RAW_REQUIRED_TABLES;raw_columns=schema_bootstrap.RAW_REQUIRED_COLUMNS
  schema_bootstrap.apply_python_migration=lambda c,name:False if name.endswith('.026_global_order_origins') else real_apply(c,name)
  schema_bootstrap.RAW_REQUIRED_TABLES=raw_tables-set(G.TABLES)
  before_columns=dict(raw_columns)
  for table in G.TABLES:before_columns.pop(table)
  before_columns['orders']=before_columns['orders']-{G.SOURCE};schema_bootstrap.RAW_REQUIRED_COLUMNS=before_columns
  try:before=bootstrap_database(engine)
  finally:schema_bootstrap.apply_python_migration=real_apply;schema_bootstrap.RAW_REQUIRED_TABLES=raw_tables;schema_bootstrap.RAW_REQUIRED_COLUMNS=raw_columns
  assert 'python:025-spool-purchase-evidence' in before['applied']
  with Session(engine) as db:
   live=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
   gone=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
   org=_add_created(db,Order(org_id=7),'order',owner_org_id=7)
   old_uids=set(db.execute(text("SELECT economic_uid FROM orders WHERE org_id IS NULL")).scalars())
   db.execute(text('DELETE FROM orders WHERE id=:id'),{'id':gone.id});db.commit()
  old_archive=root/'old025.dump'
  backup._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(old_archive),'--dbname',backup._credentialless_cli_url(source,'odin-backup')],source,str(credential),root)
  from sqlalchemy import event
  from core.schema.migrator import acquire_schema_writer_lock
  migration_id='python:026-global-order-origins'
  def history_fault(c,cursor,statement,parameters,context,executemany):
   values=parameters.values() if isinstance(parameters,dict) else parameters
   if statement.startswith('INSERT INTO odin_schema_migrations') and migration_id in values:raise RuntimeError('Owned026 history INSERT failure')
  event.listen(engine,'before_cursor_execute',history_fault)
  try:
   try:bootstrap_database(engine)
   except RuntimeError as e:assert 'history INSERT' in str(e)
   else:raise AssertionError('History fault did not fire')
  finally:event.remove(engine,'before_cursor_execute',history_fault)
  with engine.connect() as c:
   from sqlalchemy import inspect
   assert not set(G.TABLES)&set(inspect(c).get_table_names()) and G.SOURCE not in {x['name'] for x in inspect(c).get_columns('orders')}
   assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':migration_id}).scalar_one()==0
  from concurrent.futures import ThreadPoolExecutor
  def starter(_):
   try:return bootstrap_database(engine)
   except Exception as e:
    if getattr(getattr(e,'orig',None),'sqlstate',None)!='55P03':raise
    return {'bounded_refusal':True,'applied':[]}
  with ThreadPoolExecutor(max_workers=2) as pool:starts=list(pool.map(starter,range(2)))
  assert sum(migration_id in r['applied'] for r in starts)==1
  assert bootstrap_database(engine)['applied']==[]
  with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':migration_id}).scalar_one()==1
  print('global-origin-registered-history-INSERT-atomicrollback-concurrent-singlehistory-pass;bounded_refusals='+str(sum(bool(r.get('bounded_refusal')) for r in starts)),flush=True)
  with engine.connect() as first,engine.connect() as second:
   acquire_schema_writer_lock(first)
   second.exec_driver_sql("SET LOCAL lock_timeout='50ms'")
   try:acquire_schema_writer_lock(second)
   except Exception as e:assert e.orig.sqlstate=='55P03';second.rollback()
   else:raise AssertionError('Writer advisory contention was not bounded')
   first.rollback()
   second.exec_driver_sql("SET LOCAL lock_timeout='50ms'");acquire_schema_writer_lock(second)
   assert second.execute(text("SELECT setting FROM pg_settings WHERE name='lock_timeout'")).scalar_one()=='50'
   second.rollback()
  print('global-origin-registered-finite-advisory-stricterbound-fullrollback-release-pass',flush=True)
  with engine.connect() as c:
   assert set(c.execute(text('SELECT order_uid FROM cost_global_origin_exclusions')).scalars())==old_uids
   assert c.execute(text('SELECT count(*) FROM orders WHERE economic_global_origin_nonce IS NOT NULL')).scalar_one()==0
  def add_origin(c,deleted=False):
   nonce=uuid4().hex;uid=str(uuid4())
   identifier=c.execute(text('INSERT INTO orders(economic_global_origin_nonce) VALUES(:n) RETURNING id'),{'n':nonce}).scalar_one()
   c.execute(text("INSERT INTO cost_identities VALUES(:u,'order',:id,'global',0)"),{'u':uid,'id':identifier})
   c.execute(text('UPDATE orders SET economic_uid=:u WHERE id=:id'),{'u':uid,'id':identifier})
   c.execute(text("INSERT INTO cost_global_order_origins(order_uid,resource_kind,owner_scope,owner_key,origin_nonce,proof_kind) VALUES(:u,'order','global',0,:n,'verified_platform_admin_global_creation')"),{'u':uid,'n':nonce})
   if deleted:c.execute(text('DELETE FROM orders WHERE id=:id'),{'id':identifier})
   return uid
  with engine.begin() as c:add_origin(c);add_origin(c,deleted=True);G.validate(c)
  print('global-origin-registered-actual025-upgrade-live-deleted-cutover-pass',flush=True)
  assert bootstrap_database(engine)['applied']==[]
  assert (len(canonical_functions()),len(canonical_triggers()))==(20,29)
  recovery_only=False
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
     for name,order in [('spool_purchase_evidence','evidence_nonce'),('spools','id'),('cost_source_lifetimes','birth_nonce'),('cost_identities','identity_uid'),('orders','id'),('cost_global_order_origins','order_uid'),('cost_global_origin_exclusions','order_uid')]}
  def marker():
   with engine.connect() as c:return c.execute(text("SELECT value FROM system_config WHERE key='purchase_marker'")).scalar_one()
  original=snapshot()
  with engine.connect() as c:validate_executables(c)
  negative=["ALTER FUNCTION public.orders_global_origin_guard() SECURITY DEFINER",
   "ALTER TABLE public.cost_global_order_origins DISABLE TRIGGER cost_global_order_origins_no_truncate",
   "DROP TRIGGER cost_global_origin_exclusions_no_truncate ON public.cost_global_origin_exclusions; CREATE TRIGGER cost_global_origin_exclusions_no_truncate BEFORE TRUNCATE ON public.cost_global_origin_exclusions FOR EACH STATEMENT EXECUTE FUNCTION public.cost_global_order_origins_guard()",
   "ALTER FUNCTION public.spool_purchase_evidence_guard() SECURITY DEFINER",
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
  print('global-origin-registered-canonical20-29-baseline-pass' if recovery_only else 'global-origin-registered-canonical20-29-negatives-pass',flush=True)
  args={'password_file':str(credential),'maintenance_password_file':str(maintenance_credential),'base_dir':root}
  # Actual historical archive refusal is a measured gap, not successful recovery.
  from sqlalchemy import event
  from sqlalchemy.engine import Engine
  observed=[];restore_calls=[];real_restore=backup._restore_postgres_archive
  def observe(c,cursor,statement,parameters,context,executemany):
   if statement.strip().upper().startswith(('CREATE DATABASE','TRUNCATE')):observed.append(statement.split()[0])
  def record_restore(*a,**kw):restore_calls.append(True);return real_restore(*a,**kw)
  event.listen(Engine,'before_cursor_execute',observe);backup._restore_postgres_archive=record_restore
  try:
   try:backup.validate_postgres_restore(old_archive,source,maintenance,**args)
   except BackupValidationError as e:assert 'missing required ODIN tables' in str(e)
   else:raise AssertionError('Historical025 archive unexpectedly accepted')
  finally:event.remove(Engine,'before_cursor_execute',observe);backup._restore_postgres_archive=real_restore
  assert observed==[] and restore_calls==[] and snapshot()==original and marker()=='original'
  print('global-origin-registered-old025-archive-refused-zero-target-work-HISTORICAL-RECOVERY-OPEN',flush=True)
  candidate,metadata=backup.create_postgres_backup(source,maintenance_url=maintenance,**args)
  assert metadata['restore_validated'] is True and snapshot()==original
  if not recovery_only:
   with engine.begin() as c:
    c.execute(text("UPDATE system_config SET value='\"changed\"' WHERE key='purchase_marker'"));add_origin(c,deleted=True)
   backup.stage_postgres_restore(candidate,source,maintenance,**args);engine.dispose()
   result=backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],**args)
   assert result['status']=='restored' and marker()=='original' and snapshot()==original
   with engine.connect() as c:validate_executables(c)
   print('global-origin-registered-app-backup-dataonly-history-counter-restore-pass',flush=True)
   with engine.begin() as c:
    c.execute(text("UPDATE system_config SET value='\"rollback-original\"' WHERE key='purchase_marker'"));add_origin(c,deleted=True)
   rollback_snapshot=snapshot()
   backup.stage_postgres_restore(candidate,source,maintenance,**args);engine.dispose()
   try:backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],fail_after_restore=True,**args)
   except RuntimeError as exc:assert 'Injected post-restore failure' in str(exc)
   else:raise AssertionError('Expected injected failure')
   assert marker()=='rollback-original' and snapshot()==rollback_snapshot and not backup.postgres_paths(root).manifest.exists()
   with engine.connect() as c:validate_executables(c)
   print('global-origin-registered-actual-rollback-pass',flush=True)
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
   print('global-origin-registered-malicious-marker-zero-execution-pass',flush=True)
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
  print('global-origin-registered-failedrecovery-sessiondeath-durablebarrier-pass',flush=True)
  # Only actual validated rollback recovery releases the durable consumer gate.
  recovered=backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],**args)
  assert recovered['status']=='rolled_back_after_interruption' and not backup.postgres_paths(root).manifest.exists()
  assert snapshot()==rollback_snapshot
  engine.dispose()
  ordinary=create_database_engine(source,role='api',password_file=str(credential))
  try:
   with ordinary.connect() as c:assert c.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one()==len(rollback_snapshot['cost_global_order_origins'])
  finally:ordinary.dispose()
  print('global-origin-registered-validated-recovery-alone-clears-consumer-gate-pass',flush=True)

  with admin.connect() as c:c.exec_driver_sql('DROP DATABASE global_origin_registered WITH (FORCE)')
  admin.dispose()
 print('global-origin-registered-recovery-only-pass' if recovery_only else 'global-origin-registered-backup-pass',flush=True)
if __name__=='__main__':main()
