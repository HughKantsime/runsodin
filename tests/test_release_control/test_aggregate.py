"""All components run, with audited overlap and deterministic evidence."""

import json
import sys
import threading
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from ops.release_control import aggregate, host_lock, practical_evidence
from ops.release_control.promotion_eligibility import EligibilityError, verify_eligibility
from ops.release_control.run_gate import execute, sha256, validate_result


@pytest.fixture(autouse=True)
def isolated_host_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(host_lock, "LOCK_PATH", tmp_path / "validation.lock")
    monkeypatch.delenv(host_lock.LOCK_FD_ENV, raising=False)


@pytest.mark.parametrize("failed_ids", [set(), {"cv01"}, {"cv02", "cv05", "cv10"}, {"cv06"}])
def test_aggregate_reports_every_real_result(tmp_path, monkeypatch, capsys, failed_ids):
    inventory = aggregate.load_inventory()
    monkeypatch.setattr(aggregate, "ROOT", tmp_path)
    monkeypatch.setenv("RELEASE_CONTROL_RUN_ID", "test-parallel")
    monkeypatch.setenv("DOCKER_HOST", "unix:///test/docker.sock")
    monkeypatch.setattr(aggregate.subprocess, "check_output", lambda *args, **kwargs: "abcdef0\n")
    calls = []
    executed_bytes = {}
    active = set()
    lock = threading.Lock()
    overlap = threading.Barrier(2)
    finished = set()

    def run_component(**kwargs):
        gate = kwargs["gate_id"]
        with lock:
            calls.append(kwargs)
            if gate == "cv03":
                assert {"cv02", "cv08", "cv09"} <= finished
                assert not active
            if gate in {"cv01", "cv04", "cv05", "cv07"}:
                assert {"cv02", "cv03", "cv08", "cv09"} <= finished
                assert not active
            if gate in {"cv10", "cv06"}:
                assert not active
                assert len(finished) == (8 if gate == "cv10" else 9)
            active.add(gate)
        if gate in {"cv02", "cv08"}:
            overlap.wait(timeout=10)  # Serial implementations fail this assertion.
        # Use the real result writer and schema validation with harmless commands.
        result = execute(
            gate_id=kwargs["gate_id"],
            command=[sys.executable, "-c", f"raise SystemExit({int(gate in failed_ids)})"],
            output=kwargs["output"], timeout=5, cwd=tmp_path,
        )
        passed = result["status"] == "pass"
        result["counts"] = dict(tests=1, passed=int(passed), failures=int(not passed),
                                errors=0, skipped=0, xfailed=0)
        kwargs["output"].write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        executed_bytes[kwargs["gate_id"]] = kwargs["output"].read_bytes()
        with lock:
            active.remove(gate)
            finished.add(gate)
        return result

    monkeypatch.setattr(aggregate, "execute", run_component)
    assert aggregate.main() == int(bool(failed_ids))
    run_dir = tmp_path / "artifacts/trusted-validation/test-parallel"
    assert sorted(call["gate_id"] for call in calls) == [
        component["id"].lower() for component in inventory["components"]
    ]
    by_id = {call["gate_id"]: call for call in calls}
    for component in inventory["components"]:
        call = by_id[component["id"].lower()]
        assert call["command"] == component["command"]
        assert call["timeout"] == component["timeout_seconds"]
        assert call["json_status"] == component["json_status"]
        assert call["isolate_process_group"] is True
    progress = capsys.readouterr().out.splitlines()
    assert sorted(line.split(':')[0] for line in progress[:10]) == [
        c["id"] for c in inventory["components"]
    ]
    assert progress[0].startswith(("CV02:", "CV08:"))
    assert progress[10].startswith("Aggregate wall time:")
    final = json.loads((run_dir / "result.json").read_text())
    validate_result(final)
    assert final["status"] == ("fail" if failed_ids else "pass")
    passed_count = 10 - len(failed_ids)
    assert final["counts"] == dict(tests=10, passed=passed_count, failures=10-passed_count,
                                   errors=0, skipped=0, xfailed=0)
    assert final["duration_seconds"] == round(sum(
        json.loads(Path(a["path"]).read_text())["duration_seconds"] for a in final["artifacts"]
    ), 3)
    assert "wall_time_seconds" not in final
    assert len(final["artifacts"]) == 10
    for index, (artifact, component) in enumerate(zip(final["artifacts"], inventory["components"])):
        path = Path(artifact["path"])
        assert path == run_dir / "components" / component["id"] / "result.json"
        assert artifact["sha256"] == sha256(path)
        result = json.loads(path.read_text())
        validate_result(result)
        assert result["log"]["sha256"] == sha256(Path(result["log"]["path"]))
        assert path.read_bytes() == executed_bytes[component["id"].lower()]
        assert result["status"] == ("fail" if component["id"].lower() in failed_ids else "pass")
    suite = ET.parse(run_dir / "junit.xml").getroot()
    assert suite.attrib == dict(name="trusted-validation", tests="10", failures=str(10-passed_count),
                                errors="0", skipped="0")
    assert [case.attrib["name"] for case in suite] == [c["id"].lower() for c in inventory["components"]]
    assert len(suite.findall("testcase/failure")) == 10-passed_count
    assert final["log"]["sha256"] == sha256(run_dir / "junit.xml")
    assert (run_dir / "index.html").is_file()
    if failed_ids:
        monkeypatch.setattr(practical_evidence, "TRUSTED_ROOT", run_dir.parent)
        with pytest.raises(practical_evidence.EvidenceError, match="AGGREGATE_NOT_PASSING"):
            practical_evidence.build_bundle(run_dir, "a" * 40)
        assert not (run_dir / "evidence").exists()
        # Promotion verifies a real bundle first; failed runs cannot supply one.
        with pytest.raises(EligibilityError, match="EVIDENCE_NODE_FORBIDDEN"):
            verify_eligibility(run_dir / "evidence", tmp_path / "request.json",
                               tmp_path / "validation.json", tmp_path / "promotion.json")


@pytest.mark.parametrize("mode", ["exception", "timeout", "status-only"])
def test_component_runner_failure_does_not_stop_other_components(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(aggregate, "ROOT", tmp_path)
    monkeypatch.setenv("RELEASE_CONTROL_RUN_ID", "runner-failure")
    monkeypatch.setenv("DOCKER_HOST", "unix:///test/docker.sock")
    monkeypatch.setattr(aggregate.subprocess, "check_output", lambda *a, **k: "abcdef0\n")
    calls = []

    def run(**kwargs):
        gate = kwargs["gate_id"]
        calls.append(gate)
        if gate == "cv01" and mode == "exception":
            raise RuntimeError("sensitive exception detail")
        code = "import time; time.sleep(5)" if gate == "cv01" and mode == "timeout" else "pass"
        result = execute(gate_id=gate, command=[sys.executable, "-c", code],
                         output=kwargs["output"], timeout=1, cwd=tmp_path,
                         isolate_process_group=True)
        if gate == "cv01" and mode == "status-only":
            result["status"] = "fail"
        return result

    monkeypatch.setattr(aggregate, "execute", run)
    assert aggregate.main() == 1
    assert sorted(calls) == [f"cv{i:02d}" for i in range(1, 11)]
    directory = tmp_path / "artifacts/trusted-validation/runner-failure"
    final = json.loads((directory / "result.json").read_text())
    assert final["counts"]["failures"] == 1
    assert final["timed_out"] is (mode == "timeout")
    assert "sensitive exception detail" not in (directory / "result.json").read_text()
    if mode == "exception":
        failed = json.loads((directory / "components/cv01-failed.json").read_text())
        validate_result(failed)
        assert failed["log"]["sha256"] == sha256(Path(failed["log"]["path"]))


@pytest.mark.parametrize("phases", [
    [], [[[]]], [[["CV01"]]],
    [[[f"CV{i:02d}" for i in range(1, 11)]]],
    [[["CV01", "CV03", "CV04", "CV05", "CV07", "CV08", "CV09"], ["CV02"]], [["CV10"]], [["CV06"]]],
    [[[None]]],
    # CV03 may not overlap even non-build checkout writers.
    [[["CV02"], ["CV08", "CV09"]], [["CV03"], ["CV01", "CV04", "CV05", "CV07"]],
     [["CV10"]], [["CV06"]]],
    # A private contracts artifact root does not isolate source fixture writes.
    [[["CV02"], ["CV01", "CV04", "CV05", "CV07", "CV08", "CV09"]],
     [["CV03"]], [["CV10"]], [["CV06"]]],
    # CV05 temporary secrets must not be traversed by another image build.
    [[["CV02"], ["CV08", "CV09"]], [["CV03"]],
     [["CV01", "CV04"], ["CV05", "CV07"]], [["CV10"]], [["CV06"]]],
])
def test_inventory_rejects_incomplete_or_unsafe_schedule(tmp_path, monkeypatch, phases):
    from ops.release_control import policy
    inventory = policy.load_inventory()
    inventory["execution_phases"] = phases
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory))
    monkeypatch.setattr(policy, "INVENTORY", path)
    with pytest.raises(ValueError):
        policy.load_inventory()


def test_component_timeouts_remain_unchanged():
    assert [c["timeout_seconds"] for c in aggregate.load_inventory()["components"]] == [
        2700, 5700, 2700, 2700, 3600, 5400, 3600, 1800, 1800, 1200,
    ]


@pytest.mark.parametrize("failure", ["mkdir", "validation", "write"])
def test_component_setup_validation_and_write_failures_continue(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(aggregate, "ROOT", tmp_path)
    monkeypatch.setenv("RELEASE_CONTROL_RUN_ID", "io-failure")
    monkeypatch.setenv("DOCKER_HOST", "unix:///test/docker.sock")
    monkeypatch.setattr(aggregate.subprocess, "check_output", lambda *a, **k: "abcdef0\n")
    calls = []
    original_mkdir, original_write = Path.mkdir, Path.write_text

    def mkdir(path, *args, **kwargs):
        if path.name == "CV01" and failure == "mkdir":
            raise PermissionError("private exception details")
        return original_mkdir(path, *args, **kwargs)

    def write(path, *args, **kwargs):
        if path.parent.name == "CV01" and path.name == "result.json" and failure == "write":
            raise OSError("private exception details")
        return original_write(path, *args, **kwargs)

    def validate(result):
        # This targets the second validation, after zero-count normalization.
        if result["gate_id"] == "cv01" and result["counts"]["tests"] and failure == "validation":
            raise ValueError("private exception details")
        validate_result(result)

    def run(**kwargs):
        calls.append(kwargs["gate_id"])
        # Write outside CV01 so the injected failure tests aggregate's own write.
        return execute(gate_id=kwargs["gate_id"], command=[sys.executable, "-c", "pass"],
                       output=tmp_path / f"{kwargs['gate_id']}.json", timeout=5, cwd=tmp_path)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "write_text", write)
    monkeypatch.setattr(aggregate, "validate_result", validate)
    monkeypatch.setattr(aggregate, "execute", run)
    assert aggregate.main() == 1
    assert set(calls) == {f"cv{i:02d}" for i in range(1, 11)} - ({"cv01"} if failure == "mkdir" else set())
    directory = tmp_path / "artifacts/trusted-validation/io-failure"
    final = json.loads((directory / "result.json").read_text())
    validate_result(final)
    assert final["counts"]["failures"] == 1
    assert len(final["artifacts"]) == 10
    failed = json.loads((directory / "components/cv01-failed.json").read_text())
    validate_result(failed)
    assert failed["status"] == "fail"
    assert "private exception details" not in json.dumps(final)
    assert failed["log"]["sha256"] == sha256(Path(failed["log"]["path"]))


def test_progress_is_printed_before_later_components_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(aggregate, "ROOT", tmp_path)
    monkeypatch.setenv("RELEASE_CONTROL_RUN_ID", "progress-test")
    monkeypatch.setenv("DOCKER_HOST", "unix:///test/docker.sock")
    monkeypatch.setattr(aggregate.subprocess, "check_output", lambda *a, **k: "abcdef0\n")

    def run(**kwargs):
        if kwargs["gate_id"] == "cv09":
            assert "CV08: pass" in capsys.readouterr().out
        if kwargs["gate_id"] == "cv06":
            assert "CV10: pass" in capsys.readouterr().out
        return execute(gate_id=kwargs["gate_id"], command=[sys.executable, "-c", "pass"],
                       output=kwargs["output"], timeout=5, cwd=tmp_path)

    monkeypatch.setattr(aggregate, "execute", run)
    assert aggregate.main() == 0
