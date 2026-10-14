"""Owned native PostgreSQL old-image upgrade, no host ports or real data."""
import json,subprocess,time,uuid
from pathlib import Path
OLD='sha256:9fb4ce775f12fbbf663db48cda368791833a0d2bb185e0d139b4fe3a7d1eb114'
NEW='odin-school-candidate:baseline-20261012'

def call(*args):
 r=subprocess.run(args,capture_output=True,text=True,timeout=90)
 if r.returncode:raise RuntimeError(r.stderr[-1500:])
 return r.stdout

def main():
 suffix=uuid.uuid4().hex[:12];network='odin-orm-upgrade-'+suffix;db=network+'-db'
 receipt={'passed':False,'production_data':False,'host_ports':False,'old_image':OLD,'new_image':NEW}
 stage='setup'
 try:
  call('docker','network','create','--internal',network)
  call('docker','run','-d','--name',db,'--network',network,'-e','POSTGRES_HOST_AUTH_METHOD=trust','postgres:16-alpine')
  for _ in range(60):
   p=subprocess.run(['docker','exec',db,'pg_isready','-U','postgres'],capture_output=True)
   if p.returncode==0:break
   time.sleep(.5)
  else:raise RuntimeError('Owned postgres readiness timeout')
  prefix=f"from sqlalchemy import create_engine,text,inspect;from core.schema.bootstrap import bootstrap_database; e=create_engine('postgresql+psycopg://postgres@{db}/postgres'); "
  stage='old-native-bootstrap'
  old=prefix+"bootstrap_database(e); c=e.connect(); c.execute(text(\"INSERT INTO users(username,password_hash,role,is_active) VALUES('owned-upgrade-user','synthetic-invalid-hash','viewer',true)\")); c.commit(); print('old-ready'); c.close()"
  call('docker','run','--rm','--network',network,'--platform','linux/amd64','--entrypoint','python','-w','/app/backend',OLD,'-c',old)
  stage='candidate-native-upgrade'
  new=prefix+'''\nwith e.connect() as c:
 before=c.execute(text("SELECT * FROM odin_schema_migrations WHERE migration_id='orm-baseline:1'")).all()
bootstrap_database(e)
bootstrap_database(e)
with e.connect() as c:
 assert c.execute(text("SELECT * FROM odin_schema_migrations WHERE migration_id='orm-baseline:1'")).all()==before
 assert c.execute(text("SELECT count(*) FROM users WHERE username='owned-upgrade-user'")).scalar_one()==1
 assert {'dispatch_attempt_receipts','ams_report_trays'}<=set(inspect(c).get_table_names())
with e.begin() as c:
 c.execute(text("UPDATE odin_schema_migrations SET checksum=repeat('0',64) WHERE migration_id='orm-baseline:1'"))
 tables=set(inspect(c).get_table_names())
from core.schema.migrator import MigrationError
try: bootstrap_database(e)
except MigrationError: pass
else: raise AssertionError('Corrupted history accepted')
with e.connect() as c: assert set(inspect(c).get_table_names())==tables
print('native-upgrade-preservation-repeat-corruption-refusal-pass')
'''
  result=call('docker','run','--rm','--network',network,'--platform','linux/amd64','--entrypoint','python','-w','/app/backend',NEW,'-c',new)
  assert 'native-upgrade-preservation-repeat-corruption-refusal-pass' in result
  receipt.update(passed=True,data_preserved=True,baseline_row_unchanged=True,repeat_startup=True,corrupt_history_refused=True)
 except Exception as error:
  receipt.update(failure_stage=stage,failure_type=type(error).__name__)
  raise
 finally:
  subprocess.run(['docker','rm','-fv',db],capture_output=True,timeout=20)
  cleanup=subprocess.run(['docker','network','rm',network],capture_output=True,timeout=20)
  receipt['owned_network_removed']=cleanup.returncode==0
  Path('artifacts/edu-workflow-check/orm-baseline-postgres-upgrade.json').write_text(json.dumps(receipt,indent=2)+'\n')
 print(json.dumps(receipt))
if __name__=='__main__':main()
