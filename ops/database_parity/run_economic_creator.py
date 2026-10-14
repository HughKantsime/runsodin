"""Owned internal PostgreSQL persistence probe; no host ports or application data."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from ops.database_parity.run_ams_adoption import IMAGE, run

root=Path.cwd().resolve()
name='odin-economic-creator-'+uuid.uuid4().hex[:12]
database=name+'-db'
runtime=name+'-runtime'
paths=['backend/modules/inventory/routes/spools.py', 'backend/modules/jobs/routes/jobs_crud.py', 'tests/test_contracts/test_economic_creator_wiring.py', 'backend/core/economic_source_creation.py', 'backend/core/economic_identity.py', 'backend/modules/inventory/models.py', 'backend/modules/inventory/routes/_helpers.py', 'backend/modules/jobs/models.py', 'backend/modules/jobs/schemas.py', 'backend/core/rbac.py', 'backend/core/quota.py', 'backend/modules/jobs/education_guard.py', 'backend/core/db.py', 'backend/core/db_compat.py', 'backend/core/database_config.py', 'backend/core/postgres_restore_gate.py', 'ops/database_parity/economic_creator_native.py', 'ops/database_parity/run_economic_creator.py']
receipt={'passed':False,'scope':'Affected actual PostgreSQL HTTP creator paths with correct preimport configuration, originalORMdefaults, ownership, atomicrollback and explicitnumericreuse; no canonicalfullrerun',
    'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE}
output=root/'artifacts/edu-workflow-check/economic-creator-native-current.json'
try:
    receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}','odin-school-candidate:e61fe44-amd64').strip()
    run('docker','network','create','--internal',name)
    run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg',
        '--tmpfs','/var/lib/postgresql/data:rw,size=256m','-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
    deadline=time.monotonic()+30
    while subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True,timeout=5).returncode:
        if time.monotonic()>deadline:raise RuntimeError('Owned clock database not ready')
        time.sleep(.2)
    result=run('docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,
        '--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-e','PYTHONPATH=/source/backend',
        '-e','PYTHONPYCACHEPREFIX=/tmp/odin-recent-bytecode','-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false','-e','DEBUG=false','-e','COOKIE_SECURE=false',
        '-e','ECONOMIC_CREATOR_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres',receipt['runtime_image'],
        '-c','exec runuser -u odin -- python /source/ops/database_parity/economic_creator_native.py')
    assert 'economic-creator-native-pass' in result
    receipt['native_facts']=[line for line in result.splitlines() if line.startswith('economic-creator-')]
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in receipt['source_sha256'].items())
    receipt['passed']=True
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
