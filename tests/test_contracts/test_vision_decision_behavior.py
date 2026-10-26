"""Vigil decision safety with synthetic frames, not model accuracy certification."""
import pytest
import numpy as np
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def vision(review_db, monkeypatch):
    from modules.vision import detection_thread as module
    class Predictions:
        confidence = 0.9
        calls = []
        from contextlib import contextmanager
        @contextmanager
        def decision_guard(self,name,observation=None,heuristic_generation=None):
            yield self.observation_current(name,observation) if observation else self.heuristic_current(name,heuristic_generation)
        def has_model(self, name): return name == "spaghetti"
        def configuration_state(self,name):return 1, 'ready' if self.has_model(name) else 'not_configured'
        def heuristic_current(self,name,generation):return generation==1 and not self.has_model(name)
        def infer_observation(self,name,frame):
            from modules.vision.inference_engine import InferenceObservation
            return InferenceObservation(1,True,tuple(self.infer(name,frame)))
        def observation_current(self,name,observation):return observation.available and observation.generation==1
        def infer(self, name, frame):
            self.calls.append(name)
            return [{"confidence": self.confidence, "bbox": [0, 0, 1, 1]}]
    model = Predictions()
    thread = module.PrinterVisionThread(9, "Synthetic", model,
        {"spaghetti_enabled": 1, "auto_pause": 1}, 10, 23, "RUNNING")
    monkeypatch.setattr(thread, "_capture_frame", lambda: np.full((32,32,3), 100, dtype=np.uint8))
    return thread, model, module


def test_spaghetti_confirmation_threshold_and_reset(vision, monkeypatch):
    thread, model, module = vision
    recorded = []
    monkeypatch.setattr(thread, "_on_detection", lambda kind, det, frame: recorded.append(kind))
    model.confidence = 0.64
    for _ in range(3): thread._capture_and_analyze()
    assert recorded == []
    model.confidence = 0.65
    thread._capture_and_analyze()
    assert recorded == []
    thread._capture_and_analyze()
    assert recorded == ["spaghetti"]
    assert thread._history["spaghetti"] == []


@pytest.mark.parametrize("state", ["IDLE", "FINISH", "FINISHED", "PAUSED", "FAILED", None])
def test_nonprinting_does_not_run_failure_inference(vision, monkeypatch, state):
    thread, model, module = vision
    thread.gcode_state = state
    recorded = []
    monkeypatch.setattr(thread, "_on_detection", lambda *args: recorded.append(args[0]))
    for _ in range(3): thread._capture_and_analyze()
    assert model.calls == []
    assert recorded == []


@pytest.mark.parametrize("kind,state,expected", [
    ("build_plate_empty", "IDLE", []),
    ("spaghetti", "PAUSED", []),
    ("spaghetti", "RUNNING", ["pause"]),
])
def test_detection_pause_only_for_active_failure(vision, monkeypatch, kind, state, expected):
    thread, model, module = vision
    from modules.notifications import event_dispatcher
    thread.gcode_state = state
    monkeypatch.setattr(module.frame_storage, "save_detection_frame", lambda *args: "synthetic.jpg")
    monkeypatch.setattr(thread, "_insert_detection", lambda *args: 1)
    monkeypatch.setattr(event_dispatcher, "dispatch_alert", lambda **kwargs: None)
    monkeypatch.setattr(module, "ws_push", lambda *args: None)
    pauses = []
    monkeypatch.setattr(thread, "_auto_pause", lambda: pauses.append("pause"))
    thread._on_detection(kind, {"confidence": 0.9, "bbox": []}, np.zeros((32,32,3)))
    assert pauses == expected


def test_confirmation_cooldown_and_first_layer(vision, monkeypatch):
    thread, model, module = vision
    monkeypatch.setattr(module.time, "time", lambda: 1000)
    thread._update_history("first_layer", True)
    assert thread._should_trigger("first_layer")
    thread._last_alert["first_layer"] = 950
    assert not thread._should_trigger("first_layer")
    thread._last_alert["first_layer"] = 940
    assert thread._should_trigger("first_layer")


def test_print_state_transition_clears_failure_confirmation(vision):
    thread, model, module = vision
    thread._history["spaghetti"] = [True, True]
    thread._history["build_plate_empty"] = [True, True]
    thread.update_layer(0, None, "IDLE")
    assert thread._history["spaghetti"] == []
    thread.update_layer(1, 24, "RUNNING")
    assert thread._history["build_plate_empty"] == []
    assert thread.print_job_id == 24


def test_direct_pause_guard_does_not_open_database_for_idle(vision, monkeypatch):
    thread, model, module = vision
    thread.gcode_state = "IDLE"
    class RecordingEngine:
        calls = []
        def connect(self):
            self.calls.append("connect")
            raise RuntimeError("No device database permitted")
    recording = RecordingEngine()
    monkeypatch.setattr(module, "engine", recording)
    thread._auto_pause()
    assert recording.calls == []


@pytest.mark.parametrize("succeeds", [False, True])
def test_adapter_pause_updates_only_on_success(vision, review_db, monkeypatch, succeeds):
    from sqlalchemy import text
    from modules.printers.adapters import moonraker
    thread, model, module = vision
    monkeypatch.setattr(module, "engine", review_db.get_bind())
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='synthetic.invalid',gcode_state='RUNNING' WHERE id=9"))
    review_db.execute(text("INSERT INTO vision_detections(id,printer_id,detection_type,confidence,status) VALUES(101,9,'spaghetti',0.9,'pending')"))
    review_db.commit()
    calls = []
    class Adapter:
        def __init__(self, host): assert host == "synthetic.invalid"
        def pause_print(self):
            calls.append("pause")
            return succeeds
    monkeypatch.setattr(moonraker, "MoonrakerPrinter", Adapter)
    thread.print_job_id = None
    thread._auto_pause()
    assert calls == ["pause"]
    review_db.expire_all()
    assert review_db.execute(text("SELECT gcode_state FROM printers WHERE id=9")).scalar_one() == ("PAUSED" if succeeds else "RUNNING")
    assert review_db.execute(text("SELECT status FROM vision_detections WHERE id=101")).scalar_one() == ("auto_paused" if succeeds else "pending")


def test_state_change_during_inference_prevents_pause(vision, monkeypatch):
    thread, model, module = vision
    from modules.notifications import event_dispatcher
    monkeypatch.setattr(module.frame_storage, "save_detection_frame", lambda *args: "synthetic.jpg")
    monkeypatch.setattr(thread, "_insert_detection", lambda *args: 1)
    monkeypatch.setattr(event_dispatcher, "dispatch_alert", lambda **kwargs: None)
    monkeypatch.setattr(module, "ws_push", lambda *args: None)
    pauses = []
    monkeypatch.setattr(thread, "_auto_pause", lambda: pauses.append("pause"))
    original = model.infer
    def infer(kind, frame):
        thread.update_layer(thread.current_layer, thread.print_job_id, "PAUSED")
        return original(kind, frame)
    model.infer = infer
    thread._history["spaghetti"] = [True, True]
    detections = []
    monkeypatch.setattr(thread, "_on_detection", lambda *args: detections.append(args[0]))
    thread._capture_and_analyze()
    assert pauses == []
    assert detections == []
    assert thread._history["spaghetti"] == []


@pytest.mark.parametrize("kind,layer", [("spaghetti",10),("first_layer",1),("detachment",10)])
@pytest.mark.parametrize("state", ["RUNNING", "IDLE", "PAUSED", "FAILED", None])
def test_each_failure_type_requires_running(vision, monkeypatch, kind, layer, state):
    thread, model, module = vision
    thread.gcode_state = state
    thread.current_layer = layer
    model.has_model = lambda name: name == kind
    detections = []
    monkeypatch.setattr(thread, "_on_detection", lambda name, det, frame: detections.append(name))
    for _ in range(3): thread._capture_and_analyze()
    assert bool(detections) == (state == "RUNNING")
    assert all(name == kind for name in detections)


def test_plate_ready_idle_records_and_notifies_without_pause(vision, monkeypatch):
    thread, model, module = vision
    from modules.notifications import event_dispatcher
    thread.update_layer(0, None, "IDLE")
    thread.settings["build_plate_empty_enabled"] = 1
    records, alerts, events, pauses = [], [], [], []
    monkeypatch.setattr(module.frame_storage, "save_detection_frame", lambda *args: "synthetic.jpg")
    def insert(*args):
        records.append(args[0])
        return 101
    monkeypatch.setattr(thread, "_insert_detection", insert)
    monkeypatch.setattr(event_dispatcher, "dispatch_alert", lambda **kwargs: alerts.append(kwargs))
    monkeypatch.setattr(module, "ws_push", lambda *args: events.append(args))
    monkeypatch.setattr(thread, "_auto_pause", lambda: pauses.append("pause"))
    for _ in range(3): thread._capture_and_analyze()
    assert records == ["build_plate_empty"]
    assert [a["alert_type"] for a in alerts] == ["bed_cooled"]
    assert len(events) == 1
    assert pauses == []
