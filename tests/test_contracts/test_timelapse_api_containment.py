"""Owned HTTP and filesystem proof for the temporary publisher/retirement refusals."""
import pytest
from sqlalchemy import event,text
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import owned_runtime,review_db,allowlist_app
from tests.test_contracts.test_timelapse_content_audience import media_app,capture_plain,_managed_media
from fastapi.testclient import TestClient


def snapshot(db):
    with Session(db.get_bind()) as current:
        return tuple(tuple(current.execute(text('SELECT * FROM '+table+' ORDER BY 1')).all())
                     for table in ('jobs','printers','print_files','models','timelapses','timelapse_ownership_anchors','audit_logs',
                                   'timelapse_media_lifetimes','timelapse_media_versions',
                                   'timelapse_frame_namespaces','timelapse_frame_bindings',
                                   'timelapse_publications','timelapse_publication_facts','timelapse_retained_versions',
                                   'timelapse_retirements','timelapse_retirement_objects',
                                   'timelapse_retirement_lifetime_bindings','timelapse_retirement_members',
                                   'timelapse_retirement_container_receipts'))


@pytest.mark.parametrize('operation,body',[('delete',None),('trim',{'end_seconds':2}),('speed',{'multiplier':2})])
@pytest.mark.parametrize('authority',['owner','foreign','unknown','viewer','missing'])
def test_mutation_refusal_preserves_owned_files_rows_and_audit(media_app,review_db,monkeypatch,operation,body,authority,tmp_path):
    from modules.printers import camera_routes
    client,current,media,_=media_app
    tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=False,org=1)
    frames=media.parent/'frames_owned';frames.mkdir();(frames/'frame_000003.jpg').write_bytes(b'ORIGINAL_JPEG')
    legacy=media.parent/str(tid);legacy.mkdir();(legacy/'frame_000001.jpg').write_bytes(b'LEGACY_JPEG')
    nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    review_db.rollback()
    if authority=='foreign':current['principal'].update(id=5,group_id=2)
    elif authority=='unknown':
        review_db.execute(text('UPDATE timelapses SET ownership_operation_id=NULL WHERE id=:id'),{'id':tid});review_db.commit()
    elif authority=='viewer':current['principal'].update(role='viewer')
    before=snapshot(review_db)
    identities={str(p.relative_to(media.parent)):(p.stat().st_ino,p.read_bytes()) for p in media.parent.rglob('*') if p.is_file() and '.locks' not in p.parts}
    from modules.archives import timelapse_edit,timelapse_lifetime_prepare
    from modules.archives.timelapse_retirement import RetirementUnavailable
    from fastapi import HTTPException
    effects=[];faults=[]
    def forbidden(*args,**kwargs):
        effects.append(True);pytest.fail('Refused mutation must not admit journal or perform work')
    monkeypatch.setattr(timelapse_edit.EditJournal,'admit',forbidden)
    monkeypatch.setattr(timelapse_edit.EditJournal,'work',forbidden)
    def unavailable_probe(*args,**kwargs):
        faults.append('probe');raise HTTPException(503,'Owned probe deliberately unavailable')
    original_facts=timelapse_lifetime_prepare._facts
    def unavailable_stat(path,stat):
        if path.endswith('.mp4'):
            faults.append('stat');raise RetirementUnavailable('owned_fixture_unavailable')
        return original_facts(path,stat)
    if authority=='owner':
        monkeypatch.setattr(timelapse_edit,'probe_owned_media',unavailable_probe)
        monkeypatch.setattr(timelapse_lifetime_prepare,'_facts',unavailable_stat)
    else:
        monkeypatch.setattr(timelapse_edit,'open_proven_media',forbidden)
        monkeypatch.setattr(timelapse_edit,'probe_owned_media',forbidden)
    writes=[]
    def observe(c,cur,statement,params,ctx,many):
        # Authorization uses SQLite no-op writer fences. Capture real mutations
        # independently in exact table/row snapshots rather than rejecting locks.
        if statement.lstrip().split(' ',1)[0].upper() in ('INSERT','DELETE'):writes.append(statement)
    engine=review_db.get_bind();event.listen(engine,'before_cursor_execute',observe)
    try:
        target=tid if authority!='missing' else 99999
        headers={'If-Match':'"'+nonce+'"'}
        response=client.delete(f'/timelapses/{target}',headers=headers) if operation=='delete' else client.post(f'/timelapses/{target}/{operation}',headers=headers,json={**body,'operation_id':'a'*32})
    finally:event.remove(engine,'before_cursor_execute',observe)
    assert response.status_code=={'owner':503,'foreign':404,'unknown':404,'viewer':403,'missing':404}[authority],response.text
    if authority=='owner':assert faults==(['stat'] if operation=='delete' else ['probe'])
    else:assert faults==[]
    assert effects==[]
    assert writes==[]
    assert snapshot(review_db)==before
    assert {str(p.relative_to(media.parent)):(p.stat().st_ino,p.read_bytes()) for p in media.parent.rglob('*') if p.is_file() and '.locks' not in p.parts}==identities


@pytest.mark.parametrize('operation,body',[('trim',{}),('trim',{'start_seconds':2,'end_seconds':1}),('trim',{'end_seconds':'bad'}),('speed',{'multiplier':3}),('speed',{'multiplier':True})])
def test_invalid_edit_input_preserves_rows_and_files(media_app,review_db,operation,body):
    client,_,media,_=media_app;tid=capture_plain(review_db,org=1)
    before=snapshot(review_db);original=(media.stat().st_ino,media.read_bytes())
    nonce=review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one();review_db.rollback()
    assert client.post(f'/timelapses/{tid}/{operation}',headers={'If-Match':'"'+nonce+'"'},json={**body,'operation_id':'a'*32}).status_code==400
    assert snapshot(review_db)==before
    assert (media.stat().st_ino,media.read_bytes())==original


@pytest.mark.parametrize('operation',['trim','speed'])
def test_nonready_status_refuses_without_source_or_journal_mutation(media_app,review_db,operation,monkeypatch,tmp_path):
    from pathlib import Path
    from modules.printers import camera_routes
    from modules.archives import timelapse_edit
    client,_,_,_=media_app
    tid=_managed_media(media_app,review_db,monkeypatch,tmp_path,education=False,org=1)
    row=review_db.execute(text('SELECT filename,ownership_operation_id FROM timelapses WHERE id=:id'),{'id':tid}).one()
    media=Path(camera_routes.TIMELAPSE_ROOT)/row.filename;nonce=row.ownership_operation_id
    review_db.execute(text("UPDATE timelapses SET status='capturing' WHERE id=:id"),{'id':tid});review_db.commit()
    before=snapshot(review_db);original=(media.stat().st_ino,media.read_bytes());effects=[]
    def forbidden(*args,**kwargs):
        effects.append(True);pytest.fail('Nonready refusal must precede source/probe/journal/work')
    for name in ('open_proven_media','probe_owned_media'):monkeypatch.setattr(timelapse_edit,name,forbidden)
    for name in ('admit','work'):monkeypatch.setattr(timelapse_edit.EditJournal,name,forbidden)
    response=client.post(f'/timelapses/{tid}/{operation}',headers={'If-Match':'"'+nonce+'"'},json={'end_seconds':2,'multiplier':2,'operation_id':'a'*32})
    assert response.status_code==503,response.text
    assert effects==[]
    assert snapshot(review_db)==before
    assert (media.stat().st_ino,media.read_bytes())==original


def test_manual_cleanup_refuses_timelapses_and_preserves_owned_media(allowlist_app,review_db,tmp_path):
    media=tmp_path/'original.mp4';media.write_bytes(b'ORIGINAL_MP4');original=(media.stat().st_ino,media.read_bytes())
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,created_at) VALUES(999,9,:path,'ready','2000-01-01')"),{'path':str(media)})
    review_db.execute(text("INSERT INTO alerts(id,user_id,alert_type,severity,title,created_at) VALUES(999,3,'print_complete','info','owned','2000-01-01')"));review_db.commit()
    writes=[]
    def observe(c,cur,statement,params,ctx,many):
        if 'DELETE FROM timelapses' in statement:writes.append(statement)
    engine=review_db.get_bind();event.listen(engine,'before_cursor_execute',observe)
    try:
        with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
            assert client.put('/api/config/retention',json={'timelapses_days':30,'alert_history_days':30}).status_code==200
            response=client.post('/api/admin/retention/cleanup')
    finally:event.remove(engine,'before_cursor_execute',observe)
    assert response.status_code==200
    result=response.json();assert result['status']=='partial'
    assert result['refused']=={'timelapses':{'status':'refused','reason':'lifetime_retirement_pending'}}
    assert 'timelapses' not in result['deleted'] and result['deleted']['alerts']>=1
    assert writes==[]
    with Session(engine) as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapses WHERE id=999')).scalar_one()==1
        assert db.execute(text('SELECT COUNT(*) FROM alerts WHERE id=999')).scalar_one()==0
    assert (media.stat().st_ino,media.read_bytes())==original
