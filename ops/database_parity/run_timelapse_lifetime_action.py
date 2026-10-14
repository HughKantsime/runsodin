"""Owned internal registered023 membership probe; no host ports or app data."""
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
name='odin-timelapse-lifetime-action-'+uuid.uuid4().hex[:12]
database=name+'-db'
runtime=name+'-runtime'
paths=['backend/core/schema/bootstrap.py','backend/core/schema/migrator.py','backend/core/schema/migrations/009_cost_foundation.py','backend/core/schema/migrations/021_economic_identity.py','backend/core/schema/migrations/023_timelapse_retirement_membership.py','backend/core/timelapse_retirement_membership_schema.py','backend/core/timelapse_retirement_membership_validation.py','backend/modules/archives/timelapse_lifetime_prepare.py','tests/test_contracts/test_timelapse_lifetime_action.py','backend/core/timelapse_action_fs.py','backend/modules/archives/timelapse_action_authority.py','backend/modules/archives/timelapse_lifetime_action.py','ops/database_parity/timelapse_lifetime_action_native.py','ops/database_parity/run_timelapse_lifetime_action.py']
receipt={'passed':False,'scope':'Owned physical sealed-lifetime service and native insertion fences; no route activation',
         'source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE}
output=root/'artifacts/application-certification/timelapse-lifetime-action-native.json'
try:
    receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}','odin-school-candidate:e61fe44-amd64').strip()
    run('docker','network','create','--internal',name)
    run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg',
        '--tmpfs','/var/lib/postgresql/data:rw,size=128m','-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
    deadline=time.monotonic()+30
    while subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True,timeout=5).returncode:
        if time.monotonic()>deadline:raise RuntimeError('Owned membership database not ready')
        time.sleep(.2)
    result=run_native('docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,
        '--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro',
        '-e','PYTHONPATH=/source/backend:/source','-e','PYTHONUNBUFFERED=1','-e','PYTHONPYCACHEPREFIX=/tmp/odin-membership-bytecode',
        '-e','MEMBERSHIP_NATIVE_URL=postgresql+psycopg://postgres@pg/postgres',
        '-e','BASE_URL=http://127.0.0.1:9','-e','ODIN_CONTAINER_EXEC=/usr/bin/false',
        '-e','DEBUG=false','-e','COOKIE_SECURE=false',receipt['runtime_image'],
        '-c','exec runuser -u odin -- python /source/ops/database_parity/timelapse_lifetime_action_native.py')
    receipt['test_summary']=result[-16000:]
    (root/'artifacts/application-certification/timelapse-lifetime-action-native.log').write_text(result)
    assert 'timelapse-lifetime-action-native-pass' in result
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==v for p,v in receipt['source_sha256'].items())
    receipt['passed']=True
except Exception as error:
    (root/'artifacts/application-certification/timelapse-lifetime-action-native.log').write_text(str(error))
    receipt['error_type']=type(error).__name__
    raise
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
    (root/('artifacts/application-certification/timelapse-lifetime-action-'+name.rsplit('-',1)[-1]+'.json')).write_text(json.dumps(receipt,indent=2)+'\n')
    log=root/'artifacts/application-certification/timelapse-lifetime-action-native.log'
    if log.exists():(root/('artifacts/application-certification/timelapse-lifetime-action-'+name.rsplit('-',1)[-1]+'.log')).write_text(log.read_text())
print(json.dumps(receipt))
