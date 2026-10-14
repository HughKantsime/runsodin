"""Required speed-lifetime behavior; current conservative sharing fence is red."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_timelapse_edit_publication import edit,run,prepared_source,encoder,frame_writer,review_db,owned_runtime
from tests.test_contracts.test_timelapse_lifetime_action import credential
from core.timelapse_recipes import make_recipe
from modules.archives.timelapse_lifetime_prepare import prepare_request_lifetime_retirement
from modules.archives.timelapse_lifetime_action import retire_sealed_lifetime
from core.timelapse_request_budget import VerificationBudget

def test_retire_speed_result_preserves_original_lifetime(edit):
    service,engine,root,tid,nonce,evidence=edit
    with Session(engine) as db:original_name=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    original=(root/original_name).read_bytes()
    result=run(edit,recipe=make_recipe('speed',multiplier=2))
    authority=credential(engine)
    prepare_request_lifetime_retirement(engine,root,result['result_tid'],authority,expected_nonce=result['result_nonce'],budget=VerificationBudget())
    retire_sealed_lifetime(engine,root,result['result_tid'],authority,expected_nonce=result['result_nonce'])
    assert (root/original_name).read_bytes()==original
    with Session(engine) as db:
        assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='ready'
        assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':result['result_tid']}).scalar_one()==0

def test_retire_original_with_completed_outgoing_speed_preserves_result(edit):
    service,engine,root,tid,nonce,evidence=edit
    result=run(edit,recipe=make_recipe('speed',multiplier=2))
    with Session(engine) as db:result_name=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':result['result_tid']}).scalar_one()
    before=(root/result_name).read_bytes();authority=credential(engine)
    prepare_request_lifetime_retirement(engine,root,tid,authority,expected_nonce=nonce,budget=VerificationBudget())
    retire_sealed_lifetime(engine,root,tid,authority,expected_nonce=nonce)
    assert (root/result_name).read_bytes()==before
    with Session(engine) as db:assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':result['result_tid']}).scalar_one()=='ready'

def test_after_original_retirement_speed_retirement_uses_immutable_source_facts_no_reopen(edit,monkeypatch):
    import os
    service,engine,root,tid,nonce,evidence=edit
    result=run(edit,recipe=make_recipe('speed',multiplier=2));authority=credential(engine)
    with Session(engine) as db:oldpath=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()
    prepare_request_lifetime_retirement(engine,root,tid,authority,expected_nonce=nonce,budget=VerificationBudget())
    retire_sealed_lifetime(engine,root,tid,authority,expected_nonce=nonce)
    assert not (root/oldpath).exists()
    with engine.begin() as c:
        c.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(:id,77,444,'reused-new-file.mp4','ready')"),{'id':tid})
    actual=os.open
    def open_file(path,*a,**kw):
        assert str(path) not in (oldpath,str(root/oldpath),'reused-new-file.mp4',str(root/'reused-new-file.mp4'))
        return actual(path,*a,**kw)
    monkeypatch.setattr(os,'open',open_file)
    prepare_request_lifetime_retirement(engine,root,result['result_tid'],authority,expected_nonce=result['result_nonce'],budget=VerificationBudget())
    retire_sealed_lifetime(engine,root,result['result_tid'],authority,expected_nonce=result['result_nonce'])

def test_speed_trim_chained_speed_retire_parent_preserves_child(edit):
    service,engine,root,tid,nonce,evidence=edit
    first=run(edit,recipe=make_recipe('speed',multiplier=.5))
    trim=service.execute_edit(engine,str(root),first['result_tid'],evidence,'b'*32,nonce=first['result_nonce'],recipe=make_recipe('trim',start_seconds=0,end_seconds=.1))
    child=service.execute_edit(engine,str(root),first['result_tid'],evidence,'c'*32,nonce=trim['result_nonce'],recipe=make_recipe('speed',multiplier=2))
    with Session(engine) as db:path=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':child['result_tid']}).scalar_one()
    original=(root/path).read_bytes();authority=credential(engine)
    prepare_request_lifetime_retirement(engine,root,first['result_tid'],authority,expected_nonce=trim['result_nonce'],budget=VerificationBudget())
    retire_sealed_lifetime(engine,root,first['result_tid'],authority,expected_nonce=trim['result_nonce'])
    assert (root/path).read_bytes()==original
    with Session(engine) as db:
        assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='ready'
        assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':child['result_tid']}).scalar_one()=='ready'

@pytest.mark.parametrize('boundary',['private_created','worker_admitted','output_ready','publish_pending','published','complete'])
def test_incomplete_outgoing_speed_refuses_source_retirement(edit,monkeypatch,boundary):
    service,engine,root,tid,nonce,evidence=edit;actual=service.EditJournal.advance
    def fail(self,row,saved,target,**kw):
        if target==boundary:raise RuntimeError('owned interrupted speed writer')
        return actual(self,row,saved,target,**kw)
    monkeypatch.setattr(service.EditJournal,'advance',fail)
    with pytest.raises(RuntimeError):run(edit,recipe=make_recipe('speed',multiplier=2))
    from modules.archives.timelapse_retirement import RetirementUnavailable
    with pytest.raises(RetirementUnavailable) as caught:
        prepare_request_lifetime_retirement(engine,root,tid,credential(engine),expected_nonce=nonce,budget=VerificationBudget())
    assert caught.value.reason=='writer_active'
    with Session(engine) as db:assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':tid}).scalar_one()=='ready'

@pytest.mark.parametrize('field,value',[('source_lifetime','unknown-life'),('source_printer_id',999),('source_filename','unknown-source.mp4'),('destination_path','foreign-output.mp4')])
def test_dependency_classifier_rejects_forged_relation_owner_root_or_path(edit,field,value):
    from core.timelapse_publication_schema import publications
    from modules.archives.timelapse_speed_dependencies import physical_partition
    from core.schema.migrator import MigrationError
    from modules.archives.timelapse_retirement import RetirementUnavailable
    service,engine,root,tid,nonce,evidence=edit
    result=run(edit,recipe=make_recipe('speed',multiplier=2))
    with Session(engine) as db:
        row=dict(db.execute(publications.select().where(publications.c.operation_id=='a'*32)).mappings().one())
        from core.timelapse_media_lock import acquire_media_guard
        from core.timelapse_manifest import _root
        from modules.archives.timelapse_snapshot_graph import fence_graph_scope
        # Caller fixture already holds no writer transaction.
        guard=acquire_media_guard(edit[2],edit[3])
        with _root(edit[2]) as (_,identity):scope=fence_graph_scope(db,guard,identity)
        forged=dict(row);forged[field]=value
        with pytest.raises((MigrationError,RetirementUnavailable)):
            physical_partition(db,row['source_lifetime'],[forged],scope=scope)
        guard.close()

def test_dependency_sharing_never_ignores_foreign_physical_overlap(edit):
    from core.timelapse_publication_schema import publications
    from modules.archives.timelapse_lifetime_action import _sharing
    from modules.archives.timelapse_retirement import RetirementUnavailable
    service,engine,root,tid,nonce,evidence=edit
    result=run(edit,recipe=make_recipe('speed',multiplier=2))
    from tests.test_contracts.test_timelapse_snapshot_graph import graph_session
    from modules.archives.timelapse_speed_dependencies import physical_partition
    with graph_session(edit) as (db,scope):
        row=dict(db.execute(publications.select().where(publications.c.operation_id=='a'*32)).mappings().one())
        partition=physical_partition(db,row['source_lifetime'],[row],scope=scope)
        for path in (row['private_directory'],row['destination_path']):
            plan={'binding':{'lifetime_nonce':row['source_lifetime']},'objects':[{'relative_path':path}],
                  'publications':[row],'namespaces':[],'intent':{'timelapse_id':tid}}
            with pytest.raises(RetirementUnavailable) as caught:_sharing(db,plan,partition=partition)
            assert caught.value.reason=='shared_workspace'
