"""Owned actual025 archive→trusted stage→normal026 cutover and recovery."""
import os,secrets,tempfile,uuid,json
from pathlib import Path

def main():
 with tempfile.TemporaryDirectory(prefix='historical25-owned-') as folder:
  root=Path(folder);root.chmod(0o700);credential=root/'credential';credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
  maintenance_credential=root/'maintenance';maintenance_credential.write_text(secrets.token_urlsafe(48));maintenance_credential.chmod(0o600)
  raw=os.environ['GLOBAL_ORIGIN_NATIVE_URL'];maintenance=raw.replace('postgresql+psycopg://','postgresql://');source=maintenance.rsplit('/',1)[0]+'/historical_target'
  os.environ.update(DATABASE_URL=source,DATABASE_PASSWORD_FILE=str(credential),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
  from sqlalchemy import text,create_engine
  from sqlalchemy.orm import Session
  admin=create_engine(raw,isolation_level='AUTOCOMMIT',hide_parameters=True)
  old_name='odin_validate_'+uuid.uuid4().hex[:20]
  with admin.connect() as c:c.exec_driver_sql('CREATE DATABASE '+old_name);c.exec_driver_sql('CREATE DATABASE historical_target')
  from core.database_config import create_database_engine
  from core.schema import bootstrap_database
  from core.schema.pre026_restore_contract import owned_stage_database,bootstrap_stage,validate_inventory,check_sources
  from core.economic_identity import _add_created
  from core.economic_source_creation import _create_spool
  from core.spool_purchase_evidence import write_purchase
  from modules.orders.models import Order
  from modules.system import postgres_backup_service as b
  from modules.system.backup_service import BackupValidationError
  from modules.system.postgres_canonical_executables import validate_executables
  from core.postgres_restore_gate import RestoreConsumerBlocked
  old_url=maintenance.rsplit('/',1)[0]+'/'+old_name
  old=create_database_engine(old_url,role='restore',password_file=str(credential))
  cap=owned_stage_database(old,old_name);bootstrap_stage(cap)
  actor={'id':3,'role':'admin','group_id':1,'_auth_kind':'bearer_jwt'}
  with Session(old) as db:
   live=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
   gone=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
   _add_created(db,Order(org_id=7),'order',owner_org_id=7)
   legacy_uids=set(db.execute(text('SELECT economic_uid FROM orders WHERE org_id IS NULL')).scalars())
   db.execute(text('DELETE FROM orders WHERE id=:id'),{'id':gone.id})
   db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(80,'Owned','Fixture','PLA')"))
   spool=_create_spool(db,{'filament_id':80,'org_id':1,'price':10,'initial_weight_g':1000,'status':'active'},owner_org_id=1)
   write_purchase(db,spool.id,actor,{'currency':'USD','price':'0','net_weight_g':'1000.125'},0);write_purchase(db,spool.id,actor,None,1)
   active=_create_spool(db,{'filament_id':80,'org_id':1,'status':'active'},owner_org_id=1)
   write_purchase(db,active.id,actor,{'currency':'EUR','price':'12.34','net_weight_g':'950'},0)
   db.execute(text("INSERT INTO system_config(key,value) VALUES('historical_marker','\"source25\"')"));db.commit()
  def rows(engine,table,order):
   with engine.connect() as c:return [json.loads(x) for x in c.execute(text('SELECT row_to_json(t)::text FROM '+table+' t ORDER BY '+order)).scalars()]
  core_tables=[('orders','id'),('cost_identities','identity_uid'),('spools','id'),('cost_source_lifetimes','birth_nonce'),('spool_purchase_evidence','evidence_nonce')]
  original={table:rows(old,table,order) for table,order in core_tables}
  archive=root/'original025.dump'
  def dump_old(path):b._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments','--file',str(path),'--dbname',b._credentialless_cli_url(old_url,'odin-backup')],old_url,str(credential),root)
  dump_old(archive);original_sha=b._sha256(archive)
  with old.connect() as c:validate_inventory(c)
  engine=create_database_engine(source,role='restore',password_file=str(credential));bootstrap_database(engine)
  def add_origin(c):
   nonce=uuid.uuid4().hex;uid=str(uuid.uuid4());id=c.execute(text('INSERT INTO orders(economic_global_origin_nonce) VALUES(:n) RETURNING id'),{'n':nonce}).scalar_one()
   c.execute(text("INSERT INTO cost_identities VALUES(:u,'order',:id,'global',0)"),{'u':uid,'id':id});c.execute(text('UPDATE orders SET economic_uid=:u WHERE id=:id'),{'u':uid,'id':id})
   c.execute(text("INSERT INTO cost_global_order_origins(order_uid,resource_kind,owner_scope,owner_key,origin_nonce,proof_kind) VALUES(:u,'order','global',0,:n,'verified_platform_admin_global_creation')"),{'u':uid,'n':nonce});c.execute(text('DELETE FROM orders WHERE id=:id'),{'id':id})
  with engine.begin() as c:add_origin(c);c.execute(text("INSERT INTO system_config(key,value) VALUES('historical_marker','\"target26\"')"))
  def snapshot():return {table:rows(engine,table,order) for table,order in core_tables+[('cost_global_order_origins','order_uid'),('cost_global_origin_exclusions','order_uid')]}
  def marker():
   with engine.connect() as c:return c.execute(text("SELECT value FROM system_config WHERE key='historical_marker'")).scalar_one()
  target_before=snapshot()
  args={'password_file':str(credential),'maintenance_password_file':str(maintenance_credential),'base_dir':root}
  b.postgres_paths(root).backups.mkdir(parents=True,exist_ok=True) # Actual upload/staging API precondition.
  metadata=b.validate_postgres_restore(archive,source,maintenance,**args)
  assert metadata['sha256']==original_sha and metadata['prepared_sha256']!=original_sha and metadata['source_schema_version']==25 and metadata['prepared_schema_version']==26
  assert snapshot()==target_before and marker()=='target26' and b._sha256(archive)==original_sha
  print('historical25-native-qualified-real025-staging-schema-17-24-originalhash-currenttarget-untouched-pass',flush=True)
  # Full uploaded history must match before any normal26 adoption/cutover.
  bad=root/'bad-history.dump'
  with old.begin() as c:c.execute(text("DELETE FROM odin_schema_migrations WHERE migration_id='python:025-spool-purchase-evidence'"))
  dump_old(bad)
  try:b.validate_postgres_restore(bad,source,maintenance,**args)
  except BackupValidationError as e:assert 'migration history differs' in str(e)
  else:raise AssertionError('Missing historical025 ledger adopted')
  assert snapshot()==target_before
  # Source-owned after-import fault leaves live target and original intact.
  real_restore=b._restore_postgres_archive
  def fail_after_import(*a,**kw):
   result=real_restore(*a,**kw)
   if kw.get('_contract') is not None:raise RuntimeError('Owned after025 data import failure')
   return result
  b._restore_postgres_archive=fail_after_import
  try:
   try:b.validate_postgres_restore(archive,source,maintenance,**args)
   except RuntimeError as e:assert 'after025' in str(e)
   else:raise AssertionError('After-import fault did not fire')
  finally:b._restore_postgres_archive=real_restore
  assert snapshot()==target_before and b._sha256(archive)==original_sha
  assert not list(b.postgres_paths(root).backups.glob('.odin-trusted025-*')) and not list(b.postgres_paths(root).backups.glob('.odin-upgraded026-*'))
  print('historical25-native-missingledger-noadoption-postimportfault-zero-target-mutation-cleanup-pass',flush=True)
  staged=b.stage_postgres_restore(archive,source,maintenance,**args)
  assert staged['candidate_sha256']==original_sha and b._sha256(b.postgres_paths(root).pending)==original_sha
  engine.dispose();result=b.apply_pending_postgres_restore(source,maintenance,acknowledgement=original_sha,**args)
  assert result['status']=='restored' and result['candidate_sha256']==original_sha and marker()=='source25'
  after=snapshot()
  for table,expected in original.items():
   actual=after[table]
   if table=='orders':actual=[{k:v for k,v in row.items() if k!='economic_global_origin_nonce'} for row in actual]
   assert actual==expected
  assert after['cost_global_order_origins']==[] and {r['order_uid'] for r in after['cost_global_origin_exclusions']}==legacy_uids
  with engine.connect() as c:
   assert c.execute(text('SELECT count(*) FROM orders WHERE economic_global_origin_nonce IS NOT NULL')).scalar_one()==0
   assert c.execute(text("SELECT count(*) FROM odin_schema_migrations WHERE migration_id='python:026-global-order-origins'")).scalar_one()==1
   validate_executables(c)
  print('historical25-native-actual-stage-originalACK-dataonly-restore-importBEFORE26-allretainedglobalUIDsexcluded-purchase-org-preserved-pass',flush=True)
  with engine.begin() as c:add_origin(c);c.execute(text("UPDATE system_config SET value='\"rollback26\"' WHERE key='historical_marker'"))
  rollback_state=snapshot()
  b.stage_postgres_restore(archive,source,maintenance,**args);engine.dispose()
  try:b.apply_pending_postgres_restore(source,maintenance,acknowledgement=original_sha,fail_after_restore=True,**args)
  except RuntimeError as e:assert 'Injected post-restore' in str(e)
  else:raise AssertionError('Postrestore fault did not fire')
  assert snapshot()==rollback_state and marker()=='rollback26'
  print('historical25-native-actual-postrestore-failure-distinct26-rollback-history-pass',flush=True)
  b.stage_postgres_restore(archive,source,maintenance,**args);engine.dispose();calls=[]
  def fail_recovery(*a,**kw):
   if a[1]==source:
    calls.append(True)
    if len(calls)==2:raise RuntimeError('Owned26 rollback failure')
   return real_restore(*a,**kw)
  b._restore_postgres_archive=fail_recovery
  try:
   try:b.apply_pending_postgres_restore(source,maintenance,acknowledgement=original_sha,fail_after_restore=True,**args)
   except BackupValidationError as e:assert 'recovery incomplete' in str(e)
   else:raise AssertionError('Rollback failure did not fire')
  finally:b._restore_postgres_archive=real_restore
  barrier,barrier_engine=b._FAILED_RESTORE_BARRIERS.pop(source);barrier.close();barrier_engine.dispose()
  blocked=None
  try:
   blocked=create_database_engine(source,role='api',password_file=str(credential))
   with blocked.connect():pass
  except RestoreConsumerBlocked:pass
  else:raise AssertionError('Unfinished historical restore admitted ordinary consumer')
  finally:
   if blocked is not None:blocked.dispose()
  engine.dispose()
  recovery=b.apply_pending_postgres_restore(source,maintenance,acknowledgement=original_sha,**args)
  assert recovery['status']=='rolled_back_after_interruption' and snapshot()==rollback_state and marker()=='rollback26'
  assert not b.postgres_paths(root).manifest.exists() and b._sha256(archive)==original_sha
  ordinary=create_database_engine(source,role='api',password_file=str(credential))
  with ordinary.connect() as c:assert c.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one()==len(rollback_state['cost_global_order_origins'])
  ordinary.dispose();engine.dispose();old.dispose();check_sources()
  with admin.connect() as c:
   assert c.execute(text("SELECT count(*) FROM pg_database WHERE datname LIKE 'odin_validate_%' AND datname<>:old"),{'old':old_name}).scalar_one()==0
   c.exec_driver_sql('DROP DATABASE historical_target WITH (FORCE)');c.exec_driver_sql('DROP DATABASE '+old_name+' WITH (FORCE)')
  admin.dispose()
  assert not list(b.postgres_paths(root).backups.glob('.odin-upgraded026-*'))
  print('historical25-native-failedrollback-sessionend-durablegate-validated26-recovery-alone-reopens-ownedstage-cleanup-original-preserved-pass',flush=True)
 print('historical25-native-pass',flush=True)
if __name__=='__main__':main()
