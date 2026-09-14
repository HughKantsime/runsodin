import json
import sys
from pathlib import Path

import pytest

from ops.release_control.run_gate import command_environment, execute, validate_result


def _junit_command(xml: str) -> list[str]:
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])"
    return [sys.executable, "-c", code, "{junit}", xml]


def _run_junit(tmp_path: Path, xml: str):
    return execute(gate_id="fixture", command=_junit_command(xml), output=tmp_path / "result.json",
                   timeout=5, junit=tmp_path / "junit.xml", cwd=tmp_path)


def test_RS01_clean_junit_passes(tmp_path):
    result = _run_junit(tmp_path, '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="ok"/></testsuite>')
    assert result["status"] == "pass"
    assert result["counts"]["passed"] == 1


def test_RS02_zero_test_junit_fails(tmp_path):
    result = _run_junit(tmp_path, '<testsuite tests="0" failures="0" errors="0" skipped="0"/>')
    assert result["status"] == "fail"


def test_RS03_hidden_testcase_failure_fails(tmp_path):
    result = _run_junit(tmp_path, '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="bad"><failure/></testcase></testsuite>')
    assert result["status"] == "fail"


def test_RS04_declared_derived_mismatch_fails(tmp_path):
    result = _run_junit(tmp_path, '<testsuite tests="2" failures="0" errors="0" skipped="0"><testcase name="one"/></testsuite>')
    assert result["status"] == "fail"


def test_RS05_skip_fails(tmp_path):
    result = _run_junit(tmp_path, '<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase name="skip"><skipped message="skip"/></testcase></testsuite>')
    assert result["status"] == "fail"


def test_RS06_xfail_fails(tmp_path):
    result = _run_junit(tmp_path, '<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase name="xf"><skipped type="pytest.xfail"/></testcase></testsuite>')
    assert result["status"] == "fail"
    assert result["counts"]["xfailed"] == 1


def test_RS07_timeout_fails(tmp_path):
    result = execute(gate_id="timeout", command=[sys.executable, "-c", "import time; time.sleep(2)"],
                     output=tmp_path / "result.json", timeout=1, cwd=tmp_path)
    assert result["status"] == "fail" and result["timed_out"] is True


def test_RS08_missing_required_artifact_fails(tmp_path):
    result = execute(gate_id="missing", command=[sys.executable, "-c", "pass"],
                     output=tmp_path / "result.json", timeout=5,
                     expected_artifacts=["missing.json"], cwd=tmp_path)
    assert result["status"] == "fail"


def test_RS09_invalid_result_schema_is_rejected():
    with pytest.raises(ValueError, match="schema violation"):
        validate_result({"schema_version": 1, "status": "maybe"})


def test_RS10_scanner_error_fails(tmp_path):
    result = execute(gate_id="scanner", command=[sys.executable, "-c", "raise SystemExit(2)"],
                     output=tmp_path / "result.json", timeout=5, cwd=tmp_path)
    assert result["status"] == "fail"


def test_RS11_hard_finding_fails(tmp_path):
    result = execute(gate_id="scanner", command=[sys.executable, "-c", "raise SystemExit(1)"],
                     output=tmp_path / "result.json", timeout=5, cwd=tmp_path)
    assert result["status"] == "fail"


def test_RS12_secret_pattern_is_redacted_and_fails(tmp_path):
    command = [sys.executable, "-c", "print('-----BEGIN PRIVATE KEY-----')"]
    result = execute(gate_id="secret", command=command, output=tmp_path / "result.json", timeout=5, cwd=tmp_path)
    assert result["status"] == "fail"
    assert "BEGIN PRIVATE KEY" not in Path(result["log"]["path"]).read_text()


def test_gate_subprocess_does_not_inherit_unknown_host_variables(tmp_path, monkeypatch):
    marker = "ambient-credential-that-must-not-cross-boundary"
    monkeypatch.setenv("UNRELATED_CLOUD_SECRET", marker)
    command = [sys.executable, "-c", "import os; print(os.getenv('UNRELATED_CLOUD_SECRET', 'absent'))"]
    result = execute(gate_id="environment", command=command, output=tmp_path / "result.json",
                     timeout=5, cwd=tmp_path)
    log = Path(result["log"]["path"]).read_text()
    assert result["status"] == "pass"
    assert log.strip() == "absent"
    assert marker not in log


def test_environment_override_must_be_explicitly_allowlisted():
    with pytest.raises(ValueError, match="not allowlisted"):
        command_environment(overrides={"UNLISTED": "value"})


def test_retained_text_artifact_is_redacted_and_fails_gate(tmp_path):
    artifact = tmp_path / "scanner.json"
    command = [sys.executable, "-c", "from pathlib import Path; Path('scanner.json').write_text('-----BEGIN PRIVATE KEY-----')"]
    result = execute(gate_id="artifact_secret", command=command, output=tmp_path / "result.json",
                     timeout=5, expected_artifacts=["scanner.json"], cwd=tmp_path)
    assert result["status"] == "fail"
    assert "BEGIN PRIVATE KEY" not in artifact.read_text()
    assert "BEGIN PRIVATE KEY" not in json.dumps(result)
