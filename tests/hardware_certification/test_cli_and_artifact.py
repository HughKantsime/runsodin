from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.hardware_certification import artifact as artifact_module
from ops.hardware_certification import cli as cli_module
from ops.hardware_certification import evidence as evidence_module
from ops.hardware_certification import runner as runner_module
from ops.hardware_certification.artifact import git_full_commit, git_identity, publish_result
from ops.hardware_certification.cli import main
from ops.hardware_certification.evidence import verify_artifact
from ops.hardware_certification.runner import _sanitize_junit
from ops.hardware_certification.passive.live import observe_target_file
from ops.hardware_certification.passive.observers import ObservationError, ObservationSummary, observe_elegoo
from ops.hardware_certification.passive.parsers import ParsedSample
from ops.hardware_certification.passive.transports import PassiveWebSocketTransport


CORRELATION_KEY = "ab" * 32
MISSING = object()
INVALID_ELEGOO_VERSIONS = (
    MISSING, None, "", "   ", "unknown", "UNKNOWN", " Unknown ", "N/A", "N / A",
    "Not Available", "UNDEFINED", "nil", "not set", "TBD", "pending", "V1/unsafe",
    123, True,
)


@pytest.fixture(autouse=True)
def _simulate_clean_source_for_published_artifacts(monkeypatch):
    commit, _dirty = git_identity()
    monkeypatch.setattr(artifact_module, "git_identity", lambda: (commit, False))
    monkeypatch.setattr(evidence_module, "git_identity", lambda: (commit, False))


def _result(run_id: str, commit: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1, "run_id": run_id, "git_commit": commit,
        "mode": "observe", "protocol": "moonraker",
        "certification_level": "live_passive_observation", "status": "pass",
        "started_at": now, "ended_at": now,
        "assertion_counts": {"executed": 1, "passed": 1, "failed": 0, "blocked": 0, "skipped": 0, "xfailed": 0},
        "metrics": {"valid_sample_count": 2, "freshness_seconds": 0.1, "reconnect_count": 0, "model_family": "Fictional", "target_correlation_sha256": "c" * 64, "capabilities": ["status"]},
        "assertions": [{"id": "two_valid_samples", "status": "pass", "reason_code": "observed", "duration_ms": 1}],
    }


def test_publish_result_is_private_verified_and_has_clickable_html(tmp_path: Path):
    commit, _dirty = git_identity()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    run_dir = publish_result(_result(run_id, commit), tmp_path / "evidence")
    manifest, results = verify_artifact(run_dir)
    assert manifest["status"] == "pass" and results["moonraker"]["status"] == "pass"
    assert (run_dir / "diagnostics.json").is_file()
    assert (run_dir / "index.html").read_text().startswith("<!doctype html>")
    assert (run_dir.stat().st_mode & 0o777) == 0o700
    assert all((item.stat().st_mode & 0o777) == 0o600 for item in run_dir.iterdir())


def test_publish_result_never_exposes_partial_directory(tmp_path: Path, monkeypatch):
    commit, _dirty = git_identity()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    output = tmp_path / "evidence"
    monkeypatch.setattr(artifact_module, "scan_tree", lambda _path: ["synthetic finding"])
    with pytest.raises(artifact_module.ArtifactError, match="privacy"):
        publish_result(_result(run_id, commit), output)
    assert not (output / run_id).exists()
    assert not any(path.name.startswith(".") for path in output.iterdir())


def test_publish_result_cleans_staging_on_keyboard_interrupt(tmp_path: Path, monkeypatch):
    commit, _dirty = git_identity()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    output = tmp_path / "evidence"

    def interrupted(_path):
        raise KeyboardInterrupt

    monkeypatch.setattr(artifact_module, "scan_tree", interrupted)
    with pytest.raises(KeyboardInterrupt):
        publish_result(_result(run_id, commit), output)
    assert list(output.iterdir()) == []


def test_replay_runner_cleans_staging_on_keyboard_interrupt(tmp_path: Path, monkeypatch):
    output = tmp_path / "replay"
    monkeypatch.setattr(runner_module, "ARTIFACT_ROOT", output)
    monkeypatch.setattr(
        runner_module, "_git",
        lambda *args: "7d46cf1" * 5 + "7d46c" if args == ("rev-parse", "HEAD") else "dirty",
    )

    def fake_pytest(command, **_kwargs):
        junit = Path(next(item.split("=", 1)[1] for item in command if item.startswith("--junitxml=")))
        junit.write_text(
            '<testsuite hostname="operator-host.local" tests="4" failures="0" errors="0" skipped="0">'
            '<testcase classname="tests.hardware" name="bambu_case"/>'
            '<testcase classname="tests.hardware" name="elegoo_case"/>'
            '<testcase classname="tests.hardware" name="moonraker_case"/>'
            '<testcase classname="tests.hardware" name="prusalink_case"/>'
            '</testsuite>',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    def interrupted(_path):
        raise KeyboardInterrupt

    monkeypatch.setattr(runner_module.subprocess, "run", fake_pytest)
    monkeypatch.setattr(runner_module, "scan_tree", interrupted)
    with pytest.raises(KeyboardInterrupt):
        runner_module.main()
    assert list(output.iterdir()) == []


def test_replay_runner_records_full_commit_but_keeps_short_default_run_suffix():
    source = Path(runner_module.__file__).read_text(encoding="utf-8")
    assert 'commit = _git("rev-parse", "HEAD")' in source
    assert "f\"{started.strftime('%Y%m%dT%H%M%SZ')}-{commit[:7]}\"" in source


def test_replay_verifier_full_commit_helper_records_full_identity():
    commit = git_full_commit()
    assert len(commit) == 40
    int(commit, 16)


def test_replay_junit_reconstruction_discards_failure_text_paths_and_properties(tmp_path: Path):
    junit = tmp_path / "junit.xml"
    secret = "ODIN-CERT-FICTIONAL-SECRET"
    junit.write_text(
        f'<testsuite hostname="operator-host.local" tests="1" failures="1" errors="0" skipped="0">'
        f'<properties><property name="endpoint" value="10.20.30.40"/></properties>'
        f'<testcase classname="tests.hardware" name="leaky {secret}">'
        f'<failure message="{secret}">/Users/operator/private/file.gcode</failure>'
        f'<system-out>device/ABC123/report</system-out></testcase></testsuite>',
        encoding="utf-8",
    )
    _sanitize_junit(junit)
    retained = junit.read_text(encoding="utf-8")
    for forbidden in (secret, "operator-host.local", "10.20.30.40", "/Users/operator", "file.gcode", "device/ABC123/report", "system-out", "properties"):
        assert forbidden not in retained
    assert '<failure type="sanitized"' in retained


def test_cli_failure_output_is_constant_and_never_echoes_exception(capsys, monkeypatch):
    secret = "ODIN-CERT-FICTIONAL-SECRET"

    def fail(_args):
        raise RuntimeError(f"connection failed at 10.20.30.40 with {secret}")

    monkeypatch.setattr(cli_module, "_report", fail)
    assert cli_module.main(["report", "/nonexistent/evidence"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "hardware certification failed: report_failed\n"
    assert secret not in captured.err and "10.20.30.40" not in captured.err


def test_authorize_template_cli_writes_default_deny_mode_0600(tmp_path: Path):
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps({
        "schema_version": 1, "protocol": "moonraker", "target_alias": "lab-printer",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Fictional", "connection": {"host": "127.0.0.1", "port": 7125},
    }))
    target_path.chmod(0o600)
    output = tmp_path / "authorization.json"
    assert main([
        "authorize-template", "--target-config", str(target_path),
        "--actions", "pause,resume,cancel", "--output", str(output),
    ]) == 0
    payload = json.loads(output.read_text())
    assert payload["authorization_state"] == "DRAFT"
    assert all(not item["approved"] for item in payload["actions"])
    assert (output.stat().st_mode & 0o777) == 0o600


def test_authorize_template_rejects_artifact_tree_and_insecure_output_parent(tmp_path: Path):
    target_path = tmp_path / "target.json"
    target_path.write_text(json.dumps({
        "schema_version": 1, "protocol": "moonraker", "target_alias": "lab-printer",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Fictional", "connection": {"host": "127.0.0.1", "port": 7125},
    }))
    target_path.chmod(0o600)
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir(mode=0o700)
    assert main([
        "authorize-template", "--target-config", str(target_path),
        "--actions", "pause", "--output", str(artifact_root / "authorization.json"),
        "--artifact-root", str(artifact_root),
    ]) == 2
    assert not (artifact_root / "authorization.json").exists()

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o777)
    insecure.chmod(0o777)
    assert main([
        "authorize-template", "--target-config", str(target_path),
        "--actions", "pause", "--output", str(insecure / "authorization.json"),
        "--artifact-root", str(artifact_root),
    ]) == 2
    assert not (insecure / "authorization.json").exists()


def test_verify_cli_has_no_optional_commit_override():
    with pytest.raises(SystemExit):
        cli_module.build_parser().parse_args([
            "verify-artifact", "/fictional/evidence", "--commit", "deadbee",
        ])


def test_public_verify_and_report_commands_reject_changed_head(tmp_path: Path, monkeypatch):
    commit, _dirty = git_identity()
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    run_dir = publish_result(_result(run_id, commit), tmp_path / "evidence")
    monkeypatch.setattr(evidence_module, "git_identity", lambda: ("deadbee", False))
    assert main(["verify-artifact", str(run_dir)]) == 2
    assert main(["report", str(run_dir)]) == 2


@pytest.mark.parametrize("reason_code", ["target_identity_mismatch", "device_version_mismatch"])
def test_elegoo_identity_mismatch_failures_publish_schema_valid_evidence(
    tmp_path: Path, reason_code: str,
):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "elegoo", "target_alias": "lab",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Centauri Carbon",
        "connection": {"host": "127.0.0.1", "port": 3030},
    }))
    target.chmod(0o600)

    def observer(_target):
        raise ObservationError(reason_code)

    result = observe_target_file(
        target, artifact_root=tmp_path / "evidence", observer=observer,
    )
    run_dir = publish_result(result, tmp_path / "evidence")
    manifest, results = verify_artifact(
        run_dir, expected_mode="observe", expected_protocol="elegoo",
    )
    assert manifest["status"] == "fail"
    assert results["elegoo"]["assertions"][0]["reason_code"] == reason_code


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in ("FirmwareVersion", "ProtocolVersion")
        for value in INVALID_ELEGOO_VERSIONS
    ],
    ids=[
        f"{field}-{'missing' if value is MISSING else type(value).__name__ + '-' + str(value).strip()}"
        for field in ("FirmwareVersion", "ProtocolVersion")
        for value in INVALID_ELEGOO_VERSIONS
    ],
)
def test_invalid_elegoo_versions_publish_fail_closed_evidence(
    tmp_path: Path, field: str, value: object,
):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "elegoo", "target_alias": "lab",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Centauri Carbon",
        "connection": {"host": "127.0.0.1", "port": 3030},
    }))
    target.chmod(0o600)
    attributes = {
        "MachineName": "Centauri Carbon",
        "FirmwareVersion": "V1.0.0", "ProtocolVersion": "V3.0.0",
    }
    if value is MISSING:
        attributes.pop(field)
    else:
        attributes[field] = value
    frames = iter((json.dumps({
        "Topic": "sdcp/attributes/ODIN-CERT-FICTIONAL", "Attributes": attributes,
    }),))

    def observer(_target):
        socket = type("Socket", (), {
            "recv": lambda self: next(frames), "close": lambda self: None,
        })()
        return observe_elegoo(PassiveWebSocketTransport(socket))

    result = observe_target_file(
        target, artifact_root=tmp_path / "evidence", observer=observer,
    )
    run_dir = publish_result(result, tmp_path / "evidence")
    manifest, results = verify_artifact(
        run_dir, expected_mode="observe", expected_protocol="elegoo",
    )
    assert manifest["status"] == "fail"
    assert results["elegoo"]["assertions"][0]["reason_code"] == "device_version_mismatch"


@pytest.mark.parametrize(
    "value", (MISSING, None, "", "   ", 123, True, [], {}),
    ids=("missing", "none", "empty", "whitespace", "integer", "boolean", "list", "object"),
)
def test_invalid_elegoo_models_publish_fail_closed_evidence(tmp_path: Path, value: object):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "elegoo", "target_alias": "lab",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Centauri Carbon",
        "connection": {"host": "127.0.0.1", "port": 3030},
    }))
    target.chmod(0o600)
    attributes = {
        "MachineName": "Centauri Carbon",
        "FirmwareVersion": "V1.0.0", "ProtocolVersion": "V3.0.0",
    }
    if value is MISSING:
        attributes.pop("MachineName")
    else:
        attributes["MachineName"] = value
    frames = iter((json.dumps({
        "Topic": "sdcp/attributes/ODIN-CERT-FICTIONAL", "Attributes": attributes,
    }),))

    def observer(_target):
        socket = type("Socket", (), {
            "recv": lambda self: next(frames), "close": lambda self: None,
        })()
        return observe_elegoo(PassiveWebSocketTransport(socket))

    result = observe_target_file(
        target, artifact_root=tmp_path / "evidence", observer=observer,
    )
    run_dir = publish_result(result, tmp_path / "evidence")
    manifest, results = verify_artifact(
        run_dir, expected_mode="observe", expected_protocol="elegoo",
    )
    assert manifest["status"] == "fail"
    assert results["elegoo"]["assertions"][0]["reason_code"] == "model_identity_unavailable"


def test_dotted_elegoo_firmware_publishes_verified_pass_evidence(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "elegoo", "target_alias": "lab",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Centauri Carbon",
        "connection": {"host": "127.0.0.1", "port": 3030},
    }))
    target.chmod(0o600)
    frames = iter((
        json.dumps({
            "Topic": "sdcp/attributes/ODIN-CERT-FICTIONAL",
            "Attributes": {
                "MachineName": "Centauri Carbon", "FirmwareVersion": "01.08.00.00",
                "ProtocolVersion": "V3.0.0",
            },
        }),
        json.dumps({
            "Topic": "sdcp/status/ODIN-CERT-FICTIONAL",
            "Status": {"CurrentStatus": [0], "PrintInfo": {"Status": 0, "CurrentTicks": 1}},
        }),
        json.dumps({
            "Topic": "sdcp/status/ODIN-CERT-FICTIONAL",
            "Status": {"CurrentStatus": [1], "PrintInfo": {"Status": 8, "CurrentTicks": 2}},
        }),
    ))

    def observer(_target):
        socket = type("Socket", (), {
            "recv": lambda self: next(frames), "close": lambda self: None,
        })()
        return observe_elegoo(PassiveWebSocketTransport(socket))

    result = observe_target_file(
        target, artifact_root=tmp_path / "evidence", observer=observer,
    )
    run_dir = publish_result(result, tmp_path / "evidence")
    manifest, results = verify_artifact(
        run_dir, expected_mode="observe", expected_protocol="elegoo",
    )
    assert manifest["status"] == "pass"
    assert results["elegoo"]["metrics"]["firmware_version"] == "01.08.00.00"


def test_edu_live_module_entrypoint_is_importable_from_repository_root():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = "backend:."
    completed = subprocess.run(
        [sys.executable, "-m", "ops.edu_readiness.verify_live", "--help"],
        cwd=Path.cwd(), env=environment, text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--hardware-evidence-dir" in completed.stdout
    assert "--hardware-identity-file" in completed.stdout
    makefile = Path("Makefile").read_text(encoding="utf-8")
    assert "-m ops.edu_readiness.verify_live" in makefile
    assert "--hardware-identity-file" in makefile
    assert "hardware evidence import requires both HARDWARE_EVIDENCE_DIR and HARDWARE_IDENTITY_FILE" in makefile


def test_observe_result_never_retains_target_secrets_or_job_identity(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "moonraker", "target_alias": "private-lab-name",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Fictional", "connection": {
            "host": "127.0.0.1", "port": 7125, "api_key": "ODIN-CERT-FICTIONAL-SECRET",
        },
    })); target.chmod(0o600)

    def observer(_target):
        sample = ParsedSample("printing", "private-student-project.gcode", capabilities=frozenset({"status"}))
        return ObservationSummary(
            (sample, sample), ("status",), api_version="v1.0",
            freshness_seconds=0.1, observed_model_family="Fictional",
        )

    result = observe_target_file(target, artifact_root=tmp_path / "evidence", observer=observer)
    retained = json.dumps(result)
    assert result["status"] == "pass" and result["metrics"]["valid_sample_count"] == 2
    for forbidden in ("private-lab-name", "127.0.0.1", "ODIN-CERT-FICTIONAL-SECRET", "private-student-project.gcode"):
        assert forbidden not in retained


def test_observe_result_rejects_reported_model_mismatch(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "moonraker", "target_alias": "lab",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Voron", "connection": {"host": "127.0.0.1", "port": 7125},
    }))
    target.chmod(0o600)

    def observer(_target):
        sample = ParsedSample("idle", "", capabilities=frozenset({"status"}))
        return ObservationSummary(
            (sample, sample), ("status",), freshness_seconds=0.1,
            observed_model_family="Different Model",
        )

    result = observe_target_file(
        target, artifact_root=tmp_path / "evidence", observer=observer,
    )
    assert result["status"] == "fail"
    assert result["assertions"][0]["reason_code"] == "model_family_mismatch"


def test_observe_result_fails_without_observed_model_identity(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "moonraker", "target_alias": "lab",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Voron", "connection": {"host": "127.0.0.1", "port": 7125},
    }))
    target.chmod(0o600)

    def observer(_target):
        sample = ParsedSample("idle", "", capabilities=frozenset({"status"}))
        return ObservationSummary((sample, sample), ("status",), freshness_seconds=0.1)

    result = observe_target_file(target, artifact_root=tmp_path / "evidence", observer=observer)
    assert result["status"] == "fail"
    assert result["assertions"][0]["reason_code"] == "model_identity_unavailable"
    assert result["metrics"]["model_family"] == "unknown"


def test_unexpected_observer_failure_result_is_schema_valid_and_publishable(tmp_path: Path):
    target = tmp_path / "target.json"
    target.write_text(json.dumps({
        "schema_version": 1, "protocol": "moonraker", "target_alias": "lab",
        "evidence_correlation_key": CORRELATION_KEY,
        "model_family": "Voron", "connection": {"host": "127.0.0.1", "port": 7125},
    }))
    target.chmod(0o600)

    def observer(_target):
        raise RuntimeError("fictional transport failure")

    result = observe_target_file(target, artifact_root=tmp_path / "evidence", observer=observer)
    assert result["assertions"][0]["reason_code"] == "unexpected_failure"
    assert result["metrics"]["model_family"] == "unknown"
    run_dir = publish_result(result, tmp_path / "evidence")
    manifest, results = verify_artifact(run_dir, expected_mode="observe", expected_protocol="moonraker")
    assert manifest["status"] == "fail"
    assert results["moonraker"]["status"] == "fail"
