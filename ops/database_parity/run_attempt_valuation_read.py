"""Owned PostgreSQL attempt-valuation-read probe; private files, internal network only."""
import hashlib,json,subprocess,time,uuid,secrets,os
from pathlib import Path
from ops.database_parity.run_ams_adoption import IMAGE,run
root=Path.cwd().resolve();name='odin-attempt-valuation-read-'+uuid.uuid4().hex[:12];database=name+'-db';runtime=name+'-runtime'
paths=['backend/core/attempt_start_plan.py', 'backend/core/attempt_start_transaction.py', 'backend/core/dispatch_observations.py', 'backend/core/dispatch_observation_executor.py', 'backend/core/order_production.py', 'backend/modules/orders/production.py', 'backend/core/economic_source_creation.py', 'backend/core/dispatch_claims.py', 'backend/core/dispatch_receipts.py', 'backend/core/dispatch_identity.py', 'backend/modules/printers/dispatch.py', 'backend/modules/printers/dispatch_admission.py', 'backend/core/schema/bootstrap.py', 'backend/core/schema/migrator.py', 'backend/modules/archives/attempt_accounting.py', 'backend/modules/archives/first_start_valuation.py', 'backend/modules/organizations/education_policy.py', 'backend/modules/organizations/services.py', 'backend/modules/orders/routes/orders_crud.py', 'backend/core/global_order_creation.py', 'backend/core/spool_purchase_evidence.py', 'backend/core/finance_currency.py', 'ops/database_parity/attempt_valuation_read_native.py', 'ops/database_parity/run_attempt_valuation_read.py', 'backend/core/financial_read_authority.py', 'backend/core/auth.py', 'backend/core/dependencies.py', 'backend/core/rbac.py', 'backend/core/db.py', 'backend/core/database_config.py', 'backend/core/db_compat.py', 'backend/core/postgres_restore_gate.py', 'backend/core/audit_actor.py', 'backend/core/middleware/audit_actor.py', 'backend/core/api_token_policy.py', 'backend/modules/orders/routes/attempt_valuation.py', 'backend/modules/orders/valuation_schemas.py', 'backend/modules/archives/valuation_private_read.py', 'backend/modules/archives/valuation_parent_lock.py', 'backend/modules/archives/valuation_financial_graph.py', 'backend/modules/archives/valuation_content_visibility.py', 'backend/modules/archives/ownership.py', 'backend/modules/archives/valuation_payload.py', 'backend/modules/archives/valuation_projection.py', 'backend/modules/archives/valuation_retained_evidence.py', 'backend/modules/inventory/routes/spools.py', 'backend/modules/inventory/routes/_helpers.py', 'backend/modules/organizations/routes_auth.py', 'backend/core/session_tracking.py']
receipt={'passed':False,'scope':'Actual PostgreSQL HTTP private org/company valuation after genuine025 order dispatch capture; currentcarrier andaliasfences bothorders; no physical callback orCOGS certification','source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_provenance':'Existing published1.9.24 immutable runtime, current source mounted read-only; no pull/build','external_stubs':'Physical sender, idle telemetry and notifications only; actual JWT company origin/order/schedule/025 source, admission, scope, capture and bootstrap'}
output=root/'artifacts/application-certification/attempt-valuation-read-native.json';log=root/'artifacts/application-certification/attempt-valuation-read-native.log';private=name+'-credentials';writer=name+'-credential-writer';password=secrets.token_urlsafe(48)
try:
 receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}','f4ecb5d6dd0b').strip()
 run('docker','network','create','--internal',name)
 run('docker','volume','create',private)
 pg_uid=run('docker','run','--rm','--network','none','--entrypoint','id',IMAGE,'-u','postgres').strip()
 pg_gid=run('docker','run','--rm','--network','none','--entrypoint','id',IMAGE,'-g','postgres').strip()
 assert pg_uid.isdigit() and pg_gid.isdigit() and int(pg_uid)>0
 written=subprocess.run(['docker','run','-i','--rm','--name',writer,'--network',name,'--user','0','--entrypoint','/bin/sh','-v',private+':/run/owned-power-secret',receipt['runtime_image'],'-c','chmod 700 /run/owned-power-secret && umask 077 && cat > /run/owned-power-secret/password && chmod 600 /run/owned-power-secret/password && chown -R '+pg_uid+':'+pg_gid+' /run/owned-power-secret && stat -c %u:%g:%a /run/owned-power-secret/password'],input=password,text=True,capture_output=True,timeout=30)
 if written.returncode:raise RuntimeError('Owned private credential initialization failed')
 receipt['database_entrypoint_credential_owner_mode']=written.stdout.strip()
 assert receipt['database_entrypoint_credential_owner_mode']==pg_uid+':'+pg_gid+':600'
 run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg','--tmpfs','/var/lib/postgresql/data:rw,size=128m','-v',private+':/run/owned-power-secret:ro','-e','POSTGRES_PASSWORD_FILE=/run/owned-power-secret/password',IMAGE)
 deadline=time.monotonic()+30
 while subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True,timeout=5).returncode:
  if time.monotonic()>deadline:raise RuntimeError('Owned PostgreSQL not ready')
  time.sleep(.2)
 receipt['database_postgres_uid']=run('docker','exec',database,'id','-u','postgres').strip()
 assert receipt['database_postgres_uid']==pg_uid
 receipt['database_postgres_gid']=pg_gid
 for reader in ('0',pg_uid):
  run('docker','exec','--user',reader,database,'sh','-c','test -r /run/owned-power-secret/password')
 receipt['database_root_and_postgres_private_file_readable']=True
 result=subprocess.run(['docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,'--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-v',private+':/run/owned-power-secret:ro','-e','PYTHONPATH=/source/backend:/source','-e','PYTHONUNBUFFERED=1','-e','PYTHONPYCACHEPREFIX=/tmp/owned-power-bytecode','-e','VALUATION_NATIVE_PASSWORD_FILE=/tmp/owned-power-private/password','-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false','-e','DEBUG=false','-e','COOKIE_SECURE=false','-e','VALUATION_READ_DIAGNOSTIC='+os.environ.get('VALUATION_READ_DIAGNOSTIC','0'),'-e','VALUATION_READ_FOCUSED_NEGATIVE='+os.environ.get('VALUATION_READ_FOCUSED_NEGATIVE','0'),receipt['runtime_image'],'-c','mkdir -m700 /tmp/owned-power-private && cp /run/owned-power-secret/password /tmp/owned-power-private/password && chown -R odin:odin /tmp/owned-power-private && chmod 600 /tmp/owned-power-private/password && exec runuser -u odin -- python /source/ops/database_parity/attempt_valuation_read_native.py'],text=True,capture_output=True,timeout=600)
 log.write_text(result.stdout+result.stderr)
 if result.returncode:raise RuntimeError('Owned native attempt-valuation-read probe failed; see preserved private-free log')
 diagnostic=os.environ.get('VALUATION_READ_DIAGNOSTIC')=='1'
 focused=os.environ.get('VALUATION_READ_FOCUSED_NEGATIVE')=='1'
 assert ('attempt-valuation-read-diagnostic-collected' if diagnostic else 'attempt-valuation-read-focused-negative-pass' if focused else 'attempt-valuation-read-native-pass') in result.stdout
 receipt['focused_negative_pass']=focused
 receipt['diagnostic_only']=diagnostic
 receipt['diagnostic_errors']=[json.loads(line.partition('valuation-read-diagnostic-')[2]) for line in result.stdout.splitlines() if line.startswith('valuation-read-diagnostic-')]
 receipt['test_summary']=result.stdout[-6000:]
 assert 'runtime-private-file uid=10001 mode=600' in result.stdout
 receipt['runtime_credential_uid']=10001;receipt['runtime_credential_mode']='600'
 receipt['credential_boundary']='DB0600 file and0700 directory owned by actual postgres UID/GID; root and postgres read admitted; runtime owns separate copied0600 file; no world-readable or trust auth'
 assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in receipt['source_sha256'].items())
 receipt['passed']=not diagnostic and not focused
 receipt['diagnostic_collected']=diagnostic
except BaseException as error:
 receipt['error_type']=type(error).__name__
 if not log.exists():log.write_text('Probe did not reach runtime completion: '+type(error).__name__+'\n')
 raise
finally:
 subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
 receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
 receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
 subprocess.run(['docker','rm','-f',writer],capture_output=True,timeout=20)
 receipt['owned_private_credentials_removed']=subprocess.run(['docker','volume','rm',private],capture_output=True,timeout=20).returncode==0
 receipt['after_source_sha256']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
 receipt['source_pins_match']=receipt['after_source_sha256']==receipt['source_sha256']
 output.write_text(json.dumps(receipt,indent=2)+'\n')
 unique=root/('artifacts/application-certification/attempt-valuation-read-'+name.rsplit('-',1)[-1])
 unique.with_suffix('.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if log.exists():unique.with_suffix('.log').write_text(log.read_text())
print(json.dumps({'passed':receipt['passed'],'pins':len(paths),'cleanup':receipt['owned_database_removed'] and receipt['owned_network_removed'] and receipt['owned_private_credentials_removed']}))
