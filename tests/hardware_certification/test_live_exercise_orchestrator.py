from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.hardware_certification import live_exercise
from ops.hardware_certification import artifact as artifact_module
from ops.hardware_certification import evidence as evidence_module
from ops.hardware_certification.active import Observation
from ops.hardware_certification.artifact import git_identity
from ops.hardware_certification.authorization import create_template
from ops.hardware_certification.evidence import EvidenceError, verify_artifact
from ops.hardware_certification.security import canonical_json_sha256


CORRELATION_KEY = "ab" * 32


@pytest.fixture(autouse=True)
def _simulate_clean_source_for_published_artifacts(monkeypatch):
    commit, _dirty = git_identity()
    monkeypatch.setattr(artifact_module, "git_identity", lambda: (commit, False))
    monkeypatch.setattr(evidence_module, "git_identity", lambda: (commit, False))
    monkeypatch.setattr(live_exercise, "git_identity", lambda: (commit, False))


class _Backend:
    def __init__(self, remote_name: str):
        self.remote_name = remote_name
        self.current = Observation("idle")
        self.closed = False

    def observe(self): return self.current
    def upload(self, remote_name): return remote_name == self.remote_name
    def start(self, remote_name):
        self.current = Observation("printing", remote_name, "job-1")
        return True
    def pause(self, _job_id): return False
    def close(self): self.closed = True


def _authorized_bambu_inputs(tmp_path: Path, actions: list[str]):
    commit, _dirty = git_identity()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "bambu", "target_alias": "lab-printer",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Fictional", "connection": {
            "host": "127.0.0.1", "port": 8883,
            "device_token": "ODIN-CERT-FICTIONAL-DEVICE",
            "access_code": "ODIN-CERT-FICTIONAL-ACCESS",
        },
    })); target.chmod(0o600)
    asset = tmp_path / "fixture.3mf"
    asset.write_bytes(b"ODIN disposable fixture"); asset.chmod(0o600)
    authorization = create_template(
        run_id, target, actions,
        test_asset_sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
    )
    authorization.update(
        authorization_state="AUTHORIZED", operator_confirmation=authorization["challenge"],
        operator_name="Test Operator", physical_area_clear=True,
        disposable_job_confirmed=True, emergency_stop_ready=True,
    )
    for action in authorization["actions"]:
        action["approved"] = True
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(json.dumps(authorization)); authorization_path.chmod(0o600)
    return target, asset, authorization_path


def test_live_exercise_consumes_authorization_then_publishes_sanitized_artifact(tmp_path: Path, monkeypatch):
    commit, _dirty = git_identity()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "bambu", "target_alias": "lab-printer",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Fictional", "connection": {
            "host": "127.0.0.1", "port": 8883,
            "device_token": "ODIN-CERT-FICTIONAL-DEVICE",
            "access_code": "ODIN-CERT-FICTIONAL-ACCESS",
        },
    })); target.chmod(0o600)
    asset = tmp_path / "fixture.3mf"
    asset.write_bytes(b"ODIN disposable fixture"); asset.chmod(0o600)
    authorization = create_template(
        run_id, target, ["upload", "start"],
        test_asset_sha256=hashlib.sha256(asset.read_bytes()).hexdigest(),
    )
    authorization.update(
        authorization_state="AUTHORIZED", operator_confirmation=authorization["challenge"],
        operator_name="Test Operator", physical_area_clear=True,
        disposable_job_confirmed=True, emergency_stop_ready=True,
    )
    for action in authorization["actions"]:
        action["approved"] = True
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(json.dumps(authorization)); authorization_path.chmod(0o600)
    created: list[_Backend] = []

    class FakeSession:
        def __init__(self, _target, remote_name, protected_asset):
            assert not authorization_path.exists()
            assert protected_asset.path == asset.resolve()
            created.append(_Backend(remote_name))

        def backend(self): return created[-1]

    monkeypatch.setattr(live_exercise, "BambuLiveSession", FakeSession)
    run_dir = live_exercise.execute_live_exercise(
        authorization_path=authorization_path, target_path=target,
        ledger_path=tmp_path / "state" / "used", artifact_root=tmp_path / "evidence",
        asset_path=asset,
    )
    assert created[0].closed is True
    manifest, results = verify_artifact(run_dir, expected_mode="exercise", expected_protocol="bambu")
    assert manifest["status"] == "pass"
    scope = results["bambu"]["authorization_scope_sha256"]
    assert manifest["authorization_scope_sha256"] == scope
    retained_scope = results["bambu"]["authorization_scope"]
    assert "target_alias" not in retained_scope
    assert scope == canonical_json_sha256(retained_scope)
    assert retained_scope["actions"] == ["upload", "start"]
    retained = "".join(path.read_text(errors="ignore") for path in run_dir.iterdir())
    assert authorization["target_config_sha256"] not in retained
    assert "ODIN-CERT-FICTIONAL-ACCESS" not in retained

    action_tamper_dir = tmp_path / "action-tamper"
    shutil.copytree(run_dir, action_tamper_dir)
    action_result_path = action_tamper_dir / "bambu.json"
    action_result = json.loads(action_result_path.read_text())
    action_result["authorization_scope"]["actions"] = ["upload"]
    action_result["authorization_scope_sha256"] = canonical_json_sha256(
        action_result["authorization_scope"]
    )
    action_result_path.write_text(json.dumps(action_result)); action_result_path.chmod(0o600)
    action_manifest_path = action_tamper_dir / "manifest.json"
    action_manifest = json.loads(action_manifest_path.read_text())
    action_manifest["authorization_scope"] = action_result["authorization_scope"]
    action_manifest["authorization_scope_sha256"] = action_result["authorization_scope_sha256"]
    action_manifest["files"]["bambu.json"] = hashlib.sha256(action_result_path.read_bytes()).hexdigest()
    action_manifest_path.write_text(json.dumps(action_manifest)); action_manifest_path.chmod(0o600)
    with pytest.raises(EvidenceError, match="action evidence is incoherent"):
        verify_artifact(action_tamper_dir)

    result_path = run_dir / "bambu.json"
    tampered_result = json.loads(result_path.read_text())
    tampered_result["authorization_scope_sha256"] = "b" * 64
    result_path.write_text(json.dumps(tampered_result)); result_path.chmod(0o600)
    manifest_path = run_dir / "manifest.json"
    tampered_manifest = json.loads(manifest_path.read_text())
    tampered_manifest["files"]["bambu.json"] = hashlib.sha256(result_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(tampered_manifest)); manifest_path.chmod(0o600)
    with pytest.raises(EvidenceError, match="authorization scope"):
        verify_artifact(run_dir)


def test_consumed_authorization_setup_failure_publishes_sanitized_evidence(tmp_path: Path, monkeypatch):
    target, asset, authorization_path = _authorized_bambu_inputs(tmp_path, ["upload", "start"])

    class FailingSession:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("ODIN-CERT-FICTIONAL-ACCESS must never be retained")

    monkeypatch.setattr(live_exercise, "BambuLiveSession", FailingSession)
    run_dir = live_exercise.execute_live_exercise(
        authorization_path=authorization_path, target_path=target,
        ledger_path=tmp_path / "state" / "used", artifact_root=tmp_path / "evidence",
        asset_path=asset,
    )
    manifest, results = verify_artifact(run_dir, expected_mode="exercise", expected_protocol="bambu")
    assert manifest["status"] == "fail"
    assert results["bambu"]["executed_actions"] == []
    assert results["bambu"]["assertions"][-1]["id"] == "active_failure"
    assert "ODIN-CERT-FICTIONAL-ACCESS" not in "".join(
        path.read_text(errors="ignore") for path in run_dir.iterdir()
    )


def test_consumed_authorization_close_failure_publishes_failure_after_actions(tmp_path: Path, monkeypatch):
    target, asset, authorization_path = _authorized_bambu_inputs(tmp_path, ["upload", "start"])

    class CloseFailBackend(_Backend):
        def close(self):
            self.closed = True
            raise RuntimeError("fictional cleanup failure")

    class CloseFailSession:
        def __init__(self, _target, remote_name, _asset):
            self.instance = CloseFailBackend(remote_name)

        def backend(self): return self.instance

    monkeypatch.setattr(live_exercise, "BambuLiveSession", CloseFailSession)
    run_dir = live_exercise.execute_live_exercise(
        authorization_path=authorization_path, target_path=target,
        ledger_path=tmp_path / "state" / "used", artifact_root=tmp_path / "evidence",
        asset_path=asset,
    )
    _manifest, results = verify_artifact(run_dir, expected_mode="exercise", expected_protocol="bambu")
    assert results["bambu"]["executed_actions"] == ["upload", "start"]
    assert results["bambu"]["assertions"][-1]["id"] == "active_failure"
    assert results["bambu"]["status"] == "fail"


def test_consumed_authorization_interrupt_publishes_before_reraising(tmp_path: Path, monkeypatch):
    target, asset, authorization_path = _authorized_bambu_inputs(tmp_path, ["upload", "start"])

    class InterruptBackend(_Backend):
        def start(self, _remote_name):
            raise KeyboardInterrupt

    class InterruptSession:
        def __init__(self, _target, remote_name, _asset):
            self.instance = InterruptBackend(remote_name)

        def backend(self): return self.instance

    monkeypatch.setattr(live_exercise, "BambuLiveSession", InterruptSession)
    with pytest.raises(KeyboardInterrupt):
        live_exercise.execute_live_exercise(
            authorization_path=authorization_path, target_path=target,
            ledger_path=tmp_path / "state" / "used", artifact_root=tmp_path / "evidence",
            asset_path=asset,
        )
    run_dirs = list((tmp_path / "evidence").iterdir())
    assert len(run_dirs) == 1
    _manifest, results = verify_artifact(
        run_dirs[0], expected_mode="exercise", expected_protocol="bambu",
    )
    assert results["bambu"]["executed_actions"] == ["upload"]
    assert results["bambu"]["assertions"][-1]["id"] == "active_failure"


def test_consumed_authorization_interrupt_survives_cleanup_failure(tmp_path: Path, monkeypatch):
    target, asset, authorization_path = _authorized_bambu_inputs(tmp_path, ["upload", "start"])

    class InterruptAndCloseFailBackend(_Backend):
        def start(self, _remote_name):
            raise KeyboardInterrupt

        def close(self):
            self.closed = True
            raise RuntimeError("fictional cleanup failure")

    class InterruptAndCloseFailSession:
        def __init__(self, _target, remote_name, _asset):
            self.instance = InterruptAndCloseFailBackend(remote_name)

        def backend(self): return self.instance

    monkeypatch.setattr(live_exercise, "BambuLiveSession", InterruptAndCloseFailSession)
    with pytest.raises(KeyboardInterrupt):
        live_exercise.execute_live_exercise(
            authorization_path=authorization_path, target_path=target,
            ledger_path=tmp_path / "state" / "used", artifact_root=tmp_path / "evidence",
            asset_path=asset,
        )
    run_dirs = list((tmp_path / "evidence").iterdir())
    assert len(run_dirs) == 1
    _manifest, results = verify_artifact(
        run_dirs[0], expected_mode="exercise", expected_protocol="bambu",
    )
    assert results["bambu"]["executed_actions"] == ["upload"]
    assert results["bambu"]["assertions"][-1]["id"] == "active_failure"
