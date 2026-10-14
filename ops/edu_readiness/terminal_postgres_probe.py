"""Native owned PostgreSQL claim retirement and actual HMS callback acceptance."""
import json,os,secrets,tempfile,sys
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
# Satisfy the real configuration path with an ephemeral private test credential.
secret=Path(tempfile.mkdtemp())/'db-secret';secret.write_text(secrets.token_hex(24));secret.chmod(0o600)
os.environ['DATABASE_URL']='postgresql://postgres@'+sys.argv[1]+'/postgres'
os.environ['DATABASE_PASSWORD_FILE']=str(secret)
from core.db import engine
from core.schema.bootstrap import bootstrap_database
from sqlalchemy import text,insert
from core import dispatch_claims
from core.dispatch_target_lock import acquire_target_lock
from core.dispatch_receipts import store_admission_receipt
from core.dispatch_receipt_schema import DispatchAttemptReceipt
from core.db_utils import _ConnectionAdapter
bootstrap_database(engine)
with engine.begin() as c:c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owner fixture',true),(2,'Tenant fixture',true)"))
native_acquire=acquire_target_lock
def acquire_target_lock(engine,key):
    failures=[]
    def trace(frame,event,arg):
        if event=='exception' and frame.f_code.co_filename.endswith('dispatch_target_lock.py'):
            error=arg[1]
            failures.append({'stage':frame.f_code.co_name,'line':frame.f_lineno,'type':type(error).__name__,'sqlstate':getattr(getattr(error,'orig',error),'sqlstate',None)})
        return trace
    sys.settrace(trace)
    try:result=native_acquire(engine,key)
    finally:sys.settrace(None)
    if result.status=='uncertain':print(json.dumps({'target_status':result.status,'failures':failures}),file=sys.stderr)
    return result
key=secrets.token_bytes(32)
checks=[]

def fixture(identifier,status=None):
    now=datetime.now(timezone.utc);start=now-timedelta(seconds=10)
    token=secrets.token_hex(16);physical=secrets.token_hex(32);config=secrets.token_hex(32);payload=secrets.token_hex(32)
    name='odin-dispatch-'+token+'.gcode'
    with engine.begin() as c:
        c.execute(text("INSERT INTO models(id,name,org_id) VALUES(:id,'Native owned payload',2)"),{'id':identifier})
        c.execute(text("INSERT INTO printers(id,name,is_active,api_type,org_id,shared) VALUES(:id,:name,true,'moonraker',1,true)"),{'id':identifier,'name':'Native owned printer '+str(identifier)})
        c.execute(text("INSERT INTO jobs(id,model_id,printer_id,item_name,status,charged_to_org_id,actual_start,payload_sha256) VALUES(:id,:id,:id,'Native owned job','printing',2,:start,:payload)"),{'id':identifier,'start':start,'payload':payload})
        c.execute(insert(dispatch_claims.TABLE).values(printer_id=identifier,job_id=identifier,physical_key=physical,configuration_key=config,payload_sha256=payload,owner_org_id=1,state='adapter_entered',token=token))
        store_admission_receipt(c,token=token,job={'id':identifier,'printer_id':identifier,'education_submission_id':None},creds={'plug_configuration':{}},key=key,configuration_key=config,filename=name,broker_connection=None,power_accepted=True)
        c.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,filename,job_name,status,started_at,observed_received_at) VALUES(:id,:id,:id,:name,:name,'running',:start,:now)"),{'id':identifier,'name':name,'start':start,'now':now})
        c.execute(text('UPDATE dispatch_attempt_receipts SET observed_print_job_id=:id WHERE claim_token=:token'),{'id':identifier,'token':token})
        c.execute(text("UPDATE printer_dispatch_claims SET state='accepted' WHERE token=:token"),{'token':token})
        if status:
            c.execute(text('UPDATE jobs SET status=:status,actual_end=:now WHERE id=:id'),{'id':identifier,'status':status,'now':now})
            c.execute(text('UPDATE print_jobs SET status=:status,ended_at=:now WHERE id=:id'),{'id':identifier,'status':status,'now':now})
    return dict(printer_id=identifier,physical_key=physical,configuration_key=config,owner_org_id=1,key=key),name,token

for identifier,status in enumerate(('completed','cancelled','failed'),1):
    args,name,token=fixture(identifier,status)
    failures=[]
    def trace(frame,event,arg):
        if event=='exception' and frame.f_code.co_filename.endswith('dispatch_target_lock.py'):
            error=arg[1]
            failures.append({'stage':frame.f_code.co_name,'line':frame.f_lineno,'type':type(error).__name__,'sqlstate':getattr(getattr(error,'orig',error),'sqlstate',None)})
        return trace
    sys.settrace(trace)
    target=acquire_target_lock(engine,args['physical_key'])
    sys.settrace(None)
    if target.status!='acquired':
        print(json.dumps({'target_status':target.status,'failures':failures}),file=sys.stderr)
        raise AssertionError('Native target lock not acquired')
    try:assert dispatch_claims.retire_terminal_locked(engine,**args,target_handle=target.handle)=='retired'
    finally:assert target.handle.close().released
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE token=:token'),{'token':token}).scalar_one()==0
        assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE claim_token=:token'),{'token':token}).scalar_one()==identifier
    checks.append('native-'+status+'-shared-tenant-retirement')

for identifier,corruption in enumerate(('uncertain','unbound','owner'),10):
    args,name,token=fixture(identifier,'completed')
    with engine.begin() as c:
        if corruption=='uncertain':c.execute(text("UPDATE printer_dispatch_claims SET state='uncertain' WHERE token=:token"),{'token':token})
        elif corruption=='unbound':c.execute(text('UPDATE dispatch_attempt_receipts SET observed_print_job_id=NULL WHERE claim_token=:token'),{'token':token})
        else:c.execute(text('UPDATE printer_dispatch_claims SET owner_org_id=2 WHERE token=:token'),{'token':token})
    target=acquire_target_lock(engine,args['physical_key']);assert target.status=='acquired'
    try:assert dispatch_claims.retire_terminal_locked(engine,**args,target_handle=target.handle)=='retained'
    finally:assert target.handle.close().released
    checks.append('native-'+corruption+'-retained')

from modules.notifications import error_handling
from modules.printers.monitors import mqtt_printer,mqtt_job_lifecycle
from modules.archives import archive
from core.registry import registry
from types import SimpleNamespace
import time
registry._providers={}
@contextmanager
def database():
    raw=engine.raw_connection()
    try:yield _ConnectionAdapter(raw,True)
    finally:raw.close()
error_handling.get_db=database;mqtt_printer.get_db=database;mqtt_job_lifecycle.get_db=database
error_handling.dispatch_alert=lambda **kw:None;archive.create_print_archive=lambda *a,**kw:None
mqtt_printer.ws_push=lambda *a,**kw:None
for identifier,terminal in enumerate(('IDLE','FINISH'),20):
    args,name,token=fixture(identifier)
    monitor=mqtt_printer.PrinterMonitor(identifier,'Owned native monitor','192.0.2.1','SYNTHETIC','')
    monitor._state={'gcode_state':'RUNNING','subtask_name':name,'gcode_file':name}
    monitor._current_job_id=identifier;monitor._linked_job_id=identifier;monitor._last_gcode_state='RUNNING'
    monitor._last_heartbeat=time.time();monitor._last_spool_check=time.time();monitor._camera_discovered=True
    monitor._dispatch_alert=lambda **kw:None;monitor._trigger_reschedule=lambda:None
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':terminal,'subtask_name':name,'hms':[{'attr':0x0C000300,'code':0x00030006}]}}))
    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()=='failed'
        assert c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':identifier}).scalar_one()=='failed'
    assert monitor._current_job_id is None
    checks.append('native-HMS-before-'+terminal)
# Real configured target/restore exclusion, not a plain-engine substitute.
from core.database_config import RESTORE_ADVISORY_LOCK,create_database_engine
from psycopg.pq import TransactionStatus
from sqlalchemy import create_engine,event
plain=create_engine(engine.url)
engine.dispose()
with engine.connect() as fresh:
    assert fresh.connection.driver_connection.info.transaction_status==TransactionStatus.IDLE
checks.append('fresh-configured-connection-idle-after-setup')
target_key=secrets.token_hex(32)
lock=acquire_target_lock(engine,target_key);assert lock.status=='acquired'
assert lock.handle._restore_required and lock.handle.held()
assert acquire_target_lock(engine,target_key).status=='conflict'
assert lock.handle.held()
checks.append('configured-competing-target-refused')
with plain.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
    assert c.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':RESTORE_ADVISORY_LOCK}).scalar_one() is False
assert lock.handle.close().released
checks.append('configured-restore-exclusive-refused-while-target-held')
# A pool checkout contaminated with another advisory lock must refuse.
def contaminate(dbapi,record,proxy):
    with dbapi.cursor() as cursor:cursor.execute('SELECT pg_advisory_lock(987654321)')
    dbapi.commit()
event.listen(engine,'checkout',contaminate)
try:assert acquire_target_lock(engine,secrets.token_hex(32)).status=='uncertain'
finally:event.remove(engine,'checkout',contaminate)
checks.append('unrelated-advisory-refused')
lock=acquire_target_lock(engine,secrets.token_hex(32));assert lock.status=='acquired'
with lock.handle._connection.connection.driver_connection.cursor() as c:
    c.execute('SELECT pg_advisory_unlock_shared(%s)',(RESTORE_ADVISORY_LOCK,))
assert not lock.handle.held() and not lock.handle.close().released
checks.append('lost-restore-share-invalidates-target-proof')
engine.dispose()
with plain.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
    assert c.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':RESTORE_ADVISORY_LOCK}).scalar_one() is True
    assert c.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':RESTORE_ADVISORY_LOCK}).scalar_one() is True
checks.append('restore-exclusive-available-after-configured-sessions-close')
plain.dispose()
monitor_engine=create_database_engine(os.environ['DATABASE_URL'],role='monitor-bambu',password_file=str(secret))
try:
    monitored=acquire_target_lock(monitor_engine,secrets.token_hex(32));assert monitored.status=='acquired'
    assert monitored.handle._restore_required and monitored.handle.held()
    assert monitored.handle.close().released
    fresh=acquire_target_lock(monitor_engine,secrets.token_hex(32));assert fresh.status=='acquired'
    assert fresh.handle.close().released
finally:monitor_engine.dispose()
checks.append('monitor-role-fresh-after-invalidation-preserves-both-locks')
print(json.dumps({'passed':True,'checks':checks,'scope':'native SQL/locks and real HMS callback; seeded admitted receipts/observations; no printer transport or external alerts'}))
