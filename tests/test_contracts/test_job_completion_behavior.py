"""Actual completion, committed deductions and real local Spoolman delivery."""
import json
import threading
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_spoolman_delivery_behavior import spoolman_peer

@pytest.mark.parametrize("peer_status", [200,503])
def test_completion_commits_once_and_pushes_once(review_db, spoolman_peer, monkeypatch, peer_status):
    from core import db as core_db
    from core.dependencies import get_current_user
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    from modules.jobs.routes import jobs_lifecycle
    services, received, status = spoolman_peer
    status[0] = peer_status
    review_db.execute(text("INSERT INTO models(id,name,org_id,color_requirements) VALUES(101,'Fixture model',1,:req)"), {"req":json.dumps({"slot1":{"grams":12.75}})})
    review_db.execute(text("INSERT INTO jobs(id,item_name,model_id,printer_id,status,charged_to_org_id,quantity,hold) VALUES(101,'Fixture print',101,9,'printing',1,1,0)"))
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,status,location_printer_id,location_slot,remaining_weight_g,initial_weight_g,spoolman_spool_id,org_id) VALUES(101,101,'active',9,1,100,100,42,1)"))
    # Spec item 3: location alone is not a binding or proof of the used tray.
    review_db.execute(text("UPDATE filament_slots SET assigned_spool_id=101 WHERE printer_id=9 AND slot_number=1"))
    review_db.execute(text("UPDATE jobs SET ams_mapping='[0]' WHERE id=101"))
    review_db.commit()
    binding = review_db.get_bind()
    monkeypatch.setattr(core_db, "SessionLocal", sessionmaker(bind=binding))
    app = FastAPI(); app.include_router(jobs_lifecycle.router)
    def request_db():
        with Session(binding) as session: yield session
    app.dependency_overrides[core_db.get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: {"id":3,"role":"operator","group_id":1,"is_active":True}
    original_thread = threading.Thread
    background, committed = [], []
    def new_thread(*args, **kwargs):
        target = kwargs.get("target")
        if target and target.__name__ == "_push_async":
            def checked_target(*a):
                with Session(binding) as fresh:
                    committed.append(fresh.execute(text("SELECT remaining_weight_g FROM spools WHERE id=101")).scalar_one())
                target(*a)
            kwargs["target"] = checked_target
            result = original_thread(*args, **kwargs)
            background.append(result)
            return result
        return original_thread(*args, **kwargs)
    monkeypatch.setattr(threading, "Thread", new_thread)
    with TestClient(app) as client:
        first = client.post("/jobs/101/complete")
        assert first.status_code == 200, first.text
        for worker in background:
            worker.join(timeout=10)
            assert not worker.is_alive()
        retry = client.post("/jobs/101/complete")
        assert retry.status_code == 200, retry.text
    assert len(background) == 1
    assert committed == [87.25]
    assert received == [("/api/v1/spool/42/use", {"use_weight":12.75})]
    with Session(binding) as fresh:
        assert fresh.execute(text("SELECT remaining_weight_g FROM spools WHERE id=101")).scalar_one() == 87.25
        assert fresh.execute(text("SELECT count(*) FROM spool_usage WHERE job_id=101")).scalar_one() == 1
        assert fresh.execute(text("SELECT weight_used_g FROM spool_usage WHERE job_id=101")).scalar_one() == 12.75
        state, notes = fresh.execute(text("SELECT status,notes FROM jobs WHERE id=101")).one()
        assert state == "completed"
        assert ("Spoolman push failed" in notes) == (peer_status == 503)
        assert "127.0.0.1" not in notes and "HTTP 503" not in notes
