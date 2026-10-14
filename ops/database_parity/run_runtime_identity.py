"""Owned offline root/uid10001 identity boundary probe; no host-path mutation."""
import hashlib
import json
from pathlib import Path
import subprocess
import uuid


def run(*args):
    value=subprocess.run(args,text=True,capture_output=True,timeout=30)
    if value.returncode:
        raise RuntimeError(value.stdout+value.stderr)
    return value.stdout


def main():
    image='sha256:9fb4ce775f12fbbf663db48cda368791833a0d2bb185e0d139b4fe3a7d1eb114'
    name='odin-runtime-proof-'+uuid.uuid4().hex[:12]
    sources={Path('docker/runtime_identity.py'):'/usr/local/libexec/odin-runtime-identity.py',
        Path('backend/core/runtime_identity.py'):'/usr/local/libexec/runtime_reader.py',
        Path('ops/database_parity/runtime_identity_native.py'):'/usr/local/libexec/runtime_test.py'}
    receipt=dict(passed=False,image=image,source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    for path in (
        Path('backend/modules/printers/telemetry/bambu/trusted_ams_acquisition.py'),
        Path('backend/modules/printers/telemetry/bambu/ams_acquisition_session.py'),
        Path('backend/modules/printers/telemetry/bambu/ams_acquisition_collector.py'),
        Path('ops/database_parity/trusted_ams_native.py'),Path(__file__).relative_to(Path.cwd())):
        receipt['source_sha256'][str(path)]=hashlib.sha256(path.read_bytes()).hexdigest()
    output=Path('artifacts/edu-workflow-check');output.mkdir(parents=True,exist_ok=True)
    try:
        run('docker','run','-d','--name',name,'--platform','linux/amd64','--network','none',
            '-e','PYTHONPYCACHEPREFIX=/tmp/odin-runtime-probe-bytecode',
            '-v',str(Path.cwd().resolve())+':/source:ro',
            '--entrypoint','python',image,'-c','import time;time.sleep(120)')
        run('docker','exec',name,'python','-I','-S','-c',"import os;os.makedirs('/usr/local/libexec',exist_ok=True)")
        for source,destination in sources.items():
            run('docker','cp',str(source),name+':'+destination)
        run('docker','exec',name,'python','-I','-S','-c',
            "import os; [os.chown(p,0,0) or os.chmod(p,0o444) for p in ('/usr/local/libexec/odin-runtime-identity.py','/usr/local/libexec/runtime_reader.py','/usr/local/libexec/runtime_test.py')]")
        run('docker','exec',name,'python','-I','-S','/usr/local/libexec/odin-runtime-identity.py')
        bound=run('docker','exec','--user','10001:10001','-e','ODIN_AMS_RUNTIME_READY=1',
            name,'python','/source/ops/database_parity/trusted_ams_native.py')
        assert 'trusted-ams-native-child-binding-pass' in bound
        receipt['actual_child_binding_passed']=True
        receipt['child_network_fixture']=True
        result=run('docker','exec',name,'python','-I','-S','/usr/local/libexec/runtime_test.py')
        receipt.update(json.loads(result))
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest()==digest for p,digest in receipt['source_sha256'].items())
    except Exception as error:
        (output/'runtime-identity-native-current.log').write_text(str(error))
        raise
    finally:
        cleanup=subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
        receipt['owned_container_removed']=cleanup.returncode==0
        (output/'runtime-identity-native-current.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    main()
