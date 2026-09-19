from __future__ import annotations

import hashlib
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def monitor_db(tmp_path: Path):
    from core.schema import bootstrap_database

    engine = create_engine(f"sqlite:///{tmp_path / 'monitor.db'}")
    bootstrap_database(engine, BACKEND)
    with engine.begin() as connection:
        for statement in (
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
            "INSERT INTO education_cost_center_printers "
            "(org_id,cost_center_id,printer_id,state,granted_by) VALUES (1,7,9,'active',2)",
            "INSERT INTO education_upload_operations "
            "(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,"
            "expected_bytes,expected_hash,reservation_released_at) "
            "VALUES ('upload-monitor',1,1,'committed',100,100,100,'digest',CURRENT_TIMESTAMP)",
            "INSERT INTO models (id,name,category,org_id) "
            "VALUES (12,'Class Gear','Education Submission',1)",
            "INSERT INTO jobs "
            "(id,model_id,item_name,quantity,status,printer_id,duration_hours,submitted_by,"
            "approved_by,charged_to_user_id,charged_to_org_id,hold,priority) "
            "VALUES (13,12,'Class Gear',1,'scheduled',9,1.0,1,2,1,1,0,3)",
        ):
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT INTO print_files "
                "(id,filename,original_filename,project_name,stored_path,org_id,created_by,"
                "storage_bytes,blob_state,compatibility_facts_json,model_id) "
                "VALUES (11,'upload.3mf','gear.3mf','Class Gear','/opaque/upload.3mf',"
                "1,1,100,'present',:facts,12)"
            ),
            {"facts": json.dumps({"fixture": True})},
        )
        connection.execute(text("UPDATE models SET print_file_id=11 WHERE id=12"))
        connection.execute(
            text(
                "INSERT INTO education_submissions "
                "(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,"
                "submitted_by,status,lifecycle_revision,approved_printer_id,approved_by,"
                "compatibility_engine_version) VALUES "
                "(14,1,'upload-monitor',13,11,12,7,1,'scheduled',3,9,2,'education-compatibility-v1')"
            )
        )
    db = Session(engine)
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _reserve(db: Session):
    from modules.organizations.education_policy import reserve_dispatch

    reservation = reserve_dispatch(
        db, job_id=13, printer_id=9, expected_revision=3, extension=".3mf"
    )
    assert reservation is not None
    return reservation


def _observation(db: Session, filename: str, *, printer_id: int = 9) -> int:
    result = db.execute(
        text(
            "INSERT INTO print_jobs (printer_id,filename,job_name,started_at,status) "
            "VALUES (:printer_id,:filename,:filename,CURRENT_TIMESTAMP,'running') RETURNING id"
        ),
        {"printer_id": printer_id, "filename": filename},
    )
    return int(result.scalar_one())


def test_token_parser_is_anchored_and_extension_tolerant() -> None:
    from modules.organizations.education_policy import parse_education_token

    token = "0123456789abcdef0123456789abcdef"
    assert parse_education_token(f"/cache/odin-{token}.3mf") == token
    assert parse_education_token(f"ODIN-{token}.GCODE") == token
    assert parse_education_token(f"prefix-odin-{token}.3mf") is None
    assert parse_education_token(f"odin-{token}-suffix.3mf") is None
    assert parse_education_token("student-file.3mf") is None


def test_reservation_stores_only_digest_and_fences_scheduler(monitor_db: Session) -> None:
    from modules.organizations.education_policy import reset_stale_schedule, reserve_dispatch

    reservation = _reserve(monitor_db)
    claim = monitor_db.execute(
        text("SELECT token_digest,state,authority_revision FROM education_monitor_claims")
    ).one()
    assert reservation["token"] not in claim.token_digest
    assert claim.token_digest == hashlib.sha256(reservation["token"].encode()).hexdigest()
    assert tuple(claim)[1:] == ("reserved", 3)
    assert reservation["remote_filename"] == f"odin-{reservation['token']}.3mf"
    assert reserve_dispatch(
        monitor_db, job_id=13, printer_id=9, expected_revision=3, extension=".3mf"
    ) is None
    assert reset_stale_schedule(
        monitor_db,
        submission_id=14,
        job_id=13,
        printer_id=9,
        expected_revision=3,
    ) is False


def test_clean_cancellation_leaves_late_start_quarantined(monitor_db: Session) -> None:
    from modules.organizations.education_policy import (
        cancel_dispatch_reservation,
        claim_monitor_observation,
        terminal_monitor_observation,
    )

    reservation = _reserve(monitor_db)
    assert cancel_dispatch_reservation(
        monitor_db,
        claim_id=reservation["claim_id"],
        submission_id=14,
        job_id=13,
        printer_id=9,
        expected_revision=3,
    )
    print_job_id = _observation(monitor_db, reservation["remote_filename"])
    claimed = claim_monitor_observation(
        monitor_db,
        print_job_id=print_job_id,
        printer_id=9,
        observed_filename=reservation["remote_filename"],
    )
    assert claimed == {
        "education_owned": False,
        "education_reserved": True,
        "authorized": False,
        "transitioned": False,
    }
    terminal = terminal_monitor_observation(
        monitor_db,
        print_job_id=print_job_id,
        printer_id=9,
        terminal_status="completed",
    )
    assert terminal["quarantined"] is True
    assert terminal["education_reserved"] is True
    assert monitor_db.execute(text("SELECT status FROM jobs WHERE id=13")).scalar_one() == "scheduled"
    assert monitor_db.execute(
        text("SELECT status FROM education_submissions WHERE id=14")
    ).scalar_one() == "scheduled"


def test_confirm_claim_and_terminal_are_one_authority_graph(monitor_db: Session) -> None:
    from modules.organizations.education_policy import (
        claim_monitor_observation,
        confirm_dispatch_started,
        terminal_monitor_observation,
    )

    reservation = _reserve(monitor_db)
    confirmed = confirm_dispatch_started(
        monitor_db,
        claim_id=reservation["claim_id"],
        submission_id=14,
        job_id=13,
        printer_id=9,
        expected_revision=3,
    )
    assert confirmed["transitioned"] is True
    assert confirmed["lifecycle_revision"] == 4
    print_job_id = _observation(monitor_db, reservation["remote_filename"])
    claimed = claim_monitor_observation(
        monitor_db,
        print_job_id=print_job_id,
        printer_id=9,
        observed_filename=reservation["remote_filename"],
    )
    assert claimed["authorized"] is True
    assert claimed["recovered"] is False
    terminal = terminal_monitor_observation(
        monitor_db,
        print_job_id=print_job_id,
        printer_id=9,
        terminal_status="completed",
        duration_seconds=120,
    )
    assert terminal["transitioned"] is True
    assert terminal["lifecycle_revision"] == 5
    assert tuple(
        monitor_db.execute(
            text("SELECT status,lifecycle_revision FROM education_submissions WHERE id=14")
        ).one()
    ) == ("completed", 5)
    assert monitor_db.execute(text("SELECT status FROM jobs WHERE id=13")).scalar_one() == "completed"
    assert monitor_db.execute(
        text("SELECT status FROM print_jobs WHERE id=:id"), {"id": print_job_id}
    ).scalar_one() == "completed"
    assert monitor_db.execute(text("SELECT COUNT(*) FROM education_monitor_claims")).scalar_one() == 0
    assert monitor_db.execute(text("SELECT COUNT(*) FROM education_audit_events")).scalar_one() == 3
    duplicate = terminal_monitor_observation(
        monitor_db,
        print_job_id=print_job_id,
        printer_id=9,
        terminal_status="completed",
    )
    assert duplicate["idempotent"] is True
    assert duplicate["transitioned"] is False


def test_exact_token_recovers_reserved_crash_window(monitor_db: Session) -> None:
    from modules.organizations.education_policy import claim_monitor_observation

    reservation = _reserve(monitor_db)
    print_job_id = _observation(monitor_db, reservation["remote_filename"])
    result = claim_monitor_observation(
        monitor_db,
        print_job_id=print_job_id,
        printer_id=9,
        observed_filename=reservation["remote_filename"],
    )
    assert result["authorized"] is True
    assert result["recovered"] is True
    assert result["lifecycle_revision"] == 4
    assert tuple(
        monitor_db.execute(
            text("SELECT status,lifecycle_revision FROM education_submissions WHERE id=14")
        ).one()
    ) == ("printing", 4)
    assert tuple(
        monitor_db.execute(
            text("SELECT state,authority_revision FROM education_monitor_claims")
        ).one()
    ) == ("running", 4)


def test_right_token_on_wrong_printer_is_quarantined_not_owned(monitor_db: Session) -> None:
    from modules.organizations.education_policy import claim_monitor_observation

    reservation = _reserve(monitor_db)
    print_job_id = _observation(monitor_db, reservation["remote_filename"], printer_id=10)
    result = claim_monitor_observation(
        monitor_db,
        print_job_id=print_job_id,
        printer_id=10,
        observed_filename=reservation["remote_filename"],
    )
    assert result["education_owned"] is False
    assert result["education_reserved"] is True
    assert result["authorized"] is False
    assert monitor_db.execute(
        text("SELECT scheduled_job_id FROM print_jobs WHERE id=:id"), {"id": print_job_id}
    ).scalar_one_or_none() is None


def test_cursor_style_executor_uses_bound_values_and_savepoints(monitor_db: Session) -> None:
    from modules.organizations.education_policy import reserve_dispatch

    raw_connection = monitor_db.connection().connection.driver_connection
    reservation = reserve_dispatch(
        raw_connection,
        job_id=13,
        printer_id=9,
        expected_revision=3,
        extension=".3mf",
    )
    assert reservation is not None
    monitor_db.commit()
    assert monitor_db.execute(
        text("SELECT state FROM education_monitor_claims WHERE claim_id=:claim_id"),
        {"claim_id": reservation["claim_id"]},
    ).scalar_one() == "reserved"


@pytest.mark.parametrize(
    ("raw_state", "internal_state"),
    [
        ("complete", "IDLE"),
        ("cancelled", "IDLE"),
        ("error", "FAILED"),
        ("standby", "IDLE"),
    ],
)
def test_moonraker_parser_preserves_raw_terminal_truth(
    raw_state: str, internal_state: str
) -> None:
    from modules.printers.parsing.moonraker import parse_status

    parsed = parse_status({"print_stats": {"state": raw_state, "filename": "fixture.gcode"}})
    assert parsed["raw_print_state"] == raw_state
    assert parsed["internal_state"] == internal_state


def test_prusalink_stopped_maps_to_cancelled_not_completed(monkeypatch) -> None:
    import time
    from modules.notifications import event_dispatcher
    from modules.printers.adapters.prusalink import PrusaLinkState, PrusaLinkStatus
    from modules.printers.monitors import prusalink_monitor

    calls: list[tuple[str, tuple]] = []
    monkeypatch.setattr(
        event_dispatcher,
        "on_print_cancelled",
        lambda *args: calls.append(("cancelled", args)),
    )
    monkeypatch.setattr(
        event_dispatcher,
        "on_print_complete",
        lambda *args: calls.append(("completed", args)),
    )

    class _NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(prusalink_monitor.threading, "Thread", _NoopThread)
    monitor = prusalink_monitor.PrusaLinkMonitorThread(9, "Prusa", "127.0.0.1")
    monitor._last_state = "PRINTING"
    monitor._last_heartbeat = time.time()
    monitor._process_status(
        PrusaLinkStatus(
            state=PrusaLinkState.STOPPED,
            internal_state="STOPPED",
            filename="odin-0123456789abcdef0123456789abcdef.gcode",
        )
    )
    assert [name for name, _ in calls] == ["cancelled"]


def test_shared_monitor_wrapper_suppresses_generic_fanout_and_recovers_after_restart(
    monitor_db: Session, monkeypatch
) -> None:
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import job_events
    from modules.organizations.education_policy import (
        confirm_dispatch_started,
        reserve_dispatch,
    )
    from modules.organizations.services import EducationPolicyService

    reservation = reserve_dispatch(
        monitor_db, job_id=13, printer_id=9, expected_revision=3, extension=".gcode"
    )
    assert reservation is not None
    monitor_db.commit()
    confirmed = confirm_dispatch_started(
        monitor_db,
        claim_id=reservation["claim_id"],
        submission_id=14,
        job_id=13,
        printer_id=9,
        expected_revision=3,
    )
    assert confirmed["transitioned"] is True
    monitor_db.commit()

    engine = monitor_db.get_bind()

    @contextmanager
    def test_db():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()

    policy = EducationPolicyService()
    monkeypatch.setattr(job_events, "get_db", test_db)
    monkeypatch.setattr(job_events, "_education_policy", lambda: policy)

    class _ForbiddenBus:
        def publish(self, event):
            raise AssertionError("generic event bus fanout must be suppressed")

    monkeypatch.setattr(job_events, "get_event_bus", lambda: _ForbiddenBus())
    monkeypatch.setattr(
        job_events,
        "dispatch_alert",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("generic alert fanout must be suppressed")
        ),
    )
    monkeypatch.setattr(
        job_events,
        "increment_care_counters",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("care accounting must be suppressed")
        ),
    )

    print_job_id = job_events.job_started(
        printer_id=9,
        job_name=reservation["remote_filename"],
    )
    assert print_job_id is not None
    job_events._active_print_jobs.clear()
    job_events.on_print_complete(
        9,
        reservation["remote_filename"],
        duration_seconds=45,
    )

    monitor_db.expire_all()
    assert tuple(
        monitor_db.execute(
            text("SELECT status,lifecycle_revision FROM education_submissions WHERE id=14")
        ).one()
    ) == ("completed", 5)
    assert monitor_db.execute(text("SELECT status FROM jobs WHERE id=13")).scalar_one() == "completed"


def test_bambu_token_start_and_print_stopping_hms_use_only_education_policy(
    monitor_db: Session, monkeypatch
) -> None:
    from core import registry as registry_module
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import error_handling
    from modules.organizations.education_policy import reserve_dispatch
    from modules.organizations.services import EducationPolicyService
    from modules.printers.monitors import mqtt_job_lifecycle

    reservation = reserve_dispatch(
        monitor_db, job_id=13, printer_id=9, expected_revision=3, extension=".3mf"
    )
    assert reservation is not None
    monitor_db.commit()
    engine = monitor_db.get_bind()

    @contextmanager
    def test_db():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()

    policy = EducationPolicyService()
    monkeypatch.setattr(mqtt_job_lifecycle, "get_db", test_db)
    monkeypatch.setattr(mqtt_job_lifecycle, "_education_policy", lambda: policy)
    forbidden: list[str] = []
    print_job_id, linked_job_id, _ = mqtt_job_lifecycle.record_job_started(
        printer_id=9,
        printer_name="P1S-09",
        state={
            "subtask_name": reservation["remote_filename"],
            "gcode_file": "Metadata/plate_1.gcode",
            "job_id": "bambu-fixture",
            "total_layer_num": 100,
        },
        dispatch_alert_fn=lambda **kwargs: forbidden.append("start-alert"),
        trigger_reschedule_fn=lambda: forbidden.append("reschedule"),
    )
    assert print_job_id is not None
    assert linked_job_id == 13
    assert forbidden == []

    monkeypatch.setattr(error_handling, "get_db", test_db)
    monkeypatch.setattr(
        registry_module.registry,
        "get_provider",
        lambda name: policy if name == "EducationPolicyProvider" else None,
    )
    monkeypatch.setattr(
        error_handling,
        "dispatch_alert",
        lambda *args, **kwargs: forbidden.append("external-alert"),
    )
    recorded: list[bool] = []
    original_record_error = error_handling.record_error

    def capture_record_error(*args, **kwargs):
        recorded.append(bool(kwargs.get("create_alert", True)))
        return original_record_error(*args, **kwargs)

    monkeypatch.setattr(error_handling, "record_error", capture_record_error)
    result = error_handling.process_hms_errors(
        9,
        [{"attr": int("0C000300", 16), "code": int("00030006", 16)}],
        observed_filename=reservation["remote_filename"],
    )
    assert result["education_suppressed"] is True
    assert recorded == [False]
    assert forbidden == []
    monitor_db.expire_all()
    assert tuple(
        monitor_db.execute(
            text("SELECT status,lifecycle_revision FROM education_submissions WHERE id=14")
        ).one()
    ) == ("failed", 5)
    assert monitor_db.execute(text("SELECT status FROM jobs WHERE id=13")).scalar_one() == "failed"
    assert monitor_db.execute(
        text("SELECT status FROM print_jobs WHERE id=:id"), {"id": print_job_id}
    ).scalar_one() == "failed"


def test_bambu_first_running_packet_correlates_before_hms(monkeypatch) -> None:
    import time
    import types
    from types import SimpleNamespace
    from modules.notifications import event_dispatcher

    paho = types.ModuleType("paho")
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_client = types.ModuleType("paho.mqtt.client")
    paho_client.Client = object
    paho_mqtt.client = paho_client
    paho.mqtt = paho_mqtt
    monkeypatch.setitem(sys.modules, "paho", paho)
    monkeypatch.setitem(sys.modules, "paho.mqtt", paho_mqtt)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", paho_client)
    from modules.printers.monitors.mqtt_printer import PrinterMonitor

    order: list[str] = []
    monitor = PrinterMonitor(9, "P1S-09", "127.0.0.1", "serial", "code")
    monitor._last_heartbeat = time.time()
    monitor._last_spool_check = time.time()
    monkeypatch.setattr(
        monitor,
        "_on_state_change",
        lambda old, new: order.append(f"state:{new}"),
    )
    monkeypatch.setattr(
        event_dispatcher,
        "process_hms_errors",
        lambda *args, **kwargs: order.append("hms"),
    )
    monitor._on_status(
        SimpleNamespace(
            raw_data={
                "print": {
                    "gcode_state": "RUNNING",
                    "subtask_name": "odin-0123456789abcdef0123456789abcdef.3mf",
                    "hms": [{"attr": 1, "code": 2}],
                }
            }
        )
    )
    assert order == ["state:RUNNING", "hms"]


def test_hms_reserved_token_stays_suppressed_when_policy_classification_fails(
    monkeypatch,
) -> None:
    from core import registry as registry_module
    from modules.notifications import error_handling

    class FakeConnection:
        def cursor(self):
            return self

        def execute(self, *args, **kwargs):
            return self

        def fetchone(self):
            return None

        def commit(self):
            return None

    @contextmanager
    def fake_db():
        yield FakeConnection()

    class BrokenPolicy:
        def classify_monitor_observation(self, *args, **kwargs):
            raise RuntimeError("classification unavailable")

    recorded: list[dict] = []
    monkeypatch.setattr(error_handling, "get_db", fake_db)
    monkeypatch.setattr(
        registry_module.registry,
        "get_provider",
        lambda name: BrokenPolicy() if name == "EducationPolicyProvider" else None,
    )
    monkeypatch.setattr(
        error_handling,
        "record_error",
        lambda **kwargs: recorded.append(kwargs),
    )

    result = error_handling.process_hms_errors(
        9,
        [{"attr": 1, "code": 2}],
        observed_filename=f"odin-{'a' * 32}.3mf",
    )

    assert result["education_suppressed"] is True
    assert recorded and recorded[0]["create_alert"] is False
