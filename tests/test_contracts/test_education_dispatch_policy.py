from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


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


def test_dispatch_denial_occurs_before_any_hardware_adapter(monkeypatch, tmp_path):
    from modules.printers import dispatch

    print_file = tmp_path / "project.3mf"
    print_file.write_bytes(b"fixture")
    job = _job(_facts()) | {
        "stored_path": str(print_file),
        "original_filename": "project.3mf",
        "bed_x_mm": 256,
        "bed_y_mm": 256,
        "compatible_api_types": "bambu",
    }
    monkeypatch.setattr(dispatch, "_load_job", lambda job_id: job)
    monkeypatch.setattr(dispatch, "_get_printer_info", lambda printer_id: _printer(ip="test"))
    monkeypatch.setattr(
        dispatch,
        "_authorize_education_hardware_action",
        lambda loaded_job, printer: (False, "policy denied before hardware"),
    )
    hardware_calls = []
    monkeypatch.setattr(
        dispatch,
        "_dispatch_bambu",
        lambda *args, **kwargs: hardware_calls.append(args) or (True, "unexpected"),
    )

    assert dispatch.dispatch_job(9, 72) == (False, "policy denied before hardware")
    assert hardware_calls == []


def test_education_dispatch_uses_reserved_filename_and_requires_confirmation(
    monkeypatch, tmp_path
):
    from modules.printers import dispatch

    print_file = tmp_path / "project.3mf"
    print_file.write_bytes(b"fixture")
    job = _job(_facts()) | {
        "stored_path": str(print_file),
        "original_filename": "student-secret-name.3mf",
        "bed_x_mm": 256,
        "bed_y_mm": 256,
        "compatible_api_types": "bambu",
    }
    reservation = {
        "claim_id": "claim-1",
        "submission_id": 4,
        "job_id": 72,
        "printer_id": 9,
        "authority_revision": 3,
        "remote_filename": "odin-0123456789abcdef0123456789abcdef.3mf",
    }
    provider = object()
    hardware_calls = []
    confirmations = []
    monkeypatch.setattr(dispatch, "_load_job", lambda job_id: job)
    monkeypatch.setattr(dispatch, "_get_printer_info", lambda printer_id: _printer(ip="test"))
    monkeypatch.setattr(
        dispatch, "_authorize_education_hardware_action", lambda loaded, printer: (True, "v1")
    )
    monkeypatch.setattr(
        dispatch,
        "_reserve_education_hardware_action",
        lambda loaded, extension: ((provider, reservation), None),
    )
    monkeypatch.setattr(
        dispatch,
        "_dispatch_bambu",
        lambda *args: hardware_calls.append(args) or (True, "Print started"),
    )
    monkeypatch.setattr(
        dispatch,
        "_confirm_education_dispatch",
        lambda selected, reserved: confirmations.append((selected, reserved))
        or {"transitioned": True},
    )

    assert dispatch.dispatch_job(9, 72) == (True, "Print started")
    assert hardware_calls[0][2] == reservation["remote_filename"]
    assert "student-secret-name" not in hardware_calls[0][2]
    assert confirmations == [(provider, reservation)]


def test_education_adapter_failure_cancels_only_its_reservation(monkeypatch, tmp_path):
    from modules.printers import dispatch

    print_file = tmp_path / "project.3mf"
    print_file.write_bytes(b"fixture")
    job = _job(_facts()) | {
        "stored_path": str(print_file),
        "original_filename": "project.3mf",
        "bed_x_mm": 256,
        "bed_y_mm": 256,
        "compatible_api_types": "bambu",
    }
    reservation = {
        "claim_id": "claim-1",
        "submission_id": 4,
        "job_id": 72,
        "printer_id": 9,
        "authority_revision": 3,
        "remote_filename": "odin-0123456789abcdef0123456789abcdef.3mf",
    }
    provider = object()
    cancellations = []
    monkeypatch.setattr(dispatch, "_load_job", lambda job_id: job)
    monkeypatch.setattr(dispatch, "_get_printer_info", lambda printer_id: _printer(ip="test"))
    monkeypatch.setattr(
        dispatch, "_authorize_education_hardware_action", lambda loaded, printer: (True, "v1")
    )
    monkeypatch.setattr(
        dispatch,
        "_reserve_education_hardware_action",
        lambda loaded, extension: ((provider, reservation), None),
    )
    monkeypatch.setattr(dispatch, "_dispatch_bambu", lambda *args: (False, "upload failed"))
    monkeypatch.setattr(
        dispatch,
        "_cancel_education_reservation",
        lambda selected, reserved: cancellations.append((selected, reserved)) or True,
    )

    assert dispatch.dispatch_job(9, 72) == (False, "upload failed")
    assert cancellations == [(provider, reservation)]


def test_physical_success_with_lost_education_cas_is_not_clean_success(
    monkeypatch, tmp_path
):
    from modules.printers import dispatch

    print_file = tmp_path / "project.3mf"
    print_file.write_bytes(b"fixture")
    job = _job(_facts()) | {
        "stored_path": str(print_file),
        "original_filename": "project.3mf",
        "bed_x_mm": 256,
        "bed_y_mm": 256,
        "compatible_api_types": "bambu",
    }
    reservation = {
        "remote_filename": "odin-0123456789abcdef0123456789abcdef.3mf",
    }
    monkeypatch.setattr(dispatch, "_load_job", lambda job_id: job)
    monkeypatch.setattr(dispatch, "_get_printer_info", lambda printer_id: _printer(ip="test"))
    monkeypatch.setattr(
        dispatch, "_authorize_education_hardware_action", lambda loaded, printer: (True, "v1")
    )
    monkeypatch.setattr(
        dispatch,
        "_reserve_education_hardware_action",
        lambda loaded, extension: ((object(), reservation), None),
    )
    monkeypatch.setattr(dispatch, "_dispatch_bambu", lambda *args: (True, "Print started"))
    monkeypatch.setattr(
        dispatch, "_confirm_education_dispatch", lambda provider, reserved: {"transitioned": False}
    )

    success, message = dispatch.dispatch_job(9, 72)
    assert success is False
    assert "physical action requires reconciliation" in message


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
