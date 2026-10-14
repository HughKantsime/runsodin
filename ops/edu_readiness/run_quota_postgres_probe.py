"""Disposable internal PostgreSQL and pinned local dependency image; current source mounted."""
import hashlib,json,subprocess,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
IMAGE='sha256:a6e2b1cb91ee5acbcaa17b1dab0b0cedbd71d53361933dbfb46139511eac66f4'
def call(*args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=90)
    if result.returncode:raise RuntimeError(result.stderr[-1500:])
    return result.stdout
call('docker','image','inspect',IMAGE)
network='odin-quota-pg-'+uuid.uuid4().hex[:12];database=network+'-db'
files=['backend/core/quota.py','backend/core/errors.py','ops/edu_readiness/quota_postgres_probe.py']
receipt={'passed':False,'dependency_image':IMAGE,'production_data':False,'host_ports':False,
         'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}}
try:
    call('docker','network','create','--internal',network)
    call('docker','run','-d','--name',database,'--network',network,'-e','POSTGRES_HOST_AUTH_METHOD=trust','postgres:16-alpine')
    for _ in range(60):
        if subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True).returncode==0:break
        time.sleep(.5)
    else:raise RuntimeError('Owned PG readiness timeout')
    mounts=[]
    for source,dest in [(ROOT/'backend/core/quota.py','/app/backend/core/quota.py'),(ROOT/'backend/core/errors.py','/app/backend/core/errors.py'),(ROOT/files[-1],'/verify/probe.py')]:
        mounts+=['--mount',f'type=bind,src={source},dst={dest},readonly']
    output=call('docker','run','--rm','--network',network,'--platform','linux/amd64','--entrypoint','python','-e','PYTHONPATH=/app/backend',*mounts,IMAGE,'/verify/probe.py',database)
    receipt.update(json.loads(output))
finally:
    subprocess.run(['docker','rm','-fv',database],capture_output=True,timeout=20)
    result=subprocess.run(['docker','network','rm',network],capture_output=True,timeout=20)
    receipt['owned_network_removed']=result.returncode==0
    (ROOT/'artifacts/quota-exhaustion/postgres-current.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
