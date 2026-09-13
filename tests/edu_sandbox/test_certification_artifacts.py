from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

import ops.edu_sandbox.certify as certify_module

from ops.edu_sandbox.certify import (
    _assert_complete_pass_evidence,
    _assert_prepared_blocked_evidence,
    _junit,
    _write_manifest,
)
from ops.edu_sandbox.errors import SandboxError
from ops.edu_sandbox.report import render_report
from ops.edu_sandbox.state import LifecycleState, Phase


def test_blocked_external_is_a_junit_failure_not_a_skip(tmp_path: Path) -> None:
    output = tmp_path / "junit.xml"
    _junit(
        output,
        [{"name": "activation", "status": "BLOCKED_EXTERNAL", "detail": "issuer artifact required"}],
    )
    suite = ET.parse(output).getroot()
    assert suite.attrib == {"tests": "1", "failures": "1", "errors": "0", "skipped": "0"}
    assert suite.find("./testcase/failure").attrib["message"] == "BLOCKED_EXTERNAL"


def test_html_report_escapes_untrusted_manifest_text(tmp_path: Path) -> None:
    output = tmp_path / "index.html"
    manifest = {
        "status": "FAIL",
        "sandbox_id": '<script>alert("sandbox")</script>',
        "run_id": "run&one",
        "source_commit": "deadbeef",
        "candidate_image_id": "sha256:abc",
        "summary": '<img src=x onerror="alert(1)">',
        "phases": [{"name": "<b>phase</b>", "status": "FAIL", "detail": "a&b"}],
    }
    render_report(manifest, output)
    source = output.read_text(encoding="utf-8")
    assert "<script>" not in source
    assert "<img src=x" not in source
    assert "<b>phase</b>" not in source
    assert "&lt;script&gt;" in source
    assert "a&amp;b" in source


def test_manifest_fixture_contains_no_secret_values() -> None:
    fixture = json.dumps(
        {
            "license_sha256": "a" * 64,
            "installation_id_sha256": "b" * 64,
            "device_public_key_sha256": "c" * 64,
        }
    )
    for forbidden in ("license_key", "password", "private_key", "bearer", "email"):
        assert forbidden not in fixture.lower()


def test_manifest_redacts_known_secret_before_any_artifact_write(tmp_path: Path) -> None:
    secret = "operator-secret-value"
    directory = tmp_path / "run"
    _write_manifest(
        directory,
        {
            "status": "FAIL",
            "sandbox_id": "school-one",
            "run_id": "run-one",
            "phases": [{"name": "failure", "status": "FAIL", "detail": f"error: {secret}"}],
        },
        [secret],
    )
    retained = "\n".join(
        path.read_text(encoding="utf-8")
        for path in directory.iterdir()
        if path.is_file()
    )
    assert secret not in retained
    assert "[REDACTED]" in retained
    assert (directory / "phase-log.json").is_file()


def test_unlicensed_smoke_purges_prepare_crash_before_state_publish(
    tmp_path: Path, monkeypatch
) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    sandbox_directory = state_root / "school-one"
    purged = []

    class CrashBeforeStateRuntime:
        def __init__(self, supplied_root):
            assert supplied_root == state_root

        def _paths(self, sandbox_id):
            assert sandbox_id == "school-one"
            return SimpleNamespace(
                root=state_root,
                directory=sandbox_directory,
                state=sandbox_directory / "state.json",
            )

        def prepare(self, sandbox_id, *, allow_dirty):
            assert sandbox_id == "school-one"
            sandbox_directory.mkdir(mode=0o700)
            (sandbox_directory / ".state-crash").write_text(
                "partial", encoding="utf-8"
            )
            raise KeyboardInterrupt("synthetic pre-state crash")

        def purge(self, sandbox_id, *, confirm):
            assert sandbox_id == confirm == "school-one"
            (sandbox_directory / ".state-crash").unlink()
            sandbox_directory.rmdir()
            purged.append(sandbox_id)
            return {"status": "PASS", "absence": {"controller_directory_absent": True}}

    monkeypatch.setattr(certify_module, "SandboxRuntime", CrashBeforeStateRuntime)
    monkeypatch.setattr(
        certify_module,
        "_source_privacy_preflight",
        lambda: {"files_scanned": 1, "fixture_set_sha256": "a" * 64},
    )

    exit_code, report = certify_module.run_unlicensed_smoke(
        "school-one",
        state_root=state_root,
        artifact_root=tmp_path / "artifacts",
        allow_dirty=True,
    )
    manifest = json.loads(report.with_name("manifest.json").read_text())
    assert exit_code == 1
    assert purged == ["school-one"]
    assert manifest["cleanup_status"] == "PASS"
    assert not sandbox_directory.exists()


def _complete_evidence() -> dict[str, object]:
    identity = {
        "installation_id_sha256": "b" * 64,
        "device_public_key_sha256": "c" * 64,
        "license_sha256": "d" * 64,
    }
    return {
        "activation_request_sha256": "a" * 64,
        "privacy": {"files_scanned": 2, "fixture_set_sha256": "e" * 64},
        "status_before": {
            "phase": "ACTIVE",
            "observed_status": "READY",
            "observed_license_expired": False,
        },
        "active_readiness": {
            "heartbeat": {"bambu_heartbeat_age_seconds": 1},
            "images": {"odin": "sha256:x"},
            "network_isolation": {"application_has_no_edge_route": True},
        },
        "school_graph": {"printer_count": 4, "student_write_denied": True},
        "reset": {"identity_before": identity, "identity_after": dict(identity), "verification": {"reset_sentinel_removed": True}},
        "expiry": {"after": {"phase": "EXPIRED", "observed_status": "EXPIRED"}},
        "purge": {"absence": {"controller_directory_absent": True, "candidate_tag_absent": True}},
    }


def test_complete_certification_evidence_is_required_for_pass() -> None:
    evidence = _complete_evidence()
    _assert_complete_pass_evidence(evidence)
    del evidence["activation_request_sha256"]
    with pytest.raises(SandboxError, match="activation-request proof"):
        _assert_complete_pass_evidence(evidence)


def test_complete_certification_rejects_degraded_or_expired_start() -> None:
    evidence = _complete_evidence()
    evidence["status_before"]["observed_status"] = "DEGRADED"
    with pytest.raises(SandboxError, match="ready unexpired"):
        _assert_complete_pass_evidence(evidence)
    evidence["status_before"]["observed_status"] = "READY"
    evidence["status_before"]["observed_license_expired"] = True
    with pytest.raises(SandboxError, match="ready unexpired"):
        _assert_complete_pass_evidence(evidence)


def test_blocked_certification_requires_healthy_prepared_state_and_topology() -> None:
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        compose_project="odin-edu-school-one",
        candidate_image_id="sha256:" + "a" * 64,
    )
    observed = {
        "phase": "PREPARED",
        "observed_status": "STOPPED",
        "candidate_image_id": state.candidate_image_id,
    }
    topology = {
        "sandbox_network_internal": True,
        "application_has_no_edge_route": True,
        "proxy_is_only_edge_member": True,
        "proxy_loopback_only": True,
        "network_members": {
            "sandbox": ["odin-edu-school-one-prepare", "odin-edu-school-one-prepare-proxy"],
            "edge": ["odin-edu-school-one-prepare-proxy"],
        },
    }
    current = {
        "identity_matches": True,
        "owned_resources_match": True,
        "owned_containers_absent": True,
        "networks_quiesced": True,
        "network_members": {"sandbox": [], "edge": []},
    }
    _assert_prepared_blocked_evidence(state, observed, topology, current)

    observed["observed_status"] = "DEGRADED"
    with pytest.raises(SandboxError, match="healthy PREPARED"):
        _assert_prepared_blocked_evidence(state, observed, topology, current)

    observed["observed_status"] = "STOPPED"
    topology["network_members"]["edge"].append("foreign-container")
    with pytest.raises(SandboxError, match="network membership"):
        _assert_prepared_blocked_evidence(state, observed, topology, current)
    topology["network_members"]["edge"].pop()
    current["network_members"]["edge"].append("foreign-container")
    with pytest.raises(SandboxError, match="current prepared resource proof"):
        _assert_prepared_blocked_evidence(state, observed, topology, current)


def test_html_report_retains_structured_proof(tmp_path: Path) -> None:
    output = tmp_path / "index.html"
    evidence = _complete_evidence()
    render_report({"status": "PASS", "evidence": evidence}, output)
    source = output.read_text(encoding="utf-8")
    assert "Retained proof" in source
    assert "activation_request_sha256" in source
    assert "student_write_denied" in source
