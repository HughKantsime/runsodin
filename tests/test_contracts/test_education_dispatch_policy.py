from __future__ import annotations

import hashlib
import json
import pytest
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def _fact(value, key):
    return {
        "present": value is not None and value != [],
        "recognized": True,
        "source_member": "Metadata/project_settings.config",
        "source_key": key,
        "value": value,
    }


def _job(facts):
    return {
        "file_hash": hashlib.sha256(b"fixture").hexdigest(),
        "id": 72,
        "item_name": "class project",
        "status": "scheduled",
        "printer_id": 9,
        "education_submission_id": 4,
        "education_lifecycle_revision": 3,
        "compatibility_facts_json": json.dumps(facts),
    }


def _facts():
    return {
        "api_types": _fact(["bambu"], "printer_model"),
        "machine": _fact("X1C", "printer_model"),
        "bed": _fact({"x_mm": 256, "y_mm": 256}, "printer_model"),
        "nozzle": _fact(0.4, "nozzle_diameter"),
        "materials": _fact(["PLA"], "filament_type"),
    }


def _printer(**changes):
    result = {
        "api_type": "bambu",
        "machine_type": "X1C",
        "bed_x_mm": 256,
        "bed_y_mm": 256,
        "nozzle_diameter": 0.4,
        "active_materials": ["PLA"],
    }
    result.update(changes)
    return result


class _Policy:
    def __init__(self, authorized=True):
        self.authorized = authorized

    def authorize_dispatch(self, db, *, job_id, printer_id, expected_revision):
        return {
            "id": 4,
            "lifecycle_revision": expected_revision,
            "authorized": self.authorized,
        }


def test_education_dispatch_uses_live_slot_facts_and_reconciles_drift(monkeypatch):
    from core.registry import registry
    from modules.printers import dispatch

    policy = _Policy()
    monkeypatch.setattr(registry, "get_provider", lambda name: policy)
    reconciled = []
    monkeypatch.setattr(
        dispatch,
        "_reconcile_education_denial",
        lambda provider, context, job_id, expected_revision, reason: reconciled.append(
            (job_id, expected_revision, reason)
        ),
    )

    allowed = dispatch._authorize_education_hardware_action(_job(_facts()), _printer())
    denied = dispatch._authorize_education_hardware_action(
        _job(_facts()), _printer(active_materials=["PETG"])
    )

    assert allowed == (True, "education-compatibility-v1")
    assert denied[0] is False
    assert "material_mismatch" in denied[1]
    assert reconciled and reconciled[0][0] == 72
    assert reconciled[0][1] == 3


def test_stale_dispatch_reconciliation_uses_revision_loaded_by_worker(monkeypatch):
    from core.registry import registry
    from modules.printers import dispatch

    class StalePolicy(_Policy):
        def authorize_dispatch(self, db, *, job_id, printer_id, expected_revision):
            return {
                "id": 4,
                "lifecycle_revision": expected_revision + 1,
                "authorized": False,
            }

    monkeypatch.setattr(registry, "get_provider", lambda name: StalePolicy())
    calls = []
    monkeypatch.setattr(
        dispatch,
        "_reconcile_education_denial",
        lambda provider, context, job_id, expected_revision, reason: calls.append(
            (context["lifecycle_revision"], expected_revision)
        ),
    )

    denied = dispatch._authorize_education_hardware_action(_job(_facts()), _printer())
    assert denied[0] is False
    assert calls == [(4, 3)]


def test_dispatch_denial_occurs_before_any_hardware_adapter(actual_education_dispatch, monkeypatch):
    from modules.printers import dispatch, dispatch_acquisition, smart_plug
    db, uploaded = actual_education_dispatch
    db.execute(text("UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9"))
    db.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("Denied Education authority must not perform device operations")
    monkeypatch.setattr(dispatch, "_dispatch_bambu", forbidden)
    monkeypatch.setattr(smart_plug, "prepare_bounded_dispatch_power", forbidden)
    monkeypatch.setattr(dispatch_acquisition, "bounded_status_is_idle", forbidden)
    ok, reason = dispatch.dispatch_job(9, uploaded["job_id"])
    assert not ok and "entitlement changed" in reason
    assert db.execute(text("SELECT count(*) FROM printer_dispatch_claims")).scalar_one() == 0


@pytest.fixture
def actual_education_dispatch(review_db, tmp_path, monkeypatch):
    from tests.test_contracts.test_education_connected_print_workflow import setup_workflow
    from modules.printers import dispatch
    uploaded = setup_workflow(review_db, tmp_path, monkeypatch, "legacy", 1)
    monkeypatch.setattr(dispatch, "_ws", lambda *args: None)
    monkeypatch.setattr(dispatch, "_notify_start_outcome", lambda *args, **kwargs: None)
    return review_db, uploaded


def test_education_dispatch_uses_reserved_filename_and_requires_confirmation(actual_education_dispatch, monkeypatch):
    from modules.printers import dispatch
    db, uploaded = actual_education_dispatch
    calls = []
    def adapter(job_id, path, filename, creds, **options):
        assert Path(path).is_file()
        assert db.execute(text("SELECT state FROM printer_dispatch_claims WHERE job_id=:id"), {"id": job_id}).scalar_one() == "adapter_entered"
        db.rollback()
        calls.append(filename)
        return True, "Print started"
    monkeypatch.setattr(dispatch, "_dispatch_bambu", adapter)
    assert dispatch.dispatch_job(9, uploaded["job_id"]) == (True, "Print started")
    assert len(calls) == 1 and calls[0].startswith("odin-") and calls[0].endswith(".3mf")
    assert db.execute(text("SELECT s.status,j.status FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id"), {"id": uploaded["id"]}).one() == ("printing", "printing")
    assert db.execute(text("SELECT state FROM printer_dispatch_claims")).scalar_one() == "accepted"


def test_education_adapter_failure_cancels_only_its_reservation(actual_education_dispatch, monkeypatch):
    from modules.printers import dispatch
    db, uploaded = actual_education_dispatch
    from modules.organizations.services import EducationPolicyService
    db.execute(text("UPDATE jobs SET status='scheduled',printer_id=10 WHERE id=13"))
    db.execute(text("UPDATE education_submissions SET status='scheduled',approved_printer_id=10 WHERE id=14"))
    unrelated = EducationPolicyService().reserve_dispatch(db, job_id=13, printer_id=10, expected_revision=1, extension=".3mf")
    assert unrelated
    db.commit()
    monkeypatch.setattr(dispatch, "_dispatch_bambu", lambda *args, **kwargs: (False, "upload failed"))
    ok, reason = dispatch.dispatch_job(9, uploaded["job_id"])
    assert not ok and "upload failed" in reason and "reconciliation" in reason
    retained = db.execute(text("SELECT claim_id,job_id,printer_id,state FROM education_monitor_claims")).all()
    assert retained == [(unrelated["claim_id"], 13, 10, "reserved")]
    assert db.execute(text("SELECT state FROM printer_dispatch_claims")).scalar_one() == "uncertain"


def test_physical_success_with_lost_education_cas_is_not_clean_success(actual_education_dispatch, monkeypatch):
    from modules.printers import dispatch
    db, uploaded = actual_education_dispatch
    monkeypatch.setattr(dispatch, "_dispatch_bambu", lambda *args, **kwargs: (True, "Print started"))
    monkeypatch.setattr(dispatch, "_confirm_education_dispatch", lambda *args: {"transitioned": False})
    ok, reason = dispatch.dispatch_job(9, uploaded["job_id"])
    assert not ok and "physical action requires reconciliation" in reason
    assert db.execute(text("SELECT state FROM education_monitor_claims")).scalar_one() == "reserved"
    assert db.execute(text("SELECT state FROM printer_dispatch_claims")).scalar_one() == "uncertain"


def test_central_policy_authorizes_current_pin_and_atomically_reconciles():
    from modules.organizations.education_policy import (
        authorize_dispatch,
        reconcile_dispatch_denial,
    )

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE printers (id INTEGER PRIMARY KEY, org_id INTEGER, is_active BOOLEAN, shared BOOLEAN)",
            "CREATE TABLE education_cost_centers (id INTEGER PRIMARY KEY, org_id INTEGER, state TEXT)",
            "CREATE TABLE education_cost_center_printers (org_id INTEGER, cost_center_id INTEGER, printer_id INTEGER, state TEXT)",
            "CREATE TABLE jobs (id INTEGER PRIMARY KEY, charged_to_org_id INTEGER, status TEXT, printer_id INTEGER, notes TEXT)",
            "CREATE TABLE education_submissions (id INTEGER PRIMARY KEY, org_id INTEGER, cost_center_id INTEGER, job_id INTEGER, status TEXT, lifecycle_revision INTEGER, approved_printer_id INTEGER, approved_by INTEGER, compatibility_engine_version TEXT, updated_at TEXT)",
            "INSERT INTO printers VALUES (9, 2, 1, 0)",
            "INSERT INTO education_cost_centers VALUES (7, 2, 'active')",
            "INSERT INTO education_cost_center_printers VALUES (2, 7, 9, 'active')",
            "INSERT INTO jobs VALUES (72, 2, 'scheduled', 9, NULL)",
            "INSERT INTO education_submissions VALUES (4, 2, 7, 72, 'scheduled', 3, 9, 5, 'old-engine', NULL)",
        ):
            connection.execute(text(statement))

    with Session(engine) as db:
        context = authorize_dispatch(db, job_id=72, printer_id=9, expected_revision=3)
        assert context and context["authorized"] is True
        assert reconcile_dispatch_denial(
            db,
            submission_id=4,
            job_id=72,
            expected_revision=2,
            reason="stale worker",
        ) is False
        current = db.execute(
            text(
                "SELECT status, lifecycle_revision FROM education_submissions WHERE id=4"
            )
        ).one()
        assert tuple(current) == ("scheduled", 3)
        assert reconcile_dispatch_denial(
            db,
            submission_id=4,
            job_id=72,
            expected_revision=3,
            reason="slot changed",
        ) is True

    with engine.connect() as connection:
        submission = connection.execute(
            text(
                "SELECT status, lifecycle_revision, approved_printer_id, "
                "compatibility_engine_version FROM education_submissions WHERE id=4"
            )
        ).one()
        job = connection.execute(
            text("SELECT status, printer_id, notes FROM jobs WHERE id=72")
        ).one()
    assert tuple(submission) == ("submitted", 4, None, None)
    assert job.status == "submitted"
    assert job.printer_id is None
    assert "slot changed" in job.notes


@pytest.mark.parametrize("obstacle", ["changed", "missing_hash", "invalid_hash", "directory"])
def test_education_dispatch_rejects_unverified_contents_before_reservation(actual_education_dispatch, monkeypatch, obstacle):
    from modules.printers import dispatch, dispatch_acquisition, smart_plug
    db, uploaded = actual_education_dispatch
    job = dispatch._load_job(uploaded["job_id"])
    path = Path(job["stored_path"])
    if obstacle == "changed":
        with path.open("ab") as target: target.write(b"changed-after-approval")
    elif obstacle == "directory":
        path.unlink()
        path.mkdir()
    else:
        db.execute(text("UPDATE print_files SET file_hash=:hash WHERE id=:id"),
                   {"hash": None if obstacle == "missing_hash" else "invalid", "id": job["resolved_print_file_id"]})
        db.commit()
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid payload must not perform device operations")
    monkeypatch.setattr(dispatch, "_dispatch_bambu", forbidden)
    monkeypatch.setattr(smart_plug, "prepare_bounded_dispatch_power", forbidden)
    monkeypatch.setattr(dispatch_acquisition, "bounded_status_is_idle", forbidden)
    ok, reason = dispatch.dispatch_job(9, uploaded["job_id"])
    assert not ok
    assert ("integrity" in reason or (obstacle == "directory" and "regular" in reason)
            or (obstacle == "changed" and "payload changed" in reason))
    assert db.execute(text("SELECT count(*) FROM printer_dispatch_claims")).scalar_one() == 0
    assert db.execute(text("SELECT count(*) FROM education_monitor_claims")).scalar_one() == 0


@pytest.mark.parametrize('mode', ['lost_ack', 'exception'])
def test_uncertain_print_command_preserves_education_reservation(actual_education_dispatch, monkeypatch, mode):
    from modules.printers import dispatch
    db, uploaded = actual_education_dispatch
    commands = []
    def accepted_without_ack(*args):
        commands.append("accepted")
        if mode == "exception":
            raise TimeoutError("synthetic acknowledgment lost")
        return False
    monkeypatch.setattr(dispatch, "_dispatch_bambu", lambda *args, **kwargs: (dispatch._submit_print_command(accepted_without_ack), "Print started"))
    ok, reason = dispatch.dispatch_job(9, uploaded["job_id"])
    assert not ok and "reconciliation" in reason and commands == ["accepted"]
    assert db.execute(text("SELECT state FROM education_monitor_claims")).scalar_one() == "reserved"
    assert db.execute(text("SELECT state FROM printer_dispatch_claims")).scalar_one() == "uncertain"
