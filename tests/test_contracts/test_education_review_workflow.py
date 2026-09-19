from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _fact(value, member, key):
    return {
        "present": True,
        "recognized": True,
        "source_member": member,
        "source_key": key,
        "value": value,
    }


def _file_facts():
    return {
        "api_types": _fact(["bambu"], "Metadata/project_settings.config", "printer_model"),
        "machine": _fact("P1S", "Metadata/project_settings.config", "printer_model"),
        "bed": _fact(
            {"x_mm": 256.0, "y_mm": 256.0},
            "Metadata/project_settings.config",
            "bed_shape",
        ),
        "nozzle": _fact(0.4, "Metadata/project_settings.config", "nozzle_diameter"),
        "materials": _fact(["PLA"], "Metadata/project_settings.config", "filament_type"),
    }


@pytest.fixture()
def review_db(tmp_path: Path):
    from core.schema import bootstrap_database

    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO groups (id,name,is_org) VALUES (1,'school',1),(2,'other',1)")
        )
        connection.execute(
            text(
                "INSERT INTO users (id,username,email,password_hash,role,is_active,group_id) VALUES "
                "(1,'student','student@example.test','fixture','viewer',1,1),"
                "(2,'manager','manager@example.test','fixture','viewer',1,1),"
                "(3,'admin','admin@example.test','fixture','admin',1,1),"
                "(4,'outsider','outsider@example.test','fixture','viewer',1,1),"
                "(5,'foreign','foreign@example.test','fixture','admin',1,2)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_cost_centers "
                "(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) "
                "VALUES (7,1,'engineering','eng','Engineering','ENG','active',1,3)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_cost_center_grants "
                "(org_id,cost_center_id,user_id,role,state,granted_by) VALUES "
                "(1,7,1,'student','active',3),(1,7,2,'manager','active',3)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO printers "
                "(id,name,model,machine_type,is_active,api_type,bed_x_mm,bed_y_mm,"
                "nozzle_diameter,org_id) VALUES "
                "(9,'P1S-09','Bambu Lab P1S','P1S',1,'bambu',256,256,0.4,1),"
                "(10,'P1S-10','Bambu Lab P1S','P1S',1,'bambu',256,256,0.6,1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO filament_slots (printer_id,slot_number,filament_type) VALUES "
                "(9,1,'PLA'),(10,1,'PLA')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_cost_center_printers "
                "(org_id,cost_center_id,printer_id,state,granted_by) VALUES "
                "(1,7,9,'active',3),(1,7,10,'active',3)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_upload_operations "
                "(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,"
                "expected_bytes,expected_hash,reservation_released_at) "
                "VALUES ('upload-1',1,1,'committed',100,100,100,'digest',CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO print_files "
                "(id,filename,original_filename,project_name,stored_path,org_id,created_by,"
                "storage_bytes,blob_state,compatibility_facts_json) "
                "VALUES (11,'upload-1.3mf','gear.3mf','Class Gear','/opaque/upload-1.3mf',"
                "1,1,100,'present',:facts)"
            ),
            {"facts": json.dumps(_file_facts(), sort_keys=True)},
        )
        connection.execute(
            text(
                "INSERT INTO models (id,name,print_file_id,category,org_id) "
                "VALUES (12,'Class Gear',11,'Education Submission',1)"
            )
        )
        connection.execute(text("UPDATE print_files SET model_id=12 WHERE id=11"))
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(id,model_id,item_name,status,submitted_by,charged_to_user_id,charged_to_org_id) "
                "VALUES (13,12,'Class Gear','submitted',1,1,1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO education_submissions "
                "(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,"
                "submitted_by,status,lifecycle_revision) "
                "VALUES (14,1,'upload-1',13,11,12,7,1,'submitted',1)"
            )
        )
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _principal(user_id, role="viewer", org_id=1):
    return {
        "id": user_id,
        "username": f"user-{user_id}",
        "role": role,
        "group_id": org_id,
        "is_active": True,
        "_auth_kind": "session_jwt",
    }


def test_private_projection_respects_student_manager_admin_and_tenant(review_db):
    from modules.organizations.education_review_service import list_visible_submissions

    assert len(list_visible_submissions(review_db, principal=_principal(1))["items"]) == 1
    assert len(list_visible_submissions(review_db, principal=_principal(2))["items"]) == 1
    assert len(
        list_visible_submissions(review_db, principal=_principal(3, role="admin"))["items"]
    ) == 1
    assert list_visible_submissions(review_db, principal=_principal(4))["items"] == []
    assert list_visible_submissions(
        review_db, principal=_principal(5, role="admin", org_id=2)
    )["items"] == []
    item = list_visible_submissions(review_db, principal=_principal(2))["items"][0]
    assert "filename" not in item
    assert "email" not in item
    assert item["submitter_username"] == "student"


def test_manager_approval_is_atomic_audited_notified_and_replayable(review_db):
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval

    body = SubmissionApproval(revision=1, printer_id=9, command_id=uuid.uuid4())
    result = approve_submission(
        review_db,
        submission_id=14,
        body=body,
        principal=_principal(2),
    )
    assert result["status"] == "pending"
    assert result["approved_printer_id"] == 9
    assert result["lifecycle_revision"] == 2
    assert result["compatibility"]["compatible"] is True
    states = review_db.execute(
        text(
            "SELECT s.status,s.lifecycle_revision,s.approved_printer_id,j.status AS job_status,"
            "j.printer_id FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=14"
        )
    ).one()
    assert tuple(states) == ("pending", 2, 9, "pending", 9)
    assert review_db.execute(
        text("SELECT COUNT(*) FROM education_audit_events")
    ).scalar_one() == 1
    assert review_db.execute(
        text("SELECT COUNT(*) FROM education_notification_outbox")
    ).scalar_one() == 1

    replay = approve_submission(
        review_db,
        submission_id=14,
        body=body,
        principal=_principal(2),
    )
    assert replay == result
    assert review_db.execute(
        text("SELECT COUNT(*) FROM education_audit_events")
    ).scalar_one() == 1


def test_incompatible_approval_fails_closed_without_partial_rows(review_db):
    from core.errors import OdinError
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval

    with pytest.raises(OdinError) as rejected:
        approve_submission(
            review_db,
            submission_id=14,
            body=SubmissionApproval(revision=1, printer_id=10, command_id=uuid.uuid4()),
            principal=_principal(2),
        )
    assert rejected.value.code.value == "resource_conflict"
    assert review_db.execute(
        text("SELECT status FROM education_submissions WHERE id=14")
    ).scalar_one() == "submitted"
    assert review_db.execute(text("SELECT status FROM jobs WHERE id=13")).scalar_one() == "submitted"
    assert review_db.execute(text("SELECT COUNT(*) FROM education_commands")).scalar_one() == 0
    assert review_db.execute(text("SELECT COUNT(*) FROM education_audit_events")).scalar_one() == 0
    assert review_db.execute(
        text("SELECT COUNT(*) FROM education_notification_outbox")
    ).scalar_one() == 0


def test_shared_printer_and_cross_submission_command_reuse_fail_closed(review_db):
    from core.errors import OdinError
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval

    review_db.execute(text("UPDATE printers SET shared=1 WHERE id=9"))
    review_db.commit()
    with pytest.raises(OdinError) as shared:
        approve_submission(
            review_db,
            submission_id=14,
            body=SubmissionApproval(revision=1, printer_id=9, command_id=uuid.uuid4()),
            principal=_principal(2),
        )
    assert shared.value.code.value == "not_found"
    review_db.execute(text("UPDATE printers SET shared=0 WHERE id=9"))
    review_db.commit()

    command_id = uuid.uuid4()
    body = SubmissionApproval(revision=1, printer_id=9, command_id=command_id)
    approve_submission(
        review_db,
        submission_id=14,
        body=body,
        principal=_principal(2),
    )
    review_db.execute(
        text(
            "INSERT INTO education_upload_operations "
            "(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,expected_bytes,"
            "expected_hash,reservation_released_at) "
            "VALUES ('upload-2',1,1,'committed',100,100,100,'digest2',CURRENT_TIMESTAMP)"
        )
    )
    review_db.execute(
        text(
            "INSERT INTO print_files "
            "(id,filename,original_filename,project_name,stored_path,org_id,created_by,storage_bytes,"
            "blob_state,compatibility_facts_json) SELECT 21,'upload-2.3mf','gear-2.3mf',"
            "'Class Gear 2','/opaque/upload-2.3mf',org_id,created_by,storage_bytes,blob_state,"
            "compatibility_facts_json FROM print_files WHERE id=11"
        )
    )
    review_db.execute(
        text(
            "INSERT INTO models (id,name,print_file_id,category,org_id) "
            "VALUES (22,'Class Gear 2',21,'Education Submission',1)"
        )
    )
    review_db.execute(text("UPDATE print_files SET model_id=22 WHERE id=21"))
    review_db.execute(
        text(
            "INSERT INTO jobs "
            "(id,model_id,item_name,status,submitted_by,charged_to_user_id,charged_to_org_id) "
            "VALUES (23,22,'Class Gear 2','submitted',1,1,1)"
        )
    )
    review_db.execute(
        text(
            "INSERT INTO education_submissions "
            "(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,"
            "status,lifecycle_revision) VALUES (24,1,'upload-2',23,21,22,7,1,'submitted',1)"
        )
    )
    review_db.commit()
    with pytest.raises(OdinError) as reused:
        approve_submission(
            review_db,
            submission_id=24,
            body=body,
            principal=_principal(2),
        )
    assert reused.value.code.value == "idempotency_conflict"
    assert review_db.execute(
        text("SELECT status FROM education_submissions WHERE id=24")
    ).scalar_one() == "submitted"


def test_rejection_records_reason_only_on_job_and_hides_cross_tenant(review_db):
    from core.errors import OdinError
    from modules.organizations.education_review_service import reject_submission
    from modules.organizations.education_schemas import SubmissionRejection

    with pytest.raises(OdinError) as hidden:
        reject_submission(
            review_db,
            submission_id=14,
            body=SubmissionRejection(revision=1, reason="No", command_id=uuid.uuid4()),
            principal=_principal(5, role="admin", org_id=2),
        )
    assert hidden.value.code.value == "not_found"

    result = reject_submission(
        review_db,
        submission_id=14,
        body=SubmissionRejection(
            revision=1,
            reason="Needs stronger wall thickness",
            command_id=uuid.uuid4(),
        ),
        principal=_principal(2),
    )
    assert result["status"] == "rejected"
    row = review_db.execute(
        text(
            "SELECT s.status,s.lifecycle_revision,s.approved_printer_id,j.status AS job_status,"
            "j.rejected_reason FROM education_submissions s JOIN jobs j ON j.id=s.job_id "
            "WHERE s.id=14"
        )
    ).one()
    assert tuple(row) == (
        "rejected",
        2,
        None,
        "rejected",
        "Needs stronger wall thickness",
    )
    details = json.loads(
        review_db.execute(text("SELECT details_json FROM education_audit_events")).scalar_one()
    )
    assert details["reason_recorded"] is True
    assert "Needs stronger wall thickness" not in json.dumps(details)


def test_stale_revision_and_revoked_manager_leave_graph_unchanged(review_db):
    from core.errors import OdinError
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval

    with pytest.raises(OdinError) as stale:
        approve_submission(
            review_db,
            submission_id=14,
            body=SubmissionApproval(revision=2, printer_id=9, command_id=uuid.uuid4()),
            principal=_principal(2),
        )
    assert stale.value.code.value == "revision_conflict"
    review_db.execute(
        text(
            "UPDATE education_cost_center_grants SET state='revoked',revoked_by=3,"
            "revoked_at=CURRENT_TIMESTAMP WHERE cost_center_id=7 AND user_id=2 AND role='manager'"
        )
    )
    review_db.commit()
    with pytest.raises(OdinError) as revoked:
        approve_submission(
            review_db,
            submission_id=14,
            body=SubmissionApproval(revision=1, printer_id=9, command_id=uuid.uuid4()),
            principal=_principal(2),
        )
    assert revoked.value.code.value == "not_found"
    assert review_db.execute(
        text("SELECT status FROM education_submissions WHERE id=14")
    ).scalar_one() == "submitted"
