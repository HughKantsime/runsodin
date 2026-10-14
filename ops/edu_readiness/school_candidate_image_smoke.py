"""Offline, owned-container acceptance of the assembled school candidate image."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'artifacts/edu-workflow-check'
IMAGE = 'sha256:0206afb045e2bd961e10c3cc91b1f417e6826ac2a3ca4fb082a1a795bf759f68'
SOURCE_PINS = 'school-candidate-ams-image-source-pins.json'
RECEIPT_NAME = 'school-candidate-ams-image-smoke.json'


def command(*args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        # Docker output can contain application data. Never propagate it.
        raise RuntimeError('Owned image check command failed')
    return result.stdout


def main():
    name = 'odin-school-ams-smoke-' + uuid.uuid4().hex[:12]
    receipt = dict(image=IMAGE, source_pins=SOURCE_PINS, network='none', production_mounts=False, passed=False)
    pins = json.loads((EVIDENCE / SOURCE_PINS).read_text())
    sources = {p: sha for p, sha in pins.items() if p.startswith('backend/') and p.endswith('.py')}
    try:
        architecture = command('docker', 'image', 'inspect', IMAGE, '--format', '{{.Architecture}}').strip()
        assert architecture == 'amd64'
        receipt['architecture'] = architecture
        command('docker', 'run', '-d', '--name', name, '--platform', 'linux/amd64',
                '--network', 'none', IMAGE)
        deadline = time.monotonic() + 120
        ready = None
        while time.monotonic() < deadline:
            try:
                ready = json.loads(command('docker', 'exec', name, 'python', '-c',
                    "import json,urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read().decode())", timeout=10))
                if ready.get('ready') is True:
                    break
            except Exception:
                pass
            time.sleep(.5)
        assert ready and ready.get('ready') is True
        receipt['readiness'] = ready
        # HTTP readiness can precede supervisor's startsecs transition.
        while time.monotonic() < deadline:
            status_result = subprocess.run(['docker','exec',name,'supervisorctl','status'],capture_output=True,text=True,timeout=10)
            supervision = [[line.split()[0], line.split()[1]] for line in status_result.stdout.splitlines() if len(line.split()) >= 2]
            if status_result.returncode == 0 and len(supervision) == 9 and all(state == 'RUNNING' for _, state in supervision):
                break
            time.sleep(.5)
        receipt['supervision'] = supervision
        assert len(supervision) == 9 and all(state == 'RUNNING' for _, state in supervision)
        receipt['supervision'] = supervision
        backend_pid = int(command('docker', 'exec', name, 'supervisorctl', 'pid', 'backend').strip())
        probe_code = """import json,os
try:
 pid=PID
 status=open('/proc/'+str(pid)+'/status').read().splitlines()
 uid=next(line for line in status if line.startswith('Uid:')).split()[1:]
 assert uid==['10001']*4
 flags=open('/proc/'+str(pid)+'/environ','rb').read().split(bytes([0]))
 assert b'ODIN_AMS_RUNTIME_READY=1' in flags
 os.environ['ODIN_AMS_RUNTIME_READY']='1'
 from core.runtime_identity import read_runtime_identity,revalidate_runtime_identity
 value=read_runtime_identity();revalidate_runtime_identity(value)
 print(json.dumps({'startup_flag':True,'runtime_bound':True}))
except Exception as error:
 print(json.dumps({'stage':'supervised-runtime','error_type':type(error).__name__,'errno':getattr(error,'errno',None)}))
""".replace('PID', str(backend_pid))
        runtime_result = json.loads(command('docker', 'exec', '--user', '10001:10001', '--workdir', '/app/backend', name, 'python', '-c', probe_code))
        receipt['runtime_probe'] = runtime_result
        assert runtime_result == {'startup_flag':True,'runtime_bound':True}
        receipt['actual_backend_startup_flag'] = True
        receipt['actual_uid10001_runtime_bound'] = True
        program = "import hashlib,json;expected=" + repr(sources) + ";print(json.dumps([p for p,h in expected.items() if hashlib.sha256(open('/app/'+p,'rb').read()).hexdigest()!=h]))"
        mismatches = json.loads(command('docker', 'exec', name, 'python', '-c', program))
        assert not mismatches
        receipt['packaged_python_source'] = dict(count=len(sources), mismatches=mismatches)
        catalog_path = 'backend/core/schema/orm_baselines.json'
        if catalog_path in pins:
            catalog_probe = "import hashlib;print(hashlib.sha256(open('/app/" + catalog_path + "','rb').read()).hexdigest())"
            assert command('docker','exec',name,'python','-c',catalog_probe).strip() == pins[catalog_path]
            receipt['packaged_baseline_catalog_hash_verified'] = True
        probe = "import sqlite3,json;c=sqlite3.connect('file:/data/odin.db?mode=ro',uri=True);print(json.dumps([x[0] for x in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")]))"
        tables = set(json.loads(command('docker', 'exec', name, 'python', '-c', probe)))
        required = {'ams_read_generations','ams_current_reads','ams_report_packets','ams_report_trays',
                    'printer_dispatch_claims','dispatch_attempt_receipts','dispatch_sender_registrations'}
        assert required <= tables
        assert 'timelapse_frame_namespaces' not in tables
        receipt['required_school_tables'] = sorted(required)
        receipt['passed'] = True
    except Exception as error:
        receipt['failure_type'] = type(error).__name__
        raise
    finally:
        cleanup = subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
        receipt['owned_container_removed'] = cleanup.returncode == 0
        (EVIDENCE / RECEIPT_NAME).write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__ == '__main__':
    main()
