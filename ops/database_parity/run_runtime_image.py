"""Disposable derived-image startup/ownership proof, not release certification."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

BASE='odin-school-candidate:e61fe44-amd64'


def run(*args,timeout=30):
    value=subprocess.run(args,text=True,capture_output=True,timeout=timeout)
    if value.returncode:
        raise RuntimeError(value.stdout+value.stderr)
    return value.stdout


def main():
    root=Path.cwd().resolve()
    suffix=uuid.uuid4().hex[:12]
    name='odin-runtime-image-'+suffix
    image='odin-runtime-boundary:'+suffix
    context=Path(tempfile.mkdtemp(prefix='odin-runtime-context-'))
    output=root/'artifacts/edu-workflow-check';output.mkdir(parents=True,exist_ok=True)
    receipt={'passed':False,'scope':'derived-image ownership/startup/filesystem proof, not release or all API workflow acceptance',
        'base_image':run('docker','image','inspect','--format','{{.Id}}',BASE).strip()}
    def execute(code,user='0',ready=False):
        args=['docker','exec','--user',user]
        if ready:
            args+=['-e','ODIN_AMS_RUNTIME_READY=1']
        return run(*args,name,'python','-c',code)
    def health():
        code="import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2);print(r.status)"
        deadline=time.monotonic()+45
        while True:
            try:
                if execute(code).strip()=='200':
                    return
            except RuntimeError:
                pass
            if time.monotonic()>=deadline:
                raise RuntimeError('Owned image did not become ready')
            time.sleep(.5)
    def identity():
        return json.loads(execute("from core.runtime_identity import read_runtime_identity;import json;v=read_runtime_identity();print(json.dumps([v.incarnation,v.clock_domain]))",user='10001:10001',ready=True))
    try:
        shutil.copytree(root/'backend',context/'backend',ignore=shutil.ignore_patterns(
            '__pycache__','*.pyc','*.db','*.db-*','.env','backups'))
        for source,destination in [('docker/entrypoint.sh','entrypoint.sh'),
                                   ('docker/runtime_identity.py','runtime_identity.py')]:
            shutil.copy2(root/source,context/destination)
        shutil.copy2(root/'docker/branding_defaults.py',context/'branding_defaults.py')
        dockerfile='''FROM odin-school-candidate:e61fe44-amd64
USER root
COPY backend/ /app/backend/
COPY entrypoint.sh /usr/local/bin/odin-entrypoint
COPY runtime_identity.py /usr/local/libexec/odin-runtime-identity.py
COPY branding_defaults.py /usr/local/libexec/odin-branding-defaults.py
RUN chown -R root:root /app && mkdir -p /usr/local/share/odin/branding-defaults && cp -a /app/backend/static/branding/. /usr/local/share/odin/branding-defaults/ && rm -rf /app/backend/static/branding && ln -s /data/static/branding /app/backend/static/branding && chmod -R go-w /app && chmod 0555 /usr/local/bin/odin-entrypoint && chmod 0444 /usr/local/libexec/odin-runtime-identity.py /usr/local/libexec/odin-branding-defaults.py && chown -R odin:odin /app/go2rtc /data
ENV PYTHONPATH=/app/backend
ENTRYPOINT ["/usr/local/bin/odin-entrypoint"]
'''
        (context/'Dockerfile').write_text(dockerfile)
        receipt['source_sha256']={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (root/'Dockerfile',root/'docker/entrypoint.sh',root/'docker/runtime_identity.py',root/'docker/branding_defaults.py',root/'backend/core/runtime_identity.py',root/'backend/core/branding_storage.py',root/'backend/core/app.py',root/'backend/modules/system/routes_settings.py')}
        result=run('docker','build','--platform','linux/amd64','-t',image,str(context),timeout=180)
        (output/'runtime-image-build-current.log').write_text(result)
        receipt['image_id']=run('docker','image','inspect','--format','{{.Id}}',image).strip()
        run('docker','run','-d','--name',name,'--network','none','--tmpfs','/data:rw,size=256m',image)
        health()
        first=identity()
        permission_code='''import json,os
result={}
for path in ('/usr/local/bin/odin-entrypoint','/usr/local/libexec/odin-runtime-identity.py','/app/entrypoint.sh','/app/backend/core/schema/bootstrap.py'):
 try:
  with open(path,'a'):pass
  result[path]=False
 except PermissionError:result[path]=True
for path in ('/data/uploads','/data/backups','/data/vision_models','/data/static/branding','/app/backend/static/branding','/app/go2rtc'):
 os.makedirs(path,exist_ok=True)
 filename=path+'/owned-permission-probe'
 with open(filename,'w') as target:target.write('owned test')
 os.unlink(filename)
 result[path]=True
print(json.dumps(result))
'''
        receipt['permissions']=json.loads(execute(permission_code,user='10001:10001'))
        assert all(receipt['permissions'].values())
        branding_check='''import urllib.request,hashlib
from pathlib import Path
path=Path('/data/static/branding/favicon.png')
original=path.read_bytes()
assert urllib.request.urlopen('http://127.0.0.1:8000/static/branding/favicon.png',timeout=2).read()==original
path.write_bytes(original+b'owned custom suffix')
print(hashlib.sha256(path.read_bytes()).hexdigest())
'''
        custom=execute(branding_check,user='10001:10001').strip()
        run('docker','exec','--user','10001:10001',name,'python','-I','-S','/usr/local/libexec/odin-branding-defaults.py')
        preserved=execute("import hashlib;from pathlib import Path;print(hashlib.sha256(Path('/data/static/branding/favicon.png').read_bytes()).hexdigest())",user='10001:10001').strip()
        assert preserved==custom
        receipt['branding_url_exact_bytes']=True
        receipt['custom_branding_preserved']=True
        status=run('docker','exec',name,'supervisorctl','status')
        (output/'runtime-image-supervisor-current.log').write_text(status)
        assert 'backend' in status and 'mqtt_monitor' in status and 'RUNNING' in status
        run('docker','restart',name)
        health()
        second=identity()
        assert first[0]!=second[0] and first[1]==second[1]
        receipt['actual_container_restart_rotates_identity']=True
        # Missing protected creator is an owned image fault injection. An old
        # valid record remains, but new workers must inherit availability=0.
        execute("import os;os.rename('/usr/local/libexec/odin-runtime-identity.py','/usr/local/libexec/identity-disabled-for-owned-test')")
        run('docker','restart',name)
        health()
        flag_code='''import os,json
found=[]
for pid in os.listdir('/proc'):
 if not pid.isdigit() or pid==str(os.getpid()):continue
 try:
  command=open('/proc/'+pid+'/cmdline','rb').read()
  if b'uvicorn' not in command and b'modules.printers.monitors.mqtt_monitor' not in command:continue
  environment=open('/proc/'+pid+'/environ','rb').read().split(b'\\0')
  found.append(next((v.split(b'=',1)[1].decode() for v in environment if v.startswith(b'ODIN_AMS_RUNTIME_READY=')),None))
 except (OSError,UnicodeError):continue
print(json.dumps(found))
'''
        flags=json.loads(execute(flag_code,user='10001:10001'))
        assert flags and all(flag=='0' for flag in flags)
        receipt['failed_creator_preserves_ordinary_readiness']=True
        receipt['new_workers_refuse_old_record_availability']=True
        assert all(hashlib.sha256((root/p).read_bytes()).hexdigest()==digest for p,digest in receipt['source_sha256'].items())
        receipt['passed']=True
    except Exception as error:
        (output/'runtime-image-current.log').write_text(str(error))
        raise
    finally:
        removed=subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=30)
        receipt['owned_container_removed']=removed.returncode==0
        deleted=subprocess.run(['docker','image','rm',image],capture_output=True,timeout=30)
        receipt['owned_image_tag_removed']=deleted.returncode==0
        shutil.rmtree(context)
        (output/'runtime-image-current.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    main()
