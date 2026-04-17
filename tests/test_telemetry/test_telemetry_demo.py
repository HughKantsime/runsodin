"""Contract tests for the demo engine (Phase 6 minimum viable)."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from backend.modules.printers.telemetry.demo import (
    DemoEngine,
    DemoMarker,
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


class TestSeekAndLoop:
    """Runtime seek + loop controls on DemoEngine (T6.4 + T6.6)."""

    def test_seek_state(self):
        state = DemoState()
        assert state.consume_seek() is None
        state.request_seek("2026-04-16T14:50:00Z")
        # first read consumes it
        assert state.consume_seek() == "2026-04-16T14:50:00Z"
        # second read is cleared
        assert state.consume_seek() is None

    def test_loop_window_validation(self):
        state = DemoState()
        with pytest.raises(ValueError, match="reversed"):
            state.set_loop("2026-04-16T15:00:00Z", "2026-04-16T14:00:00Z")

    def test_loop_window_set_and_clear(self):
        state = DemoState()
        assert state.loop_window is None
        state.set_loop("2026-04-16T14:00:00Z", "2026-04-16T15:00:00Z")
        assert state.loop_window == (
            "2026-04-16T14:00:00Z",
            "2026-04-16T15:00:00Z",
        )
        state.clear_loop()
        assert state.loop_window is None

    def test_binary_search_finds_iso(self):
        from backend.modules.printers.telemetry.demo import _find_iso_index
        events = [
            ("2026-04-16T14:00:00Z", 1.0, {}),
            ("2026-04-16T14:30:00Z", 2.0, {}),
            ("2026-04-16T15:00:00Z", 3.0, {}),
            ("2026-04-16T15:30:00Z", 4.0, {}),
        ]
        assert _find_iso_index(events, "2026-04-16T14:00:00Z") == 0
        assert _find_iso_index(events, "2026-04-16T14:15:00Z") == 1  # first at/after
        assert _find_iso_index(events, "2026-04-16T15:00:00Z") == 2
        assert _find_iso_index(events, "2026-04-16T16:00:00Z") is None  # past end
        assert _find_iso_index([], "any") is None

    def test_engine_seek_to_api(self):
        engine = DemoEngine.from_scenario(
            "print-complete",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=1000.0,
        )
        engine.start()
        try:
            engine.seek_to("2026-04-16T14:45:00Z")
            # state records the request until a publisher consumes it
            assert engine.state.seek_to_iso == "2026-04-16T14:45:00Z" or engine.state.seek_to_iso is None
            done = engine.wait_until_done(timeout=30)
            assert done
        finally:
            engine.stop()

    def test_engine_set_and_clear_loop(self):
        engine = DemoEngine.from_scenario(
            "print-complete",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=1000.0,
        )
        # set + clear should not require engine to be started
        engine.set_loop("2026-04-16T14:43:30Z", "2026-04-16T14:44:00Z")
        assert engine.state.loop_window is not None
        engine.clear_loop()
        assert engine.state.loop_window is None


class TestMarkers:
    """T6.8 — markers fire when scenario crosses their at_iso (no UI needed)."""

    def test_load_dramatic_failure_markers(self):
        """dramatic-failure has a markers.yaml with 5 cues."""
        scenario = DemoScenario.load(SCENARIOS_DIR, "dramatic-failure")
        assert len(scenario.markers) >= 5
        assert all(isinstance(m, DemoMarker) for m in scenario.markers)
        # sanity on first marker content
        first = scenario.markers[0]
        assert first.at_iso.startswith("2026-04-16T14:")
        assert "PREPARE" in first.label or "FINISH" in first.label

    def test_no_markers_file_is_ok(self):
        """Scenarios without markers.yaml load cleanly with empty markers."""
        scenario = DemoScenario.load(SCENARIOS_DIR, "ams-swap")
        assert scenario.markers == []

    def test_marker_callback_fires_when_crossed(self):
        """Playing dramatic-failure at high speed must fire every marker once."""
        fired: list[DemoMarker] = []
        engine = DemoEngine.from_scenario(
            "dramatic-failure",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=10000.0,  # very fast
            on_marker=fired.append,
        )
        engine.start()
        try:
            done = engine.wait_until_done(timeout=60)
            assert done
        finally:
            engine.stop()

        # all 5+ markers should have fired (each at most once)
        assert len(fired) == len(engine.scenario.markers)
        # unique
        assert len({m.at_iso for m in fired}) == len(fired)

    def test_marker_callback_raises_does_not_crash(self):
        """Callback exceptions must be swallowed + logged."""
        def bad_callback(m):
            raise RuntimeError("boom")

        engine = DemoEngine.from_scenario(
            "dramatic-failure",
            scenarios_dir=SCENARIOS_DIR,
            fixtures_dir=FIXTURES_DIR,
            speed=10000.0,
            on_marker=bad_callback,
        )
        engine.start()
        try:
            done = engine.wait_until_done(timeout=60)
            assert done  # engine completes despite callback crashes
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
