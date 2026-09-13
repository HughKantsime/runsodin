from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ops.edu_readiness import verify_live
from ops.edu_readiness.artifact_scan import scan_text
from ops.hardware_certification.authorization import (
    AuthorizationError,
    consume_authorization,
    create_template,
    validate_authorization,
)
from ops.hardware_certification.evidence import EvidenceError, EvidenceExpired, import_live_gate
from ops.hardware_certification.config import load_target
from ops.hardware_certification import security as certification_security
from ops.hardware_certification.security import (
    SecurityError, SensitiveValueRegistry,
    load_protected_json,
    resolve_private_target,
)


CORRELATION_KEY = "ab" * 32


def _target_config() -> dict:
    return {
        "schema_version": 1,
        "protocol": "bambu",
        "target_alias": "lab-printer",
        "model_family": "X1",
        "evidence_correlation_key": CORRELATION_KEY,
        "connection": {
            "host": "printer.cert.test",
            "port": 8883,
            "device_token": "ODIN-CERT-FICTIONAL-DEVICE",
            "access_code": "ODIN-CERT-FICTIONAL-ACCESS",
        },
    }


def _write_protected(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    return path


def _authorized_template(target_path: Path, now: datetime) -> dict:
    authorization = create_template(
        "20260912T120000Z-7d46cf1", target_path, ["pause"], now=now,
    )
    authorization.update(
        authorization_state="AUTHORIZED", operator_confirmation=authorization["challenge"],
        operator_name="Test Operator", physical_area_clear=True,
        disposable_job_confirmed=True, emergency_stop_ready=True,
    )
    authorization["actions"][0]["approved"] = True
    return authorization


def test_protected_json_rejects_symlink_and_broad_mode(tmp_path: Path):
    target = _write_protected(tmp_path / "target.json", _target_config())
    link = tmp_path / "target-link.json"
    link.symlink_to(target)
    with pytest.raises(SecurityError, match="symlink"):
        load_protected_json(link)

    target.chmod(0o640)
    with pytest.raises(SecurityError, match="0400 or 0600"):
        load_protected_json(target)


def test_sensitive_target_registry_redacts_every_registered_identity_value():
    target = _target_config()
    registry = SensitiveValueRegistry.from_target(target, "10.20.30.40")
    leaked = (
        "printer.cert.test ODIN-CERT-FICTIONAL-DEVICE "
        "ODIN-CERT-FICTIONAL-ACCESS lab-printer 10.20.30.40"
    )
    redacted = registry.redact(leaked)
    assert "[REDACTED]" in redacted
    for forbidden in (
        "printer.cert.test", "ODIN-CERT-FICTIONAL-DEVICE",
        "ODIN-CERT-FICTIONAL-ACCESS", "lab-printer", "10.20.30.40",
    ):
        assert forbidden not in redacted


@pytest.mark.parametrize(
    ("value", "label"),
    [
        ("10.20.30.40", "private IP address"),
        ("fd00:1234:5678::42", "IPv6 address"),
        ("maker-lab-printer.local", "local hostname"),
        ("maker-lab-printer.internal", "internal hostname"),
        ("device/REALDEVICE123/report", "raw MQTT topic"),
        ("student-capstone-final.gcode", "printer job filename"),
        ('{"mainboard_id":"REALMAINBOARD123"}', "printer identifier"),
    ],
)
def test_recognizable_hardware_identity_corpus_is_rejected(value, label):
    assert any(label in finding for finding in scan_text(value, "evidence.txt"))


@pytest.mark.parametrize("firmware", ["01.08.00.00", "01.09.00.00", "6.2.0"])
def test_dotted_firmware_versions_are_not_misclassified_as_ip_addresses(firmware):
    assert not any("IP address" in finding for finding in scan_text(
        json.dumps({"firmware_version": firmware}), "evidence.json",
    ))


def test_every_emitted_observation_reason_is_publishable_by_both_closed_schemas():
    source_root = Path("ops/hardware_certification")
    emitted = set()
    for path in source_root.rglob("*.py"):
        emitted.update(re.findall(r'ObservationError\("([a-z0-9_]+)"', path.read_text()))
    schemas = Path("ops/hardware_certification/schemas")
    assertion_schema = json.loads((schemas / "assertion-result.schema.json").read_text())
    result_schema = json.loads((schemas / "protocol-result.schema.json").read_text())
    standalone = set(assertion_schema["properties"]["reason_code"]["enum"])
    embedded = set(
        result_schema["properties"]["assertions"]["items"]["properties"]["reason_code"]["enum"]
    )
    assert emitted <= standalone
    assert emitted <= embedded


def test_protocol_specific_authorization_and_result_schemas_reject_cross_protocol_actions():
    schema_root = Path("ops/hardware_certification/schemas")
    authorization_schema = json.loads((schema_root / "authorization.schema.json").read_text())
    result_schema = json.loads((schema_root / "protocol-result.schema.json").read_text())
    authorization = {
        "schema_version": 1, "nonce": "a" * 32, "challenge": "b" * 24,
        "expires_at": "2026-09-12T12:15:00Z",
        "run_id": "20260912T120000Z-7d46cf1", "target_config_sha256": "c" * 64,
        "protocol": "prusalink", "target_alias": "lab", "authorization_state": "AUTHORIZED",
        "operator_confirmation": "b" * 24, "operator_name": "Operator",
        "physical_area_clear": True, "disposable_job_confirmed": True,
        "emergency_stop_ready": True,
        "actions": [{"name": "upload", "approved": True}],
    }
    assert list(Draft202012Validator(authorization_schema).iter_errors(authorization))
    result = {
        "schema_version": 1, "run_id": "20260912T120000Z-7d46cf1", "git_commit": "7d46cf1",
        "mode": "exercise", "protocol": "elegoo", "certification_level": "live_authorized_exercise",
        "status": "pass", "started_at": "2026-09-12T12:00:00Z", "ended_at": "2026-09-12T12:00:01Z",
        "authorization_scope_sha256": "d" * 64, "requested_actions": ["upload"],
        "executed_actions": ["upload"],
        "assertion_counts": {"executed": 1, "passed": 1, "failed": 0, "blocked": 0, "skipped": 0, "xfailed": 0},
        "metrics": {},
        "assertions": [{"id": "action_upload", "status": "pass", "reason_code": "transition_observed", "duration_ms": 1}],
    }
    assert list(Draft202012Validator(result_schema).iter_errors(result))


@pytest.mark.parametrize("mode", ["observe", "replay"])
def test_nonexercise_result_and_manifest_schemas_reject_active_scope(mode):
    schema_root = Path("ops/hardware_certification/schemas")
    result_schema = json.loads((schema_root / "protocol-result.schema.json").read_text())
    manifest_schema = json.loads((schema_root / "manifest.schema.json").read_text())
    active_only = {
        "authorization_scope": {
            "schema_version": 1, "run_id": "20260912T120000Z-7d46cf1",
            "protocol": "bambu", "actions": ["pause"],
            "expires_at": "2026-09-12T12:15:00Z", "model_family": "X1",
            "target_correlation_sha256": "c" * 64,
        },
        "authorization_scope_sha256": "d" * 64,
        "requested_actions": ["pause"], "executed_actions": ["pause"],
    }
    result = {
        "schema_version": 1, "run_id": "20260912T120000Z-7d46cf1",
        "git_commit": "7d46cf1", "mode": mode, "protocol": "bambu",
        "certification_level": "live_passive_observation" if mode == "observe" else "simulated_replay",
        "status": "pass", "started_at": "2026-09-12T12:00:00Z",
        "ended_at": "2026-09-12T12:00:01Z",
        "assertion_counts": {"executed": 1, "passed": 1, "failed": 0, "blocked": 0, "skipped": 0, "xfailed": 0},
        "metrics": ({"valid_sample_count": 2, "freshness_seconds": 0.1,
                     "reconnect_count": 0, "model_family": "X1",
                     "target_correlation_sha256": "c" * 64,
                     "capabilities": ["status"]} if mode == "observe" else {}),
        "assertions": [{"id": "proof", "status": "pass", "reason_code": "observed", "duration_ms": 1}],
        **active_only,
    }
    manifest = {
        "schema_version": 1, "run_id": result["run_id"], "git_commit": "7d46cf1",
        "git_dirty": False, "mode": mode, "protocol": "bambu", "status": "pass",
        "started_at": result["started_at"], "ended_at": result["ended_at"],
        "duration_seconds": 1, "junit": {"tests": 1, "failures": 0, "errors": 0, "skipped": 0, "xfailed": 0},
        "implementation_sha256": "e" * 64, "sanitizer_passed": True,
        "result_references": ["bambu.json"], "files": {"bambu.json": "f" * 64},
        **active_only,
    }
    assert list(Draft202012Validator(result_schema).iter_errors(result))
    assert list(Draft202012Validator(manifest_schema).iter_errors(manifest))


def test_private_target_resolution_rejects_public_and_multiple_addresses(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 0))],
    )
    with pytest.raises(SecurityError, match="private"):
        resolve_private_target("printer.cert.test", 8883)

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.3", 0)),
        ],
    )
    with pytest.raises(SecurityError, match="exactly one"):
        resolve_private_target("printer.cert.test", 8883)

    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.20.30.255", 0))],
    )
    with pytest.raises(SecurityError, match="unicast"):
        resolve_private_target("printer.cert.test", 8883)


@pytest.mark.parametrize(
    ("family", "address"),
    [
        (socket.AF_INET, "192.0.2.1"),
        (socket.AF_INET, "198.51.100.10"),
        (socket.AF_INET, "203.0.113.10"),
        (socket.AF_INET, "240.0.0.1"),
        (socket.AF_INET6, "2001:db8::1"),
        (socket.AF_INET6, "2001:4860:4860::8888"),
    ],
    ids=("documentation-v4-a", "documentation-v4-b", "documentation-v4-c", "reserved-v4", "documentation-v6", "public-v6"),
)
def test_private_target_resolution_rejects_special_use_ranges(monkeypatch, family, address):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *_args, **_kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 0))],
    )
    with pytest.raises(SecurityError, match="private"):
        resolve_private_target("printer.cert.test", 8883)


@pytest.mark.parametrize(
    ("family", "address"),
    [
        (socket.AF_INET, "10.1.2.3"),
        (socket.AF_INET, "172.31.2.3"),
        (socket.AF_INET, "192.168.2.3"),
        (socket.AF_INET, "127.0.0.1"),
        (socket.AF_INET, "169.254.2.3"),
        (socket.AF_INET6, "fd00::1234"),
        (socket.AF_INET6, "::1"),
        (socket.AF_INET6, "fe80::1234"),
    ],
    ids=("rfc1918-a", "rfc1918-b", "rfc1918-c", "loopback-v4", "link-local-v4", "ula-v6", "loopback-v6", "link-local-v6"),
)
def test_private_target_resolution_accepts_only_explicit_local_ranges(monkeypatch, family, address):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *_args, **_kwargs: [(family, socket.SOCK_STREAM, 6, "", (address, 0))],
    )
    assert resolve_private_target("printer.cert.test", 8883) == address


def test_target_config_is_rejected_from_any_canonical_artifact_subtree(tmp_path: Path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    other_tree = artifact_root / "edu-readiness"
    other_tree.mkdir(parents=True)
    target = _write_protected(other_tree / "target.json", _target_config())
    monkeypatch.setattr(certification_security, "CANONICAL_ARTIFACT_ROOT", artifact_root)
    with pytest.raises(SecurityError, match="artifact tree"):
        load_target(target, artifact_root=tmp_path / "different-output")


def test_authorization_template_is_default_deny(tmp_path: Path):
    target_path = _write_protected(tmp_path / "target.json", _target_config())
    template = create_template(
        run_id="20260912T120000Z-7d46cf1",
        target_path=target_path,
        actions=["pause", "resume", "stop"],
        now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    assert template["authorization_state"] == "DRAFT"
    assert template["operator_confirmation"] == ""
    assert template["physical_area_clear"] is False
    assert template["disposable_job_confirmed"] is False
    assert template["emergency_stop_ready"] is False
    assert all(item["approved"] is False for item in template["actions"])
    with pytest.raises(AuthorizationError):
        validate_authorization(
            template,
            target_path=target_path,
            now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda a: a.update(authorization_state="AUTHORIZED"),
        lambda a: a.update(operator_confirmation=a["challenge"]),
        lambda a: a.update(physical_area_clear=True),
        lambda a: a["actions"][0].update(approved=True),
    ],
)
def test_partially_edited_authorization_remains_denied(tmp_path: Path, mutation):
    target_path = _write_protected(tmp_path / "target.json", _target_config())
    auth = create_template(
        run_id="20260912T120000Z-7d46cf1",
        target_path=target_path,
        actions=["pause"],
        now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    mutation(auth)
    with pytest.raises(AuthorizationError):
        validate_authorization(
            auth,
            target_path=target_path,
            now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
        )


def test_fully_explicit_authorization_passes_and_expired_fails(tmp_path: Path):
    target_path = _write_protected(tmp_path / "target.json", _target_config())
    auth = create_template(
        run_id="20260912T120000Z-7d46cf1",
        target_path=target_path,
        actions=["pause"],
        now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    auth.update(
        authorization_state="AUTHORIZED",
        operator_confirmation=auth["challenge"],
        operator_name="Test Operator",
        physical_area_clear=True,
        disposable_job_confirmed=True,
        emergency_stop_ready=True,
    )
    auth["actions"][0]["approved"] = True
    validate_authorization(
        auth,
        target_path=target_path,
        now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(AuthorizationError, match="expired"):
        validate_authorization(
            auth,
            target_path=target_path,
            now=datetime(2026, 9, 12, 12, 16, tzinfo=timezone.utc),
        )


def test_unknown_and_prusalink_split_actions_are_rejected(tmp_path: Path):
    config = _target_config()
    config["protocol"] = "prusalink"
    config["connection"] = {
        "host": "printer.cert.test",
        "port": 443,
        "api_key": "ODIN-CERT-FICTIONAL-KEY",
    }
    target_path = _write_protected(tmp_path / "target.json", config)
    with pytest.raises(AuthorizationError):
        create_template("20260912T120000Z-7d46cf1", target_path, ["upload"], now=datetime.now(timezone.utc))
    with pytest.raises(AuthorizationError):
        create_template("20260912T120000Z-7d46cf1", target_path, ["arbitrary_method"], now=datetime.now(timezone.utc))


def test_upload_template_requires_exact_asset_hash_and_elegoo_requires_filename(tmp_path: Path):
    target_path = _write_protected(tmp_path / "target.json", _target_config())
    with pytest.raises(AuthorizationError, match="asset SHA-256"):
        create_template("20260912T120000Z-7d46cf1", target_path, ["upload", "start"])

    config = _target_config()
    config["protocol"] = "elegoo"
    config["connection"] = {"host": "printer.cert.test", "port": 3030}
    elegoo_path = _write_protected(tmp_path / "elegoo.json", config)
    with pytest.raises(AuthorizationError, match="filename"):
        create_template("20260912T120000Z-7d46cf1", elegoo_path, ["pause"])
    template = create_template(
        "20260912T120000Z-7d46cf1", elegoo_path, ["pause"],
        elegoo_filename="ODIN-CERT-FICTIONAL.ctb",
    )
    assert "ODIN-CERT-FICTIONAL.ctb" not in json.dumps(template)


def test_protocol_connection_schema_rejects_wrong_secret_shape(tmp_path: Path):
    target = _target_config()
    target["connection"] = {
        "host": "printer.cert.test", "port": 8883, "api_key": "wrong-protocol-secret"
    }
    target_path = _write_protected(tmp_path / "target.json", target)
    with pytest.raises(AuthorizationError, match="target config"):
        create_template("20260912T120000Z-7d46cf1", target_path, ["pause"])


def test_authorization_is_one_time_consumed(tmp_path: Path):
    target_path = _write_protected(tmp_path / "target.json", _target_config())
    auth = create_template(
        run_id="20260912T120000Z-7d46cf1", target_path=target_path,
        actions=["pause"], now=datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc),
    )
    auth.update(
        authorization_state="AUTHORIZED", operator_confirmation=auth["challenge"],
        operator_name="Test Operator", physical_area_clear=True,
        disposable_job_confirmed=True, emergency_stop_ready=True,
    )
    auth["actions"][0]["approved"] = True
    auth_path = _write_protected(tmp_path / "authorization.json", auth)
    ledger = tmp_path / "state" / "used-nonces"
    consumed, consumed_target = consume_authorization(
        auth_path, target_path=target_path, ledger_path=ledger,
        now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
    )
    assert consumed["nonce"] == auth["nonce"]
    assert consumed_target == _target_config()
    assert not auth_path.exists()
    replay = _write_protected(tmp_path / "authorization-replay.json", auth)
    with pytest.raises(AuthorizationError, match="already consumed"):
        consume_authorization(
            replay, target_path=target_path, ledger_path=ledger,
            now=datetime(2026, 9, 12, 12, 1, tzinfo=timezone.utc),
        )


def test_authorization_ledger_rejects_broad_mode_symlink_artifact_tree_and_clock_rollback(tmp_path: Path):
    created = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
    target_path = _write_protected(tmp_path / "target.json", _target_config())

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    broad = state / "broad-ledger"
    broad.write_text(""); broad.chmod(0o644)
    broad_auth = _write_protected(tmp_path / "broad-auth.json", _authorized_template(target_path, created))
    with pytest.raises(AuthorizationError, match="mode must be 0600"):
        consume_authorization(
            broad_auth, target_path=target_path, ledger_path=broad,
            now=created + timedelta(minutes=1),
        )

    real = state / "real-ledger"
    real.write_text(""); real.chmod(0o600)
    link = state / "linked-ledger"
    link.symlink_to(real)
    link_auth = _write_protected(tmp_path / "link-auth.json", _authorized_template(target_path, created))
    with pytest.raises(AuthorizationError, match="symlink"):
        consume_authorization(
            link_auth, target_path=target_path, ledger_path=link,
            now=created + timedelta(minutes=1),
        )

    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    artifact_auth = _write_protected(tmp_path / "artifact-auth.json", _authorized_template(target_path, created))
    with pytest.raises(AuthorizationError, match="outside artifact trees"):
        consume_authorization(
            artifact_auth, target_path=target_path, ledger_path=evidence / "used",
            artifact_root=evidence, now=created + timedelta(minutes=1),
        )

    ledger = state / "timestamped-ledger"
    first_auth = _write_protected(tmp_path / "first-auth.json", _authorized_template(target_path, created))
    consume_authorization(
        first_auth, target_path=target_path, ledger_path=ledger,
        now=created + timedelta(minutes=2),
    )
    rollback_auth = _write_protected(tmp_path / "rollback-auth.json", _authorized_template(target_path, created))
    with pytest.raises(AuthorizationError, match="clock rollback"):
        consume_authorization(
            rollback_auth, target_path=target_path, ledger_path=ledger,
            now=created + timedelta(minutes=1),
        )


def test_authorization_commit_mismatch_rejects_before_consumption(tmp_path: Path):
    target_path = _write_protected(tmp_path / "target.json", _target_config())
    auth = create_template("20260912T120000Z-7d46cf1", target_path, ["pause"])
    auth.update(
        authorization_state="AUTHORIZED", operator_confirmation=auth["challenge"],
        operator_name="Test Operator", physical_area_clear=True,
        disposable_job_confirmed=True, emergency_stop_ready=True,
    )
    auth["actions"][0]["approved"] = True
    auth_path = _write_protected(tmp_path / "authorization.json", auth)
    with pytest.raises(AuthorizationError, match="commit"):
        consume_authorization(
            auth_path, target_path=target_path, ledger_path=tmp_path / "used",
            expected_commit="deadbee",
        )
    assert auth_path.exists()


def test_replay_evidence_cannot_import_as_live_gate(tmp_path: Path):
    manifest = {
        "schema_version": 1,
        "run_id": "20260912T120000Z-7d46cf1",
        "git_commit": "7d46cf1",
        "git_dirty": False,
        "mode": "replay",
        "protocol": "bambu",
        "status": "pass",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {},
    }
    with pytest.raises(EvidenceError, match="observe"):
        import_live_gate(manifest, expected_protocol="bambu", expected_commit="7d46cf1", result={})


def test_passive_modules_do_not_import_active_application_surfaces():
    root = Path("ops/hardware_certification")
    passive_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "passive").rglob("*.py")
    )
    forbidden = (
        "telemetry.bambu.commands",
        "telemetry.bambu.ftp_upload",
        "adapters.bambu",
        "adapters.elegoo",
        "adapters.moonraker",
        "adapters.prusalink",
        "getattr(",
        "import ftplib",
    )
    for needle in forbidden:
        assert needle not in passive_source


def test_edu_hardware_rows_ignore_legacy_environment_credentials(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EDU_BAMBU_CERT_HOST", "printer.cert.test")
    monkeypatch.setenv("EDU_BAMBU_CERT_ACCESS_CODE", "must-not-be-read")
    monkeypatch.setenv("EDU_BAMBU_CERT_DEVICE_TOKEN", "must-not-be-read")
    monkeypatch.setattr(verify_live.subprocess, "check_output", lambda *_args, **_kwargs: "7d46cf1\n")
    def legacy_probe(*_args, **_DONOTUSE):
        raise AssertionError("legacy probe called")

    monkeypatch.setattr(verify_live, "_probe_hardware", legacy_probe)
    verify_live.hardware_rows("20260912T120000Z-7d46cf1", tmp_path)
    for protocol in ("bambu", "elegoo", "moonraker", "prusalink"):
        row = json.loads((tmp_path / f"hardware_{protocol}_live.json").read_text())
        assert row["status"] == "blocked"
        assert row["metrics"]["certification_level"] == "none"


def test_edu_hardware_rows_map_expired_evidence_to_blocked(tmp_path: Path, monkeypatch):
    evidence = tmp_path / "evidence"
    rows = tmp_path / "rows"
    rows.mkdir()
    for protocol in ("bambu", "elegoo", "moonraker", "prusalink"):
        (evidence / protocol).mkdir(parents=True)
    identity = _write_protected(tmp_path / "identity.json", {
        "schema_version": 1,
        "protocols": {
            protocol: {
                "model_family": "Fictional", "firmware_version": "unknown",
                "api_version": "unknown",
            }
            for protocol in ("bambu", "elegoo", "moonraker", "prusalink")
        },
    })
    monkeypatch.setattr(verify_live.subprocess, "check_output", lambda *_args, **_kwargs: "7d46cf1\n")
    observed_kwargs = []

    def expired(*_args, **kwargs):
        observed_kwargs.append(kwargs)
        raise EvidenceExpired("stale")

    monkeypatch.setattr(
        verify_live, "verify_artifact", expired,
    )
    verify_live.hardware_rows("20260912T120000Z-7d46cf1", rows, evidence, identity)
    for protocol in ("bambu", "elegoo", "moonraker", "prusalink"):
        row = json.loads((rows / f"hardware_{protocol}_live.json").read_text())
        assert row["status"] == "blocked"
        assert row["metrics"]["certification_level"] == "expired"
        assert "recaptured" in row["findings"][0]
    assert all(call["expected_model_family"] == "Fictional" for call in observed_kwargs)
    assert all(call["expected_firmware_version"] == "unknown" for call in observed_kwargs)
    assert all(call["expected_api_version"] == "unknown" for call in observed_kwargs)


def test_edu_hardware_import_rejects_evidence_without_current_identity_file(tmp_path: Path, monkeypatch):
    evidence = tmp_path / "evidence"
    rows = tmp_path / "rows"
    rows.mkdir()
    (evidence / "bambu").mkdir(parents=True)
    monkeypatch.setattr(verify_live.subprocess, "check_output", lambda *_args, **_kwargs: "7d46cf1\n")
    monkeypatch.setattr(
        verify_live, "verify_artifact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("verifier must not run")),
    )
    verify_live.hardware_rows("20260912T120000Z-7d46cf1", rows, evidence)
    row = json.loads((rows / "hardware_bambu_live.json").read_text())
    assert row["status"] == "fail"
    assert row["metrics"]["certification_level"] == "invalid"
