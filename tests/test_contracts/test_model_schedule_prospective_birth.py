"""Model queue entry must retain sanctioned prospective economic identity."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators,job_identity
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize("name,scope,key",[("admin","org",1),("platform","global",0),("unassigned",None,None)])
def test_model_schedule_has_fresh_owned_identity(creators,review_db,name,scope,key):
    from modules.models_library.routes.models_crud import router
    from modules.models_library.models import Model
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    model=Model(name="Owned schedule source",org_id=1 if name=="admin" else None,build_time_hours=1)
    review_db.add(model);review_db.commit()
    response=client.post(f"/api/models/{model.id}/schedule",headers=auth(name),json={"quantity":2,"priority":4,"notes":"owned"})
    assert response.status_code==200,response.text
    identifier=response.json()["job_id"]
    review_db.expire_all()
    row=job_identity(review_db,identifier)
    assert tuple(row[2:4])==(scope,key)
    assert bool(row[1])==(scope is not None)
    assert review_db.execute(text("SELECT quantity,priority,notes FROM jobs WHERE id=:id"),{"id":identifier}).one()==(2,4,"owned")


@pytest.mark.parametrize("name",["admin","platform","unassigned"])
def test_model_schedule_matches_original_raw_insert_omitted_column_semantics(creators,review_db,name,request=None):
    # Both independent INSERT statements must observe one clock value. SQLite's
    # CURRENT_TIMESTAMP is statement-scoped, so crossing a second otherwise
    # makes this exact-column oracle fail despite identical default semantics.
    from sqlalchemy import event
    engine = review_db.get_bind()
    def freeze_clock(raw, *_):
        raw.create_function("current_timestamp", 0, lambda: "2026-10-23 12:00:00")
    event.listen(engine, "checkout", freeze_clock)
    if request is not None:
        request.addfinalizer(lambda: event.remove(engine, "checkout", freeze_clock))
    freeze_clock(review_db.connection().connection.driver_connection)
    from modules.models_library.routes.models_crud import router
    from modules.models_library.models import Model
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    model=Model(name="Raw semantic oracle",org_id=1 if name=="admin" else None,build_time_hours=0)
    review_db.add(model);review_db.commit()
    response=client.post(f"/api/models/{model.id}/schedule",headers=auth(name),json={"quantity":1,"priority":5,"notes":None})
    assert response.status_code==200,response.text
    identifier=response.json()["job_id"]
    # These are exactly the original raw INSERT columns, independent of the new
    # ORM constructor. The reference deliberately lets the actual DB govern all
    # omitted columns rather than copying them from the new row.
    columns="item_name,model_id,duration_hours,colors_required,quantity,priority,status,printer_id,hold,is_locked,estimated_cost,suggested_price,charged_to_user_id,charged_to_org_id,submitted_by,due_date,notes,print_file_id,payload_sha256,plate_index"
    reference=review_db.execute(text(f"INSERT INTO jobs ({columns}) SELECT {columns} FROM jobs WHERE id=:id RETURNING id"),{"id":identifier}).scalar_one()
    rows=review_db.execute(text("SELECT * FROM jobs WHERE id IN (:new,:old) ORDER BY id"),{"new":identifier,"old":reference}).mappings().all()
    actual,original=dict(rows[0]),dict(rows[1])
    assert actual["created_at"] == actual["updated_at"] == "2026-10-23 12:00:00"
    for row in (actual,original):
        row.pop("id");row.pop("economic_uid")
    assert actual==original
    review_db.rollback()


def test_model_identity_insert_fault_rolls_back_job_registry_and_quota(creators,review_db):
    from fastapi.testclient import TestClient
    from modules.models_library.routes.models_crud import router
    from modules.models_library.models import Model
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    model=Model(name="Owned failure",org_id=1)
    review_db.add(model);review_db.commit()
    def counts():
        return tuple(review_db.execute(text(sql)).scalar_one() for sql in ("SELECT count(*) FROM jobs","SELECT count(*) FROM cost_identities","SELECT coalesce(sum(jobs_used),0) FROM quota_usage WHERE user_id=3"))
    before=counts();review_db.commit()
    review_db.execute(text("CREATE TRIGGER owned_fail_model_identity BEFORE INSERT ON cost_identities WHEN NEW.resource_kind='job' BEGIN SELECT RAISE(ABORT, 'owned model identity fault'); END"));review_db.commit()
    response=TestClient(client.app,raise_server_exceptions=False).post(f"/api/models/{model.id}/schedule",headers=auth(),json={})
    assert response.status_code==500
    review_db.expire_all()
    assert counts()==before
