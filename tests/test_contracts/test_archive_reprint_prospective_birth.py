"""Actual JWT archive reprint prospective birth; no physical dispatch."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_archive_reprint_behavior import reprint_client
from tests.test_contracts.test_education_review_workflow import review_db


def test_archive_reprint_has_real_actor_owned_fresh_identity(reprint_client,review_db):
    from core.dependencies import get_current_user
    from core.auth import create_access_token
    client,_,_=reprint_client
    client.app.dependency_overrides.pop(get_current_user)
    response=client.post("/archives/101/reprint",headers={"Authorization":"Bearer "+create_access_token({"sub":"admin"})},json={"printer_id":9})
    assert response.status_code==200,response.text
    identifier=response.json()["job_id"]
    review_db.expire_all()
    row=review_db.execute(text("SELECT j.economic_uid,c.owner_scope,c.owner_key,c.resource_kind,c.resource_id FROM jobs j LEFT JOIN cost_identities c ON c.identity_uid=j.economic_uid WHERE j.id=:id"),{"id":identifier}).one()
    assert row[0]
    assert tuple(row[1:])==("org",1,"job",identifier)



def test_archive_reprint_registry_fault_rolls_back_job_and_registry(reprint_client,review_db):
    from core.dependencies import get_current_user
    from core.auth import create_access_token
    client,_,_=reprint_client
    client.app.dependency_overrides.pop(get_current_user)
    def counts():
        return tuple(review_db.execute(text("SELECT count(*) FROM "+table)).scalar_one() for table in ("jobs","cost_identities"))
    before=counts();review_db.commit()
    review_db.execute(text("CREATE TRIGGER owned_fail_reprint_registry BEFORE INSERT ON cost_identities WHEN NEW.resource_kind='job' BEGIN SELECT RAISE(ABORT, 'owned reprint registry failure'); END"));review_db.commit()
    response=client.post("/archives/101/reprint",headers={"Authorization":"Bearer "+create_access_token({"sub":"admin"})},json={"printer_id":9})
    assert response.status_code==500
    review_db.expire_all()
    assert counts()==before


@pytest.mark.parametrize("path",["legacy","v2"])
def test_unqualified_single_plate_ams_source_refuses_before_claim_or_transport(reprint_client,review_db,monkeypatch,path):
    from core import db as core_db
    from modules.printers import dispatch
    from modules.printers.telemetry import feature_flag
    client,_,_=reprint_client
    review_db.execute(text("UPDATE print_files SET filaments_json='[{\"type\":\"PLA\"}]' WHERE id=101"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(9,4,'PLA')"));review_db.commit()
    response=client.post("/archives/101/reprint",json={"printer_id":9,"plate_index":0,"ams_mapping":[{"source_slot":0,"target_slot":4}]})
    assert response.status_code==200,response.text
    identifier=response.json()["job_id"]
    monkeypatch.setattr(core_db,"engine",review_db.get_bind())
    monkeypatch.setattr(feature_flag,"is_v2_enabled",lambda:path=="v2")
    monkeypatch.setattr(dispatch,"_dispatch_bambu",lambda *a,**k:pytest.fail("Unqualified source touched transport"))
    ok,reason=dispatch.dispatch_job(9,identifier)
    assert not ok and "AMS source or plate is unsupported" in reason
    assert review_db.execute(text("SELECT count(*) FROM printer_dispatch_claims")).scalar_one()==0
    assert review_db.execute(text("SELECT status FROM jobs WHERE id=:id"),{"id":identifier}).scalar_one()=="pending"
