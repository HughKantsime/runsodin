"""Read-time names preserve raw evidence and follow model/file renames."""
import json
import pytest
from sqlalchemy import text, event
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db, interactive_headers
from core.display_names import human_name, filename_stem, display_names, job_name


@pytest.mark.parametrize('filename,expected', [('Squirtle.gcode.3mf','Squirtle'),('latch-v3.2-H2D-150-slot2-HF.gcode','latch-v3.2-H2D-150-slot2-HF'),('Part.BGCODE','Part'),('Part.3mf','Part')])
def test_filename_stem(filename,expected):
    assert filename_stem(filename)==expected


def test_precedence_and_transport_suppression():
    assert human_name('raw',model_name='Model',original_filename='File.gcode')=='Model'
    assert human_name('Metadata title',original_filename='Squirtle.gcode.3mf')=='Squirtle'
    assert human_name('Reprint: odin-dispatch-ab12-1234.gcode')=='Unnamed print'
    assert human_name('Useful stored name')=='Useful stored name'
    assert human_name(model_name='latch-v3.2-H2D-150-slot2-HF.gcode')=='latch-v3.2-H2D-150-slot2-HF'
    assert human_name('first-layer-corners.gcode')=='first-layer-corners'
    assert human_name(model_name='Latch v3.2')=='Latch v3.2'


@pytest.fixture
def named_graph(review_db):
    review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(8001,'Squirtle',1)"))
    review_db.execute(text("INSERT INTO print_files(id,filename,original_filename,model_id,org_id) VALUES(8001,'transport.gcode','original.gcode.3mf',8001,1)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,model_id,print_file_id,charged_to_org_id,status) VALUES(8001,'odin-dispatch-abcd',8001,8001,1,'pending')"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) VALUES(8001,9,8001,'odin-dispatch-abcd','failed','2026-10-29')"))
    review_db.execute(text("INSERT INTO print_archives(id,print_job_id,job_id,printer_id,print_name,status,print_file_id,org_id,ownership_state) VALUES(8001,8001,8001,9,'odin-dispatch-abcd','failed',8001,1,'known')"))
    review_db.commit()
    return review_db


def test_rename_propagates_to_job_print_archive_and_audit(token_client,named_graph):
    from modules.models_library.routes import models_crud
    client,_=token_client;client.app.include_router(models_crud.router)
    from modules.jobs.routes import jobs_crud, presets
    from modules.archives.routes import archives_crud
    for router in (jobs_crud.router, presets.router, archives_crud.router):client.app.include_router(router)
    headers=interactive_headers()
    for kind in ('job','print_job','archive'):
        assert display_names(named_graph,kind,[8001])[8001]=='Squirtle'
    response=client.patch('/models/8001',headers=headers,json={'name':'Squirtle revised'})
    assert response.status_code==200,response.text
    for kind in ('job','print_job','archive'):
        assert display_names(named_graph,kind,[8001])[8001]=='Squirtle revised'
    for path in ('/jobs', '/print-jobs', '/archives'):
        response=client.get(path,headers=headers)
        assert response.status_code==200,response.text
        rows=response.json()
        if isinstance(rows,dict):rows=rows['items']
        assert next(row for row in rows if row['id']==8001)['display_name']=='Squirtle revised'
    assert named_graph.execute(text("SELECT COUNT(*) FROM audit_logs WHERE entity_id=8001 AND action='model.renamed'")).scalar_one()==1
    assert named_graph.execute(text('SELECT job_name FROM print_jobs WHERE id=8001')).scalar_one()=='odin-dispatch-abcd'


def test_batched_names_and_file_model_fallback(named_graph):
    named_graph.execute(text('UPDATE jobs SET model_id=NULL WHERE id=8001'))
    assert display_names(named_graph,'job',[8001])[8001]=='Squirtle'
    named_graph.execute(text('UPDATE print_files SET model_id=NULL WHERE id=8001'))
    statements=[]
    def capture(conn,cursor,statement,parameters,context,many):statements.append(statement)
    event.listen(named_graph.get_bind(),'before_cursor_execute',capture)
    try:
        assert display_names(named_graph,'job',[8001]*100)[8001]=='original'
    finally:event.remove(named_graph.get_bind(),'before_cursor_execute',capture)
    assert len(statements)==1


def test_activity_includes_outside_failed_and_unlinked_prints(token_client,review_db):
    from modules.jobs.routes import presets
    client,_=token_client;client.app.include_router(presets.router)
    for identifier,status,name in [(8101,'completed','Outside print'),(8102,'failed','Failed attempt'),(8103,'running','odin-dispatch-deadbeef')]:
        review_db.execute(text('INSERT INTO print_jobs(id,printer_id,status,job_name,started_at) VALUES(:id,9,:status,:name,\'2026-10-29\')'),{'id':identifier,'status':status,'name':name})
    review_db.commit()
    for path in ('/print-jobs','/print-jobs/unlinked'):
        response=client.get(path,headers=interactive_headers())
        assert response.status_code==200
        rows={row['id']:row for row in response.json()}
        assert {8101,8102,8103} <= rows.keys()
        assert rows[8102]['status']=='failed'
        assert rows[8101]['scheduled_job_id'] is None
        assert [rows[i]['display_name'] for i in (8101,8102,8103)]==['Outside print','Failed attempt','Unnamed print']


def test_file_rename_audited_and_viewer_refused(token_client,named_graph):
    from modules.models_library.routes import print_files
    client,issue=token_client;client.app.include_router(print_files.router)
    named_graph.execute(text('UPDATE print_files SET model_id=NULL WHERE id=8001'))
    named_graph.execute(text('UPDATE jobs SET model_id=NULL WHERE id=8001'))
    named_graph.commit()
    response=client.patch('/print-files/8001/name',headers=interactive_headers(),json={'original_filename':'Human latch.gcode'})
    assert response.status_code==200,response.text
    assert display_names(named_graph,'print_job',[8001])[8001]=='Human latch'
    assert named_graph.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='print_file.renamed'")).scalar_one()==1
    named_graph.execute(text("UPDATE users SET role='viewer' WHERE id=3"));named_graph.commit()
    assert client.patch('/print-files/8001/name',headers=issue(['write:models']),json={'original_filename':'No.gcode'}).status_code==403


from tests.test_contracts.test_print_file_upload_ownership_behavior import upload_client, upload, GCODE


@pytest.mark.parametrize('extension',['.gcode','.bgcode','.gcode.3mf'])
def test_upload_names_model_from_original_filename(upload_client,review_db,extension):
    from pathlib import Path
    client,issue,_=upload_client
    content=Path('tests/fixtures/release_gate/candidate-calibration-cube.3mf').read_bytes() if extension.endswith('.3mf') else GCODE
    response=upload(client,issue(['write:models']),'Squirtle'+extension,content)
    assert response.status_code==200,response.text
    name=review_db.execute(text('SELECT name FROM models WHERE id=:id'),{'id':response.json()['model_id']}).scalar_one()
    assert name=='Squirtle'


from tests.test_contracts.test_cert_D_jobsB_monitor import lifecycle


@pytest.mark.parametrize('with_model',[True,False])
def test_observed_print_attachment_uses_model_or_stem(lifecycle,with_model):
    from modules.printers.monitors import mqtt_job_lifecycle
    db,start,_,_=lifecycle
    if with_model:
        db.execute(text("INSERT INTO models(id,name,org_id) VALUES(8301,'Operator model name',1)"))
    db.execute(text("INSERT INTO print_files(id,filename,original_filename,model_id,org_id) VALUES(8301,'outside.gcode','Original part.gcode',:model,1)"),{'model':8301 if with_model else None})
    db.commit()
    print_id,linked,weights=start(name='outside.gcode')
    assert linked is None
    identifier=mqtt_job_lifecycle.record_job_ended(9,'Synthetic',print_id,None,'completed',{'subtask_name':'outside.gcode'},weights,lambda **kw:None)
    assert identifier is not None
    assert db.execute(text('SELECT item_name FROM jobs WHERE id=:id'),{'id':identifier}).scalar_one()==('Operator model name' if with_model else 'Original part')
    assert db.execute(text('SELECT job_name FROM print_jobs WHERE id=:id'),{'id':print_id}).scalar_one()=='outside.gcode'


def test_job_keeps_operator_name_but_not_derived_names():
    assert job_name('Order 12 cabinet locks', model_name='cabinet lock v3.1', original_filename='lock.3mf', project_name='Flexi 3')=='Order 12 cabinet locks'
    assert job_name('Flexi 3', model_name='Squirtle', original_filename='Squirtle.3mf', project_name='Flexi 3')=='Squirtle'
    assert job_name('Reprint: odin-dispatch-ab12', model_name='cabinet lock v3.1')=='cabinet lock v3.1'
    assert job_name('lock.gcode.3mf', model_name='cabinet lock v3.1', original_filename='lock.gcode.3mf')=='cabinet lock v3.1'
    assert job_name('part.gcode', original_filename=None)=='part'
