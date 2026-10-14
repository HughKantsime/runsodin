"""Pinned owned Linux codec/preflight/resource fixture, no network or database."""
import hashlib,json,subprocess,uuid
from pathlib import Path
ROOT=Path.cwd().resolve()
IMAGE='ghcr.io/hughkantsime/odin@sha256:f4ecb5d6dd0b8b8216fb29c680a887624714f79c0e7236e6ec20b22e2ca2899e'
name='odin-editor-codec-'+uuid.uuid4().hex[:12]
files=['backend/core/timelapse_derivative_timing.py','backend/core/timelapse_derivative_worker.py','backend/core/timelapse_probe_worker.py',
       'backend/core/media_derivative_capability.py','backend/core/media_probe_capability.py','backend/core/media_process_dispatch.py',
       'backend/core/sender_process_group.py','backend/core/media_process_capability.py','backend/core/timelapse_media_worker.py',
       'backend/core/timelapse_media_lock.py','backend/core/timelapse_recipes.py','ops/database_parity/timelapse_derivative_codec_native.py','ops/database_parity/run_timelapse_derivative_codec.py']
def pins():return {f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}
receipt={'passed':False,'scope':'Actual Linux codec, all6 rational speeds+trim sampledindices/counts/PTS; owned typed transform/readonly preflight/oneframe reuse; inherited hardFsize using smaller owned cap, expired probe child cleanup. No DB or HTTP service claim.', 'runtime_ref':IMAGE,'before_source_sha256':pins()}
output=ROOT/'artifacts/application-certification/timelapse-derivative-codec-current.json'
try:
    receipt['runtime_image']=subprocess.run(['docker','image','inspect','--format','{{.Id}}',IMAGE],capture_output=True,text=True,check=True).stdout.strip()
    result=subprocess.run(['docker','run','--rm','--name',name,'--platform','linux/amd64','--network','none','--read-only','--tmpfs','/tmp:rw,mode=1777',
        '--mount','type=bind,src='+str(ROOT)+',dst=/workspace,readonly','-e','PYTHONPATH=/workspace/backend','--entrypoint','python',IMAGE,
        '/workspace/ops/database_parity/timelapse_derivative_codec_native.py'],capture_output=True,text=True,timeout=600)
    log=ROOT/'artifacts/application-certification/timelapse-derivative-codec-resources.log';log.write_text(result.stdout+result.stderr)
    receipt['exit_code']=result.returncode;receipt['stdout_sha256']=hashlib.sha256(log.read_bytes()).hexdigest()
    receipt['facts']=[json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
    receipt['after_source_sha256']=pins()
    assert result.returncode==0 and receipt['after_source_sha256']==receipt['before_source_sha256']
    assert len([f for f in receipt['facts'] if f['marker']=='codec_recipe'])==7
    assert any(f['marker']=='actual_during_write_hard_fsize' for f in receipt['facts'])
    assert any(f['marker']=='actual_shared_deadline_child_cleanup' for f in receipt['facts'])
    receipt['passed']=True
finally:
    subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
    receipt['owned_container_absent']=subprocess.run(['docker','inspect',name],capture_output=True,timeout=20).returncode!=0
    output.write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps({'passed':receipt['passed'],'receipt':str(output),'owned_container_absent':receipt['owned_container_absent']}))
