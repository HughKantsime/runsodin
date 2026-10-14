"""Offline old-candidate to new-candidate upgrade using an owned test volume."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / 'artifacts/edu-workflow-check/school-candidate-ams-upgrade.json'
OLD = 'sha256:9fb4ce775f12fbbf663db48cda368791833a0d2bb185e0d139b4fe3a7d1eb114'
NEW = 'sha256:0206afb045e2bd961e10c3cc91b1f417e6826ac2a3ca4fb082a1a795bf759f68'


def call(*args, timeout=30):
    result = subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode:
        raise RuntimeError('Owned upgrade check command failed')
    return result.stdout


def wait_ready(name):
    deadline = time.monotonic()+120
    while time.monotonic()<deadline:
        try:
            value=json.loads(call('docker','exec',name,'python','-c',
                "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=3).read().decode())",timeout=10))
            if value.get('ready') is True:return value
        except Exception:pass
        time.sleep(.5)
    raise RuntimeError('Owned upgrade did not become ready')


def main():
    suffix=uuid.uuid4().hex[:12]
    volume='odin-school-upgrade-'+suffix
    names=['odin-school-before-'+suffix,'odin-school-after-'+suffix]
    receipt={'passed':False,'old_image':OLD,'new_image':NEW,'network':'none','production_data':False}
    stage='create-owned-volume'
    try:
        call('docker','volume','create',volume)
        stage='start-old-baseline'
        call('docker','run','-d','--name',names[0],'--network','none','--platform','linux/amd64',
             '--mount','type=volume,src='+volume+',dst=/data',OLD)
        receipt['baseline_readiness']=wait_ready(names[0])
        stage='owned-fixtures'
        fixture="""import os,sqlite3
c=sqlite3.connect('/data/odin.db')
c.execute("INSERT INTO groups(id,name,is_org) VALUES(851,'Owned upgrade fixture',1)")
c.execute("INSERT INTO printers(id,name,api_type,org_id,is_active) VALUES(851,'Owned disabled upgrade fixture','bambu',851,0)")
c.commit();c.close()
p='/data/static/branding/favicon.png'
open(p,'wb').write(b'owned preexisting favicon bytes')
os.chown(p,10001,10001);os.chmod(p,0o644)
"""
        call('docker','exec',names[0],'python','-c',fixture)
        # Key hashes stay only in this process's memory, never in the receipt.
        key_probe="import json,hashlib;print(json.dumps({p:hashlib.sha256(open('/data/'+p,'rb').read()).hexdigest() for p in ['.encryption_key','.jwt_secret','.odin-install-id']}))"
        ledger_probe="import json,sqlite3;c=sqlite3.connect('file:/data/odin.db?mode=ro',uri=True);print(json.dumps(c.execute(\"SELECT migration_id,checksum,dialect,applied_at FROM odin_schema_migrations WHERE migration_id='orm-baseline:1'\").fetchone()))"
        original_baseline=json.loads(call('docker','exec',names[0],'python','-c',ledger_probe))
        keys=json.loads(call('docker','exec',names[0],'python','-c',key_probe))
        call('docker','rm','-f',names[0])
        stage='start-assembled-candidate'
        call('docker','run','-d','--name',names[1],'--network','none','--platform','linux/amd64',
             '--mount','type=volume,src='+volume+',dst=/data',NEW)
        receipt['candidate_readiness']=wait_ready(names[1])
        stage='preservation-and-schema'
        current=json.loads(call('docker','exec',names[1],'python','-c',key_probe))
        assert current==keys
        receipt['encryption_jwt_install_identity_preserved']=True
        assert json.loads(call('docker','exec',names[1],'python','-c',ledger_probe)) == original_baseline
        receipt['original_orm_baseline_row_unchanged']=True
        probe="""import json,sqlite3,urllib.request
c=sqlite3.connect('file:/data/odin.db?mode=ro',uri=True)
assert c.execute('SELECT name,is_org FROM groups WHERE id=851').fetchone()==('Owned upgrade fixture',1)
assert c.execute('SELECT name,api_type,org_id,is_active FROM printers WHERE id=851').fetchone()==('Owned disabled upgrade fixture','bambu',851,0)
names={x[0] for x in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert {'ams_read_generations','ams_current_reads','ams_report_packets','ams_report_trays','printer_dispatch_claims','dispatch_attempt_receipts','dispatch_sender_registrations'}<=names
assert open('/data/static/branding/favicon.png','rb').read()==b'owned preexisting favicon bytes'
assert urllib.request.urlopen('http://127.0.0.1:8000/static/branding/favicon.png',timeout=3).read()==b'owned preexisting favicon bytes'
assert c.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
print(json.dumps({'synthetic_data_preserved':True,'custom_branding_preserved_and_served':True,'schema_and_integrity_verified':True}))
"""
        receipt.update(json.loads(call('docker','exec',names[1],'python','-c',probe)))
        receipt['passed']=True
    except Exception as error:
        receipt['failure_stage']=stage
        receipt['failure_type']=type(error).__name__
        raise
    finally:
        for name in names:
            subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
        cleanup=subprocess.run(['docker','volume','rm',volume],capture_output=True,timeout=20)
        receipt['owned_volume_removed']=cleanup.returncode==0
        OUTPUT.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':main()
