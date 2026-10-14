"""Owned PostgreSQL attempt-start-callback probe; private files, internal network only."""
import hashlib,json,subprocess,time,uuid,secrets
from pathlib import Path
from ops.database_parity.run_ams_adoption import IMAGE,run
root=Path.cwd().resolve();name='odin-attempt-start-callback-'+uuid.uuid4().hex[:12];database=name+'-db';runtime=name+'-runtime'
paths=['backend/core/attempt_start_observation.py', 'backend/core/attempt_start_plan.py', 'backend/core/attempt_start_transaction.py', 'backend/core/dispatch_observations.py', 'backend/core/economic_source_creation.py', 'backend/core/interfaces/education_policy.py', 'backend/core/order_production.py', 'backend/modules/archives/attempt_accounting.py', 'backend/modules/archives/first_start_valuation.py', 'backend/modules/archives/services.py', 'backend/modules/notifications/job_events.py', 'backend/modules/orders/production.py', 'backend/modules/organizations/education_policy.py', 'backend/modules/organizations/services.py', 'backend/modules/printers/monitors/moonraker_monitor.py', 'backend/modules/printers/monitors/mqtt_job_lifecycle.py', 'backend/core/schema/bootstrap.py', 'backend/core/schema/migrator.py', 'backend/core/dispatch_claims.py', 'backend/core/dispatch_receipts.py', 'backend/core/dispatch_identity.py', 'backend/modules/printers/dispatch.py', 'backend/modules/printers/dispatch_admission.py', 'backend/modules/orders/routes/orders_crud.py', 'backend/modules/inventory/routes/spools.py', 'ops/database_parity/attempt_start_callback_native.py', 'ops/database_parity/run_attempt_start_callback.py']
receipt={'passed':False,'scope':'Actual current PostgreSQL org/global signed order/schedule/025 source and three-protocol real callbacks/private capture, committed fresh event, replay and deferred outer rollback; no hardware or actual COGS','source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_provenance':'Existing published1.9.24 immutable runtime, current source mounted read-only; no pull/build','external_stubs':'Owned synthetic JWT/users; only physical sender/idle telemetry/notification outputs stubbed; real HTTP creators, dispatch and callbacks'}
output=root/'artifacts/application-certification/attempt-start-callback-native.json';log=root/'artifacts/application-certification/attempt-start-callback-native.log';private=name+'-credentials';writer=name+'-credential-writer';password=secrets.token_urlsafe(48)
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
 result=subprocess.run(['docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,'--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-v',private+':/run/owned-power-secret:ro','-e','PYTHONPATH=/source/backend:/source','-e','PYTHONUNBUFFERED=1','-e','PYTHONPYCACHEPREFIX=/tmp/owned-power-bytecode','-e','CALLBACK_NATIVE_PASSWORD_FILE=/tmp/owned-power-private/password','-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false','-e','DEBUG=false','-e','COOKIE_SECURE=false',receipt['runtime_image'],'-c','mkdir -m700 /tmp/owned-power-private && cp /run/owned-power-secret/password /tmp/owned-power-private/password && chown -R odin:odin /tmp/owned-power-private && chmod 600 /tmp/owned-power-private/password && exec runuser -u odin -- python /source/ops/database_parity/attempt_start_callback_native.py'],text=True,capture_output=True,timeout=600)
 log.write_text(result.stdout+result.stderr)
 if result.returncode:raise RuntimeError('Owned native attempt-start-callback probe failed; see preserved private-free log')
 assert 'attempt-start-callback-native-pass' in result.stdout
 receipt['test_summary']=result.stdout[-6000:]
 assert 'runtime-private-file uid=10001 mode=600' in result.stdout
 receipt['runtime_credential_uid']=10001;receipt['runtime_credential_mode']='600'
 receipt['credential_boundary']='DB0600 file and0700 directory owned by actual postgres UID/GID; root and postgres read admitted; runtime owns separate copied0600 file; no world-readable or trust auth'
 assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in receipt['source_sha256'].items())
 receipt['passed']=True
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
 output.write_text(json.dumps(receipt,indent=2)+'\n')
 unique=root/('artifacts/application-certification/attempt-start-callback-'+name.rsplit('-',1)[-1])
 unique.with_suffix('.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if log.exists():unique.with_suffix('.log').write_text(log.read_text())
print(json.dumps({'passed':receipt['passed'],'pins':len(paths),'cleanup':receipt['owned_database_removed'] and receipt['owned_network_removed'] and receipt['owned_private_credentials_removed']}))
