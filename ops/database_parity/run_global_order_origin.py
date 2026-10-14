"""Owned internal PostgreSQL persistence probe; no host ports or application data."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from ops.database_parity.run_ams_adoption import IMAGE, run

RUNTIME_REF='ghcr.io/hughkantsime/odin@sha256:f4ecb5d6dd0b8b8216fb29c680a887624714f79c0e7236e6ec20b22e2ca2899e'
root=Path.cwd().resolve()
name='odin-global-origin-'+uuid.uuid4().hex[:12]
database=name+'-db'
runtime=name+'-runtime'
paths=['backend/core/global_order_origin_schema.py','backend/core/schema/migrations/026_global_order_origins.py','backend/core/schema/migrations/021_economic_identity.py','backend/core/schema/migrations/009_cost_foundation.py','backend/core/schema/bootstrap.py','ops/database_parity/global_order_origin_native.py','ops/database_parity/run_global_order_origin.py']
receipt={'passed':False,'scope':'Unregistered026 foundation on owned actual public PostgreSQL, cutover/deleted roots/adoption/retained nonce/bidirectional/exact FK/drift/rollback; activation/backup registration separate',
    'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE,'runtime_ref':RUNTIME_REF,
    'runtime_provenance':'Published v1.9.24 AMD64 explicitly approved by root; current source mounted read-only; no prior candidate receipt transfer'}
output=root/'artifacts/edu-workflow-check/global-order-origin-native-current.json'
try:
    receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}',RUNTIME_REF).strip()
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
        '-e','GLOBAL_ORIGIN_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres',receipt['runtime_image'],
        '-c','exec runuser -u odin -- python /source/ops/database_parity/global_order_origin_native.py')
    assert 'global-origin-native-pass' in result
    receipt['native_facts']=[line for line in result.splitlines() if line.startswith('global-origin-')]
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in receipt['source_sha256'].items())
    receipt['passed']=True
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
