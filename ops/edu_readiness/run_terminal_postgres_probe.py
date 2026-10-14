"""Run acceptance against an owned internal PostgreSQL, no host ports."""
import json,subprocess,time,uuid,os,hashlib
from pathlib import Path
IMAGE=os.environ.get('ODIN_NATIVE_CANDIDATE','sha256:a6e2b1cb91ee5acbcaa17b1dab0b0cedbd71d53361933dbfb46139511eac66f4')
def call(*args):
 r=subprocess.run(args,capture_output=True,text=True,timeout=90)
 if r.returncode:raise RuntimeError(r.stderr[-1800:])
 return r.stdout
IMAGE=json.loads(call('docker','image','inspect',IMAGE))[0]['Descriptor']['digest']
suffix=uuid.uuid4().hex[:12];network='odin-terminal-pg-'+suffix;db=network+'-db'
receipt={'passed':False,'image':IMAGE,'production_data':False,'host_ports':False}
try:
 call('docker','network','create','--internal',network)
 call('docker','run','-d','--name',db,'--network',network,'-e','POSTGRES_HOST_AUTH_METHOD=trust','postgres:16-alpine')
 for _ in range(60):
  if subprocess.run(['docker','exec',db,'pg_isready','-U','postgres'],capture_output=True).returncode==0:break
  time.sleep(.5)
 else:raise RuntimeError('Owned PostgreSQL readiness timeout')
 probe=Path('ops/edu_readiness/terminal_postgres_probe.py').resolve()
 receipt['probe_source_sha256']=hashlib.sha256(probe.read_bytes()).hexdigest()
 output=call('docker','run','--rm','--network',network,'--platform','linux/amd64','--entrypoint','python','-e','PYTHONPATH=/app/backend','-w','/app/backend','--mount','type=bind,src='+str(probe)+',dst=/verify/probe.py,readonly',IMAGE,'/verify/probe.py',db)
 receipt.update(json.loads(output))
except Exception as error:
 receipt['failure_type']=type(error).__name__
 raise
finally:
 subprocess.run(['docker','rm','-fv',db],capture_output=True,timeout=20)
 cleanup=subprocess.run(['docker','network','rm',network],capture_output=True,timeout=20)
 receipt['owned_network_removed']=cleanup.returncode==0
 Path('artifacts/edu-workflow-check/terminal-postgres-current.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
