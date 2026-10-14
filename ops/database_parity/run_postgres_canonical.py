"""Owned internal native currency transaction probe; no host ports or app data."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from ops.database_parity.run_ams_adoption import IMAGE, run

def run_native(*args):
    value=subprocess.run(args,text=True,capture_output=True,timeout=600)
    if value.returncode:raise RuntimeError(value.stdout+value.stderr)
    return value.stdout

root=Path.cwd().resolve()
name='odin-canonical-'+uuid.uuid4().hex[:12]
database=name+'-db'
runtime=name+'-runtime'
paths=['backend/core/schema/bootstrap.py','backend/core/schema/migrations/002_education_tenant_integrity.py','backend/core/schema/migrations/009_cost_foundation.py','backend/core/schema/migrations/021_economic_identity.py','backend/core/schema/migrator.py','backend/core/schema/migrations/023_timelapse_retirement_membership.py','backend/core/timelapse_retirement_membership_schema.py','backend/core/timelapse_retirement_membership_validation.py','backend/core/schema/migrations/024_audit_actor.py','backend/core/audit_actor_schema.py','backend/core/audit_actor.py','backend/core/schema/migrations/022_economic_source_lifetimes.py','backend/core/economic_source_creation.py','backend/core/db_compat.py','backend/core/db.py','backend/modules/archives/timelapse_capture.py','backend/modules/archives/timelapse_encode.py','backend/modules/archives/timelapse_lifetime_prepare.py','backend/core/postgres_restore_gate.py','backend/core/database_config.py','backend/scripts/wait_for_database_schema.py','backend/modules/system/postgres_backup_service.py','backend/modules/system/postgres_canonical_executables.py','tests/test_contracts/test_postgres_canonical_executables.py','ops/database_parity/postgres_canonical_native.py','ops/database_parity/run_postgres_canonical.py']
receipt={'passed':False,'scope':'Native app backup/restore/rollback, exact canonical executable guards and zero-execution negatives; 022 source lifetimes,023 sealed audit-pruned replay,024 erasure ledger and deferred cyclic data restore',
         'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE}
output=root/'artifacts/application-certification/postgres-canonical-native.json'
try:
    receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}','odin-school-candidate:e61fe44-amd64').strip()
    run('docker','network','create','--internal',name)
    run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg',
        '--tmpfs','/var/lib/postgresql/data:rw,size=128m','-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
    deadline=time.monotonic()+30
    while subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True,timeout=5).returncode:
        if time.monotonic()>deadline:raise RuntimeError('Owned currency database not ready')
        time.sleep(.2)
    result=run_native('docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,
        '--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro',
        '-e','PYTHONPATH=/source/backend:/source','-e','PYTHONUNBUFFERED=1','-e','PYTHONPYCACHEPREFIX=/tmp/odin-currency-bytecode',
        '-e','CANONICAL_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres',
        '-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false',
        '-e','DEBUG=false','-e','COOKIE_SECURE=false',receipt['runtime_image'],
        '-c','exec runuser -u odin -- python /source/ops/database_parity/postgres_canonical_native.py')
    receipt['test_summary']=result[-16000:]
    (root/'artifacts/application-certification/postgres-canonical-native.log').write_text(result)
    assert 'canonical-native-malicious-marker-zero-execution-pass' in result
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in receipt['source_sha256'].items())
    receipt['passed']=True
except Exception as error:
    (root/'artifacts/application-certification/postgres-canonical-native.log').write_text(str(error))
    receipt['error_type']=type(error).__name__
    raise
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
    (root/('artifacts/application-certification/postgres-canonical-'+name.rsplit('-',1)[-1]+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
    log=root/'artifacts/application-certification/postgres-canonical-native.log'
    if log.exists():(root/('artifacts/application-certification/postgres-canonical-'+name.rsplit('-',1)[-1]+'.log')).write_text(log.read_text())
print(json.dumps(receipt))
