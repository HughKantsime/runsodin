"""cert-D F04-4.2-02: a printer chosen in ODIN Studio (POST /api/prints) is an explicit pin."""
from datetime import datetime, timezone

from sqlalchemy import text

from tests.test_contracts.test_economic_creator_wiring import creators  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def _studio_upload(creators, review_db, tmp_path, monkeypatch, printer_id):
    from modules.models_library.routes import print_files
    client, auth, _, _ = creators
    client.app.include_router(print_files.router, prefix="/api")
    storage = tmp_path / "studio-uploads"; storage.mkdir()
    monkeypatch.setenv("PRINT_FILE_STORAGE_ROOT", str(storage))
    print_files.limiter._storage.reset()
    review_db.execute(text("UPDATE printers SET api_type='moonraker', model=NULL, machine_type=NULL"))
    review_db.commit()
    response = client.post("/api/prints", headers=auth(), data={"target_printer_id": str(printer_id), "note": "Studio pin"},
                           files={"file": ("studio-pin.gcode", b"; Synthetic queue-only G-code\nG28\n", "application/octet-stream")})
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


def test_studio_printer_choice_pins_the_job_and_survives_scheduling(creators, review_db, tmp_path, monkeypatch):
    from core.registry import registry
    from modules.jobs.pinning import pinned_printer_id
    from modules.jobs.scheduler import Scheduler, SchedulerConfig
    from modules.jobs.schemas import JobResponse
    from modules.jobs.models import Job
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    job_id = _studio_upload(creators, review_db, tmp_path, monkeypatch, 10)
    review_db.expire_all()
    assert pinned_printer_id(*review_db.execute(text("SELECT target_type,target_filter FROM jobs WHERE id=:id"),
                                                {"id": job_id}).one()) == 10
    job = review_db.get(Job, job_id)
    assert JobResponse.model_validate(job, from_attributes=True).pinned_printer_id == 10
    # With the pinned printer disabled the job waits; it is never placed on printer 9.
    review_db.execute(text("UPDATE jobs SET hold=FALSE, duration_hours=1 WHERE id=:id"), {"id": job_id})
    review_db.execute(text("UPDATE printers SET is_active=(id<>10)"))
    review_db.commit()
    Scheduler(SchedulerConfig(horizon_days=1)).run(review_db, start_date=datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    review_db.expire_all()
    status, printer = review_db.execute(text("SELECT status,printer_id FROM jobs WHERE id=:id"), {"id": job_id}).one()
    assert status == "pending" and printer in (None, 10)
