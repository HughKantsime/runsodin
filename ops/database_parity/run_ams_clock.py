"""Owned internal PostgreSQL clock probe; no host ports or application data."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from ops.database_parity.run_ams_adoption import IMAGE, run

root=Path.cwd().resolve()
name='odin-ams-clock-'+uuid.uuid4().hex[:12]
database=name+'-db'
runtime=name+'-runtime'
paths=['backend/modules/printers/telemetry/bambu/ams_database_clock.py',
       'backend/modules/printers/telemetry/bambu/ams_receipt_time.py',
       'backend/core/runtime_identity.py','ops/database_parity/ams_clock_native.py',
       'ops/database_parity/run_ams_clock.py','ops/database_parity/run_ams_adoption.py']
receipt={'passed':False,'scope':'actual PostgreSQL live clock and caller transaction, not evidence writer or runtime admission',
    'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE}
output=root/'artifacts/edu-workflow-check/ams-db-clock-native-current.json'
try:
    receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}','odin-school-candidate:e61fe44-amd64').strip()
    run('docker','network','create','--internal',name)
    run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg',
        '--tmpfs','/var/lib/postgresql/data:rw,size=128m','-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
    deadline=time.monotonic()+30
    while subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True,timeout=5).returncode:
        if time.monotonic()>deadline:raise RuntimeError('Owned clock database not ready')
        time.sleep(.2)
    result=run('docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,
        '--entrypoint','python','-v',str(root)+':/source:ro','-e','PYTHONPATH=/source/backend',
        '-e','PYTHONPYCACHEPREFIX=/tmp/odin-clock-bytecode',
        '-e','AMS_TEST_DB_URL=postgresql+psycopg://postgres@pg/postgres',receipt['runtime_image'],
        '/source/ops/database_parity/ams_clock_native.py')
    assert 'native-ams-clock-pass' in result
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in receipt['source_sha256'].items())
    receipt['passed']=True
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
