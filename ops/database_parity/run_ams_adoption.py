"""Owned internal-network PostgreSQL adoption/restore probe, no host ports."""
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

IMAGE='postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea'


def run(*args,input=None):
    value=subprocess.run(args,input=input,text=True,capture_output=True,timeout=60)
    if value.returncode:
        raise RuntimeError(value.stdout+value.stderr)
    return value.stdout


def main():
    suffix=uuid.uuid4().hex[:12]
    network='odin-ams-adoption-'+suffix
    database=network+'-db'
    runtime=network+'-runtime'
    root=Path.cwd().resolve()
    output=root/'artifacts/edu-workflow-check'
    output.mkdir(parents=True,exist_ok=True)
    runtime_image=run('docker','image','inspect','--format','{{.Id}}',
                      'odin-school-candidate:e61fe44-amd64').strip()
    receipt={'passed':False,'scope':'native adoption and dump/restore, not service authority',
             'postgres_image':IMAGE,
             'runtime_image':runtime_image,
             'source_sha256':{str(path):hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (Path('backend/core/ams_evidence_schema.py'),
                              Path('backend/core/schema/bootstrap.py'),
                              Path('backend/core/schema/migrator.py'),
                              Path('backend/modules/printers/__init__.py'),
                              Path('backend/core/schema/migrations/020_ams_evidence.py'),
                              Path('ops/database_parity/ams_adoption_native.py'),
                              Path('ops/database_parity/run_ams_adoption.py'))}}
    def probe(db,restore=False):
        return run('docker','run','--rm','--name',runtime,'--platform','linux/amd64',
            '--network',network,'--entrypoint','python',
            '-v',str(root)+':/source:ro','-e','PYTHONPATH=/source/backend',
            '-e','AMS_TEST_DB_URL=postgresql+psycopg://postgres@pg/'+db,
            '-e','AMS_RESTORE_ONLY='+('1' if restore else '0'),
            runtime_image,'/source/ops/database_parity/ams_adoption_native.py')
    try:
        run('docker','network','create','--internal',network)
        run('docker','run','-d','--name',database,'--network',network,'--network-alias','pg',
            '--tmpfs','/var/lib/postgresql/data:rw,size=128m',
            '-e','POSTGRES_HOST_AUTH_METHOD=trust',IMAGE)
        deadline=time.monotonic()+30
        while True:
            value=subprocess.run(['docker','exec',database,'pg_isready','-U','postgres'],
                                 capture_output=True,timeout=5)
            if value.returncode==0:
                break
            if time.monotonic()>=deadline:
                raise RuntimeError('Owned native database not ready')
            time.sleep(.2)
        run('docker','exec',database,'createdb','-U','postgres','bootstrap_probe')
        first=probe('postgres')
        dump=run('docker','exec',database,'pg_dump','-U','postgres','--no-owner','postgres')
        run('docker','exec',database,'createdb','-U','postgres','restored')
        run('docker','exec','-i',database,'psql','-U','postgres','-d','restored','-v','ON_ERROR_STOP=1',input=dump)
        second=probe('restored',True)
        second_dump=run('docker','exec',database,'pg_dump','-U','postgres','--no-owner','restored')
        run('docker','exec',database,'createdb','-U','postgres','restored_twice')
        run('docker','exec','-i',database,'psql','-U','postgres','-d','restored_twice','-v','ON_ERROR_STOP=1',input=second_dump)
        third=probe('restored_twice',True)
        assert 'native-ams-adoption-pass' in first and 'native-ams-adoption-restored-pass' in second and 'native-ams-adoption-restored-pass' in third
        fingerprints=lambda result:[line for line in result.splitlines() if line.startswith('restored-checks-sha256=')]
        assert len(fingerprints(second))==1 and fingerprints(second)==fingerprints(third)
        (output/'ams-adoption-native-current.log').write_text(first+second+third)
        receipt['dump_sha256']=hashlib.sha256(dump.encode()).hexdigest()
        assert all(hashlib.sha256(Path(path).read_bytes()).hexdigest()==digest
                   for path,digest in receipt['source_sha256'].items())
        receipt['passed']=True
    except Exception as error:
        (output/'ams-adoption-native-current.log').write_text(str(error))
        raise
    finally:
        subprocess.run(['docker','rm','-f',runtime],capture_output=True,timeout=20)
        deleted=subprocess.run(['docker','rm','-f',database],capture_output=True,timeout=20)
        removed=subprocess.run(['docker','network','rm',network],capture_output=True,timeout=20)
        receipt['owned_database_removed']=deleted.returncode==0
        receipt['owned_network_removed']=removed.returncode==0
        (output/'ams-adoption-native-current.json').write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))


if __name__=='__main__':
    main()
