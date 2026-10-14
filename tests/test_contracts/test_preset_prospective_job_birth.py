"""Actual authenticated preset schedule and retained economic identity."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators, job_identity
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize("name,scope,key", [("admin","org",1),("platform","global",0),("unassigned",None,None)])
def test_preset_fresh_job_preserves_defaults_and_attests_only_real_owner(creators, review_db, name, scope, key):
    from modules.jobs.routes.presets import router
    from modules.jobs.models import PrintPreset
    client, auth, _, _ = creators
    client.app.include_router(router, prefix="/api")
    preset = PrintPreset(name="Owned preset", item_name="Preset part", quantity=2, priority=4, duration_hours=1.5, notes="owned notes")
    review_db.add(preset); review_db.commit()
    response = client.post(f"/api/presets/{preset.id}/schedule", headers=auth(name))
    assert response.status_code == 200, response.text
    identifier=response.json()["id"]
    review_db.expire_all()
    row=job_identity(review_db,identifier)
    assert tuple(row[2:4]) == (scope,key)
    assert bool(row[1]) == (scope is not None)
    assert review_db.execute(text("SELECT item_name,quantity,priority,duration_hours,notes FROM jobs WHERE id=:id"),{"id":identifier}).one() == ("Preset part",2,4,1.5,"owned notes")
    if row[1]:
        assert tuple(row[4:]) == ("job",identifier)
        assert review_db.execute(text("SELECT count(*) FROM cost_job_bindings WHERE job_uid=:uid"),{"uid":row[1]}).scalar_one() == 0


def test_preset_identity_insert_failure_rolls_back_job_registry_and_quota(creators, review_db):
    from fastapi.testclient import TestClient
    from modules.jobs.routes.presets import router
    from modules.jobs.models import PrintPreset
    client,auth,_,_=creators
    client.app.include_router(router,prefix="/api")
    preset=PrintPreset(name="Failure fixture")
    review_db.add(preset);review_db.commit()
    def state():
        return tuple(review_db.execute(text(sql)).scalar_one() for sql in (
            "SELECT count(*) FROM jobs", "SELECT count(*) FROM cost_identities",
            "SELECT coalesce(sum(jobs_used),0) FROM quota_usage WHERE user_id=3"))
    before=state();review_db.commit()
    review_db.execute(text("CREATE TRIGGER owned_fail_preset_identity BEFORE INSERT ON cost_identities WHEN NEW.resource_kind='job' BEGIN SELECT RAISE(ABORT, 'owned identity insert failure'); END"));review_db.commit()
    failing=TestClient(client.app,raise_server_exceptions=False)
    response=failing.post(f"/api/presets/{preset.id}/schedule",headers=auth())
    assert response.status_code == 500
    review_db.expire_all()
    assert state() == before


@pytest.mark.parametrize("operation",["create","delete","schedule"])
@pytest.mark.parametrize("scope",["read","write","write:jobs"])
def test_preset_mutations_require_actual_key_write_scope(creators,review_db,operation,scope):
    from modules.jobs.routes.presets import router
    from modules.jobs.models import PrintPreset
    client,_,token,_=creators
    client.app.include_router(router,prefix="/api")
    preset=PrintPreset(name="Owned scope preset")
    review_db.add(preset);review_db.commit()
    def snapshot():
        return (tuple(review_db.execute(text("SELECT id,name FROM print_presets ORDER BY id")).all()),tuple(review_db.execute(text("SELECT id,item_name FROM jobs ORDER BY id")).all()),tuple(review_db.execute(text("SELECT user_id,jobs_used FROM quota_usage ORDER BY user_id")).all()))
    before=snapshot();review_db.commit()
    method,path,body,status={"create":("POST","/api/presets",{"name":"New owned preset"},201),"delete":("DELETE",f"/api/presets/{preset.id}",None,204),"schedule":("POST",f"/api/presets/{preset.id}/schedule",None,200)}[operation]
    response=client.request(method,path,headers=token([scope]),**({"json":body} if body else {}))
    assert response.status_code==(403 if scope=="read" else status),response.text
    review_db.expire_all()
    if scope=="read":assert snapshot()==before
    else:assert snapshot()!=before
