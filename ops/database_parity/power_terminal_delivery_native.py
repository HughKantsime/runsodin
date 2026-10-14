"""Owned native producer/bootstrap delivery proof; no power command or timer fires."""
import os,secrets,tempfile,threading,sys,traceback
_sensitive=[]
class RedactedOutput:
 def __init__(self,stream):self.stream=stream
 def write(self,value):
  for secret in _sensitive:
   if secret:value=value.replace(secret,"<owned credential redacted>")
  return self.stream.write(value)
 def flush(self):return self.stream.flush()
 def __getattr__(self,name):return getattr(self.stream,name)
sys.stdout=RedactedOutput(sys.stdout);sys.stderr=RedactedOutput(sys.stderr)
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48);_sensitive.append(os.environ['JWT_SECRET_KEY'])
_config=tempfile.TemporaryDirectory(prefix='owned-power-config-')
os.environ['ODIN_DATA_DIR']=_config.name
os.environ['DATABASE_PASSWORD_FILE']=os.environ['POWER_NATIVE_PASSWORD_FILE']
os.environ['DATABASE_URL']='postgresql://postgres@pg/postgres'
from sqlalchemy import create_engine,text,event
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core import db as core_db,event_bus,events,db_utils
from core.event_bus import InMemoryEventBus
from core.db_utils import _ConnectionAdapter
from core.monitor_bootstrap import bootstrap_monitor_authority
from modules.printers import smart_plug
from modules.printers.monitors import mqtt_job_lifecycle as mqtt,moonraker_monitor as moon
from modules.notifications import job_events,event_dispatcher,alert_dispatch
from modules.archives import archive
from core import dispatch_claims


def main():
 credential_path=Path(os.environ['POWER_NATIVE_PASSWORD_FILE']);credential_stat=credential_path.stat()
 assert credential_stat.st_uid==os.geteuid() and credential_stat.st_mode & 0o777==0o600
 password=credential_path.read_text().strip();_sensitive.append(password)
 print('runtime-private-file uid='+str(os.geteuid())+' mode=600')
 engine=create_engine(URL.create('postgresql+psycopg',username='postgres',password=password,host='pg',database='postgres'),hide_parameters=True)
 bootstrap_database(engine)
 # Owned probe-only deferred fault: fail actual COMMIT, never fabricate a SQLA event failure.
 with engine.begin() as c:
  c.execute(text('CREATE TABLE owned_power_commit_targets(print_job_id INTEGER PRIMARY KEY)'))
  c.execute(text("""CREATE FUNCTION owned_power_commit_fault() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='completed' AND EXISTS(SELECT 1 FROM owned_power_commit_targets WHERE print_job_id=NEW.id) THEN RAISE EXCEPTION 'Owned deferred commit fault' USING ERRCODE='23514'; END IF; RETURN NEW; END $$"""))
  c.execute(text('CREATE CONSTRAINT TRIGGER owned_power_commit_fault AFTER UPDATE ON print_jobs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_power_commit_fault()'))
 core_db.engine=engine;db_utils.engine=engine;moon.engine=engine;smart_plug.engine=engine
 bus=InMemoryEventBus();event_bus._bus=bus
 # Stub external notification/archive/polling effects only; database policy remains real.
 for module,names in ((event_dispatcher,('dispatch_alert',)),(alert_dispatch,('dispatch_alert','_start_bed_cooled_monitor')),(job_events,('dispatch_alert','_start_bed_cooled_monitor')),(archive,('create_print_archive',))):
  for name in names:setattr(module,name,lambda *args,**kwargs:None)
 commands=[];timers=[]
 smart_plug.power_off=lambda pid,**kwargs:commands.append(('off',pid))
 smart_plug.power_on=lambda pid,**kwargs:commands.append(('on',pid))
 class InertTimer:
  def __init__(self,delay,callback):self.delay=delay;self.callback=callback;self.cancelled=False;timers.append(self)
  def start(self):pass
  def cancel(self):self.cancelled=True
 smart_plug.threading.Timer=InertTimer
 bootstrap_monitor_authority();bootstrap_monitor_authority()
 assert len(bus._handlers[events.JOB_STARTED])==len(bus._handlers[events.JOB_COMPLETED])==1
 retirement_returned={};original_retire=dispatch_claims.retire_observed_terminal
 def retired(*args,**kwargs):
  value=original_retire(*args,**kwargs)
  retirement_returned[kwargs['print_job_id']]=value
  return value
 dispatch_claims.retire_observed_terminal=retired
 received=[];fixtures={}
 def observe(message):
  pj=message.data['print_job_id'];fixture=fixtures[pj]
  # A physically separate PostgreSQL connection must see committed terminal state.
  with engine.connect() as c:
   assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':pj}).scalar_one()=='completed'
   assert c.execute(text('SELECT status FROM jobs WHERE id=:id'),{'id':fixture['job']}).scalar_one()=='completed'
   if fixture.get('submission'):
    assert c.execute(text('SELECT status FROM education_submissions WHERE id=:id'),{'id':fixture['submission']}).scalar_one()=='completed'
    assert c.execute(text('SELECT count(*) FROM education_monitor_claims WHERE job_id=:id'),{'id':fixture['job']}).scalar_one()==0
  assert pj in retirement_returned
  received.append(message)
 bus.subscribe(events.JOB_COMPLETED,observe)
 counter=0
 def seed(education):
  nonlocal counter
  counter+=1;name=f'Owned power {counter}'
  with engine.begin() as c:
   org=c.execute(text('INSERT INTO groups(name,is_org) VALUES(:name,TRUE) RETURNING id'),{'name':name}).scalar_one()
   student=c.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id) VALUES(:name,'owned-invalid','viewer',TRUE,:org) RETURNING id"),{'name':name+' student','org':org}).scalar_one()
   teacher=c.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id) VALUES(:name,'owned-invalid','viewer',TRUE,:org) RETURNING id"),{'name':name+' teacher','org':org}).scalar_one()
   pid=c.execute(text("INSERT INTO printers(name,is_active,api_type,api_host,org_id,gcode_state,last_seen,model,machine_type,bed_x_mm,bed_y_mm,nozzle_diameter,plug_type,plug_host,plug_auto_on,plug_auto_off,plug_cooldown_minutes,plug_power_state) VALUES(:name,TRUE,'bambu','192.0.2.1',:org,'FINISH',CURRENT_TIMESTAMP,'Bambu Lab P1S','P1S',256,256,0.4,'tasmota','192.0.2.1',FALSE,TRUE,1,TRUE) RETURNING id"),{'name':name,'org':org}).scalar_one()
   model=c.execute(text("INSERT INTO models(name,category,org_id) VALUES(:name,:category,:org) RETURNING id"),{'name':name,'category':'Education Submission' if education else 'Owned generic','org':org}).scalar_one()
   file=c.execute(text("INSERT INTO print_files(filename,original_filename,project_name,stored_path,org_id,created_by,storage_bytes,blob_state,compatibility_facts_json,model_id) VALUES('owned.gcode','owned.gcode',:name,:path,:org,:uid,100,'present','{}',:model) RETURNING id"),{'name':name,'path':'/opaque/'+name+'.gcode','org':org,'uid':student,'model':model}).scalar_one()
   c.execute(text('UPDATE models SET print_file_id=:file WHERE id=:model'),{'file':file,'model':model})
   job=c.execute(text("INSERT INTO jobs(model_id,print_file_id,item_name,quantity,status,printer_id,duration_hours,submitted_by,approved_by,charged_to_user_id,charged_to_org_id,hold,priority,actual_start) VALUES(:model,:file,:name,1,:status,:pid,1.0,:student,:teacher,:student,:org,FALSE,3,CURRENT_TIMESTAMP) RETURNING id"),{'model':model,'file':file,'name':name,'status':'scheduled' if education else 'printing','pid':pid,'student':student,'teacher':teacher,'org':org}).scalar_one()
   fixture={'printer':pid,'job':job,'name':name,'education':education}
   if education:
    center=c.execute(text("INSERT INTO education_cost_centers(org_id,name_key,code_key,display_name,code,state,revision,created_by) VALUES(:org,'owned','owned','Owned','OWNED','active',1,:uid) RETURNING id"),{'org':org,'uid':teacher}).scalar_one()
    c.execute(text("INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,state,granted_by) VALUES(:org,:center,:pid,'active',:uid)"),{'org':org,'center':center,'pid':pid,'uid':teacher})
    op=secrets.token_hex(16)
    c.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,expected_hash,reservation_released_at) VALUES(:op,:org,:uid,'committed',100,100,100,:hash,CURRENT_TIMESTAMP)"),{'op':op,'org':org,'uid':student,'hash':'0'*64})
    sub=c.execute(text("INSERT INTO education_submissions(org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status,lifecycle_revision,approved_printer_id,approved_by,compatibility_engine_version) VALUES(:org,:op,:job,:file,:model,:center,:student,'scheduled',3,:pid,:teacher,'education-compatibility-v1') RETURNING id"),{'org':org,'op':op,'job':job,'file':file,'model':model,'center':center,'student':student,'pid':pid,'teacher':teacher}).scalar_one()
    fixture['submission']=sub
  if education:
   from modules.organizations.education_policy import reserve_dispatch,confirm_dispatch_started,claim_monitor_observation
   with Session(engine) as db:
    reservation=reserve_dispatch(db,job_id=job,printer_id=pid,expected_revision=3,extension='.3mf');assert reservation
    _sensitive.extend([reservation['token'],reservation['remote_filename']])
    result=confirm_dispatch_started(db,claim_id=reservation['claim_id'],submission_id=sub,job_id=job,printer_id=pid,expected_revision=3);assert result['transitioned'] is True
    filename=reservation['remote_filename']
    pj=db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,started_at,status) VALUES(:pid,:name,:name,CURRENT_TIMESTAMP,'running') RETURNING id"),{'pid':pid,'name':filename}).scalar_one()
    result=claim_monitor_observation(db,print_job_id=pj,printer_id=pid,observed_filename=filename);assert result['authorized'] is True
    db.commit()
  else:
   filename='owned.gcode'
   with engine.begin() as c:pj=c.execute(text("INSERT INTO print_jobs(printer_id,scheduled_job_id,filename,job_name,status,started_at) VALUES(:pid,:job,:filename,:name,'running',CURRENT_TIMESTAMP) RETURNING id"),{'pid':pid,'job':job,'filename':filename,'name':name}).scalar_one()
  fixture.update(print_job=pj,filename=filename);fixtures[pj]=fixture
  return fixture
 def invoke(source,f,status):
  if source=='mqtt':return mqtt.record_job_ended(f['printer'],f['name'],f['print_job'],f['job'],status,{'subtask_name':f['name']},{},lambda **kwargs:None)
  if source=='job_event':return job_events.job_completed(f['printer'],f['print_job'],success=status=='completed',duration_seconds=120)
  monitor=moon.MoonrakerMonitor(f['printer'],f['name'],'192.0.2.1');monitor._current_job_db_id=f['print_job'];monitor._last_filename=f['filename']
  return monitor._job_ended(status,SimpleNamespace(filename=f['filename'],filament_used_mm=0,error_message=None),**({'education_terminal_status':status} if f['education'] else {}))
 counts={'successful_families':0,'failed_controls':0,'rollback_controls':0,'optional_failure_controls':0,'concurrent_duplicate':0}
 for source in ('mqtt','moonraker','job_event'):
  for education in (False,True):
   for status in ('completed','failed'):
    f=seed(education);start_timers=len(timers);start_received=len(received)
    invoke(source,f,status);invoke(source,f,status)
    with engine.connect() as c:
     assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':f['print_job']}).scalar_one()==status
     if education:assert c.execute(text('SELECT status FROM education_submissions WHERE id=:id'),{'id':f['submission']}).scalar_one()==status
    expected=1 if status=='completed' else 0
    assert len(timers)-start_timers==len(received)-start_received==expected
    counts['successful_families' if expected else 'failed_controls']+=1
   # Fault before real commit: no completion or cooldown can escape rollback.
   f=seed(education);start_timers=len(timers);start_received=len(received)
   with engine.begin() as c:c.execute(text('INSERT INTO owned_power_commit_targets(print_job_id) VALUES(:id)'),{'id':f['print_job']})
   try:invoke(source,f,'completed')
   finally:
    with engine.begin() as c:c.execute(text('DELETE FROM owned_power_commit_targets WHERE print_job_id=:id'),{'id':f['print_job']})
   with engine.connect() as c:
    assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':f['print_job']}).scalar_one()=='running'
    if education:
     assert c.execute(text('SELECT status FROM education_submissions WHERE id=:id'),{'id':f['submission']}).scalar_one()=='printing'
     assert c.execute(text('SELECT count(*) FROM education_monitor_claims WHERE job_id=:id'),{'id':f['job']}).scalar_one()==1
   assert len(timers)==start_timers and len(received)==start_received
   counts['rollback_controls']+=1
  # Optional external postcommit failure cannot suppress the committed event.
  f=seed(False);start_timers=len(timers);start_received=len(received)
  module=alert_dispatch if source=='mqtt' else event_dispatcher if source=='moonraker' else job_events
  original=module.dispatch_alert
  def fail_optional(*args,**kwargs):raise RuntimeError('Owned optional notification fault')
  module.dispatch_alert=fail_optional
  try:invoke(source,f,'completed')
  finally:module.dispatch_alert=original
  assert len(timers)-start_timers==len(received)-start_received==1
  counts['optional_failure_controls']+=1
 # Actual concurrent raw-DBAPI callbacks serialize the same generic print row.
 f=seed(False);start_timers=len(timers);start_received=len(received);barrier=threading.Barrier(2);original=job_events.prepare_print_job_write
 def prepared(*args,**kwargs):barrier.wait(timeout=5);return original(*args,**kwargs)
 job_events.prepare_print_job_write=prepared
 errors=[]
 def concurrent():
  try:invoke('job_event',f,'completed')
  except BaseException as error:errors.append(type(error).__name__)
 threads=[threading.Thread(target=concurrent) for _ in range(2)]
 for thread in threads:thread.start()
 for thread in threads:thread.join(timeout=15)
 job_events.prepare_print_job_write=original
 assert not errors and all(not thread.is_alive() for thread in threads)
 assert len(timers)-start_timers==len(received)-start_received==1
 message=received[-1]
 assert message.source_module=='notifications'
 assert message.data=={'printer_id':f['printer'],'print_job_id':f['print_job'],'scheduled_job_id':f['job'],'status':'completed','success':True,'job_name':f['name'],'duration_seconds':120}
 counts['concurrent_duplicate']=1
 assert commands==[]
 assert all(timer.delay==60 for timer in timers)
 with engine.begin() as c:
  c.execute(text('DROP TRIGGER owned_power_commit_fault ON print_jobs'));c.execute(text('DROP FUNCTION owned_power_commit_fault()'));c.execute(text('DROP TABLE owned_power_commit_targets'))
 engine.dispose();_config.cleanup()
 print('power-terminal-delivery-native-pass: '+str(counts)+'; real-bootstrap/all3-producers/generic+EDU/committed-fresh-observer/claim-retirement-before-event/concurrent-payload; inert-timers/no-power-command; generic dispatch claims absent, retirement-helper ordering only; OFF-ON races unproved')
if __name__=='__main__':
 try:main()
 except BaseException:
  sys.stderr.write(traceback.format_exc());sys.exit(1)
