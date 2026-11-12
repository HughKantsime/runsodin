"""Per-printer timelapse capture status: states, visibility, controlled work, RBAC."""
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db  # noqa: F401
from modules.organizations.routes_controlled_parts import change_mark
from modules.printers import routes_timelapse_status as status_module
from modules.printers.models import Printer

NOW = datetime(2026, 11, 12, 10, 0, 20, tzinfo=timezone.utc)
CREATED = "2026-11-12 10:00:00"
KEYS = {"enabled", "capturing", "print_job_id", "started_at", "frames_captured",
        "last_frame_at", "last_error", "will_capture_external_prints"}


@pytest.fixture(autouse=True)
def camera_url(monkeypatch):
    monkeypatch.setattr(status_module, "get_camera_url", lambda printer: "rtsp://synthetic.invalid/camera")


def configure(db, *, enabled=True, state="RUNNING", camera=True, active=True):
    db.execute(text("UPDATE printers SET timelapse_enabled=:e, gcode_state=:s, camera_enabled=:c, is_active=:a WHERE id=1"),
               {"e": enabled, "s": state, "c": camera, "a": active})
    db.commit()


def start_capture(db, *, frames=0, created=CREATED, nonce="a" * 32):
    from core.timelapse_ownership_schema import anchors
    db.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"))
    db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,frame_count,created_at) "
                    "VALUES(50,1,'printer_1/synthetic.mp4','capturing',:f,:c)"), {"f": frames, "c": created})
    db.execute(anchors.insert(), {
        "operation_id": nonce, "timelapse_id": 50, "printer_id": 1, "scheduled_job_id": 1,
        "filename": "printer_1/synthetic.mp4", "owner_state": "known", "owner_org_id": 1,
        "ownership_source": "first_capture_job_file", "print_file_id": 1,
        # A known owner must be the capturing printer's organization (the to_owner rule).
        "capture_printer_org_id": 1, "capture_printer_shared": False})
    db.execute(text("UPDATE timelapses SET print_job_id=1,ownership_operation_id=:n WHERE id=50"), {"n": nonce})
    db.commit()


def status_of(db, printer_id=1, now=NOW):
    db.expire_all()
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    return status_module.build_timelapse_status(db, printer, now=now)


def test_off(database):
    configure(database, enabled=False)
    result = status_of(database)
    assert set(result) == KEYS
    assert result["enabled"] is False and result["capturing"] is False
    assert result["last_error"] is None and result["will_capture_external_prints"] is False


def test_capturing_reports_frames_and_last_frame(database, monkeypatch):
    configure(database)
    start_capture(database, frames=412)
    monkeypatch.setattr(status_module, "_frame_stats", lambda db, nonce: (NOW - timedelta(seconds=20), False))
    result = status_of(database)
    assert result["enabled"] and result["capturing"]
    assert result["print_job_id"] == 1 and result["frames_captured"] == 412
    assert result["started_at"] == "2026-11-12T10:00:00+00:00"
    assert result["last_frame_at"] == "2026-11-12T10:00:00+00:00"
    assert result["last_error"] is None


def test_capturing_before_first_frame_uses_real_frame_query(database):
    configure(database)
    start_capture(database)
    result = status_of(database)
    assert result["capturing"] and result["frames_captured"] == 0
    assert result["last_frame_at"] is None and result["last_error"] is None


def test_stale_capture_reports_missing_frames(database):
    configure(database)
    start_capture(database)
    result = status_of(database, now=NOW + timedelta(minutes=10))
    assert result["capturing"] and result["last_error"] == status_module.MSG_STALE


def test_blocked_frame_reports_retry(database, monkeypatch):
    configure(database)
    start_capture(database, frames=3)
    monkeypatch.setattr(status_module, "_frame_stats", lambda db, nonce: (NOW - timedelta(seconds=5), True))
    assert status_of(database)["last_error"] == status_module.MSG_BLOCKED


def test_frames_not_arriving_outranks_a_blocked_frame(database, monkeypatch):
    configure(database)
    start_capture(database, frames=3)
    monkeypatch.setattr(status_module, "_frame_stats", lambda db, nonce: (NOW - timedelta(seconds=600), True))
    assert status_of(database)["last_error"] == status_module.MSG_STALE


def test_a_job_printing_for_a_while_with_no_capture_says_the_recorder_has_not_started(database):
    configure(database)
    database.execute(text("UPDATE jobs SET printer_id=1,status='printing',actual_start=:t WHERE id=1"),
                     {"t": NOW - timedelta(minutes=30)})
    database.commit()
    result = status_of(database)
    assert result["capturing"] is False and result["last_error"] == status_module.MSG_NOT_STARTED
    database.execute(text("UPDATE jobs SET actual_start=:t WHERE id=1"), {"t": NOW - timedelta(seconds=40)})
    database.commit()
    assert status_of(database)["last_error"] is None      # the first moments of a print


def test_paused_print_is_not_capturing(database):
    configure(database, state="PAUSE")
    start_capture(database, frames=9)
    result = status_of(database)
    assert result["capturing"] is False and result["last_error"] == status_module.MSG_NOT_PRINTING


def test_waiting_for_first_daemon_tick_is_not_an_error(database):
    configure(database)
    database.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"))
    database.commit()
    result = status_of(database)
    assert result["enabled"] and result["capturing"] is False and result["last_error"] is None


def test_idle_printer_has_no_error(database):
    configure(database, state="IDLE")
    result = status_of(database)
    assert result["enabled"] and result["capturing"] is False and result["last_error"] is None


def test_external_print_is_not_recorded_and_reported(database):
    """A print with no scheduled job (print_jobs row only) gets no timelapse today."""
    from modules.archives.timelapse_capture import get_active_job_for_printer
    configure(database)
    database.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,job_name,status,started_at) "
                          "VALUES(60,1,NULL,'external.3mf','running',CURRENT_TIMESTAMP)"))
    database.commit()
    # The daemon's own selection finds nothing to record...
    assert get_active_job_for_printer(database, 1) is None
    # ...and the status says so, in plain words.
    result = status_of(database)
    assert result["capturing"] is False
    assert result["will_capture_external_prints"] is False
    assert result["last_error"] == status_module.MSG_NO_JOB


@pytest.mark.parametrize("kwargs,message", [
    ({"active": False}, status_module.MSG_INACTIVE),
    ({"camera": False}, status_module.MSG_CAMERA_OFF),
])
def test_setup_problems_are_explained(database, kwargs, message):
    configure(database, **kwargs)
    assert status_of(database)["last_error"] == message


def test_missing_camera_source_is_explained(database, monkeypatch):
    configure(database)
    monkeypatch.setattr(status_module, "get_camera_url", lambda printer: None)
    assert status_of(database)["last_error"] == status_module.MSG_NO_CAMERA


def test_messages_carry_no_paths_or_credentials():
    assert not any("/" in m or "@" in m or "\\" in m for m in status_module.MESSAGES)


def call(db, printer_id, user):
    return status_module.get_timelapse_status(printer_id, Response(), db, user)


def test_unknown_printer_is_404(database):
    with pytest.raises(HTTPException) as error:
        call(database, 999, actor(2))
    assert error.value.status_code == 404


def test_other_organization_printer_is_hidden_unless_shared(database):
    database.execute(text("UPDATE printers SET org_id=5, shared=0 WHERE id=2"))
    database.commit()
    viewer = {"id": 9, "role": "viewer", "group_id": 7, "_auth_kind": "session_jwt"}
    with pytest.raises(HTTPException) as error:
        call(database, 2, viewer)
    assert error.value.status_code == 404
    database.execute(text("UPDATE printers SET shared=1 WHERE id=2"))
    database.commit()
    shared = call(database, 2, viewer)
    assert set(shared) == KEYS and shared["print_job_id"] is None   # another organization's job id stays private


def test_controlled_work_is_refused_to_non_members(database, tier_license):
    tier_license("enterprise")
    change_mark(database, "files", 1, mark(), actor())
    database.execute(text("UPDATE jobs SET printer_id=1,status='printing' WHERE id=1"))
    database.commit()
    configure(database)
    with pytest.raises(HTTPException) as error:
        call(database, 1, actor(2))
    assert error.value.status_code == 403
    assert error.value.detail["code"] == "controlled_part_group_required"
    assert call(database, 1, actor(1))["enabled"] is True


@pytest.fixture
def status_client(token_client, review_db):
    client, issue = token_client
    client.app.include_router(status_module.router)
    return client, issue


PATH = "/printers/9/timelapse-status"


def test_unauthenticated_is_refused(status_client):
    client, _ = status_client
    assert client.get(PATH).status_code == 401


def test_unrelated_token_scope_is_refused(status_client):
    client, issue = status_client
    assert client.get(PATH, headers=issue(["write:jobs"])).status_code == 403


@pytest.mark.parametrize("scope", ["read:printers", "read", "admin"])
def test_read_scopes_get_the_status_block(status_client, review_db, scope):
    client, issue = status_client
    review_db.execute(text("UPDATE printers SET timelapse_enabled=1,camera_enabled=1,is_active=1,gcode_state='IDLE' WHERE id=9"))
    review_db.commit()
    response = client.get(PATH, headers=issue([scope]))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == KEYS
    assert body["enabled"] is True and body["capturing"] is False
    assert body["will_capture_external_prints"] is False
    assert response.headers["cache-control"] == "private, no-store"


def test_unknown_printer_over_http_is_404(status_client):
    client, issue = status_client
    assert client.get("/printers/987654/timelapse-status", headers=issue(["read:printers"])).status_code == 404
