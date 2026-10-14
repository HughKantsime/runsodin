"""Owned current025 PostgreSQL delivery probe; private files, internal network only."""
import hashlib,json,subprocess,time,uuid,secrets
from pathlib import Path
from ops.database_parity.run_ams_adoption import IMAGE,run
root=Path.cwd().resolve();name='odin-historical-power-profiles-'+uuid.uuid4().hex[:12];database=name+'-db';runtime=name+'-runtime'
paths=['backend/core/schema/pre026_restore_contract.py', 'backend/core/schema/pre026_restore_integrity.py', 'backend/modules/system/pre026_restore_stage.py', 'backend/core/schema/bootstrap.py', 'backend/core/schema/migrator.py', 'backend/modules/system/postgres_backup_service.py', 'backend/modules/system/postgres_canonical_executables.py', 'backend/core/database_config.py', 'backend/core/postgres_restore_gate.py', 'backend/core/global_order_origin_schema.py', 'backend/core/schema/migrations/026_global_order_origins.py', 'backend/core/power_target_operation_schema.py', 'backend/core/power_target_operations.py', 'backend/core/schema/migrations/027_power_target_operations.py', 'ops/database_parity/historical_power_profiles_native.py', 'ops/database_parity/run_historical_power_profiles.py']
receipt={'passed':False,'scope':'Actual closed historical025/026 source-created data-only staging then current027 restore, rollback and failed-recovery gates; no commands','source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_provenance':'Existing published1.9.24 immutable runtime, current source mounted read-only; no pull/build','external_stubs':'Synthetic historical source rows only; actual bootstrap/pg_dump/pg_restore and restore lifecycle'}
output=root/'artifacts/application-certification/historical-power-profiles-native.json';log=root/'artifacts/application-certification/historical-power-profiles-native.log';private=name+'-credentials';writer=name+'-credential-writer';password=secrets.token_urlsafe(48)
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
 run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg','--tmpfs','/var/lib/postgresql/data:rw,size=512m','-v',private+':/run/owned-power-secret:ro','-e','POSTGRES_PASSWORD_FILE=/run/owned-power-secret/password',IMAGE)
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
 result=subprocess.run(['docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,'--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-v',private+':/run/owned-power-secret:ro','-e','PYTHONPATH=/source/backend:/source','-e','PYTHONUNBUFFERED=1','-e','PYTHONPYCACHEPREFIX=/tmp/owned-power-bytecode','-e','HISTORICAL_NATIVE_PASSWORD_FILE=/tmp/owned-power-private/password','-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false','-e','DEBUG=false','-e','COOKIE_SECURE=false',receipt['runtime_image'],'-c','mkdir -m700 /tmp/owned-power-private && cp /run/owned-power-secret/password /tmp/owned-power-private/password && chown -R odin:odin /tmp/owned-power-private && chmod 600 /tmp/owned-power-private/password && for version in 25 26; do runuser -u odin -- env HISTORICAL_PROFILE_VERSION=$version python /source/ops/database_parity/historical_power_profiles_native.py || exit 1; done'],text=True,capture_output=True,timeout=1800)
 log.write_text(result.stdout+result.stderr)
 if result.returncode:raise RuntimeError('Owned native power probe failed; see preserved private-free log')
 assert 'historical-profile-25-to27-native-pass' in result.stdout and 'historical-profile-26-to27-native-pass' in result.stdout
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
 unique=root/('artifacts/application-certification/historical-power-profiles-'+name.rsplit('-',1)[-1])
 unique.with_suffix('.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if log.exists():unique.with_suffix('.log').write_text(log.read_text())
print(json.dumps({'passed':receipt['passed'],'pins':len(paths),'cleanup':receipt['owned_database_removed'] and receipt['owned_network_removed'] and receipt['owned_private_credentials_removed']}))
