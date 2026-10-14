"""Owned unregistered priced-estimate storage probe; private files, internal network only."""
import hashlib,json,subprocess,time,uuid,secrets,os
from pathlib import Path
from ops.database_parity.run_ams_adoption import IMAGE,run
root=Path.cwd().resolve();name='odin-priced-estimate-restore-'+uuid.uuid4().hex[:12];database=name+'-db';runtime=name+'-runtime'
paths=['backend/core/priced_estimate_restore.py', 'backend/core/priced_estimate_schema.py', 'backend/core/priced_estimate_validation.py', 'backend/core/priced_job_estimate.py', 'backend/core/economic_identity.py', 'backend/core/schema/migrations/009_cost_foundation.py', 'backend/core/schema/migrations/021_economic_identity.py', 'backend/core/schema/bootstrap.py', 'backend/core/schema/migrator.py', 'backend/core/db.py', 'backend/core/db_utils.py', 'backend/core/db_compat.py', 'backend/core/database_config.py', 'backend/core/postgres_restore_gate.py', 'backend/core/base.py', 'backend/core/models.py', 'backend/modules/jobs/models.py', 'ops/database_parity/priced_estimate_restore_native.py', 'ops/database_parity/run_priced_estimate_restore.py']
receipt={'diagnostic_only':os.environ.get('PRICED_RESTORE_DIAGNOSTIC')=='1','passed':False,'scope':'Actual PostgreSQL owned customarchive sevenfamily rawCOPY staging/guardedreplay: genuine021 source identity, nonempty revisioned histories/currentNULL, commit+process interruption; no registration/ordinaryrestore/API authority','source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_provenance':'Existing published1.9.24 immutable runtime, current source mounted read-only; no pull/build','external_stubs':'Direct SQL source/actor storage fixtures, genuine021 jobs; no HTTP principal/account capability or physical devices'}
output=root/'artifacts/application-certification/priced-estimate-restore-native.json';log=root/'artifacts/application-certification/priced-estimate-restore-native.log';private=name+'-credentials';writer=name+'-credential-writer';password=secrets.token_urlsafe(48)
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
 run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg','--tmpfs','/var/lib/postgresql/data:rw,size=256m','-v',private+':/run/owned-power-secret:ro','-e','POSTGRES_PASSWORD_FILE=/run/owned-power-secret/password',IMAGE)
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
 result=subprocess.run(['docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,'--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-v',private+':/run/owned-power-secret:ro','-e','PYTHONPATH=/source/backend:/source','-e','PYTHONUNBUFFERED=1','-e','PYTHONPYCACHEPREFIX=/tmp/owned-power-bytecode','-e','PRICED_ESTIMATE_NATIVE_PASSWORD_FILE=/tmp/owned-power-private/password','-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false','-e','PRICED_RESTORE_DIAGNOSTIC='+os.environ.get('PRICED_RESTORE_DIAGNOSTIC','0'),'-e','DEBUG=false','-e','COOKIE_SECURE=false','-e','RETIREMENT_FINAL_DIAGNOSTIC='+os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC','0'),receipt['runtime_image'],'-c','mkdir -m700 /tmp/owned-power-private && cp /run/owned-power-secret/password /tmp/owned-power-private/password && chown -R odin:odin /tmp/owned-power-private && chmod 600 /tmp/owned-power-private/password && exec runuser -u odin -- python /source/ops/database_parity/priced_estimate_restore_native.py'],text=True,capture_output=True,timeout=600)
 log.write_text(result.stdout+result.stderr)
 if result.returncode:raise RuntimeError('Owned native priced-estimate-restore probe failed; see preserved private-free log')
 assert ('priced-estimate-restore-envelope-diagnostic-complete' if receipt['diagnostic_only'] else 'priced-estimate-restore-native-pass') in result.stdout
 receipt['test_summary']=result.stdout[-5000:]
 receipt['runtime_credential_uid']=10001;receipt['runtime_credential_mode']='600'
 receipt['credential_boundary']='DB0600 credential owned by actual postgres UID/GID; separate runtime0600 copy owned by UID10001; internal network/no host ports'
 assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==h for p,h in receipt['source_sha256'].items())
 receipt['diagnostic_completed']=receipt['diagnostic_only']
 receipt['passed']=not receipt['diagnostic_only']
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
 unique=root/('artifacts/application-certification/priced-estimate-restore-'+name.rsplit('-',1)[-1])
 unique.with_suffix('.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if log.exists():unique.with_suffix('.log').write_text(log.read_text())
print(json.dumps({'passed':receipt['passed'],'pins':len(paths),'cleanup':receipt['owned_database_removed'] and receipt['owned_network_removed'] and receipt['owned_private_credentials_removed']}))
