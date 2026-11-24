"""Persisted content history, current authority, exact diff and failure atomicity."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_profile_library_behavior import profile_client, token_client, review_db


def _history(db):
 return list(db.execute(text('SELECT profile_id,revision_number,raw_content,file_format,slicer,category,created_by FROM printer_profile_revisions ORDER BY profile_id,revision_number')).all())


def test_content_put_records_original_and_new_content_with_metadata_once(profile_client,review_db):
 client,issue,calls,_=profile_client;headers=issue(['read:models','write:models'])
 original=client.get('/profiles/101/export',headers=headers).content
 changed='{\r\n "nozzle_temp": 212\r\n}\r\n'
 response=client.put('/profiles/101',headers=headers,json={'raw_content':changed,'name':'Synthetic changed','changelog':'Exact content'})
 assert response.status_code==200,response.text
 rows=_history(review_db);assert [r.revision_number for r in rows]==[1,2]
 assert rows[0].raw_content.encode()==original and rows[1].raw_content==changed
 assert rows[1].created_by==3
 assert client.get('/profiles/101/export',headers=headers).content==changed.encode()
 assert client.get('/profiles/101',headers=headers).json()['name']=='Synthetic changed'
 timestamps=review_db.execute(text('SELECT created_at FROM printer_profile_revisions')).scalars().all()
 assert all(value and value!='CURRENT_TIMESTAMP' for value in timestamps)
 assert calls==[]


@pytest.mark.parametrize('method,path,body',[
 ('get','/profiles/102/revisions',None),('get','/profiles/102/revisions/1',None),
 ('get','/profiles/102/diff?from_revision=1&to_revision=2',None),
 ('post','/profiles/102/revisions',{'raw_content':'foreign'}),('post','/profiles/102/revert/1',None),
])
def test_foreign_profile_history_is_opaque_and_unchanged(profile_client,review_db,method,path,body):
 client,issue,calls,_=profile_client;before=_history(review_db)
 response=client.request(method,path,headers=issue(['read:models','write:models']),json=body)
 assert response.status_code==404,response.text
 assert _history(review_db)==before and calls==[]


@pytest.mark.parametrize('path',['/profiles/101/revisions','/profiles/101/revert/1'])
def test_read_only_token_cannot_create_history(profile_client,review_db,path):
 client,issue,_,_=profile_client
 response=client.post(path,headers=issue(['read:models']),json={'raw_content':'unexpected'})
 assert response.status_code==403 and _history(review_db)==[]


def test_operator_cannot_mutate_same_org_other_creator(profile_client,review_db):
 client,issue,_,_=profile_client
 review_db.execute(text('UPDATE printer_profiles SET created_by=2 WHERE id=101'));review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"));review_db.commit()
 response=client.post('/profiles/101/revisions',headers=issue(['write:models']),json={'raw_content':'unexpected'})
 assert response.status_code==403 and _history(review_db)==[]


def test_admin_can_edit_same_org_other_creator(profile_client,review_db):
 client,issue,_,_=profile_client
 review_db.execute(text('UPDATE printer_profiles SET created_by=2 WHERE id=101'));review_db.execute(text("UPDATE users SET role='admin' WHERE id=3"));review_db.commit()
 response=client.post('/profiles/101/revisions',headers=issue(['write:models']),json={'raw_content':'synthetic authorized'})
 assert response.status_code==200,response.text
 assert [row.created_by for row in _history(review_db)]==[2,3]


def test_revision_diff_and_revert_keep_prior_history_and_restore_current_export(profile_client,review_db):
 client,issue,calls,_=profile_client;headers=issue(['read:models','write:models'])
 before=client.get('/profiles/101/export',headers=headers).text
 assert client.post('/profiles/101/revisions',headers=headers,json={'raw_content':'synthetic new\n'}).status_code==200
 baseline=_history(review_db)
 diff=client.get('/profiles/101/diff?from_revision=1&to_revision=2',headers=headers)
 assert diff.status_code==200,diff.text
 assert '-'+before in diff.json()['diff'] and '+synthetic new\n' in diff.json()['diff']
 response=client.post('/profiles/101/revert/1',headers=headers)
 assert response.status_code==200 and response.json()['revision_number']==3
 assert _history(review_db)[:2]==baseline
 assert client.get('/profiles/101/export',headers=headers).text==before
 assert calls==[]


def test_missing_revert_never_snapshots_or_changes_content(profile_client,review_db):
 client,issue,_,_=profile_client;before=review_db.execute(text('SELECT raw_content FROM printer_profiles WHERE id=101')).scalar_one()
 response=client.post('/profiles/101/revert/999',headers=issue(['write:models']))
 assert response.status_code==404 and _history(review_db)==[]
 assert review_db.execute(text('SELECT raw_content FROM printer_profiles WHERE id=101')).scalar_one()==before


@pytest.mark.parametrize('operation',['revision','revert'])
def test_actual_audit_failure_rolls_back_content_and_history(profile_client,review_db,monkeypatch,operation):
 from core.models import AuditLog
 from modules.system import profile_history
 client,issue,_,_=profile_client;headers=issue(['read:models','write:models'])
 if operation=='revert':assert client.post('/profiles/101/revisions',headers=headers,json={'raw_content':'synthetic baseline'}).status_code==200
 before=_history(review_db);content=review_db.execute(text('SELECT raw_content FROM printer_profiles WHERE id=101')).scalar_one()
 def invalid_audit(db,*args,**kwargs):db.add(AuditLog(action=None,entity_type='profile',entity_id=101))
 monkeypatch.setattr(profile_history,'log_audit',invalid_audit)
 response=client.post('/profiles/101/revisions',headers=headers,json={'raw_content':'must rollback'}) if operation=='revision' else client.post('/profiles/101/revert/1',headers=headers)
 assert response.status_code==500,response.text
 assert 'NOT NULL' not in response.text and 'INSERT INTO' not in response.text
 assert _history(review_db)==before
 assert review_db.execute(text('SELECT raw_content FROM printer_profiles WHERE id=101')).scalar_one()==content


def test_authorized_delete_cascades_history_and_removed_parent_denies_access(profile_client,review_db):
 client,issue,_,_=profile_client;headers=issue(['read:models','write:models'])
 from sqlalchemy import event
 @event.listens_for(review_db.get_bind(),'connect')
 def enforce(dbapi,record):dbapi.execute('PRAGMA foreign_keys=ON')
 review_db.get_bind().dispose()
 assert client.post('/profiles/101/revisions',headers=headers,json={'raw_content':'synthetic'}).status_code==200
 assert len(_history(review_db))==2
 assert client.delete('/profiles/101',headers=headers).status_code==204
 assert _history(review_db)==[]
 assert client.get('/profiles/101/revisions',headers=headers).status_code==404


@pytest.mark.parametrize('value',[None,123,{},[]])
def test_invalid_content_never_creates_initial_snapshot(profile_client,review_db,value):
 client,issue,_,_=profile_client
 response=client.post('/profiles/101/revisions',headers=issue(['write:models']),json={'raw_content':value})
 assert response.status_code==400 and _history(review_db)==[]

@pytest.mark.parametrize('original',['synthetic oversized original'])
def test_invalid_legacy_original_denies_without_inventing_history(profile_client,review_db,monkeypatch,original):
 from modules.system import profile_history
 client,issue,_,_=profile_client
 review_db.execute(text('UPDATE printer_profiles SET raw_content=:value WHERE id=101'),{'value':original});review_db.commit()
 monkeypatch.setattr(profile_history,'MAX_CONTENT_BYTES',8)
 response=client.post('/profiles/101/revisions',headers=issue(['write:models']),json={'raw_content':'small'})
 assert response.status_code==400,response.text
 assert _history(review_db)==[]
 assert review_db.execute(text('SELECT raw_content FROM printer_profiles WHERE id=101')).scalar_one()==original


def test_oversized_historical_restore_denied_without_new_revision(profile_client,review_db,monkeypatch):
 from modules.system import profile_history
 client,issue,_,_=profile_client;headers=issue(['write:models'])
 assert client.post('/profiles/101/revisions',headers=headers,json={'raw_content':'small'}).status_code==200
 before=_history(review_db);monkeypatch.setattr(profile_history,'MAX_CONTENT_BYTES',8)
 response=client.post('/profiles/101/revert/1',headers=headers)
 assert response.status_code==400,response.text
 assert _history(review_db)==before
 assert review_db.execute(text('SELECT raw_content FROM printer_profiles WHERE id=101')).scalar_one()=='small'


def test_defensive_null_original_rejected_before_snapshot(profile_client,review_db,monkeypatch):
 from modules.system import profile_history
 from types import SimpleNamespace
 client,issue,_,_=profile_client
 original=profile_history.accessible_profile
 def null_snapshot(db,identifier,actor,**kwargs):
  row=original(db,identifier,actor,**kwargs)
  return SimpleNamespace(**{**dict(row._mapping),'raw_content':None})
 monkeypatch.setattr(profile_history,'accessible_profile',null_snapshot)
 response=client.post('/profiles/101/revisions',headers=issue(['write:models']),json={'raw_content':'small'})
 assert response.status_code==400 and _history(review_db)==[]


def test_actual_delete_audit_failure_rolls_back_profile_and_history(profile_client,review_db,monkeypatch):
 from modules.system import profile_routes
 from core.models import AuditLog
 client,issue,_,_=profile_client;headers=issue(['write:models'])
 assert client.post('/profiles/101/revisions',headers=headers,json={'raw_content':'synthetic'}).status_code==200
 before=_history(review_db)
 def invalid_audit(db,*args,**kwargs):db.add(AuditLog(action=None,entity_type='profile',entity_id=101))
 monkeypatch.setattr(profile_routes,'log_audit',invalid_audit)
 response=client.delete('/profiles/101',headers=headers)
 assert response.status_code==500
 assert _history(review_db)==before
 assert review_db.execute(text('SELECT COUNT(*) FROM printer_profiles WHERE id=101')).scalar_one()==1


def test_unpaired_unicode_content_rejected_without_history(profile_client,review_db):
 client,issue,_,_=profile_client
 response=client.post('/profiles/101/revisions',headers={**issue(['write:models']),'Content-Type':'application/json'},content=b'{"raw_content":"\\ud800"}')
 assert response.status_code==400 and _history(review_db)==[]


@pytest.mark.parametrize('role,group,has_orgs', [
    ('admin', 1, True), ('admin', None, True), ('operator', None, True),
    ('admin', 1, False), ('operator', 1, False),
])
def test_unscoped_profile_and_history_writes(profile_client, review_db, role, group, has_orgs):
    client, issue, _, _ = profile_client
    if not has_orgs:
        review_db.execute(text('UPDATE groups SET is_org=FALSE'))
    review_db.execute(text('UPDATE users SET group_id=:group,role=:role WHERE id=3'), {'group': group, 'role': role})
    review_db.commit()
    headers = issue(['read:models', 'write:models'])
    for profile_id in ([102, 103] if has_orgs else [103]):
        foreign = profile_id == 102 and (role != 'admin' or group is not None)
        refused = has_orgs and group == 1 and profile_id == 103
        path = f'/profiles/{profile_id}'
        for suffix in ('', '/revisions'):
            assert client.get(path + suffix, headers=headers).status_code == (404 if foreign else 200)
        for method, suffix, body in (
            ('put', '', {'name': 'Updated'}), ('post', '/revisions', {'raw_content': '{}'}),
            ('post', '/revert/1', None), ('delete', '', None),
        ):
            response = client.request(method, path + suffix, headers=headers, json=body)
            if refused:
                assert response.status_code == 403
                assert response.json()['detail'] == 'This profile belongs to no organization. Ask a platform administrator to change it.'
            elif foreign:
                assert response.status_code == 404
            else:
                assert response.status_code == (204 if method == 'delete' else 200), response.text
        if refused or foreign:
            assert not review_db.execute(text('SELECT 1 FROM printer_profile_revisions WHERE profile_id=:id'),
                                         {'id': profile_id}).first()
