"""Actual PostgreSQL direct Education graph service; HTTP auth is separate evidence."""
import hashlib,io,json,os,secrets,tempfile,zipfile
from pathlib import Path
from uuid import UUID,uuid4


def payload(zero=False):
    # Same owned sliced3MF evidence as licensed host HTTP fixture, no hardware.
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('Metadata/slice_info.config',f'<config><plate><metadata key="prediction" value="{0 if zero else 3600}"/><metadata key="weight" value="{0 if zero else 12.5}"/><metadata key="nozzle_diameters" value="0.4"/><metadata key="printer_model_id" value="BL-P001"/><filament id="1" type="PLA" color="#FFFFFF" used_m="4" used_g="{0 if zero else 12.5}"/></plate></config>')
        z.writestr('Metadata/project_settings.config',json.dumps({'printer_model':'Bambu Lab P1S','nozzle_diameter':['0.4'],'filament_type':['PLA'],'bed_shape':'0x0,256x0,256x256,0x256'}))
        z.writestr('Metadata/plate_1.json',json.dumps({'nozzle_diameter':0.4}))
        z.writestr('Metadata/plate_1.gcode','; sliced\nG28\n')
        z.writestr('3D/3dmodel.model','<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><metadata name="Title">Class Gear</metadata><resources/><build/></model>')
    return stream.getvalue()


def main():
 with tempfile.TemporaryDirectory(prefix='education-job-native-owned-') as folder:
  root=Path(folder);root.chmod(0o700);credential=root/'credential';credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
  os.environ.update(DATABASE_URL=os.environ['EDUCATION_JOB_NATIVE_URL'],DATABASE_PASSWORD_FILE=str(credential),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
  from sqlalchemy import text,inspect
  from sqlalchemy.orm import Session
  from core.db import engine
  from core.schema import bootstrap_database
  from core.errors import OdinError
  from modules.organizations import education_submission_service as service
  bootstrap_database(engine)
  with engine.begin() as c:
   c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned school',true),(2,'Owned foreign',true)"))
   c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'student','fixture','viewer',true,1),(2,'manager','fixture','viewer',true,1),(3,'admin','fixture','admin',true,1)"))
   c.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) VALUES(7,1,'engineering','eng','Engineering','ENG','active',1,3)"))
   c.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,7,1,'student','active',3),(1,7,2,'manager','active',3)"))
   c.execute(text("INSERT INTO education_storage_accounts(org_id,scope_kind,scope_id,user_id,reserved_bytes,accounted_bytes) VALUES(1,'user',1,1,0,0),(1,'tenant',1,NULL,0,0)"))
  principal={'id':1,'username':'student','role':'viewer','group_id':1,'is_active':True,'_auth_kind':'bearer_jwt'}
  def prepare(zero=False):
   operation=str(uuid4());path=root/(operation+'.3mf');data=payload(zero);path.write_bytes(data)
   metadata=service.parse_3mf(str(path));digest=hashlib.sha256(data).hexdigest()
   with engine.begin() as c:
    c.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,expected_bytes,expected_hash,final_path) VALUES(:o,1,1,'renamed',:b,:b,:d,:p)"),{'o':operation,'b':len(data),'d':digest,'p':str(path)})
    c.execute(text("UPDATE education_storage_accounts SET reserved_bytes=reserved_bytes+:b WHERE org_id=1"),{'b':len(data)})
   return dict(operation_id=operation,principal=dict(principal),cost_center_id=7,original_filename='owned.3mf',final_path=path,byte_count=len(data),digest=digest,metadata=metadata,compatibility_meta=service._compatibility_meta_from_parsed(metadata))
  tables=('jobs','models','print_files','education_submissions','cost_identities','cost_job_bindings','cost_order_memberships','education_audit_events','education_notification_outbox')
  def state(c,operation):
   return ({t:c.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in tables},tuple(c.execute(text('SELECT state,reserved_bytes,accounted_bytes,reservation_released_at FROM education_upload_operations WHERE operation_id=:o'),{'o':operation}).one()),[tuple(r) for r in c.execute(text("SELECT scope_kind,reserved_bytes,accounted_bytes,revision FROM education_storage_accounts WHERE org_id=1 ORDER BY scope_kind"))])
  def assert_identity(c,result):
   row=c.execute(text('SELECT economic_uid,charged_to_org_id,charged_to_user_id,submitted_by,order_item_id FROM jobs WHERE id=:id'),{'id':result['job_id']}).one()
   assert str(UUID(row[0]))==row[0] and row[1:]==(1,1,1,None)
   assert tuple(c.execute(text('SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:u'),{'u':row[0]}).one())==('job',result['job_id'],'org',1)
   assert c.execute(text('SELECT count(*) FROM cost_job_bindings WHERE job_uid=:u'),{'u':row[0]}).scalar_one()==0
   assert c.execute(text('SELECT count(*) FROM cost_order_memberships WHERE child_uid=:u'),{'u':row[0]}).scalar_one()==0
   return row[0]
  def oracle(db,result):
   actual=db.execute(text('SELECT * FROM jobs WHERE id=:id'),{'id':result['job_id']}).mappings().one()
   with db.begin_nested() as nested:
    raw=db.execute(text("INSERT INTO jobs(model_id,item_name,status,priority,quantity,submitted_by,charged_to_user_id,charged_to_org_id,hold,is_locked,print_file_id,payload_sha256,plate_index) VALUES(:m,:name,'submitted',3,1,1,1,1,false,false,:f,:hash,0) RETURNING id"),{'m':actual['model_id'],'name':actual['item_name'],'f':actual['print_file_id'],'hash':actual['payload_sha256']}).scalar_one()
    expected=db.execute(text('SELECT * FROM jobs WHERE id=:id'),{'id':raw}).mappings().one()
    # All source columns, default NULL/zero/JSON semantics and DB timestamps.
    assert {k:v for k,v in actual.items() if k not in ('id','economic_uid')}=={k:v for k,v in expected.items() if k not in ('id','economic_uid')}
    assert tuple(db.execute(text('SELECT required_tags IS NULL,required_tags::text FROM jobs WHERE id=:id'),{'id':result['job_id']}).one())==tuple(db.execute(text('SELECT required_tags IS NULL,required_tags::text FROM jobs WHERE id=:id'),{'id':raw}).one())
    assert expected['economic_uid'] is None and actual['economic_uid'] is not None
    nested.rollback()
  for zero in (False,True):
   kwargs=prepare(zero)
   with Session(engine) as db:
    result=service._commit_submission_graph(db,**kwargs);uid=assert_identity(db,result);oracle(db,result);db.commit()
   with Session(engine) as fresh:
    assert assert_identity(fresh,result)==uid
    assert fresh.execute(text('SELECT state FROM education_upload_operations WHERE operation_id=:o'),{'o':kwargs['operation_id']}).scalar_one()=='committed'
  print('education-job-native-directservice-exact-org-standalone-crossSession-raw-oracle-zero-pass')
  # Real database default expressions, not Python surrogate values.
  kwargs=prepare()
  with Session(engine) as db:
   db.execute(text("ALTER TABLE jobs ALTER COLUMN quantity_on_bed SET DEFAULT 0"));db.execute(text("ALTER TABLE jobs ALTER COLUMN is_favorite SET DEFAULT false"));db.execute(text("ALTER TABLE jobs ALTER COLUMN required_tags SET DEFAULT '[\"owned\"]'"));db.execute(text("ALTER TABLE jobs ALTER COLUMN target_type SET DEFAULT 'model'"))
   result=service._commit_submission_graph(db,**kwargs);assert_identity(db,result);oracle(db,result);db.rollback()
  print('education-job-native-physical-default-expression-oracle-pass')
  for fault in ('allocation','outbox'):
   kwargs=prepare()
   with engine.connect() as c:before=state(c,kwargs['operation_id'])
   original_creator=service._add_created;original_execute=Session.execute;observed=[]
   def allocate(db,*a,**kw):
    result=original_creator(db,*a,**kw);observed.append(db.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':result.id}).scalar_one());raise RuntimeError('Owned postallocation failure')
   def execute(db,statement,*a,**kw):
    result=original_execute(db,statement,*a,**kw)
    if 'INSERT INTO education_notification_outbox' in str(statement):
     observed.append(original_execute(db,text('SELECT economic_uid FROM jobs WHERE id=(SELECT job_id FROM education_submissions WHERE operation_id=:o)'),{'o':kwargs['operation_id']}).scalar_one());raise RuntimeError('Owned postoutbox failure')
    return result
   if fault=='allocation':service._add_created=allocate
   else:Session.execute=execute
   try:
    with Session(engine) as db:
     try:service._commit_submission_graph(db,**kwargs)
     except RuntimeError:db.rollback()
     else:raise AssertionError('Owned fault did not fire')
   finally:service._add_created=original_creator;Session.execute=original_execute
   assert observed and observed[0] is not None
   with engine.connect() as c:assert state(c,kwargs['operation_id'])==before
  print('education-job-native-postallocation-postoutbox-full-caller-rollback-pass')
  kwargs=prepare()
  with engine.begin() as c:
   before=state(c,kwargs['operation_id'])
   c.exec_driver_sql("CREATE FUNCTION owned_education_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Owned deferred graph commit failure' USING ERRCODE='23514'; END $$")
   c.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_education_commit_failure AFTER INSERT ON education_submissions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_education_commit_failure()')
  with Session(engine) as db:
   result=service._commit_submission_graph(db,**kwargs);assert_identity(db,result)
   try:db.commit()
   except Exception as e:assert e.orig.sqlstate=='23514';db.rollback()
   else:raise AssertionError('Actual deferred COMMIT fault did not fire')
  with engine.begin() as c:
   assert state(c,kwargs['operation_id'])==before
   c.exec_driver_sql('DROP TRIGGER owned_education_commit_failure ON education_submissions');c.exec_driver_sql('DROP FUNCTION owned_education_commit_failure()')
  print('education-job-native-actual-deferred-COMMIT-full-caller-rollback-pass')
  for drift in ('principal_org','user','center','grant','operation'):
   kwargs=prepare()
   with engine.connect() as c:before=state(c,kwargs['operation_id'])
   with Session(engine) as db:
    if drift=='principal_org':kwargs['principal']['group_id']=2
    elif drift=='user':db.execute(text('UPDATE users SET is_active=false WHERE id=1'))
    elif drift=='center':db.execute(text("UPDATE education_cost_centers SET state='archived' WHERE id=7"))
    elif drift=='grant':db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1 AND role='student'"))
    else:db.execute(text('UPDATE education_upload_operations SET user_id=2 WHERE operation_id=:o'),{'o':kwargs['operation_id']})
    try:service._commit_submission_graph(db,**kwargs)
    except OdinError as e:assert e.status==404;db.rollback()
    else:raise AssertionError('Persisted authority drift accepted')
   with engine.connect() as c:assert state(c,kwargs['operation_id'])==before
  for table,where,params in [('users','id=1',{}),('education_cost_centers','id=7',{}),('education_cost_center_grants',"user_id=1 AND role='student'",{}),('education_upload_operations','operation_id=:o',None)]:
   kwargs=prepare();params=params if params is not None else {'o':kwargs['operation_id']}
   with engine.connect() as first,Session(engine) as second:
    before=state(first,kwargs['operation_id']);first.execute(text('SELECT 1 FROM '+table+' WHERE '+where+' FOR UPDATE'),params)
    try:service._commit_submission_graph(second,**kwargs)
    except Exception as e:assert e.orig.sqlstate=='55P03';second.rollback()
    else:raise AssertionError('Current authority row contention ignored')
    first.rollback()
    assert state(first,kwargs['operation_id'])==before
  print('education-job-native-current-persisted-authority-scope-row-contention-pass')
  engine.dispose()
 print('education-job-native-directservice-pass')
if __name__=='__main__':main()
