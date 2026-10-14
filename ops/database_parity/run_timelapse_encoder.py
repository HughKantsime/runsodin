"""Owned internal PostgreSQL + existing Linux runtime; no public ports/downloads."""
import hashlib,json,subprocess,time,uuid
from pathlib import Path
from ops.database_parity.run_ams_adoption import IMAGE,run
root=Path.cwd().resolve();name='odin-encoder-'+uuid.uuid4().hex[:12];database=name+'-db';runtime=name+'-runtime'
paths=['backend/modules/archives/timelapse_encode.py','backend/modules/archives/timelapse_capture.py',
    'backend/modules/archives/timelapse_frame_capture.py','backend/modules/archives/timelapse_frame_publication.py',
    'backend/core/sender_process_group.py','backend/core/media_process_capability.py','backend/core/timelapse_media_worker.py',
    'ops/database_parity/timelapse_encoder_native.py','ops/database_parity/run_timelapse_encoder.py']
receipt={'status':'IN_PROGRESS','source_sha256':{p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths},'postgres_image':IMAGE}
output=root/'artifacts/application-certification/timelapse-encoder-native-current.json'
try:
    receipt['runtime_image']=run('docker','image','inspect','--format','{{.Id}}','odin-school-candidate:e61fe44-amd64').strip()
    run('docker','network','create','--internal',name)
    run('docker','run','-d','--name',database,'--network',name,'--network-alias','pg','--tmpfs','/var/lib/postgresql/data:rw,size=128m','-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
    deadline=time.monotonic()+30
    while subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],capture_output=True,timeout=5).returncode:
        if time.monotonic()>deadline:raise RuntimeError('Owned encoder database not ready')
        time.sleep(.2)
    result=run('docker','run','--rm','--name',runtime,'--platform','linux/amd64','--network',name,
        '--user','0','--entrypoint','/bin/sh','-v',str(root)+':/source:ro','-e','PYTHONPATH=/source/backend',
        '-e','PYTHONPYCACHEPREFIX=/tmp/encoder-bytecode','-e','ENCODER_TEST_URL=postgresql+psycopg://postgres@pg/postgres',receipt['runtime_image'],
        '-c','exec runuser -u odin -- python /source/ops/database_parity/timelapse_encoder_native.py')
    receipt['evidence']=json.loads(result.strip().splitlines()[-1]);assert receipt['evidence']['status']=='PASS'
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==value for p,value in receipt['source_sha256'].items())
    receipt['status']='PASS'
finally:
    subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
    receipt['owned_database_removed']=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20).returncode==0
    receipt['owned_network_removed']=subprocess.run(['docker','network','rm',name],capture_output=True,timeout=20).returncode==0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
