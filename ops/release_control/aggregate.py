"""Run all ten trusted validation components in fixed order."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from .policy import load_inventory
from .report import render
from .run_gate import execute, sha256, validate_result

ROOT = Path(__file__).parents[2]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,96}$")


def hardware_python() -> str:
    """Prefer the locked checkout runtime, while keeping local gates runnable."""
    checkout_runtime = ROOT / ".hardware-cert-venv/bin/python"
    return str(checkout_runtime) if checkout_runtime.is_file() else "python3.11"


def _junit(results: list[dict[str, object]], path: Path) -> None:
    failures = sum(item["status"] != "pass" for item in results)
    suite = ET.Element("testsuite", name="trusted-validation", tests=str(len(results)),
                       failures=str(failures), errors="0", skipped="0")
    for item in results:
        case = ET.SubElement(suite, "testcase", classname="trusted_validation", name=str(item["gate_id"]))
        if item["status"] != "pass":
            failure = ET.SubElement(case, "failure", message="component failed")
            failure.text = "; ".join(str(value) for value in item["findings"])
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    run_id = os.getenv("RELEASE_CONTROL_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")
    if not RUN_ID_RE.fullmatch(run_id):
        raise SystemExit("invalid RELEASE_CONTROL_RUN_ID")
    run_dir = ROOT / "artifacts/trusted-validation" / run_id
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    native = run_dir / "native"
    components_dir = run_dir / "components"
    native.mkdir()
    components_dir.mkdir()
    inventory = load_inventory()
    results: list[dict[str, object]] = []
    commit = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    evidence_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + commit
    docker_host = os.getenv("DOCKER_HOST")
    if not docker_host:
        try:
            context = json.loads(subprocess.check_output(["docker", "context", "inspect"], text=True))
            docker_host = str(context[0]["Endpoints"]["docker"]["Host"])
        except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError):
            docker_host = ""

    env_values = {
        "RELEASE_CONTROL_RUN_ID": run_id,
        "RELEASE_CONTROL_COMPONENT_DIR": str(native),
        "ODIN_CANDIDATE_RUN_ID": f"candidate-{run_id}"[:63].lower(),
        "ODIN_CANDIDATE_ARTIFACT_ROOT": str(native / "candidate"),
        "ODIN_DATABASE_PARITY_RUN_ID": f"parity-{run_id}"[:63].lower(),
        "ODIN_DATABASE_PARITY_ARTIFACT_ROOT": str(native / "database-parity"),
        "EDU_RUN_ID": evidence_id,
        "EDU_RUN_DIR": str(native / "edu-readiness"),
        "EDU_SANDBOX_ARTIFACT_ROOT": str(native / "edu-sandbox"),
        "EDU_SANDBOX_ID": f"sandbox-{run_id}"[:47].lower(),
        "ODIN_HARDWARE_ARTIFACT_ROOT": str(native / "hardware"),
        "ODIN_HARDWARE_RUN_ID": evidence_id,
        "EDU_BROWSER_PYTHON": "python3.11",
        "HARDWARE_PYTHON": hardware_python(),
        "DOCKER_HOST": docker_host,
        "PLAYWRIGHT_BROWSERS_PATH": str(Path.home() / "Library/Caches/ms-playwright"),
        "ODIN_TELEMETRY_V2": "1",
    }
    native_rel = str(native.relative_to(ROOT))
    for component in inventory["components"]:
        component_id = str(component["id"])
        output_dir = components_dir / component_id
        output_dir.mkdir()
        allowlist = tuple(str(item) for item in component["environment_allowlist"])
        overrides = {key: value for key, value in env_values.items() if key in allowlist and value}
        patterns = [str(item).format(native=native_rel) for item in component["expected_artifacts"]]
        result = execute(
            gate_id=component_id.lower(), command=[str(item) for item in component["command"]],
            output=output_dir / "result.json", timeout=int(component["timeout_seconds"]),
            expected_artifacts=patterns,
            json_status=[str(item) for item in component["json_status"]],
            cwd=(ROOT / str(component["working_directory"])).resolve(),
            environment_allowlist=allowlist, environment_overrides=overrides,
        )
        results.append(result)

    final_junit = run_dir / "junit.xml"
    _junit(results, final_junit)
    findings = [f"{item['gate_id']}: {finding}" for item in results for finding in item["findings"]]
    final: dict[str, object] = {
        "schema_version": 1, "gate_id": "trusted_validation", "status": "pass" if not findings else "fail",
        "command": ["make", "trusted-validation-gate"], "working_directory": str(ROOT),
        "started_at": min(str(item["started_at"]) for item in results),
        "ended_at": max(str(item["ended_at"]) for item in results),
        "duration_seconds": round(sum(float(item["duration_seconds"]) for item in results), 3),
        "exit_code": 0 if not findings else 1, "timed_out": any(item["timed_out"] for item in results),
        "counts": {"tests": 10, "passed": 10 - len([item for item in results if item["status"] != "pass"]),
                   "failures": len([item for item in results if item["status"] != "pass"]),
                   "errors": 0, "skipped": 0, "xfailed": 0},
        "tool_versions": {"python": sys.version.split()[0]},
        "log": {"path": str(final_junit), "sha256": sha256(final_junit), "sanitized": True},
        "artifacts": [{"path": str(components_dir / str(item["id"]) / "result.json"),
                       "sha256": sha256(components_dir / str(item["id"]) / "result.json")}
                      for item in inventory["components"]],
        "findings": findings,
    }
    validate_result(final)
    (run_dir / "result.json").write_text(json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    render(final, run_dir / "index.html")
    print(run_dir / "index.html")
    return 0 if final["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
