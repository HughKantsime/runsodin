"""Real profile HTTP/DB authorization, import and command outcomes; no hardware."""
import json
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def profile_client(token_client,review_db,monkeypatch):
 from modules.system import profile_routes
 from modules.printers.adapters import moonraker
 client,issue=token_client;client.app.include_router(profile_routes.router)
 calls=[];outcomes=[]
 class Adapter:
  def __init__(self,host,*args,**kwargs):calls.append(('construct',host))
  def send_gcode(self,command):
   calls.append(('command',command));result=outcomes.pop(0) if outcomes else True
   if isinstance(result,Exception):raise result
   return result
 monkeypatch.setattr(moonraker,'MoonrakerPrinter',Adapter)
 review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='synthetic-own.invalid' WHERE id=9"))
 review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type,api_host,is_active) VALUES(11,'Synthetic foreign',2,'moonraker','synthetic-foreign.invalid',TRUE)"))
 for identifier,org in [(101,1),(102,2),(103,None)]:
  review_db.execute(text("INSERT INTO printer_profiles(id,created_by,org_id,name,slicer,category,file_format,raw_content,tags) VALUES(:id,3,:org,:name,'klipper','temperature','json',:content,'synthetic,tuning')"),{'id':identifier,'org':org,'name':f'Synthetic profile {identifier}','content':json.dumps({'nozzle_temp':205,'bed_temp':60})})
 review_db.commit()
 return TestClient(client.app,raise_server_exceptions=False),issue,calls,outcomes

@pytest.mark.parametrize('scope',['read:models','read:printers','write:jobs'])
@pytest.mark.parametrize('operation',['create','update','delete','import','apply'])
def test_unrelated_or_read_tokens_cannot_mutate_profiles(profile_client,review_db,scope,operation):
 client,issue,calls,_=profile_client;headers=issue([scope]);before=list(review_db.execute(text('SELECT id,name,raw_content,last_applied_at FROM printer_profiles ORDER BY id')).all())
 if operation=='create':response=client.post('/profiles',headers=headers,json={'name':'Unexpected','slicer':'orca','category':'process','raw_content':'{}'})
 elif operation=='update':response=client.put('/profiles/101',headers=headers,json={'name':'Unexpected'})
 elif operation=='delete':response=client.delete('/profiles/101',headers=headers)
 elif operation=='import':response=client.post('/profiles/import',headers=headers,files={'file':('synthetic.json','{"name":"Unexpected","type":"process"}','application/json')})
 else:response=client.post('/profiles/101/apply',headers=headers,json={'printer_id':9})
 assert response.status_code==403
 assert calls==[]
 assert list(review_db.execute(text('SELECT id,name,raw_content,last_applied_at FROM printer_profiles ORDER BY id')).all())==before

@pytest.mark.parametrize('path',['/profiles','/profiles/101','/profiles/101/export'])
def test_unrelated_token_cannot_read_raw_profiles(profile_client,path):
 client,issue,_,_=profile_client
 response=client.get(path,headers=issue(['read:jobs']))
 assert response.status_code==403

def test_owned_profile_cannot_apply_to_foreign_printer(profile_client,review_db):
 client,issue,calls,_=profile_client
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':11})
 assert response.status_code==404,response.text
 assert calls==[]
 assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None

@pytest.mark.parametrize('content,filename,format,slicer,category',[('{"name":"Synthetic PETG","type":"filament","filament_type":["PETG"]}','synthetic.json','json','orca','filament'),('[print:synthetic]\nname = Synthetic INI\nlayer_height = 0.2\n','synthetic.ini','ini','prusa','process')])
def test_import_known_content_is_owned_and_roundtrips(profile_client,review_db,content,filename,format,slicer,category):
 client,issue,_,_=profile_client
 response=client.post('/profiles/import',headers=issue(['write:models']),files={'file':(filename,content,'application/octet-stream')})
 assert response.status_code==201,response.text
 identifier=response.json()['ids'][0]
 assert tuple(review_db.execute(text('SELECT org_id,created_by,file_format,slicer,category,raw_content FROM printer_profiles WHERE id=:id'),{'id':identifier}).one())==(1,3,format,slicer,category,content)
 loaded=client.get(f'/profiles/{identifier}',headers=issue(['read:models']));assert loaded.status_code==200 and loaded.json()['raw_content']==content
 exported=client.get(f'/profiles/{identifier}/export',headers=issue(['read:models']));assert exported.status_code==200 and exported.text==content
 headers=issue(['read:models']);review_db.execute(text('UPDATE api_tokens SET user_id=5 WHERE id=(SELECT MAX(id) FROM api_tokens)'));review_db.commit()
 assert client.get(f'/profiles/{identifier}',headers=headers).status_code==404

@pytest.mark.parametrize('filename,content,expected',[('broken.json','{',400),('array.json','[]',400),('blank.ini','name = Synthetic sectionless\nlayer_height = 0.2\n',201),('empty-material.json','{"name":"Synthetic empty","filament_type":[]}',201)])
def test_import_parse_failure_is_controlled_or_supported_without_global_partial_rows(profile_client,review_db,filename,content,expected):
 client,issue,_,_=profile_client;before=review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()
 response=client.post('/profiles/import',headers=issue(['write:models']),files={'file':(filename,content,'application/octet-stream')})
 assert response.status_code==expected,response.text
 if response.status_code==201:
  assert all(review_db.execute(text('SELECT org_id FROM printer_profiles WHERE id=:id'),{'id':i}).scalar_one()==1 for i in response.json()['ids'])
 else:assert review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()==before

@pytest.mark.parametrize('outcome',[False,True])
def test_apply_exact_commands_and_truthful_success_metadata(profile_client,review_db,outcome):
 client,issue,calls,outcomes=profile_client;outcomes.extend([outcome,outcome])
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
 assert response.status_code in (200,502),response.text
 assert calls==[('construct','synthetic-own.invalid'),('command','SET_HEATER_TEMPERATURE HEATER=extruder TARGET=205'),('command','SET_HEATER_TEMPERATURE HEATER=heater_bed TARGET=60')]
 assert response.json().get('applied') is outcome
 row=review_db.execute(text('SELECT last_applied_at,last_applied_printer_id FROM printer_profiles WHERE id=101')).one()
 if outcome:assert row[0] is not None and row[1]==9
 else:assert tuple(row)==(None,None)

@pytest.mark.parametrize('operation',['create','update'])
@pytest.mark.parametrize('printer_id',[11,999999])
def test_profile_printer_reference_is_authorized_before_writes(profile_client,review_db,operation,printer_id):
 client,issue,calls,_=profile_client
 before=list(review_db.execute(text('SELECT id,name,printer_id FROM printer_profiles ORDER BY id')).all())
 headers=issue(['write:models'])
 if operation=='create':response=client.post('/profiles',headers=headers,json={'name':'Synthetic reference','slicer':'orca','category':'process','raw_content':'{}','printer_id':printer_id})
 else:response=client.put('/profiles/101',headers=headers,json={'printer_id':printer_id})
 assert response.status_code==404,response.text
 assert calls==[] and list(review_db.execute(text('SELECT id,name,printer_id FROM printer_profiles ORDER BY id')).all())==before

@pytest.mark.parametrize('scopes',[['write:printers'],['read:models'],['write:models']])
def test_apply_requires_both_profile_read_and_printer_write(profile_client,scopes):
 client,issue,calls,_=profile_client
 response=client.post('/profiles/101/apply',headers=issue(scopes),json={'printer_id':9})
 assert response.status_code==403 and calls==[]

@pytest.mark.parametrize('scopes',[['read:models','write:printers'],['read','write'],['admin']])
def test_authorized_apply_compatibility(profile_client,scopes):
 client,issue,calls,_=profile_client
 response=client.post('/profiles/101/apply',headers=issue(scopes),json={'printer_id':9})
 assert response.status_code==200 and response.json()['applied'] is True
 assert len(calls)==3

@pytest.mark.parametrize('foreign_shared',[False,True])
def test_shared_foreign_printer_visibility_never_grants_profile_command(profile_client,review_db,foreign_shared):
 client,issue,calls,_=profile_client
 review_db.execute(text('UPDATE printers SET shared=:shared WHERE id=11'),{'shared':foreign_shared});review_db.commit()
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':11})
 assert response.status_code==404 and calls==[]


def test_partial_apply_reports_failure_and_preserves_previous_success_metadata(profile_client,review_db):
 client,issue,calls,outcomes=profile_client
 review_db.execute(text("UPDATE printer_profiles SET last_applied_at='2026-01-01 12:00:00',last_applied_printer_id=10 WHERE id=101"));review_db.commit()
 outcomes.extend([True,False])
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
 assert response.status_code==200 and response.json()['applied'] is False
 assert response.json()['outcome']=='partial' and response.json()['commands_sent']==1 and response.json()['commands_failed']==1
 assert tuple(review_db.execute(text('SELECT last_applied_at,last_applied_printer_id FROM printer_profiles WHERE id=101')).one())==('2026-01-01 12:00:00',10)


def test_multi_profile_database_failure_rolls_back_all_rows_and_audits(profile_client,review_db,monkeypatch):
 import io,zipfile
 from sqlalchemy.orm import Session
 client,issue,_,_=profile_client;payload=io.BytesIO()
 with zipfile.ZipFile(payload,'w') as archive:
  archive.writestr('one.json','{"name":"Synthetic first"}')
  archive.writestr('two.json','{"name":"Synthetic second"}')
 before=review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()
 audits=review_db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()
 execute=Session.execute;inserts=[]
 def interrupted(self,statement,*args,**kwargs):
  if 'INSERT INTO printer_profiles' in str(statement):
   inserts.append(True)
   if len(inserts)==2:raise RuntimeError('Synthetic insert refusal')
  return execute(self,statement,*args,**kwargs)
 monkeypatch.setattr(Session,'execute',interrupted)
 response=client.post('/profiles/import',headers=issue(['write:models']),files={'file':('synthetic.3mf',payload.getvalue(),'application/octet-stream')})
 assert response.status_code==500
 assert len(inserts)==2
 assert review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()==before
 assert review_db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()==audits


def test_profile_zip_expansion_rejected_before_any_database_writes(profile_client,review_db):
 import io,zipfile
 client,issue,_,_=profile_client;payload=io.BytesIO()
 with zipfile.ZipFile(payload,'w',compression=zipfile.ZIP_DEFLATED) as archive:
  archive.writestr('large.json',b' '*(50*1024*1024)+b'{}')
 before=review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()
 response=client.post('/profiles/import',headers=issue(['write:models']),files={'file':('synthetic.3mf',payload.getvalue(),'application/octet-stream')})
 assert response.status_code==413,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()==before


@pytest.mark.parametrize('category,payload',[('temperature',[]),('temperature',{'nozzle_temp':'invalid'}),('temperature',{'nozzle_temp':True}),('temperature',{'nozzle_temp':-1}),('macro_set',{'macros':[{'gcode':9}]}),('custom',{'gcode':[]}),('temperature',{}),('macro_set',{'macros':['  ']})])
def test_invalid_or_empty_commands_rejected_before_adapter_construction(profile_client,review_db,category,payload):
 client,issue,calls,_=profile_client
 review_db.execute(text('UPDATE printer_profiles SET category=:category,raw_content=:content WHERE id=101'),{'category':category,'content':json.dumps(payload)});review_db.commit()
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
 assert response.status_code==400,response.text
 assert calls==[]
 assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None


def test_profile_blank_commands_not_counted_as_success(profile_client,review_db):
 client,issue,calls,_=profile_client
 review_db.execute(text('UPDATE printer_profiles SET category=\'macro_set\',raw_content=:content WHERE id=101'),{'content':json.dumps({'macros':['  ','M400']})});review_db.commit()
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
 assert response.status_code==200,response.text
 assert response.json()['commands_sent']==1
 assert calls==[('construct','synthetic-own.invalid'),('command','M400')]

@pytest.mark.parametrize('results,expected',[( [True,RuntimeError('Synthetic private command detail')], 'partial'),([RuntimeError('Synthetic private command detail'),RuntimeError('Synthetic private command detail')],'failed')])
def test_sender_exceptions_report_unconfirmed_counts_without_success_anchor_or_private_log(profile_client,review_db,caplog,results,expected):
 client,issue,calls,outcomes=profile_client;outcomes.extend(results)
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
 assert response.status_code==200 and response.json()['applied'] is False and response.json()['outcome']==expected
 assert response.json()['commands_sent']==(1 if expected=='partial' else 0)
 assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None
 assert 'Synthetic private command detail' not in caplog.text and 'SET_HEATER' not in caplog.text
 assert 'RuntimeError' in caplog.text

@pytest.mark.parametrize('category,payload', [('temperature',{'nozzle_temp':205,'bed_temp':'invalid'}),('macro_set',{'macros':['M400',42]})])
def test_mixed_valid_invalid_profile_sends_nothing(profile_client,review_db,category,payload):
 client,issue,calls,_=profile_client
 review_db.execute(text('UPDATE printer_profiles SET category=:category,raw_content=:content WHERE id=101'),{'category':category,'content':json.dumps(payload)});review_db.commit()
 response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
 assert response.status_code==400 and calls==[]
