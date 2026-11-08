"""Run isolated component groups concurrently; publish in inventory order."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from .host_lock import validation_lock
from .policy import load_inventory
from .report import render
from .run_gate import execute, sanitize, sha256, utc_now, validate_result

ROOT = Path(__file__).parents[2]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,96}$")


def playwright_browsers_path() -> str:
    """Resolve the real user's browser cache before the gate isolates HOME."""
    explicit = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if explicit:
        return explicit
    if sys.platform == "darwin":
        return str(Path.home() / "Library/Caches/ms-playwright")
    return str(Path(os.getenv("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright")


def hardware_python() -> str:
    """Prefer the locked checkout runtime, while keeping local gates runnable."""
    checkout_runtime = ROOT / ".hardware-cert-venv/bin/python"
    return str(checkout_runtime) if checkout_runtime.is_file() else "python3.11"


_FAILURE_LINE = re.compile(r"^(FAILED|ERROR) \S")
FAILURE_SUMMARY_LIMIT = 20
FAILURE_LINE_CHARS = 300
FAILURE_LOG_TAIL_BYTES = 256 * 1024  # pytest prints its short summary at the end
_OUTPUT_LOCK = threading.Lock()


def _safe(text: str) -> str:
    """Sanitize first (secrets may span the cut), then bound the length."""
    sanitized, _ = sanitize(text)
    return sanitized[:FAILURE_LINE_CHARS]


def failure_summary(result: dict[str, object]) -> list[str]:
    """Findings and pytest FAILED/ERROR lines for a failed component.

    Failed runs upload no evidence, so the job log is the only triage record.
    Every line is re-sanitized here: findings can carry decoded artifact
    values that the log sanitizer never saw. Reads at most the log's last
    FAILURE_LOG_TAIL_BYTES and never raises.
    """
    if result.get("status") == "pass":
        return []
    try:
        findings = list(result.get("findings") or [])
        lines = [f"  finding: {_safe(str(item))}" for item in findings[:FAILURE_SUMMARY_LIMIT]]
        log = result.get("log")
        log_path = Path(str(log.get("path"))) if isinstance(log, dict) and log.get("sanitized") else None
        if log_path is not None and log_path.name != "junit.xml":
            try:
                with log_path.open("rb") as handle:
                    size = handle.seek(0, os.SEEK_END)
                    handle.seek(max(0, size - FAILURE_LOG_TAIL_BYTES))
                    tail = handle.read(FAILURE_LOG_TAIL_BYTES).decode("utf-8", errors="replace")
            except OSError:
                tail = ""
            matched = 0
            for line in tail.splitlines():
                if _FAILURE_LINE.match(line):
                    matched += 1
                    if matched <= FAILURE_SUMMARY_LIMIT:
                        lines.append(f"  {_safe(line.rstrip())}")
            if matched > FAILURE_SUMMARY_LIMIT:
                lines.append(f"  ... {matched - FAILURE_SUMMARY_LIMIT} more FAILED/ERROR lines")
        return lines
    except Exception as exc:  # diagnostics must never stop aggregation
        return [f"  (failure summary unavailable: {type(exc).__name__})"]


def _emit(text: str) -> None:
    """One locked write per component so parallel phases never interleave."""
    with _OUTPUT_LOCK:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


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
    with validation_lock():
        return run_aggregate()


def run_aggregate() -> int:
    inventory = load_inventory()
    run_id = os.getenv("RELEASE_CONTROL_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%dT%H%M%SZ")
    if not RUN_ID_RE.fullmatch(run_id):
        raise SystemExit("invalid RELEASE_CONTROL_RUN_ID")
    run_dir = ROOT / "artifacts/trusted-validation" / run_id
    run_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    native = run_dir / "native"
    components_dir = run_dir / "components"
    native.mkdir()
    components_dir.mkdir()
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
        "EDU_BROWSER_PYTHON": str(ROOT / ".venv-contracts/bin/python") if (ROOT / ".venv-contracts/bin/python").is_file() else "python3.11",
        "HARDWARE_PYTHON": hardware_python(),
        "DOCKER_HOST": docker_host,
        "PLAYWRIGHT_BROWSERS_PATH": playwright_browsers_path(),
        "ODIN_TELEMETRY_V2": "1",
    }
    native_rel = str(native.relative_to(ROOT))
    started = time.monotonic()

    result_paths: dict[str, Path] = {}

    def run_component(component: dict[str, object]) -> dict[str, object]:
        component_id = str(component["id"])
        output_dir = components_dir / component_id
        component_started = utc_now()
        component_clock = time.monotonic()
        try:
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
                isolate_process_group=True,
            )
            validate_result(result)
            # Native artifacts retain detailed counts; a command without JUnit
            # still represents one attempted release gate.
            if result["counts"]["tests"] == 0:
                passed = result["status"] == "pass"
                result["counts"] = {
                    "tests": 1, "passed": int(passed), "failures": int(not passed),
                    "errors": 0, "skipped": 0, "xfailed": 0,
                }
            validate_result(result)
            result_path = output_dir / "result.json"
            result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result_paths[component_id.lower()] = result_path
        except Exception as exc:
            # Include setup, validation and evidence writes in the boundary.
            # Recovery does no I/O here: even a permanently unwritable component
            # directory must not stop this worker or any later phase.
            finding = f"component runner raised {type(exc).__name__}"
            result = {
                "schema_version": 1, "gate_id": component_id.lower(), "status": "fail",
                "command": [str(item) for item in component["command"]],
                "working_directory": str(ROOT),
                "started_at": component_started, "ended_at": utc_now(),
                "duration_seconds": round(time.monotonic() - component_clock, 3),
                "exit_code": None, "timed_out": False,
                "counts": {"tests": 1, "passed": 0, "failures": 1,
                           "errors": 0, "skipped": 0, "xfailed": 0},
                "tool_versions": {"python": sys.version.split()[0]},
                # Filled with the real JUnit hash after every component finishes.
                "log": {"path": str(run_dir / "junit.xml"), "sha256": "0" * 64, "sanitized": True},
                "artifacts": [], "findings": [finding],
            }
        summary = "\n".join([f"{component_id}: {result['status']} ({result['duration_seconds']}s)", *failure_summary(result)])
        _emit(summary)
        return result

    by_id = {component["id"]: component for component in inventory["components"]}

    def run_group(group: list[str]) -> list[dict[str, object]]:
        return [run_component(by_id[component_id]) for component_id in group]

    for phase in inventory["execution_phases"]:
        with ThreadPoolExecutor(max_workers=len(phase)) as pool:
            futures = [pool.submit(run_group, group) for group in phase]
            for future in futures:
                results.extend(future.result())
    # Durable evidence stays in inventory order; progress prints at completion.
    order = {component_id.lower(): index for index, component_id in enumerate(by_id)}
    results.sort(key=lambda item: order[str(item["gate_id"])])
    final_junit = run_dir / "junit.xml"
    _junit(results, final_junit)
    for item in results:
        gate_id = str(item["gate_id"])
        if gate_id in result_paths:
            continue
        item["log"]["sha256"] = sha256(final_junit)
        # A separate path avoids the broken per-component directory/file. If
        # storage is wholly unavailable, retain its failure in aggregate/JUnit
        # and never advertise an unwritten artifact. All gates already ran.
        recovery = components_dir / f"{gate_id}-failed.json"
        try:
            recovery.write_text(json.dumps(item, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result_paths[gate_id] = recovery
        except Exception as exc:
            item["findings"].append(f"failed result persistence raised {type(exc).__name__}")
    print(f"Aggregate wall time: {time.monotonic() - started:.3f}s", flush=True)
    findings = [f"{item['gate_id']}: {finding}" for item in results for finding in item["findings"]]
    failed = any(item["status"] != "pass" for item in results) or bool(findings)
    final: dict[str, object] = {
        "schema_version": 1, "gate_id": "trusted_validation", "status": "fail" if failed else "pass",
        "command": ["make", "trusted-validation-gate"], "working_directory": str(ROOT),
        "started_at": min(str(item["started_at"]) for item in results),
        "ended_at": max(str(item["ended_at"]) for item in results),
        "duration_seconds": round(sum(float(item["duration_seconds"]) for item in results), 3),
        "exit_code": 1 if failed else 0, "timed_out": any(item["timed_out"] for item in results),
        "counts": {"tests": 10, "passed": 10 - len([item for item in results if item["status"] != "pass"]),
                   "failures": len([item for item in results if item["status"] != "pass"]),
                   "errors": 0, "skipped": 0, "xfailed": 0},
        "tool_versions": {"python": sys.version.split()[0]},
        "log": {"path": str(final_junit), "sha256": sha256(final_junit), "sanitized": True},
        "artifacts": [{"path": str(result_paths[str(item["gate_id"])]),
                       "sha256": sha256(result_paths[str(item["gate_id"])])}
                      for item in results if str(item["gate_id"]) in result_paths],
        "findings": findings,
    }
    validate_result(final)
    (run_dir / "result.json").write_text(json.dumps(final, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    render(final, run_dir / "index.html")
    print(run_dir / "index.html")
    return 0 if final["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
