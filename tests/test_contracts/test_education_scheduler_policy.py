from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _fact(value, key):
    return {
        "present": True,
        "recognized": True,
        "source_member": "Metadata/project_settings.config",
        "source_key": key,
        "value": value,
    }


def _file_facts():
    return {
        "api_types": _fact(["bambu"], "printer_model"),
        "machine": _fact("P1S", "printer_model"),
        "bed": _fact({"x_mm": 256.0, "y_mm": 256.0}, "bed_shape"),
        "nozzle": _fact(0.4, "nozzle_diameter"),
        "materials": _fact(["PLA"], "filament_type"),
    }


@pytest.fixture()
def scheduler_db(tmp_path: Path):
    from core.schema import bootstrap_database

    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        statements = (
            "INSERT INTO groups (id,name,is_org) VALUES (1,'school',1)",
            "INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) "
            "VALUES (1,'student','student@example.test','fixture','viewer',1,1),"
            "(2,'teacher','teacher@example.test','fixture','viewer',1,1)",
            "INSERT INTO education_cost_centers "
            "(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) "
            "VALUES (7,1,'engineering','eng','Engineering','ENG','active',1,2)",
            "INSERT INTO printers "
            "(id,name,model,machine_type,is_active,api_type,bed_x_mm,bed_y_mm,"
            "nozzle_diameter,org_id,shared) VALUES "
            "(9,'P1S-09','Bambu Lab P1S','P1S',1,'bambu',256,256,0.4,1,0),"
            "(10,'P1S-10','Bambu Lab P1S','P1S',1,'bambu',256,256,0.4,1,0)",
            "INSERT INTO filament_slots (printer_id,slot_number,filament_type,color) "
            "VALUES (9,1,'PLA','orange'),(10,1,'PLA','blue')",
            "INSERT INTO education_cost_center_printers "
            "(org_id,cost_center_id,printer_id,state,granted_by) "
            "VALUES (1,7,9,'active',2)",
            "INSERT INTO education_upload_operations "
            "(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,"
            "expected_bytes,expected_hash,reservation_released_at) "
            "VALUES ('upload-scheduler',1,1,'committed',100,100,100,'digest',CURRENT_TIMESTAMP)",
            "INSERT INTO models (id,name,category,org_id) "
            "VALUES (12,'Class Gear','Education Submission',1)",
            "INSERT INTO jobs "
            "(id,model_id,item_name,quantity,status,printer_id,duration_hours,colors_required,"
            "submitted_by,approved_by,charged_to_user_id,charged_to_org_id,hold,priority) "
            "VALUES (13,12,'Class Gear',1,'pending',9,1.0,'blue',1,2,1,1,0,3)",
        )
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO print_files "
                "(id,filename,original_filename,project_name,stored_path,org_id,created_by,"
                "storage_bytes,blob_state,compatibility_facts_json,model_id) "
                "VALUES (11,'upload.3mf','gear.3mf','Class Gear','/opaque/upload.3mf',"
                "1,1,100,'present',:facts,12)"
            ),
            {"facts": json.dumps(_file_facts(), sort_keys=True)},
        )
        connection.execute(text("UPDATE models SET print_file_id=11 WHERE id=12"))
        connection.execute(
            text(
                "INSERT INTO education_submissions "
                "(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,"
                "submitted_by,status,lifecycle_revision,approved_printer_id,approved_by,"
                "compatibility_engine_version) "
                "VALUES (14,1,'upload-scheduler',13,11,12,7,1,'pending',2,9,2,"
                "'education-compatibility-v1')"
            )
        )
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _install_policy(monkeypatch):
    from modules.jobs import scheduler
    from modules.organizations.services import EducationPolicyService

    policy = EducationPolicyService()
    monkeypatch.setattr(scheduler.registry, "get_provider", lambda name: policy)
    return policy


def _run(scheduler_db):
    from modules.jobs.scheduler import Scheduler

    return Scheduler().run(
        scheduler_db,
        start_date=datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc),
    )


def test_scheduler_uses_only_teacher_approved_printer_and_advances_revision(
    scheduler_db, monkeypatch
):
    _install_policy(monkeypatch)

    result = _run(scheduler_db)

    assert result.success is True
    assert result.scheduled_count == 1
    assert [assignment.printer_id for assignment in result.assignments] == [9]
    submission = scheduler_db.execute(
        text(
            "SELECT status,lifecycle_revision,approved_printer_id "
            "FROM education_submissions WHERE id=14"
        )
    ).one()
    job = scheduler_db.execute(
        text("SELECT status,printer_id FROM jobs WHERE id=13")
    ).one()
    audit = scheduler_db.execute(
        text(
            "SELECT action,lifecycle_revision FROM education_audit_events "
            "WHERE resource_id='14'"
        )
    ).one()
    assert tuple(submission) == ("scheduled", 3, 9)
    assert tuple(job) == ("scheduled", 9)
    assert tuple(audit) == ("submission.schedule", 3)
    assert scheduler_db.execute(
        text("SELECT COUNT(*) FROM education_notification_outbox")
    ).scalar_one() == 1


def test_scheduler_reconciles_revoked_entitlement_without_reassignment(
    scheduler_db, monkeypatch
):
    _install_policy(monkeypatch)
    scheduler_db.execute(
        text(
            "UPDATE education_cost_center_printers SET state='revoked' "
            "WHERE cost_center_id=7 AND printer_id=9"
        )
    )

    result = _run(scheduler_db)

    assert result.scheduled_count == 0
    assert result.skipped_count == 1
    submission = scheduler_db.execute(
        text(
            "SELECT status,lifecycle_revision,approved_printer_id "
            "FROM education_submissions WHERE id=14"
        )
    ).one()
    job = scheduler_db.execute(
        text("SELECT status,printer_id,notes FROM jobs WHERE id=13")
    ).one()
    assert tuple(submission) == ("submitted", 3, None)
    assert job.status == "submitted"
    assert job.printer_id is None
    assert "scheduler_policy_denied" in job.notes


def test_schedule_cas_miss_rolls_back_both_rows_and_emits_nothing(
    scheduler_db, monkeypatch
):
    policy = _install_policy(monkeypatch)

    assert policy.advance_schedule(
        scheduler_db,
        submission_id=14,
        job_id=13,
        printer_id=9,
        expected_revision=1,
    ) is False

    assert tuple(
        scheduler_db.execute(
            text("SELECT status,printer_id FROM jobs WHERE id=13")
        ).one()
    ) == ("pending", 9)
    assert tuple(
        scheduler_db.execute(
            text(
                "SELECT status,lifecycle_revision,approved_printer_id "
                "FROM education_submissions WHERE id=14"
            )
        ).one()
    ) == ("pending", 2, 9)
    assert scheduler_db.execute(
        text("SELECT COUNT(*) FROM education_audit_events")
    ).scalar_one() == 0
    assert scheduler_db.execute(
        text("SELECT COUNT(*) FROM education_notification_outbox")
    ).scalar_one() == 0


def test_stale_cleanup_preserves_valid_pin_and_increments_revision(
    scheduler_db, monkeypatch
):
    from modules.jobs.scheduler import Scheduler

    _install_policy(monkeypatch)
    stale_start = datetime.now(timezone.utc) - timedelta(hours=3)
    scheduler_db.execute(
        text(
            "UPDATE education_submissions SET status='scheduled',lifecycle_revision=3 WHERE id=14"
        )
    )
    scheduler_db.execute(
        text(
            "UPDATE jobs SET status='scheduled',scheduled_start=:start WHERE id=13"
        ),
        {"start": stale_start},
    )

    assert Scheduler()._cleanup_stale_schedules(scheduler_db) == 1

    submission = scheduler_db.execute(
        text(
            "SELECT status,lifecycle_revision,approved_printer_id "
            "FROM education_submissions WHERE id=14"
        )
    ).one()
    job = scheduler_db.execute(
        text("SELECT status,printer_id,scheduled_start FROM jobs WHERE id=13")
    ).one()
    assert tuple(submission) == ("pending", 4, 9)
    assert job.status == "pending"
    assert job.printer_id == 9
    assert job.scheduled_start is None


def test_stale_cleanup_reconciles_compatibility_drift(scheduler_db, monkeypatch):
    from modules.jobs.scheduler import Scheduler

    _install_policy(monkeypatch)
    scheduler_db.execute(
        text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=9")
    )
    scheduler_db.execute(
        text(
            "UPDATE education_submissions SET status='scheduled',lifecycle_revision=3 WHERE id=14"
        )
    )
    scheduler_db.execute(
        text(
            "UPDATE jobs SET status='scheduled',scheduled_start=:start WHERE id=13"
        ),
        {"start": datetime.now(timezone.utc) - timedelta(hours=3)},
    )

    assert Scheduler()._cleanup_stale_schedules(scheduler_db) == 1

    submission = scheduler_db.execute(
        text(
            "SELECT status,lifecycle_revision,approved_printer_id "
            "FROM education_submissions WHERE id=14"
        )
    ).one()
    job = scheduler_db.execute(
        text("SELECT status,printer_id,notes FROM jobs WHERE id=13")
    ).one()
    assert tuple(submission) == ("submitted", 4, None)
    assert job.status == "submitted"
    assert job.printer_id is None
    assert "material_mismatch" in job.notes


def test_missing_policy_provider_fails_closed_for_education_job(
    scheduler_db, monkeypatch
):
    from modules.jobs import scheduler

    monkeypatch.setattr(scheduler.registry, "get_provider", lambda name: None)

    result = _run(scheduler_db)

    assert result.scheduled_count == 0
    assert result.skipped_count == 1
    assert "policy unavailable" in result.errors[0]
    assert tuple(
        scheduler_db.execute(
            text("SELECT status,printer_id FROM jobs WHERE id=13")
        ).one()
    ) == ("pending", 9)
    assert tuple(
        scheduler_db.execute(
            text(
                "SELECT status,lifecycle_revision,approved_printer_id "
                "FROM education_submissions WHERE id=14"
            )
        ).one()
    ) == ("pending", 2, 9)
