"""Actual model HTTP/database lifecycles with opaque authenticated tokens."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def library_client(token_client,review_db,monkeypatch):
 from modules.models_library.routes import models_crud
 client,issue=token_client;client.app.include_router(models_crud.router)
 for identifier,org,name in [(101,1,'Synthetic own'),(102,2,'Synthetic foreign'),(103,None,'Synthetic global')]:
  review_db.execute(text('INSERT INTO models(id,name,org_id,build_time_hours,default_filament_type) VALUES(:id,:name,:org,2,\'PLA\')'),{'id':identifier,'name':name,'org':org})
  review_db.execute(text('INSERT INTO model_revisions(model_id,revision_number,changelog,uploaded_by) VALUES(:id,1,:note,3)'),{'id':identifier,'note':f'Synthetic private revision {identifier}'})
 review_db.commit()
 return client,issue

def test_model_create_read_color_edit_and_favorite_persist(library_client,review_db):
 client,issue=library_client;headers=issue(['write:models']);read=issue(['read:models'])
 request={'name':'Synthetic two-color','build_time_hours':2,'default_filament_type':'PETG','color_requirements':{'color1':{'color':'#112233','grams':15},'color2':{'color':'#AABBCC','grams':5}},'units_per_bed':2,'quantity_per_bed':3,'category':'Synthetic','notes':'Initial','markup_percent':0}
 response=client.post('/models',json=request,headers=headers);assert response.status_code==201,response.text
 identifier=response.json()['id']
 assert review_db.execute(text('SELECT org_id FROM models WHERE id=:id'),{'id':identifier}).scalar_one()==1
 loaded=client.get(f'/models/{identifier}',headers=read).json()
 assert loaded['total_filament_grams']==20 and loaded['time_per_item']==1 and loaded['filament_per_item']==10
 changed=client.patch(f'/models/{identifier}',headers=headers,json={'color_requirements':{'color1':{'color':'#445566','grams':8}},'is_favorite':True,'notes':'Edited'})
 assert changed.status_code==200,changed.text
 loaded=client.get(f'/models/{identifier}',headers=read).json()
 assert loaded['total_filament_grams']==8 and loaded['is_favorite'] is True and loaded['notes']=='Edited'
 assert client.patch(f'/models/{identifier}',headers=headers,json={'is_favorite':False}).status_code==200
 assert client.get(f'/models/{identifier}',headers=read).json()['is_favorite'] is False

@pytest.mark.parametrize('method,path',[('get','/models/102/revisions'),('post','/models/102/revisions'),('post','/models/102/revisions/1/revert')])
def test_foreign_model_revision_opaque_denial_without_revision_or_audit(library_client,review_db,method,path):
 client,issue=library_client;before=review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()
 audits=review_db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()
 response=getattr(client,method)(path,headers=issue(['read:models'] if method=='get' else ['write:models']))
 assert response.status_code==404,response.text
 assert 'Synthetic private' not in response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()==before
 assert review_db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()==audits

@pytest.mark.parametrize('identifier',[101,103])
def test_accessible_revision_list_create_revert_records_uploader_and_order(library_client,review_db,identifier):
 client,issue=library_client;read=issue(['read:models']);write=issue(['write:models'])
 before=client.get(f'/models/{identifier}/revisions',headers=read)
 assert before.status_code==200 and before.json()[0]['revision_number']==1
 response=client.post(f'/models/{identifier}/revisions?changelog=Synthetic%20second',headers=write)
 assert response.status_code==200 and response.json()['revision_number']==2
 response=client.post(f'/models/{identifier}/revisions/1/revert',headers=write)
 assert response.status_code==200 and response.json()=={'revision_number':3,'reverted_from':1}
 rows=client.get(f'/models/{identifier}/revisions',headers=read).json()
 assert [r['revision_number'] for r in rows]==[3,2,1]
 assert rows[1]['changelog']=='Synthetic second' and rows[1]['uploaded_by']==3 and rows[1]['uploaded_by_name']=='admin'
 assert all(r['created_at'] for r in rows)

@pytest.mark.parametrize('scope',['read:models','read:printers','write:jobs'])
@pytest.mark.parametrize('operation',['create_revision','revert','favorite'])
def test_unrelated_or_read_tokens_cannot_mutate_models(library_client,review_db,scope,operation):
 client,issue=library_client;headers=issue([scope]);before=review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()
 if operation=='create_revision':response=client.post('/models/101/revisions',headers=headers)
 elif operation=='revert':response=client.post('/models/101/revisions/1/revert',headers=headers)
 else:response=client.patch('/models/101',headers=headers,json={'is_favorite':True})
 assert response.status_code==403
 assert review_db.execute(text('SELECT COUNT(*) FROM model_revisions')).scalar_one()==before
 assert not review_db.execute(text('SELECT is_favorite FROM models WHERE id=101')).scalar_one()

@pytest.mark.parametrize('path',['/models/999999/revisions','/models/999999/revisions/1/revert'])
def test_missing_revision_parent_is_opaque(library_client,path):
 client,issue=library_client
 response=client.get(path,headers=issue(['read:models'])) if path.endswith('/revisions') else client.post(path,headers=issue(['write:models']))
 assert response.status_code==404


def test_foreign_revision_file_denied_before_any_filesystem_access(library_client,monkeypatch):
 from modules.models_library.routes import models_crud
 client,issue=library_client
 def forbidden(*args,**kwargs):pytest.fail('Foreign revision reached filesystem')
 monkeypatch.setattr(models_crud,'open',forbidden,raising=False)
 original=models_crud.os.makedirs
 monkeypatch.setattr(models_crud.os,'makedirs',lambda path,*args,**kwargs:forbidden() if str(path).startswith('/data/model_revisions') else original(path,*args,**kwargs))
 response=client.post('/models/102/revisions',headers=issue(['write:models']),files={'file':('synthetic.3mf',b'Synthetic no filesystem read','application/octet-stream')})
 assert response.status_code==404


def test_superadmin_can_read_and_revise_other_organization(library_client,review_db):
 client,issue=library_client
 review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'));review_db.commit()
 assert client.get('/models/102/revisions',headers=issue(['read:models'])).status_code==200
 response=client.post('/models/102/revisions',headers=issue(['write:models']))
 assert response.status_code==200 and response.json()['revision_number']==2


def test_color_patch_preserves_explicit_zero_and_unrelated_fields(library_client,review_db):
 client,issue=library_client;headers=issue(['write:models'])
 response=client.patch('/models/101',headers=headers,json={'color_requirements':{'color1':{'color':'#112233','grams':0,'filament_type':'PETG'}},'is_favorite':True})
 assert response.status_code==200,response.text
 result=client.get('/models/101',headers=issue(['read:models'])).json()
 assert result['color_requirements']=={'color1':{'color':'#112233','grams':0,'filament_type':'PETG'}}
 assert result['total_filament_grams']==0 and result['build_time_hours']==2 and result['name']=='Synthetic own'
