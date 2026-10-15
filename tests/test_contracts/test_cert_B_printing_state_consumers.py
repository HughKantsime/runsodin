"""Cert B: every consumer of "is this printer printing" uses one protocol-neutral rule.

Bambu MQTT stores RUNNING/PAUSE/IDLE/FINISH; the Moonraker monitor stores Klipper's
print_stats state upper-cased (PRINTING/PAUSED/STANDBY/COMPLETE); PrusaLink and Elegoo
store PRINTING/PAUSED/IDLE/FINISHED. Vision monitoring, its detection gates and the
fleet report must treat them alike (as the timelapse daemon does).
"""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

STATES = {201: ("bambu", "RUNNING"), 202: ("moonraker", "PRINTING"), 203: ("prusalink", "PRINTING"),
          204: ("elegoo", "PRINTING"), 205: ("moonraker", "PAUSED"), 206: ("bambu", "PAUSE"),
          207: ("moonraker", "STANDBY"), 208: ("moonraker", "COMPLETE"), 209: ("prusalink", "FINISHED"),
          210: ("bambu", "FINISH"), 211: ("moonraker", "ERROR")}


@pytest.fixture
def fleet(review_db):
    for pid, (api, state) in STATES.items():
        review_db.execute(text("INSERT INTO printers(id,name,api_type,is_active,camera_enabled,camera_url,gcode_state) "
                               "VALUES(:id,:n,:a,1,1,'rtsp://192.0.2.1/cam',:s)"), {"id": pid, "n": f"{api}-{state}", "a": api, "s": state})
    review_db.commit()
    return review_db


def test_vision_monitor_watches_printing_printers_of_every_protocol(fleet, monkeypatch):
    from modules.vision import monitor
    started = {}

    class Recorder:
        def __init__(self, printer_id, gcode_state, **_):
            self.printer_id, self.gcode_state = printer_id, gcode_state
        def start(self):
            started[self.printer_id] = self.gcode_state
    monkeypatch.setattr(monitor, "engine", fleet.get_bind())
    monkeypatch.setattr(monitor, "PrinterVisionThread", Recorder)
    daemon = monitor.VisionMonitorDaemon.__new__(monitor.VisionMonitorDaemon)
    daemon._engine, daemon._threads = None, {}
    daemon._scan_printers()
    assert sorted(started) == [201, 202, 203, 204]  # printing on every protocol; paused/idle/error are not
    # Build-plate-empty monitoring covers idle/finished printers of every protocol.
    fleet.execute(text("INSERT INTO vision_settings(printer_id,enabled,build_plate_empty_enabled) SELECT id,1,1 FROM printers WHERE id>=205"))
    fleet.commit()
    started.clear(); daemon._threads = {}
    daemon._scan_printers()
    assert sorted(started) == [201, 202, 203, 204, 207, 208, 209, 210]


class FakeEngine:
    def __init__(self):
        self.inferred = []
    def configuration_state(self, kind):
        return 1, "ready"
    def has_model(self, kind):
        return True
    def infer_observation(self, kind, frame):
        self.inferred.append(kind)
        return object()
    def observation_current(self, kind, observation):
        return False  # stop after recording which checks ran


@pytest.mark.parametrize("state,runs", [("RUNNING", True), ("PRINTING", True), ("printing", True),
                                        ("PAUSED", False), ("PAUSE", False), ("STANDBY", False), ("COMPLETE", False)])
def test_print_failure_checks_run_while_printing_on_every_protocol(state, runs):
    from modules.vision.detection_thread import PrinterVisionThread
    engine = FakeEngine()
    thread = PrinterVisionThread(printer_id=1, printer_name="t", engine=engine, settings={}, current_layer=2,
                                 print_job_id=None, gcode_state=state)
    thread._capture_frame = lambda: object()
    thread._capture_and_analyze()
    assert engine.inferred == (["spaghetti", "first_layer"] if runs else [])


@pytest.mark.parametrize("state,checks", [("STANDBY", True), ("COMPLETE", True), ("FINISHED", True), ("FINISH", True),
                                          ("IDLE", True), ("PRINTING", False), ("RUNNING", False)])
def test_build_plate_empty_check_runs_when_idle_on_every_protocol(state, checks):
    from modules.vision.detection_thread import PrinterVisionThread
    engine = FakeEngine()
    thread = PrinterVisionThread(printer_id=1, printer_name="t", engine=engine, settings={"build_plate_empty_enabled": 1,
                                 "spaghetti_enabled": 0, "first_layer_enabled": 0, "detachment_enabled": 0},
                                 current_layer=0, print_job_id=None, gcode_state=state)
    thread._capture_frame = lambda: object()
    called = []
    thread._detect_build_plate_empty = lambda frame: called.append(frame) or None
    thread._capture_and_analyze()
    assert bool(called) is checks


def test_fleet_report_counts_printing_printers_of_every_protocol(fleet):
    import re
    from modules.reporting.report_runner import generate_fleet_utilization
    html = re.sub(r"<[^>]+>", " ", generate_fleet_utilization(fleet, {}))
    html = re.sub(r"\s+", " ", html).upper()
    assert "CURRENTLY PRINTING 4" in html, html
