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
name='odin-historical25-'+uuid.uuid4().hex[:12]
database=name+'-db'
runtime=name+'-runtime'
paths=['backend/core/schema/pre026_restore_contract.py', 'backend/core/schema/pre026_restore_integrity.py', 'backend/modules/system/pre026_restore_stage.py', 'backend/core/schema/bootstrap.py', 'backend/core/schema/migrator.py', 'backend/modules/system/postgres_backup_service.py', 'backend/modules/system/postgres_canonical_executables.py', 'backend/core/database_config.py', 'backend/core/postgres_restore_gate.py', 'backend/core/global_order_origin_schema.py', 'backend/core/schema/migrations/026_global_order_origins.py', 'backend/core/spool_purchase_schema.py', 'backend/core/spool_purchase_validation.py', 'backend/core/schema/migrations/025_spool_purchase_evidence.py', 'backend/core/finance_currency.py', 'ops/database_parity/pre026_historical_restore_native.py', 'ops/database_parity/run_pre026_historical_restore.py']
receipt={'passed':False,'scope':'Actual historical025 archive originalSHA→trusted positive25contract data-onlyimport→normal26cutover→currenttarget, org/purchase/globaldeletedroots preserved, distinct26 rollback/failedrecovery/durablegate/originalblob/ownedcleanup; current26baseline separatelyproved',
    'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_ref':RUNTIME_REF,
    'runtime_provenance':'Published v1.9.24 AMD64 explicitly approved by root; current source mounted read-only; no prior candidate receipt transfer'}
if recovery_only:receipt['scope']='Affected025 actual failed recovery/session death/durable factory-or-checkout typed refusal; prior four green phases preserved separately, not rerun'
output=root/('artifacts/edu-workflow-check/historical25-restore-recovery-current.json' if recovery_only else 'artifacts/edu-workflow-check/historical25-restore-native-current.json')
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
        '-e','GLOBAL_ORIGIN_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres',receipt['runtime_image'],
        '-c','exec runuser -u odin -- python /source/ops/database_parity/pre026_historical_restore_native.py')
    native_output=root/'artifacts/edu-workflow-check/historical25-restore-native-stdout.log'
    native_output.write_text(result)
    receipt['native_stdout_sha256']=hashlib.sha256(native_output.read_bytes()).hexdigest()
    assert 'historical25-native-pass' in result
    receipt['native_facts']=[line for line in result.splitlines() if line.startswith('historical25-native-')]
    receipt['after_source_sha256']={p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}
    assert receipt['after_source_sha256']==receipt['source_sha256']
    receipt['passed']=True
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
