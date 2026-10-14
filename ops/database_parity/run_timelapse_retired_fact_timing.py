"""Owned PostgreSQL timelapse-retired timing probe; private files, internal network only."""
import hashlib,json,subprocess,time,uuid,secrets,os
from pathlib import Path
from ops.database_parity.run_ams_adoption import IMAGE,run
root=Path.cwd().resolve();name='odin-timelapse-retired-timing-'+uuid.uuid4().hex[:12];database=name+'-db';runtime=name+'-runtime'
paths=['backend/core/audit.py', 'backend/core/audit_actor.py', 'backend/core/auth.py', 'backend/core/database_config.py', 'backend/core/db.py', 'backend/core/db_compat.py', 'backend/core/dependencies.py', 'backend/core/media_derivative_capability.py', 'backend/core/media_probe_capability.py', 'backend/core/media_process_dispatch.py', 'backend/core/postgres_restore_gate.py', 'backend/core/schema/bootstrap.py', 'backend/core/schema/migrator.py', 'backend/core/sender_process_group.py', 'backend/core/timelapse_action_fs.py', 'backend/core/timelapse_derivative_timing.py', 'backend/core/timelapse_derivative_worker.py', 'backend/core/timelapse_drain_fs.py', 'backend/core/timelapse_media_lock.py', 'backend/core/timelapse_probe_worker.py', 'backend/core/timelapse_publication_schema.py', 'backend/core/timelapse_publication_validation.py', 'backend/core/timelapse_recipes.py', 'backend/core/timelapse_request_budget.py', 'backend/core/timelapse_retirement_membership_schema.py', 'backend/core/timelapse_retirement_membership_validation.py', 'backend/core/timelapse_retirement_schema.py', 'backend/core/timelapse_staging_fs.py', 'backend/modules/archives/ownership.py', 'backend/modules/archives/timelapse_action_authority.py', 'backend/modules/archives/timelapse_capture.py', 'backend/modules/archives/timelapse_edit.py', 'backend/modules/archives/timelapse_edit_source.py', 'backend/modules/archives/timelapse_encode.py', 'backend/modules/archives/timelapse_frame_capture.py', 'backend/modules/archives/timelapse_frame_publication.py', 'backend/modules/archives/timelapse_lifetime_action.py', 'backend/modules/archives/timelapse_lifetime_prepare.py', 'backend/modules/archives/timelapse_read_lease.py', 'backend/modules/archives/timelapse_retired_fact_proof.py', 'backend/modules/archives/timelapse_snapshot_graph.py', 'backend/modules/archives/timelapse_speed_dependencies.py', 'ops/database_parity/run_timelapse_editor_service.py', 'ops/database_parity/run_timelapse_final_commit.py', 'ops/database_parity/run_timelapse_retired_fact_timing.py', 'ops/database_parity/timelapse_editor_service_native.py', 'ops/database_parity/timelapse_final_commit_native.py', 'ops/database_parity/timelapse_retirement_profile.py']
receipt={'passed':False,'scope':'Actual PostgreSQL original31-member retirement unchanged60-second budget, current retired-fact proof; real editor/frame codecs and original actions unchanged','source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_provenance':'Existing published1.9.24 immutable runtime, current source mounted read-only; no pull/build','external_stubs':'Synthetic JPEG camera stream only; actual current registered bootstrap, frame journal, codec, current JWT authority and physical owned filesystem'}
output=root/'artifacts/application-certification/timelapse-retired-fact-timing-native.json';log=root/'artifacts/application-certification/timelapse-retired-fact-timing-native.log';private=name+'-credentials';writer=name+'-credential-writer';password=secrets.token_urlsafe(48)
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
 result=subprocess.run(['docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,'--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-v',private+':/run/owned-power-secret:ro','-e','PYTHONPATH=/source/backend:/source','-e','PYTHONUNBUFFERED=1','-e','PYTHONPYCACHEPREFIX=/tmp/owned-power-bytecode','-e','EDITOR_NATIVE_PASSWORD_FILE=/tmp/owned-power-private/password','-e','EDITOR_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres','-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false','-e','DEBUG=false','-e','COOKIE_SECURE=false','-e','RETIREMENT_FINAL_DIAGNOSTIC='+os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC','0'),receipt['runtime_image'],'-c','mkdir -m700 /tmp/owned-power-private && cp /run/owned-power-secret/password /tmp/owned-power-private/password && chown -R odin:odin /tmp/owned-power-private && chmod 600 /tmp/owned-power-private/password && exec runuser -u odin -- python /source/ops/database_parity/timelapse_editor_service_native.py'],text=True,capture_output=True,timeout=600)
 log.write_text(result.stdout+result.stderr)
 if result.returncode:raise RuntimeError('Owned native timelapse-retired timing probe failed; see preserved private-free log')
 assert 'editor-native-pass' in result.stdout
 receipt['retirement_profiles']=[json.loads(line.split('editor-native-retirement-profile-',1)[1]) for line in result.stdout.splitlines() if line.startswith('editor-native-retirement-profile-')]
 receipt['test_summary']=result.stdout[-5000:]
 receipt['runtime_credential_uid']=10001;receipt['runtime_credential_mode']='600'
 receipt['credential_boundary']='DB0600 credential owned by actual postgres UID/GID; separate runtime0600 copy owned by UID10001; internal network/no host ports'
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
 receipt['after_source_sha256']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
 receipt['source_pins_match']=receipt['after_source_sha256']==receipt['source_sha256']
 output.write_text(json.dumps(receipt,indent=2)+'\n')
 unique=root/('artifacts/application-certification/timelapse-retired-fact-timing-'+name.rsplit('-',1)[-1])
 unique.with_suffix('.json').write_text(json.dumps(receipt,indent=2)+'\n')
 if log.exists():unique.with_suffix('.log').write_text(log.read_text())
print(json.dumps({'passed':receipt['passed'],'pins':len(paths),'cleanup':receipt['owned_database_removed'] and receipt['owned_network_removed'] and receipt['owned_private_credentials_removed']}))
