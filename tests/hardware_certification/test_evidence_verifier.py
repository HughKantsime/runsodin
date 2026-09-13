from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ops.hardware_certification import evidence as evidence_module
from ops.hardware_certification.artifact import git_identity
from ops.hardware_certification.evidence import EvidenceError, EvidenceExpired, verify_artifact
from ops.hardware_certification.implementation import certification_implementation_sha256


def _write(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(root: Path, ended: datetime) -> None:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    commit, _dirty = git_identity()
    run_id = f"20260912T120000Z-{commit}"
    result = {
        "schema_version": 1, "run_id": run_id, "git_commit": commit,
        "mode": "observe", "protocol": "bambu",
        "certification_level": "live_passive_observation", "status": "pass",
        "started_at": (ended - timedelta(seconds=2)).isoformat(), "ended_at": ended.isoformat(),
        "assertion_counts": {"executed": 1, "passed": 1, "failed": 0, "blocked": 0, "skipped": 0, "xfailed": 0},
        "metrics": {"valid_sample_count": 2, "freshness_seconds": 0.1, "reconnect_count": 0, "model_family": "X1", "target_correlation_sha256": "c" * 64, "capabilities": ["status"]},
        "assertions": [{"id": "two_samples", "status": "pass", "reason_code": "observed", "duration_ms": 1.0}],
    }
    _write(root / "bambu.json", json.dumps(result))
    _write(root / "index.html", "<!doctype html><title>LIVE PASSIVE OBSERVATION</title>")
    _write(root / "junit.xml", '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="hardware_certification.bambu" name="two_samples"/></testsuite>')
    _write(root / "diagnostics.json", json.dumps({
        "schema_version": 1, "summary": "sanitized_protocol_result", "assertion_count": 1,
    }))
    files = {
        name: _sha(root / name)
        for name in ("bambu.json", "index.html", "junit.xml", "diagnostics.json")
    }
    manifest = {
        "schema_version": 1, "run_id": run_id, "git_commit": commit, "git_dirty": False,
        "mode": "observe", "protocol": "bambu", "status": "pass",
        "started_at": (ended - timedelta(seconds=2)).isoformat(), "ended_at": ended.isoformat(),
        "duration_seconds": 2.0,
        "junit": {"tests": 1, "failures": 0, "errors": 0, "skipped": 0, "xfailed": 0},
        "implementation_sha256": certification_implementation_sha256(),
        "sanitizer_passed": True, "result_references": ["bambu.json"], "files": files,
    }
    _write(root / "manifest.json", json.dumps(manifest))


def test_verified_observe_artifact_accepts_exact_hash_inventory(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact"
    _artifact(root, current)
    manifest, results = verify_artifact(
        root, expected_mode="observe", expected_protocol="bambu",
        now=current,
    )
    assert manifest["status"] == "pass"
    assert results["bambu"]["metrics"]["valid_sample_count"] == 2


def test_artifact_expires_after_repository_head_changes(tmp_path: Path, monkeypatch):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact"
    _artifact(root, current)
    monkeypatch.setattr(evidence_module, "git_identity", lambda: ("deadbee", False))
    with pytest.raises(EvidenceExpired, match="commit identity"):
        verify_artifact(root, now=current)


def test_dirty_artifact_is_rejected_without_optional_commit_argument(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact"
    _artifact(root, current)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["git_dirty"] = True
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="dirty-state"):
        verify_artifact(root, now=current)


def test_artifact_tamper_and_extra_file_fail_closed(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact"
    _artifact(root, current)
    _write(root / "bambu.json", "{}")
    with pytest.raises(EvidenceError, match="hash"):
        verify_artifact(root, now=current)
    _artifact(tmp_path / "extra", current)
    _write(tmp_path / "extra" / "unexpected.txt", "extra")
    with pytest.raises(EvidenceError, match="inventory"):
        verify_artifact(tmp_path / "extra", now=current)

    nested = tmp_path / "nested"
    _artifact(nested, current)
    (nested / "unmanifested").mkdir(mode=0o700)
    _write(nested / "unmanifested" / "hidden.txt", "retained but unhashed")
    with pytest.raises(EvidenceError, match="non-file entry"):
        verify_artifact(nested, now=current)

    manifest_extra = tmp_path / "manifest-extra"
    _artifact(manifest_extra, current)
    _write(manifest_extra / "notes.txt", "benign but outside the fixed format")
    manifest_path = manifest_extra / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["notes.txt"] = _sha(manifest_extra / "notes.txt")
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="format-exact"):
        verify_artifact(manifest_extra, now=current)


def test_artifact_root_symlink_is_rejected(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    real = tmp_path / "real"
    _artifact(real, current)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(EvidenceError, match="directory is invalid"):
        verify_artifact(linked, now=current)


def test_stale_physical_artifact_is_rejected(tmp_path: Path):
    ended = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact"
    _artifact(root, ended)
    with pytest.raises(EvidenceError, match="stale"):
        verify_artifact(root, now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc))


def test_artifact_permissions_and_junit_counts_fail_closed(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    broad = tmp_path / "broad"
    _artifact(broad, current)
    (broad / "bambu.json").chmod(0o644)
    with pytest.raises(EvidenceError, match="mode"):
        verify_artifact(broad, now=current)

    wrong_counts = tmp_path / "counts"
    _artifact(wrong_counts, current)
    manifest = json.loads((wrong_counts / "manifest.json").read_text())
    manifest["junit"]["tests"] = 2
    _write(wrong_counts / "manifest.json", json.dumps(manifest))
    with pytest.raises(EvidenceError, match="JUnit counts"):
        verify_artifact(wrong_counts, now=current)


def test_junit_declared_counts_cannot_hide_concrete_failure(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "forged-junit"
    _artifact(root, current)
    junit_path = root / "junit.xml"
    _write(
        junit_path,
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="two_samples"><failure message="hidden"/></testcase></testsuite>',
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["junit.xml"] = _sha(junit_path)
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="JUnit is malformed"):
        verify_artifact(root, now=current)


def test_junit_cannot_retain_machine_hostname(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "host-junit"
    _artifact(root, current)
    junit_path = root / "junit.xml"
    _write(
        junit_path,
        '<testsuite hostname="operator-host.local" tests="1" failures="0" errors="0" skipped="0">'
        '<testcase name="two_samples"/></testsuite>',
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["junit.xml"] = _sha(junit_path)
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="host identity"):
        verify_artifact(root, now=current)


def test_physical_artifact_expires_when_implementation_changes(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "old-implementation"
    _artifact(root, current)
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["implementation_sha256"] = "0" * 64
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="implementation identity"):
        verify_artifact(root, now=current)


@pytest.mark.parametrize(
    ("keyword", "value", "message"),
    [
        ("expected_model_family", "P1S", "model_family"),
        ("expected_firmware_version", "2.0", "firmware_version"),
        ("expected_api_version", "3.0", "api_version"),
    ],
)
def test_physical_artifact_expires_on_expected_device_identity_change(
    tmp_path: Path, keyword: str, value: str, message: str,
):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / keyword
    _artifact(root, current)
    with pytest.raises(EvidenceError, match=message):
        verify_artifact(root, now=current, **{keyword: value})


def test_manifest_and_result_status_mismatch_is_rejected(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "artifact"
    _artifact(root, current)
    result_path = root / "bambu.json"
    result = json.loads(result_path.read_text())
    result["status"] = "fail"
    _write(result_path, json.dumps(result))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["bambu.json"] = _sha(result_path)
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="status coherence"):
        verify_artifact(root, now=current)


def test_observe_result_status_is_recomputed_from_assertions(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "forged-status"
    _artifact(root, current)
    result_path = root / "bambu.json"
    result = json.loads(result_path.read_text())
    result["status"] = "fail"
    _write(result_path, json.dumps(result))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "fail"
    manifest["files"]["bambu.json"] = _sha(result_path)
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="result aggregate status"):
        verify_artifact(root, now=current)


def test_junit_testcase_identity_must_match_retained_assertion(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "forged-testcase"
    _artifact(root, current)
    junit_path = root / "junit.xml"
    _write(
        junit_path,
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="hardware_certification.bambu" name="different_action"/>'
        '</testsuite>',
    )
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["junit.xml"] = _sha(junit_path)
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="JUnit identity or outcome"):
        verify_artifact(root, now=current)


def test_passing_observe_evidence_requires_closed_reason_and_fresh_metrics(tmp_path: Path):
    current = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    root = tmp_path / "missing-freshness"
    _artifact(root, current)
    result_path = root / "bambu.json"
    result = json.loads(result_path.read_text())
    del result["metrics"]["freshness_seconds"]
    _write(result_path, json.dumps(result))
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["bambu.json"] = _sha(result_path)
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="protocol-result"):
        verify_artifact(root, now=current)

    unknown = tmp_path / "unknown-reason"
    _artifact(unknown, current)
    result_path = unknown / "bambu.json"
    result = json.loads(result_path.read_text())
    result["assertions"][0]["reason_code"] = "invented_claim"
    _write(result_path, json.dumps(result))
    manifest_path = unknown / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["bambu.json"] = _sha(result_path)
    _write(manifest_path, json.dumps(manifest))
    with pytest.raises(EvidenceError, match="protocol-result"):
        verify_artifact(unknown, now=current)
