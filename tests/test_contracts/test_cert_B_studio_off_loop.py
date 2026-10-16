"""Cert B round 6: the Studio queue step (row locks, payload hashing, the bounded plate
check of the stored package) runs in a worker thread, never on the event loop."""
import asyncio
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_print_file_upload_ownership_behavior import upload_client  # noqa: F401
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401

SLICED = Path("tests/fixtures/certification/cert-B/a1-acceptance-coupon.gcode.3mf")


def test_studio_queue_step_runs_off_the_event_loop(upload_client, review_db, monkeypatch, tmp_path):
    from core.registry import registry
    from modules.jobs import register_providers
    from modules.models_library import queue_payload
    monkeypatch.setenv("ODIN_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setattr(registry, "_providers", dict(registry._providers))
    register_providers(registry)
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    client, issue, _ = upload_client
    review_db.execute(text("UPDATE printers SET api_type='bambu',is_active=1,bed_x_mm=NULL,bed_y_mm=NULL WHERE id=9"))
    review_db.commit()
    seen = []
    real = queue_payload.verified_queue_payload

    def recording(row, target):
        try:
            asyncio.get_running_loop()
            seen.append("on-loop")
        except RuntimeError:
            seen.append("worker")
        return real(row, target)
    monkeypatch.setattr(queue_payload, "verified_queue_payload", recording)
    response = client.post("/prints", headers=issue(["admin"]),
                           files={"file": ("coupon.gcode.3mf", SLICED.read_bytes(), "application/octet-stream")},
                           data={"target_printer_id": "9"})
    assert response.status_code == 201, response.text
    assert seen == ["worker"]
