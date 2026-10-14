"""Owned internal PostgreSQL persistence probe; no host ports or application data."""
import os
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from ops.database_parity.run_ams_adoption import IMAGE, run

def run_native(*args):
    value=subprocess.run(args,text=True,capture_output=True,timeout=1800)
    if value.returncode:raise RuntimeError(value.stdout+value.stderr)
    return value.stdout

RUNTIME_REF='ghcr.io/hughkantsime/odin@sha256:f4ecb5d6dd0b8b8216fb29c680a887624714f79c0e7236e6ec20b22e2ca2899e'
recovery_only=False
root=Path.cwd().resolve()
name='odin-editor-service-'+uuid.uuid4().hex[:12]
database=name+'-db'
runtime=name+'-runtime'
paths=['backend/core/schema/bootstrap.py', 'backend/core/schema/migrator.py', 'backend/core/database_config.py', 'backend/core/postgres_restore_gate.py', 'backend/core/db.py', 'backend/core/db_compat.py', 'backend/core/auth.py', 'backend/core/dependencies.py', 'backend/core/audit.py', 'backend/core/audit_actor.py', 'backend/core/timelapse_recipes.py', 'backend/core/timelapse_publication_schema.py', 'backend/core/timelapse_publication_validation.py', 'backend/core/timelapse_media_lock.py','backend/core/timelapse_request_budget.py','ops/database_parity/timelapse_retirement_profile.py','backend/core/sender_process_group.py', 'backend/core/media_process_dispatch.py', 'backend/core/media_probe_capability.py', 'backend/core/timelapse_probe_worker.py', 'backend/core/media_derivative_capability.py', 'backend/core/timelapse_derivative_worker.py', 'backend/core/timelapse_derivative_timing.py', 'backend/modules/archives/timelapse_edit.py', 'backend/modules/archives/timelapse_edit_source.py', 'backend/modules/archives/timelapse_action_authority.py', 'backend/modules/archives/timelapse_read_lease.py', 'backend/modules/archives/timelapse_capture.py', 'backend/modules/archives/timelapse_frame_capture.py', 'backend/modules/archives/timelapse_encode.py', 'backend/modules/archives/timelapse_frame_publication.py', 'backend/modules/archives/timelapse_lifetime_prepare.py','backend/modules/archives/timelapse_lifetime_action.py','backend/modules/archives/timelapse_speed_dependencies.py','backend/modules/archives/timelapse_snapshot_graph.py','backend/core/timelapse_retirement_membership_validation.py','ops/database_parity/timelapse_editor_service_native.py', 'ops/database_parity/run_timelapse_editor_service.py']
receipt={'passed':False,'scope':'ActualPG editor directservice: current registered bootstrap, synthetic owned camera bytes→real frame receipts+encoder, all6 speed codecs/exactcounts/freshlifetimes/replay, actual trim/retained/superseded receipt and deferredCOMMIT rollback+published-output resume; both speed retirement orders and immutable historical source after physical deletion/numeric ID reuse. HTTP auth and UI separate host evidence',
    'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_ref':RUNTIME_REF,
    'runtime_provenance':'Published v1.9.24 AMD64 explicitly approved by root; current source mounted read-only; no prior candidate receipt transfer'}
if recovery_only:receipt['scope']='Affected025 actual failed recovery/session death/durable factory-or-checkout typed refusal; prior four green phases preserved separately, not rerun'
output=root/('artifacts/application-certification/historical25-restore-recovery-current.json' if recovery_only else 'artifacts/application-certification/timelapse-editor-service-native-current.json')
try:
    receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}',RUNTIME_REF).strip()
    run('docker','network','create','--internal',name)
    run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg',
        '--tmpfs','/var/lib/postgresql/data:rw,size=512m','-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
    deadline=time.monotonic()+30
    while subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True,timeout=5).returncode:
        if time.monotonic()>deadline:raise RuntimeError('Owned clock database not ready')
        time.sleep(.2)
    result=run_native('docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,
        '--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-e','PYTHONPATH=/source/backend',
        '-e','PYTHONPYCACHEPREFIX=/tmp/odin-recent-bytecode','-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false','-e','DEBUG=false','-e','COOKIE_SECURE=false',
        '-e','SPOOL_PURCHASE_RECOVERY_ONLY='+('1' if recovery_only else '0'),
        '-e','EDITOR_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres',receipt['runtime_image'],
        '-c','exec runuser -u odin -- python /source/ops/database_parity/timelapse_editor_service_native.py')
    native_output=root/'artifacts/application-certification/timelapse-editor-service-native-stdout.log'
    native_output.write_text(result)
    receipt['native_stdout_sha256']=hashlib.sha256(native_output.read_bytes()).hexdigest()
    assert 'editor-native-pass' in result
    receipt['native_facts']=[line for line in result.splitlines() if line.startswith('editor-native-')]
    receipt['retirement_profiles']=[json.loads(line.split('editor-native-retirement-profile-',1)[1]) for line in result.splitlines() if line.startswith('editor-native-retirement-profile-')]
    receipt['after_source_sha256']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
    assert receipt['after_source_sha256']==receipt['source_sha256']
    receipt['passed']=True
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
