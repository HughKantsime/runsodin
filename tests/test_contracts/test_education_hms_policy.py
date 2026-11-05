"""Education HMS policy still requires fresh stopped-state corroboration."""
import sys
import pytest
from contextlib import contextmanager
from sqlalchemy import text
from tests.test_contracts.test_education_monitor_policy import (
    admitted_monitor, admitted_education, actual_education_dispatch, review_db,
)


@pytest.mark.parametrize('level,retained,live,failed', [
    (1,False,'FAILED',True), (2,False,'PAUSE',True),
    (1,True,'FAILED',False), (2,False,'RUNNING',False),
    (3,False,'FAILED',False), (4,False,'FAILED',False),
])
def test_bambu_token_hms_respects_severity_and_corroboration(
    admitted_monitor, monkeypatch, level, retained, live, failed
) -> None:
    from core import registry as registry_module
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import error_handling
    from modules.organizations.education_policy import reserve_dispatch
    from modules.organizations.services import EducationPolicyService
    from modules.printers.monitors import mqtt_job_lifecycle

    monitor_db, uploaded, reservation = admitted_monitor
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
    assert linked_job_id == uploaded["job_id"]
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
        [{"attr": 0x0C000300, "code": (level << 16) | 6}],
        observed_filename=reservation["remote_filename"], live_state=live, mqtt_retained=retained,
    )
    assert result["education_suppressed"] is True
    assert recorded == [False]
    assert forbidden == []
    monitor_db.expire_all()
    assert tuple(
        monitor_db.execute(
            text("SELECT status,lifecycle_revision FROM education_submissions WHERE id=:submission"), {"submission": uploaded["id"]}
        ).one()
    ) == (("failed", 5) if failed else ("printing", 4))
    assert monitor_db.execute(text("SELECT status FROM jobs WHERE id=:job"), {"job": uploaded["job_id"]}).scalar_one() == ("failed" if failed else "printing")
    assert monitor_db.execute(
        text("SELECT status FROM print_jobs WHERE id=:id"), {"id": print_job_id}
    ).scalar_one() == ("failed" if failed else "running")


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
    assert recorded == []
    assert result["classification_state"] == "unresolved"
    assert result["retryable"] is True


