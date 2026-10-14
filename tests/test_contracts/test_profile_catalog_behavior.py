"""Advertised configuration formats, filters and version/rollback behavior."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_profile_library_behavior import profile_client,token_client,review_db


def test_klipper_cfg_import_is_owned_and_roundtrips_native_configuration(profile_client,review_db):
 client,issue,calls,_=profile_client
 content='[printer]\nkinematics: cartesian\nmax_velocity: 180\nmax_accel: 2500\n'
 imported=client.post('/profiles/import',headers=issue(['write:models']),files={'file':('Synthetic printer.cfg',content,'text/plain')})
 assert imported.status_code==201,imported.text
 identifier=imported.json()['ids'][0]
 row=review_db.execute(text('SELECT org_id,created_by,slicer,file_format,raw_content FROM printer_profiles WHERE id=:id'),{'id':identifier}).one()
 assert tuple(row)==(1,3,'klipper','cfg',content)
 exported=client.get(f'/profiles/{identifier}/export',headers=issue(['read:models']))
 assert exported.status_code==200 and exported.text==content
 assert '.cfg' in exported.headers['content-disposition']
 assert calls==[]


def test_profile_content_revisions_and_rollback_restore_exact_export(profile_client):
 client,issue,calls,_=profile_client;headers=issue(['read:models','write:models'])
 original=client.get('/profiles/101/export',headers=headers)
 assert original.status_code==200
 revision=client.post('/profiles/101/revisions',headers=headers,json={'raw_content':'{"nozzle_temp":210,"bed_temp":65}','changelog':'Synthetic tuning revision'})
 assert revision.status_code==200,revision.text
 number=revision.json()['revision_number']
 assert client.get('/profiles/101/export',headers=headers).text=='{"nozzle_temp":210,"bed_temp":65}'
 history=client.get('/profiles/101/revisions',headers=headers)
 assert history.status_code==200,history.text
 rows=history.json()
 assert any(row['revision_number']==number and row['changelog']=='Synthetic tuning revision' for row in rows)
 initial=min(row['revision_number'] for row in rows)
 reverted=client.post(f'/profiles/101/revert/{initial}',headers=headers)
 assert reverted.status_code==200,reverted.text
 assert client.get('/profiles/101/export',headers=headers).text==original.text
 assert calls==[]


@pytest.mark.parametrize('query',[
 'slicer=orca', 'category=process', 'filament_type=PETG', 'printer_id=9',
 'search=synthetic-special-tag', 'slicer=orca&category=process&filament_type=PETG&printer_id=9&search=synthetic-special-tag',
])
def test_profile_filters_return_matching_authorized_metadata(profile_client,review_db,query):
 client,issue,_,_=profile_client
 for identifier,org in [(104,1),(105,2)]:
  review_db.execute(text("INSERT INTO printer_profiles(id,created_by,org_id,name,slicer,category,file_format,raw_content,tags,filament_type,printer_id) VALUES(:id,3,:org,:name,'orca','process','json','{}','synthetic-special-tag','PETG',9)"),{'id':identifier,'org':org,'name':f'Synthetic match {identifier}'})
 review_db.commit()
 response=client.get('/profiles?'+query,headers=issue(['read:models']))
 assert response.status_code==200,response.text
 assert response.json()['total']==1
 assert [row['id'] for row in response.json()['profiles']]==[104]
 assert 'Synthetic match 105' not in response.text

@pytest.mark.parametrize('content',[b'\xff',b'no configuration section',b'[printer]\nkinematics: cartesian\x00',b'[printer]\na: 1\na: 2'])
def test_invalid_cfg_does_not_create_profile_or_contact_printer(profile_client,review_db,content):
 client,issue,calls,_=profile_client
 before=review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()
 response=client.post('/profiles/import',headers=issue(['write:models']),files={'file':('invalid.cfg',content,'text/plain')})
 assert response.status_code==400,response.text
 assert review_db.execute(text('SELECT COUNT(*) FROM printer_profiles')).scalar_one()==before
 assert calls==[]


def test_cfg_macros_and_includes_are_preserved_as_text_without_execution(profile_client):
 client,issue,calls,_=profile_client;headers=issue(['read:models','write:models','write:printers'])
 content='[include missing-local-file.cfg]\r\n[gcode_macro SYNTHETIC]\r\ngcode:\r\n    {% set value = 1 %}\r\n    M117 synthetic\r\n'
 response=client.post('/profiles/import',headers=headers,files={'file':('native.CFG',content,'text/plain')})
 assert response.status_code==201,response.text
 identifier=response.json()['ids'][0]
 exported=client.get(f'/profiles/{identifier}/export',headers=headers)
 assert exported.content==content.encode('utf-8')
 denied=client.post(f'/profiles/{identifier}/apply',headers=headers,json={'printer_id':9})
 assert denied.status_code==400 and 'import and export only' in denied.text
 assert calls==[]
