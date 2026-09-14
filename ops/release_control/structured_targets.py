"""Structured entry points for contract, security, and telemetry gates."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ops.edu_readiness.artifact_scan import scan_tree

from .report import render
from .run_gate import execute, sha256, validate_result

ROOT = Path(__file__).parents[2]


def _run_id() -> str:
    return os.getenv("RELEASE_CONTROL_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")


def _directory(target: str) -> Path:
    configured = os.getenv("RELEASE_CONTROL_COMPONENT_DIR")
    path = Path(configured) / target if configured else ROOT / "artifacts/release-control-standalone" / _run_id() / target
    path.mkdir(parents=True, exist_ok=False)
    return path


def _pytest_target(target: str, tests: str, timeout: int) -> int:
    directory = _directory(target)
    junit = directory / "junit.xml"
    result = execute(
        gate_id=target.replace("-", "_"),
        command=["python3.11", "-m", "pytest", tests,
                 "-v", "--tb=short", "-o", "xfail_strict=true", "--junitxml={junit}"],
        output=directory / "result.json", timeout=timeout, junit=junit,
        environment_allowlist=("ADMIN_USERNAME", "ADMIN_PASSWORD"),
        environment_overrides={"ADMIN_USERNAME": "ci", "ADMIN_PASSWORD": "ci"},
    )
    render(result, directory / "index.html")
    return 0 if result["status"] == "pass" else 1


def contracts() -> int:
    return _pytest_target("contracts", "tests/test_contracts/", 1200)


def telemetry_contracts() -> int:
    return _pytest_target("telemetry-contracts", "tests/test_telemetry/", 1800)


def telemetry_smoke() -> int:
    directory = _directory("telemetry-smoke")
    manifest = directory / "smoke-manifest.json"
    raw_cell = ROOT / "stress-out" / f"trusted-{_run_id()}"
    result = execute(
        gate_id="telemetry_smoke",
        command=["tests/stress/mqtt/run_ci_smoke.sh"], output=directory / "result.json",
        timeout=1200, expected_artifacts=[str(manifest.relative_to(ROOT))],
        json_status=[f"{manifest.name}=pass"],
        environment_allowlist=("ODIN_TELEMETRY_SMOKE_RESULT", "ODIN_TELEMETRY_SMOKE_CELL_DIR"),
        environment_overrides={
            "ODIN_TELEMETRY_SMOKE_RESULT": str(manifest),
            "ODIN_TELEMETRY_SMOKE_CELL_DIR": str(raw_cell),
        },
    )
    privacy_findings = scan_tree(directory)
    if privacy_findings:
        result["status"] = "fail"
        result["findings"].extend(f"artifact privacy scan: {item}" for item in privacy_findings)
        validate_result(result)
        (directory / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    render(result, directory / "index.html")
    return 0 if result["status"] == "pass" else 1


def security() -> int:
    directory = _directory("security")
    commands = [
        ("security_operational", ["python3.11", "-m", "pytest",
          "tests/privacy/test_operational_security.py", "tests/test_contracts/test_readiness_deploy_parity.py",
          "-q", "--tb=short", "-o", "xfail_strict=true", "--junitxml={junit}"], "junit"),
        ("gitleaks", ["gitleaks", "detect", "--source", ".", "--config", ".gitleaks.toml",
          "--redact=100", "--report-format", "json", "--report-path", str(directory / "gitleaks.json")], "gitleaks.json"),
        ("pip_audit", ["python3.11", "-m", "pip_audit", "-r", "backend/requirements.txt",
          "--progress-spinner", "off", "-f", "json", "-o", str(directory / "pip-audit.json")], "pip-audit.json"),
        ("npm_audit", ["bash", "-c", f"npm --prefix frontend audit --audit-level=high --json > {directory / 'npm-audit.json'}"], "npm-audit.json"),
        ("bandit", ["python3.11", "-m", "bandit", "-r", "backend/", "ops/edu_sandbox/",
          "ops/hardware_certification/", "-lll", "--exclude", "backend/vision_models_default/", "-f", "json", "-o", str(directory / "bandit.json")], "bandit.json"),
        ("semgrep", ["semgrep", "--config", "auto", "--error", "--no-git-ignore", "--json-output", str(directory / "semgrep.json"),
          "--exclude=tests/*", "--exclude=*.min.js", "backend/", "ops/edu_readiness/", "ops/edu_sandbox/", "ops/hardware_certification/"], "semgrep.json"),
        ("hadolint", ["bash", "-c", f"hadolint -f json Dockerfile > {directory / 'hadolint.json'}"], "hadolint.json"),
    ]
    results: list[dict[str, object]] = []
    for name, command, result_kind in commands:
        junit = directory / f"{name}.xml" if result_kind == "junit" else None
        expected = [] if junit else [str((directory / result_kind).relative_to(ROOT))]
        result = execute(
            gate_id=name, command=command, output=directory / f"{name}.result.json",
            timeout=900, junit=junit, expected_artifacts=expected,
            environment_allowlist=("PYTHONPATH",) if name == "security_operational" else (),
            environment_overrides={"PYTHONPATH": "backend"} if name == "security_operational" else None,
        )
        results.append(result)
    findings = [f"{item['gate_id']}: {finding}" for item in results for finding in item["findings"]]
    aggregate: dict[str, object] = {
        "schema_version": 1, "gate_id": "security", "status": "pass" if not findings else "fail",
        "command": ["security-structured"], "working_directory": str(ROOT),
        "started_at": min(str(item["started_at"]) for item in results),
        "ended_at": max(str(item["ended_at"]) for item in results),
        "duration_seconds": round(sum(float(item["duration_seconds"]) for item in results), 3),
        "exit_code": 0 if not findings else 1, "timed_out": any(item["timed_out"] for item in results),
        "counts": {"tests": sum(item["counts"]["tests"] for item in results),
                   "passed": sum(item["counts"]["passed"] for item in results),
                   "failures": sum(item["counts"]["failures"] for item in results),
                   "errors": sum(item["counts"]["errors"] for item in results),
                   "skipped": sum(item["counts"]["skipped"] for item in results),
                   "xfailed": sum(item["counts"]["xfailed"] for item in results)},
        "tool_versions": {"python": sys.version.split()[0]},
        "log": {"path": str(directory / "security.log"), "sha256": "0" * 64, "sanitized": True},
        "artifacts": [{"path": str(directory / f"{item['gate_id']}.result.json"),
                       "sha256": sha256(directory / f"{item['gate_id']}.result.json")} for item in results],
        "findings": findings,
    }
    log = directory / "security.log"
    log.write_text("\n".join(f"{item['gate_id']}: {item['status']}" for item in results) + "\n", encoding="utf-8")
    aggregate["log"]["sha256"] = sha256(log)  # type: ignore[index]
    validate_result(aggregate)
    (directory / "result.json").write_text(json.dumps(aggregate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    render(aggregate, directory / "index.html")
    return 0 if aggregate["status"] == "pass" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=("contracts", "security", "telemetry-contracts", "telemetry-smoke"))
    args = parser.parse_args()
    return {"contracts": contracts, "security": security,
            "telemetry-contracts": telemetry_contracts,
            "telemetry-smoke": telemetry_smoke}[args.target]()


if __name__ == "__main__":
    raise SystemExit(main())
