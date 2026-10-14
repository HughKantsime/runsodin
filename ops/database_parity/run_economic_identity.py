"""Owned internal PG16 probe using an existing local dependency image and MAIN source."""
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'sha256:a6e2b1cb91ee5acbcaa17b1dab0b0cedbd71d53361933dbfb46139511eac66f4'
PG = 'postgres:16-alpine'
def call(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError((result.stdout+'\n'+result.stderr)[-7500:])
    return result.stdout
suffix = uuid.uuid4().hex[:12]
network = 'odin-economic-identity-'+suffix
db = network+'-db'
receipt = {'passed':False, 'production_data':False, 'host_ports':False}
try:
    receipt['dependency_image'] = json.loads(call('docker','image','inspect',IMAGE))[0]['Id']
    receipt['postgres_image'] = json.loads(call('docker','image','inspect',PG))[0]['Id']
    call('docker','network','create','--internal',network)
    call('docker','run','-d','--pull=never','--name',db,'--network',network,'-e','POSTGRES_HOST_AUTH_METHOD=trust',receipt['postgres_image'])
    for _ in range(60):
        if subprocess.run(['docker','exec',db,'pg_isready','-U','postgres'],capture_output=True).returncode == 0:
            break
        time.sleep(.5)
    else:
        raise RuntimeError('Owned PG readiness timeout')
    output = call('docker','run','--rm','--pull=never','--network',network,'--platform','linux/amd64','--entrypoint','python',
        '-e','PYTHONPATH=/app/backend','-e','DEBUG=false','-e','COOKIE_SECURE=false','-e','JWT_SECRET_KEY=owned-registration-test-only',
        '-w','/app/backend','--mount','type=bind,src='+str(ROOT)+',dst=/app,readonly',IMAGE,
        '/app/ops/database_parity/economic_identity_native.py',db)
    receipt.update(json.loads(output.strip().splitlines()[-1]))
except Exception as error:
    receipt['failure_type'] = type(error).__name__
    raise
finally:
    receipt['owned_database_removed'] = subprocess.run(['docker','rm','-fv',db],capture_output=True,timeout=20).returncode == 0
    receipt['owned_network_removed'] = subprocess.run(['docker','network','rm',network],capture_output=True,timeout=20).returncode == 0
    paths = ['backend/core/schema/bootstrap.py','backend/core/schema/migrator.py','backend/core/schema/migrations/021_economic_identity.py',
        'backend/core/economic_identity.py','backend/modules/orders/routes/orders_crud.py',
        'tests/test_contracts/test_economic_identity_routes.py','tests/test_contracts/test_prospective_economic_identity.py',
        'ops/database_parity/economic_identity_native.py','ops/database_parity/run_economic_identity.py']
    receipt['source_pins'] = {path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in paths}
    (ROOT/'artifacts/application-certification/economic-identity-native.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
