"""The EDU JUnit wrapper must reject soft pytest outcomes."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "ops/demo/run_junit_gate.py"


def _run_fixture(tmp_path, source: str):
    test_file = tmp_path / "test_fixture.py"
    test_file.write_text(source)
    return subprocess.run(
        [
            sys.executable,
            str(GATE),
            "--",
            sys.executable,
            "-m",
            "pytest",
            str(test_file),
            "-q",
            "--junitxml={junit}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_junit_gate_accepts_only_executed_passing_tests(tmp_path):
    result = _run_fixture(tmp_path, "def test_pass():\n    assert True\n")
    assert result.returncode == 0, result.stderr
    assert "zero skips/xfails" in result.stdout


def test_junit_gate_rejects_skipped_or_xfailed_tests(tmp_path):
    result = _run_fixture(
        tmp_path,
        "import pytest\n@pytest.mark.xfail(reason='soft')\ndef test_soft():\n    assert False\n",
    )
    assert result.returncode == 1
    assert "skipped=1" in result.stderr
