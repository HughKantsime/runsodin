"""Contract tests for the demo CLI."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.modules.printers.telemetry.demo_cli import main

SCENARIOS_DIR = Path(__file__).parent.parent.parent / "demo_scenarios"
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "telemetry"


class TestCLI:
    def test_runs_print_complete_to_completion(self, capsys):
        """End-to-end: CLI starts + finishes a scenario without error."""
        exit_code = main([
            "print-complete",
            "--speed", "10000",
            "--scenarios-dir", str(SCENARIOS_DIR),
            "--fixtures-dir", str(FIXTURES_DIR),
            "--log-level", "WARNING",
        ])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "demo engine started" in out
        assert "print-complete" in out
        assert "broker URL" in out
        assert "mqtt://127.0.0.1:" in out
        assert "demo engine stopped" in out

    def test_dramatic_failure_fires_markers(self, capsys):
        """Markers from dramatic-failure should print via the on_marker
        callback during CLI run."""
        exit_code = main([
            "dramatic-failure",
            "--speed", "10000",
            "--scenarios-dir", str(SCENARIOS_DIR),
            "--fixtures-dir", str(FIXTURES_DIR),
            "--log-level", "WARNING",
        ])
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "FAILED" in out or "PAUSE" in out, (
            f"expected at least one marker to print. output:\n{out}"
        )

    def test_unknown_scenario_raises(self):
        with pytest.raises(FileNotFoundError):
            main([
                "nonexistent-scenario",
                "--speed", "10000",
                "--scenarios-dir", str(SCENARIOS_DIR),
                "--fixtures-dir", str(FIXTURES_DIR),
            ])
