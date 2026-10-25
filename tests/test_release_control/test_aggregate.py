"""Fail-fast keeps complete, verifiable reporting without running later gates."""

import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from ops.release_control import aggregate, practical_evidence
from ops.release_control.promotion_eligibility import EligibilityError, verify_eligibility
from ops.release_control.run_gate import execute, sha256, validate_result


@pytest.mark.parametrize("failed_index", [0, 4, None], ids=["first-failure", "mid-failure", "all-pass"])
def test_aggregate_fail_fast(tmp_path, monkeypatch, failed_index):
    inventory = aggregate.load_inventory()
    monkeypatch.setattr(aggregate, "ROOT", tmp_path)
    monkeypatch.setenv("RELEASE_CONTROL_RUN_ID", "test-fail-fast")
    monkeypatch.setenv("DOCKER_HOST", "unix:///test/docker.sock")
    monkeypatch.setattr(aggregate.subprocess, "check_output", lambda *args, **kwargs: "abcdef0\n")
    calls = []
    executed_bytes = {}

    def run_component(**kwargs):
        index = len(calls)
        calls.append(kwargs)
        # Use the real result writer and schema validation with harmless commands.
        result = execute(
            gate_id=kwargs["gate_id"],
            command=[sys.executable, "-c", f"raise SystemExit({int(index == failed_index)})"],
            output=kwargs["output"], timeout=5, cwd=tmp_path,
        )
        passed = result["status"] == "pass"
        result["counts"] = dict(tests=1, passed=int(passed), failures=int(not passed),
                                errors=0, skipped=0, xfailed=0)
        kwargs["output"].write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        executed_bytes[kwargs["gate_id"]] = kwargs["output"].read_bytes()
        return result

    monkeypatch.setattr(aggregate, "execute", run_component)
    assert aggregate.main() == (0 if failed_index is None else 1)
    run_dir = tmp_path / "artifacts/trusted-validation/test-fail-fast"
    expected_calls = 10 if failed_index is None else failed_index + 1
    assert [call["gate_id"] for call in calls] == [
        component["id"].lower() for component in inventory["components"][:expected_calls]
    ]
    for call, component in zip(calls, inventory["components"]):
        assert call["command"] == component["command"]
        assert call["timeout"] == component["timeout_seconds"]
        assert call["json_status"] == component["json_status"]
    final = json.loads((run_dir / "result.json").read_text())
    validate_result(final)
    assert final["status"] == ("pass" if failed_index is None else "fail")
    passed_count = 10 if failed_index is None else failed_index
    assert final["counts"] == dict(tests=10, passed=passed_count, failures=10-passed_count,
                                   errors=0, skipped=0, xfailed=0)
    assert len(final["artifacts"]) == 10
    for index, (artifact, component) in enumerate(zip(final["artifacts"], inventory["components"])):
        path = Path(artifact["path"])
        assert path == run_dir / "components" / component["id"] / "result.json"
        assert artifact["sha256"] == sha256(path)
        result = json.loads(path.read_text())
        validate_result(result)
        assert result["log"]["sha256"] == sha256(Path(result["log"]["path"]))
        if index < expected_calls:
            assert path.read_bytes() == executed_bytes[component["id"].lower()]
        else:
            assert result["status"] == "fail"
            assert result["duration_seconds"] == 0
            assert result["started_at"] == result["ended_at"]
            assert result["exit_code"] is None
            assert result["counts"] == dict(tests=1, passed=0, failures=1, errors=0, skipped=0, xfailed=0)
            assert result["findings"] == [f"not run: CV{failed_index+1:02d} failed (fail-fast)"]
            assert result["artifacts"] == []
    suite = ET.parse(run_dir / "junit.xml").getroot()
    assert suite.attrib == dict(name="trusted-validation", tests="10", failures=str(10-passed_count),
                                errors="0", skipped="0")
    assert [case.attrib["name"] for case in suite] == [c["id"].lower() for c in inventory["components"]]
    assert len(suite.findall("testcase/failure")) == 10-passed_count
    assert final["log"]["sha256"] == sha256(run_dir / "junit.xml")
    assert (run_dir / "index.html").is_file()
    if failed_index is not None:
        monkeypatch.setattr(practical_evidence, "TRUSTED_ROOT", run_dir.parent)
        with pytest.raises(practical_evidence.EvidenceError, match="AGGREGATE_NOT_PASSING"):
            practical_evidence.build_bundle(run_dir, "a" * 40)
        assert not (run_dir / "evidence").exists()
        # Promotion verifies a real bundle first; failed runs cannot supply one.
        with pytest.raises(EligibilityError, match="EVIDENCE_NODE_FORBIDDEN"):
            verify_eligibility(run_dir / "evidence", tmp_path / "request.json",
                               tmp_path / "validation.json", tmp_path / "promotion.json")
