"""Contract tests for the demo engine (Phase 6 minimum viable)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.demo import (
    DemoEngine,
    DemoPrinter,
    DemoScenario,
    DemoState,
)

SCENARIOS_DIR = Path(__file__).parent.parent.parent / "demo_scenarios"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "telemetry"


class TestDemoState:
    def test_default_speed(self):
        state = DemoState()
        assert state.speed == 1.0

    def test_set_speed(self):
        state = DemoState()
        state.set_speed(5.0)
        assert state.speed == 5.0

    def test_invalid_speed_raises(self):
        state = DemoState()
        with pytest.raises(ValueError):
            state.set_speed(0)
        with pytest.raises(ValueError):
            state.set_speed(-1)


class TestDemoScenarioLoad:
    def test_load_dramatic_failure(self):
        scenario = DemoScenario.load(SCENARIOS_DIR, "dramatic-failure")
        assert scenario.name == "dramatic-failure"
        assert len(scenario.printers) == 1
        assert scenario.printers[0].serial == "0948AD561201838"
        assert scenario.printers[0].fixture == "bambu-h2d-failure-arc.jsonl"

    def test_load_print_complete(self):
        scenario = DemoScenario.load(SCENARIOS_DIR, "print-complete")
        assert scenario.name == "print-complete"
        assert scenario.printers[0].model == "A1"

    def test_load_happy_farm_multi_printer(self):
        scenario = DemoScenario.load(SCENARIOS_DIR, "happy-farm")
        assert len(scenario.printers) == 3  # a1 + 2 h2d
        serials = [p.serial for p in scenario.printers]
        assert len(set(serials)) == 3  # unique

    def test_load_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            DemoScenario.load(SCENARIOS_DIR, "nonexistent")


class TestDemoPrinterTopic:
    def test_topic_derived(self):
        p = DemoPrinter(
            id="x", display_name="x", serial="ABC123",
            fixture="x.jsonl",
        )
        assert p.topic_report == "device/ABC123/report"


class TestDemoEngineLifecycle:
    def test_start_stop_single_printer(self):
        engine = DemoEngine.from_scenario(
            "print-complete",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=1000.0,  # fast for test
        )
        engine.start()
        try:
            assert engine.broker_url.startswith("mqtt://127.0.0.1:")
            assert len(engine._threads) == 1
            # wait_until_done with generous timeout
            done = engine.wait_until_done(timeout=30)
            assert done, "engine did not finish within 30s"
        finally:
            engine.stop()

    def test_start_stop_multi_printer(self):
        engine = DemoEngine.from_scenario(
            "happy-farm",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=1000.0,
        )
        engine.start()
        try:
            assert len(engine._threads) == 3
            done = engine.wait_until_done(timeout=60)
            assert done
        finally:
            engine.stop()

    def test_double_start_raises(self):
        engine = DemoEngine.from_scenario(
            "print-complete",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=1000.0,
        )
        engine.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                engine.start()
        finally:
            engine.stop()


class TestDemoEngineControls:
    def test_pause_resume(self):
        engine = DemoEngine.from_scenario(
            "print-complete",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=10.0,  # slower so pause effect is observable
        )
        engine.start()
        try:
            engine.pause()
            assert engine.state.paused.is_set()
            engine.resume()
            assert not engine.state.paused.is_set()
        finally:
            engine.stop()

    def test_speed_change_at_runtime(self):
        engine = DemoEngine.from_scenario(
            "print-complete",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=1.0,
        )
        engine.start()
        try:
            engine.set_speed(100.0)
            assert engine.state.speed == 100.0
        finally:
            engine.stop()


class TestQAFiles:
    """Every scenario with a qa.yaml must have valid parseable YAML."""

    @pytest.mark.parametrize("scenario_name", [
        "dramatic-failure",
        "print-complete",
    ])
    def test_qa_file_parses(self, scenario_name):
        import yaml as pyyaml
        qa_path = SCENARIOS_DIR / scenario_name / "qa.yaml"
        data = pyyaml.safe_load(qa_path.read_text())
        assert data["scenario"] == scenario_name
        assert len(data["questions"]) >= 2
        for q in data["questions"]:
            assert "q" in q
            assert "a" in q
