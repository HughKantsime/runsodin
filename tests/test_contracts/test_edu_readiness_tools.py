"""Contracts for fail-loud EDU readiness evidence tooling."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from ops.edu_readiness.aggregate import aggregate
from ops.edu_readiness.artifact_scan import scan_tree
from ops.edu_readiness.common import ResultValidationError, validate_result, write_result
from ops.edu_readiness.generate_report import render_report
from ops.edu_readiness.verify_live import validate_source_manifest


ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "ops/edu_readiness/readiness-policy.json"


def _result(gate_id: str, status: str = "pass", executed: int = 1) -> dict:
    return {
        "schema_version": 1,
        "run_id": "20260911T120000Z-f6e10cc",
        "gate_id": gate_id,
        "mandatory": True,
        "status": status,
        "started_at": "2026-09-11T12:00:00Z",
        "ended_at": "2026-09-11T12:00:01Z",
        "duration_seconds": 1.0,
        "tool_versions": {"python": "3.11"},
        "executed_count": executed,
        "skipped_count": 0,
        "xfailed_count": 0,
        "metrics": {},
        "findings": [],
        "artifacts": [],
    }


def test_makefile_uses_one_overridable_readiness_run_id_per_invocation():
    source = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "EDU_RUN_ID ?= $(shell date -u +%Y%m%dT%H%M%SZ)-$(shell git rev-parse --short HEAD)" in source
    assert "EDU_RUN_DIR ?= artifacts/edu-readiness/$(EDU_RUN_ID)" in source


def test_passing_result_cannot_hide_zero_or_skipped_checks():
    with pytest.raises(ResultValidationError, match="zero"):
        validate_result(_result("privacy", executed=0))
    result = _result("privacy")
    result["skipped_count"] = 1
    with pytest.raises(ResultValidationError, match="skips"):
        validate_result(result)


def test_aggregate_requires_every_policy_gate(tmp_path):
    write_result(tmp_path, _result("foundation"))
    summary = aggregate(tmp_path, POLICY)
    assert summary["status"] == "NOT_READY"
    assert "privacy" in summary["missing_gates"]


def test_code_controlled_scope_passes_without_live_or_manual_rows(tmp_path):
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    for gate in policy["code_controlled_gates"]:
        write_result(tmp_path, _result(gate))

    deterministic = aggregate(tmp_path, POLICY, scope="code-controlled")
    full = aggregate(tmp_path, POLICY)
    assert deterministic["status"] == "READY"
    assert deterministic["scope"] == "code-controlled"
    assert deterministic["required_gate_count"] == len(policy["code_controlled_gates"])
    assert full["status"] == "NOT_READY"
    assert "tls_live" in full["missing_gates"]
    (tmp_path / "summary.json").write_text(json.dumps(deterministic), encoding="utf-8")
    report = render_report(tmp_path)
    assert "Code-controlled gates: READY" in report
    assert "Live TLS, legal-source, physical-device, and manual contract rows were not evaluated" in report


def test_conditional_state_requires_named_acceptance(tmp_path):
    policy = json.loads(POLICY.read_text())
    for gate in policy["required_gates"]:
        status = "blocked" if gate == "hardware_elegoo_live" else "pass"
        write_result(tmp_path, _result(gate, status=status))
    assert aggregate(tmp_path, POLICY)["status"] == "NOT_READY"
    (tmp_path / "acceptances.json").write_text(json.dumps({
        "accepted_blockers": [{
            "gate_id": "hardware_elegoo_live",
            "decision_maker": "School IT Director",
            "accepted_at": "2026-09-11T12:00:00Z"
        }]
    }))
    assert aggregate(tmp_path, POLICY)["status"] == "CONDITIONALLY_READY"


def test_artifact_scan_rejects_identity_secret_and_private_ip(tmp_path):
    (tmp_path / "bad.json").write_text(json.dumps({
        "email": "student@real-school.edu",
        "password": "actual-secret",
        "host": "192.168.1.20"
    }))
    findings = scan_tree(tmp_path)
    assert any("email" in item for item in findings)
    assert any("sensitive" in item for item in findings)
    assert any("private IP" in item for item in findings)


def test_artifact_scan_accepts_synthetic_redacted_evidence(tmp_path):
    (tmp_path / "good.json").write_text(json.dumps({
        "email": "student@school.test",
        "token": "[redacted]",
        "host": "example.com"
    }))
    assert scan_tree(tmp_path) == []


def test_artifact_scan_rejects_unknown_binary_and_invalid_images(tmp_path):
    (tmp_path / "raw.db").write_bytes(b"SQLite format 3\x00")
    assert any("unsupported binary" in item for item in scan_tree(tmp_path))
    (tmp_path / "raw.db").unlink()
    (tmp_path / "bad.png").write_bytes(b"not an image")
    assert any("invalid image" in item for item in scan_tree(tmp_path))


def test_aggregate_rejects_undeclared_top_level_json(tmp_path):
    write_result(tmp_path, _result("foundation"))
    (tmp_path / "rogue.json").write_text(json.dumps(_result("rogue")), encoding="utf-8")
    summary = aggregate(tmp_path, POLICY)
    assert summary["status"] == "NOT_READY"
    assert any("undeclared gate_id" in item for item in summary["malformed_results"])


def test_html_report_escapes_gate_findings(tmp_path):
    write_result(tmp_path, {**_result("foundation", status="fail"), "findings": ["<script>alert(1)</script>"]})
    write_result(tmp_path, {
        **_result("accessibility"),
        "metrics": {
            "matrix_cases": 85,
            "keyboard_cases": 6,
            "axe_findings_by_impact": {"critical": 0, "serious": 0, "moderate": 2, "minor": 1},
            "nonblocking_axe_rule_ids": {"moderate": ["color-contrast"], "minor": ["region"]},
        },
    })
    (tmp_path / "summary.json").write_text(json.dumps({
        "status": "NOT_READY", "missing_gates": [], "failed_gates": ["foundation"], "blocked_gates": []
    }))
    report = render_report(tmp_path)
    assert "<script>alert(1)</script>" not in report
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in report
    assert "April 26, 2027" in report
    assert "April 26, 2028" in report
    assert "bambu" in report
    assert "Automated accessibility checks" in report
    assert "not production capacity evidence" in report
    assert "moderate: 2 (color-contrast)" in report
    assert "minor: 1 (region)" in report
    assert "does not measure login or WebSocket-token issuance latency" in report


def test_command_gate_rejects_success_without_parseable_assertion_count(tmp_path):
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "ops/edu_readiness/run_command_gate.py"),
            "--run-id", "20260911T120000Z-f6e10cc",
            "--run-dir", str(tmp_path),
            "--gate", "synthetic",
            "--minimum-assertions", "1",
            "--", sys.executable, "-c", "print('completed without totals')",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads((tmp_path / "synthetic.json").read_text())
    assert process.returncode == 1
    assert result["status"] == "fail"
    assert result["executed_count"] == 0
    assert "command emitted no parseable assertion counts" in result["findings"]


def test_command_gate_accepts_explicit_scanner_assertion_count(tmp_path):
    process = subprocess.run(
        [
            sys.executable,
            str(ROOT / "ops/edu_readiness/run_command_gate.py"),
            "--run-id", "20260911T120000Z-f6e10cc",
            "--run-dir", str(tmp_path),
            "--gate", "synthetic",
            "--minimum-assertions", "4",
            "--", sys.executable, "-c", "print('4 passed in security scanners')",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    result = json.loads((tmp_path / "synthetic.json").read_text())
    assert process.returncode == 0
    assert result["status"] == "pass"
    assert result["executed_count"] == 4


def test_legal_source_manifest_fails_stale_or_future_attestations():
    manifest = {
        "maximum_age_days": 90,
        "sources": [{"id": "primary", "accessed_at": "2026-01-01"}],
    }
    assert "source attestation" in validate_source_manifest(
        manifest, today=date(2026, 4, 2)
    )[0]
    manifest["sources"][0]["accessed_at"] = "2026-04-03"
    assert "future" in validate_source_manifest(
        manifest, today=date(2026, 4, 2)
    )[0]


def test_accessibility_matrix_uses_named_method_scoped_fixtures():
    source = (ROOT / "tests/accessibility/accessibility_audit.mjs").read_text(encoding="utf-8")
    assert "fixtureValues" not in source
    assert "method === 'POST' && url.pathname === '/api/auth/ws-token'" not in source
    assert "axeFindings" in source
    for row_id, fixture_name in {
        "A01": "anonymous.json",
        "A02": "fleet.json",
        "A03": "fleet.json",
        "A04": "jobs.json",
        "A05": "jobs.json",
        "A06": "admin.json",
        "A07": "admin.json",
        "A08": "reports.json",
    }.items():
        assert f"id: '{row_id}'" in source
        assert f"fixture: '{fixture_name}'" in source

    anonymous = json.loads(
        (ROOT / "tests/accessibility/fixtures/anonymous.json").read_text(encoding="utf-8")
    )
    assert anonymous["GET /api/auth/me"]["$status"] == 401
    assert anonymous["POST /api/auth/ws-token"]["$status"] == 401
    for fixture_path in (ROOT / "tests/accessibility/fixtures").glob("*.json"):
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        assert all(key.startswith("$") or key.split(" ", 1)[0] in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} for key in fixture)


def test_websocket_handler_has_no_global_api_key_compatibility():
    source = (ROOT / "backend/core/app.py").read_text(encoding="utf-8")
    websocket_source = source[source.index('@app.websocket("/ws")'):source.index('@app.websocket("/api/v1/ws")')]
    assert "settings.api_key" not in websocket_source
    assert 'payload.get("ws") is True' in websocket_source


@pytest.mark.parametrize('condition', ['missing', 'failed', 'zero', 'skipped', 'xfailed'])
def test_browser_journey_cannot_be_omitted_or_accepted_as_ready(tmp_path, condition):
    policy = json.loads(POLICY.read_text())
    assert 'browser_journey' in policy['code_controlled_gates']
    assert 'browser_journey' in policy['never_conditionally_blockable_gates']
    assert 'browser_journey' not in policy['conditionally_blockable_gates']
    for gate in policy['code_controlled_gates']:
        if gate != 'browser_journey':
            write_result(tmp_path, _result(gate))
    if condition != 'missing':
        result = _result('browser_journey', status='fail' if condition == 'failed' else 'pass')
        if condition == 'zero':
            result['executed_count'] = 0
        elif condition in ('skipped', 'xfailed'):
            result['skipped_count' if condition == 'skipped' else 'xfailed_count'] = 1
        # Deliberately malformed purported passes must fail during aggregation.
        (tmp_path / 'browser_journey.json').write_text(json.dumps(result))
    (tmp_path / 'acceptances.json').write_text(json.dumps({'accepted_blockers': [{
        'gate_id': 'browser_journey', 'decision_maker': 'synthetic-owner',
        'accepted_at': '2026-10-10T00:00:00Z',
    }]}))
    assert aggregate(tmp_path, POLICY, scope='code-controlled')['status'] == 'NOT_READY'


def test_browser_runner_rejects_reused_evidence_before_build(tmp_path, monkeypatch):
    from ops.edu_readiness import run_browser_journey as runner
    directory = tmp_path / 'raw' / 'browser_journey'
    directory.mkdir(parents=True)
    stale = directory / 'junit.xml'
    stale.write_text('stale passing evidence')
    monkeypatch.setattr(sys, 'argv', ['runner', '--run-dir', str(tmp_path)])
    monkeypatch.setattr(runner.subprocess, 'run', lambda *a, **k: pytest.fail('must not rebuild over reused evidence'))
    with pytest.raises(FileExistsError):
        runner.main()
    assert stale.read_text() == 'stale passing evidence'


def test_browser_runner_build_failure_cannot_run_tests_or_create_junit(tmp_path, monkeypatch):
    from ops.edu_readiness import run_browser_journey as runner
    calls = []
    def fail_build(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(sys, 'argv', ['runner', '--run-dir', str(tmp_path)])
    monkeypatch.setattr(runner.subprocess, 'run', fail_build)
    assert runner.main() == 1
    assert len(calls) == 1 and calls[0][0] == 'npm'
    assert not (tmp_path / 'raw' / 'browser_journey' / 'junit.xml').exists()


def test_browser_junit_removes_host_identity_without_hiding_failure(tmp_path):
    from ops.edu_readiness.run_browser_journey import remove_host_metadata
    from xml.etree import ElementTree
    path = tmp_path / 'junit.xml'
    path.write_text('<testsuites><testsuite tests="1" failures="1" errors="0" skipped="0" hostname="private-machine.local"><testcase name="visible-denial"><failure message="expected test failure"/></testcase></testsuite></testsuites>')
    remove_host_metadata(path)
    root = ElementTree.parse(path).getroot()
    suite = root.find('testsuite')
    assert 'hostname' not in suite.attrib
    assert suite.get('tests') == '1' and suite.get('failures') == '1'
    assert root.find('.//failure').get('message') == 'expected test failure'


def test_browser_runner_rejects_success_without_junit(tmp_path, monkeypatch):
    from ops.edu_readiness import run_browser_journey as runner
    monkeypatch.setattr(sys, 'argv', ['runner', '--run-dir', str(tmp_path)])
    monkeypatch.setattr(runner.subprocess, 'run', lambda command, **kwargs: subprocess.CompletedProcess(command, 0))
    assert runner.main() == 1
