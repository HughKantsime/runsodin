"""Real JWT generic file scheduling must register its fresh job identity."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators,job_identity
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize("name,scope,key",[("admin","org",1),("platform","global",0),("unassigned",None,None)])
def test_generic_file_schedule_creates_owned_prospective_job(creators,review_db,tmp_path,name,scope,key):
    from modules.models_library.routes.print_files import router
    from modules.models_library.models import Model
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    owner=1 if name=="admin" else None
    model=Model(name="Owned file model",org_id=owner)
    review_db.add(model);review_db.flush()
    source=tmp_path/"owned.gcode";source.write_text("; owned queue-only fixture")
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json,print_time_seconds) VALUES(501,:model,'owned.gcode','owned.gcode',:path,:owner,'[]',0)"),{"model":model.id,"path":str(source),"owner":owner});review_db.commit()
    response=client.post("/api/print-files/501/schedule",headers=auth(name))
    assert response.status_code==200,response.text
    review_db.expire_all()
    identifier=review_db.execute(text("SELECT job_id FROM print_files WHERE id=501")).scalar_one()
    row=job_identity(review_db,identifier)
    assert bool(row[1])==(scope is not None)
    assert tuple(row[2:])==((scope,key,"job",identifier) if scope is not None else (None,None,None,None))
    assert_original_raw_semantics(review_db,identifier,"item_name,model_id,print_file_id,duration_hours,colors_required,quantity,priority,status,printer_id,hold,is_locked,submitted_by,charged_to_user_id,charged_to_org_id")



def test_studio_real_multipart_upload_has_owned_fresh_job_identity(creators,review_db,tmp_path,monkeypatch):
    from modules.models_library.routes import print_files
    client,auth,_,_=creators
    client.app.include_router(print_files.router,prefix="/api")
    storage=tmp_path/"studio-uploads";storage.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT",str(storage))
    print_files.limiter._storage.reset()
    review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=9"));review_db.commit()
    response=client.post("/api/prints",headers=auth(),data={"target_printer_id":"9","note":"Owned Studio fixture"},files={"file":("owned-studio.gcode",b"; Synthetic queue-only G-code\n; printer_model = MK4\nG28\n","application/octet-stream")})
    assert response.status_code==201,response.text
    identifier=int(response.json()["id"])
    review_db.expire_all()
    assert review_db.execute(text("SELECT count(*) FROM print_files WHERE job_id=:id"),{"id":identifier}).scalar_one()==1
    assert list(storage.iterdir())
    row=job_identity(review_db,identifier)
    assert row[1]
    assert tuple(row[2:])==("org",1,"job",identifier)
    # The Studio printer choice is an explicit pin (target_type='specific', target_filter=<id>):
    # target_filter joins the columns the route sets; every other column keeps the raw-insert semantics.
    assert review_db.execute(text("SELECT target_type,target_filter FROM jobs WHERE id=:id"),{"id":identifier}).one()==("specific","9")
    assert_original_raw_semantics(review_db,identifier,"item_name,model_id,duration_hours,colors_required,quantity,priority,status,printer_id,hold,is_locked,notes,charged_to_user_id,charged_to_org_id,target_type,target_filter")



def assert_original_raw_semantics(db,identifier,columns):
    reference=db.execute(text(f"INSERT INTO jobs ({columns}) SELECT {columns} FROM jobs WHERE id=:id RETURNING id"),{"id":identifier}).scalar_one()
    rows=db.execute(text("SELECT * FROM jobs WHERE id IN (:new,:old) ORDER BY id"),{"new":identifier,"old":reference}).mappings().all()
    actual,original=dict(rows[0]),dict(rows[1])
    for row in (actual,original):
        row.pop("id");row.pop("economic_uid")
        # Exact payload binding is the intentional extension, tested separately
        # against actual stored bytes and readonly dispatcher selection.
        row.pop('payload_sha256');row.pop('plate_index')
        if 'print_file_id' not in columns.split(','):
            row.pop('print_file_id')
    assert actual==original
    db.rollback()


@pytest.mark.parametrize("studio",[False,True])
def test_file_job_registry_fault_rolls_back_only_current_transaction(creators,review_db,tmp_path,monkeypatch,studio):
    from fastapi.testclient import TestClient
    from modules.models_library.routes import print_files
    from modules.models_library.models import Model
    client,auth,_,_=creators
    client.app.include_router(print_files.router,prefix="/api")
    storage=tmp_path/"failure-uploads";storage.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT",str(storage));print_files.limiter._storage.reset()
    if not studio:
        model=Model(name="Owned failure file",org_id=1);review_db.add(model);review_db.flush()
        source=tmp_path/"failure.gcode";source.write_text("; owned fixture")
        review_db.execute(text("INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id,filaments_json) VALUES(501,:model,'failure.gcode','failure.gcode',:path,1,'[]')"),{"model":model.id,"path":str(source)});review_db.commit()
    def counts():
        return tuple(review_db.execute(text(sql)).scalar_one() for sql in ("SELECT count(*) FROM jobs","SELECT count(*) FROM cost_identities","SELECT coalesce(sum(jobs_used),0) FROM quota_usage WHERE user_id=3"))
    if studio:
        review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=9"));review_db.commit()
    before=counts();review_db.commit()
    review_db.execute(text("CREATE TRIGGER owned_fail_file_job_registry BEFORE INSERT ON cost_identities WHEN NEW.resource_kind='job' BEGIN SELECT RAISE(ABORT, 'owned file job registry failure'); END"));review_db.commit()
    failing=TestClient(client.app,raise_server_exceptions=False)
    if studio:
        response=failing.post("/api/prints",headers=auth(),data={"target_printer_id":"9"},files={"file":("failure-studio.gcode",b"; Synthetic queue-only fixture\n","application/octet-stream")})
    else:response=failing.post("/api/print-files/501/schedule",headers=auth())
    assert response.status_code==500,response.text
    review_db.expire_all()
    assert counts()==before
    if studio:
        # upload_3mf committed before the queue transaction. Preserve that file,
        # and prove no misleading job link survives the failed later transaction.
        assert list(storage.iterdir())
        assert review_db.execute(text("SELECT count(*) FROM print_files WHERE job_id IS NULL AND original_filename='failure-studio.gcode'")).scalar_one()==1
    else:assert review_db.execute(text("SELECT job_id FROM print_files WHERE id=501")).scalar_one() is None


@pytest.mark.parametrize("field",["id","economic_uid","economic_birth_nonce"])
def test_values_factory_rejects_caller_identity_before_writes(review_db,field):
    from modules.jobs.routes.jobs_crud import _stage_prospective_queue_values
    before=review_db.execute(text("SELECT count(*) FROM jobs")).scalar_one()
    with pytest.raises(ValueError,match="Unsupported fresh queue fields"):
        _stage_prospective_queue_values(review_db,{"item_name":"Owned fixture",field:1},{"id":3,"role":"admin","group_id":1})
    assert review_db.execute(text("SELECT count(*) FROM jobs")).scalar_one()==before



def test_studio_actual_dry_run_leaves_job_upload_and_quota_unchanged(creators,review_db,tmp_path,monkeypatch):
    from modules.models_library.routes import print_files
    client,auth,_,_=creators
    client.app.include_router(print_files.router,prefix="/api/v1")
    storage=tmp_path/"dry-run-uploads";storage.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT",str(storage));print_files.limiter._storage.reset()
    def counts():
        return tuple(review_db.execute(text("SELECT count(*) FROM "+table)).scalar_one() for table in ("jobs","print_files","models","cost_identities","quota_usage"))
    review_db.execute(text("UPDATE printers SET api_type='moonraker' WHERE id=9"));review_db.commit()
    before=counts();review_db.commit()
    response=client.post("/api/v1/prints",headers={**auth(),"X-Dry-Run":"true"},data={"target_printer_id":"9"},files={"file":("preview.gcode",b"; owned dry-run fixture","application/octet-stream")})
    assert response.status_code==200,response.text
    assert response.json()["dry_run"] is True
    review_db.expire_all()
    assert counts()==before
    assert list(storage.iterdir())==[]
