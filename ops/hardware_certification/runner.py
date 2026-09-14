"""Deterministic code-controlled hardware certification replay gate."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as XmlElementTree
from defusedxml import ElementTree

from jsonschema import validate as validate_schema

from ops.edu_readiness.artifact_scan import scan_tree
from ops.hardware_certification.artifact import atomic_write
from ops.hardware_certification.evidence import strict_junit_counts, strict_junit_outcomes, verify_artifact
from ops.hardware_certification.implementation import (
    certification_fixture_sha256, certification_implementation_sha256,
    certification_simulator_sha256,
)
from ops.hardware_certification.replay_contracts import REPLAY_ASSERTION_CASES


ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_ROOT = Path(os.getenv("ODIN_HARDWARE_ARTIFACT_ROOT", ROOT / "artifacts" / "hardware-certification"))
PROTOCOLS = ("bambu", "elegoo", "moonraker", "prusalink")
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: str) -> None:
    atomic_write(path, value)


def _protocol_case_counts(path: Path) -> dict[str, int]:
    root = ElementTree.parse(path).getroot()
    labels = [
        (case.attrib.get("classname", "") + " " + case.attrib.get("name", "")).lower()
        for case in root.iter("testcase")
    ]
    return {protocol: sum(protocol in label for label in labels) for protocol in PROTOCOLS}


def _sanitize_junit(path: Path) -> None:
    """Rebuild JUnit from a tiny identity/outcome allowlist; discard diagnostics."""
    source = ElementTree.parse(path).getroot()
    suites = [source] if source.tag == "testsuite" else list(source.findall("testsuite"))
    sanitized_root = XmlElementTree.Element("testsuites")
    safe_identity = re.compile(r"^[A-Za-z0-9_.\-\[\]]{1,240}$")

    def identity(value: str, prefix: str) -> str:
        if safe_identity.fullmatch(value):
            return value
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"{prefix}-{digest}"

    for suite_index, suite in enumerate(suites):
        cases = list(suite.findall("testcase"))
        failure_count = error_count = skipped_count = 0
        clean_suite = XmlElementTree.SubElement(sanitized_root, "testsuite", {
            "name": identity(suite.attrib.get("name", f"suite-{suite_index}"), "suite"),
            "tests": str(len(cases)), "failures": "0", "errors": "0", "skipped": "0",
        })
        for case in cases:
            clean_case = XmlElementTree.SubElement(clean_suite, "testcase", {
                "classname": identity(case.attrib.get("classname", "test"), "class"),
                "name": identity(case.attrib.get("name", "case"), "case"),
            })
            if case.findall("failure"):
                failure_count += 1
                XmlElementTree.SubElement(clean_case, "failure", {"type": "sanitized"})
            elif case.findall("error"):
                error_count += 1
                XmlElementTree.SubElement(clean_case, "error", {"type": "sanitized"})
            elif case.findall("skipped"):
                skipped_count += 1
                original = case.find("skipped")
                marker = "xfail" if original is not None and "xfail" in (
                    original.attrib.get("type", "") + original.attrib.get("message", "")
                ).lower() else "skipped"
                XmlElementTree.SubElement(clean_case, "skipped", {"type": marker})
        clean_suite.set("failures", str(failure_count))
        clean_suite.set("errors", str(error_count))
        clean_suite.set("skipped", str(skipped_count))
    rendered = XmlElementTree.tostring(sanitized_root, encoding="unicode")
    _write(path, '<?xml version="1.0" encoding="utf-8"?>' + rendered)


def _protocol_assertions(path: Path, protocol: str, duration_ms: float) -> list[dict]:
    cases = strict_junit_outcomes(path)
    assertions = []
    for assertion_id, exact_case in REPLAY_ASSERTION_CASES[protocol].items():
        passed = cases.get(exact_case) == "pass"
        assertions.append({
            "id": assertion_id, "status": "pass" if passed else "fail",
            "reason_code": "strict_suite_passed" if passed else "strict_suite_failed",
            "duration_ms": duration_ms,
        })
    return assertions


def _render(run_id: str, commit: str, status: str, counts: dict[str, int], protocol_status: dict[str, str]) -> str:
    badge = "PASS" if status == "pass" else "FAIL"
    color = "#67d58a" if status == "pass" else "#ff7b7b"
    rows = "".join(
        f"<tr><td>{html.escape(protocol)}</td><td>SIMULATED REPLAY ONLY</td><td style='font-weight:800'>{protocol_status[protocol].upper()}</td></tr>"
        for protocol in PROTOCOLS
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ODIN Hardware Certification Replay</title>
<style>body{{font:16px/1.5 system-ui;background:#101318;color:#f5f7fa;max-width:960px;margin:auto;padding:32px}}section{{background:#1a2029;border:1px solid #3b4655;border-radius:12px;padding:20px;margin:20px 0}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #3b4655;padding:10px}}code{{color:#8fcdff}}.warn{{color:#ffd166}}</style></head><body>
<h1>ODIN Hardware Certification</h1><p style="color:{color};font-size:1.3rem;font-weight:800">{badge}</p>
<p>Run <code>{html.escape(run_id)}</code> · commit <code>{html.escape(commit)}</code></p>
<section><h2>Claim boundary</h2><p class="warn"><strong>SIMULATED REPLAY ONLY.</strong> This artifact proves code-controlled transport, parser, redaction, and authorization contracts. It is not physical-device evidence and cannot satisfy an EDU live-hardware gate or Telemetry V2 cutover.</p></section>
<section><h2>Protocol results</h2><table><thead><tr><th>Protocol</th><th>Evidence level</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table></section>
<section><h2>Strict test result</h2><p>{counts['tests']} executed · {counts['failures']} failures · {counts['errors']} errors · {counts['skipped']} skipped · {counts['xfailed']} xfailed</p></section>
</body></html>"""


def main() -> int:
    started = _now()
    commit = _git("rev-parse", "--short", "HEAD")
    run_id = os.getenv("ODIN_HARDWARE_RUN_ID") or f"{started.strftime('%Y%m%dT%H%M%SZ')}-{commit}"
    ARTIFACT_ROOT.mkdir(parents=True, mode=0o700, exist_ok=True)
    ARTIFACT_ROOT.chmod(0o700)
    run_dir = ARTIFACT_ROOT / run_id
    staging = ARTIFACT_ROOT / f".{run_id}.{os.getpid()}.tmp"
    if run_dir.exists() or staging.exists():
        raise RuntimeError("hardware certification artifact already exists")
    staging.mkdir(mode=0o700)
    published = False
    junit = staging / "junit.xml"
    command = [
        sys.executable, "-m", "pytest",
        "tests/hardware/test_read_only_certification.py",
        "tests/hardware_certification",
        "-q", "--tb=short", "-o", "xfail_strict=true",
        f"--junitxml={junit}",
    ]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = "backend:."
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)
        if not junit.exists():
            _write(junit, '<?xml version="1.0"?><testsuite tests="1" failures="0" errors="1" skipped="0"><testcase name="runner_failure"><error message="pytest produced no JUnit"/></testcase></testsuite>')
        junit.chmod(0o600)
        _sanitize_junit(junit)
        counts = strict_junit_counts(junit)
        protocol_cases = _protocol_case_counts(junit)
        strict_pass = completed.returncode == 0 and counts["tests"] > 0 and not any(counts[name] for name in ("failures", "errors", "skipped", "xfailed"))
        assertion_count = len(next(iter(REPLAY_ASSERTION_CASES.values())))
        elapsed_per_assertion = round((_now() - started).total_seconds() * 1000 / assertion_count, 2)
        protocol_assertions = {
            protocol: _protocol_assertions(junit, protocol, elapsed_per_assertion)
            for protocol in PROTOCOLS
        }
        protocol_status = {
            protocol: "pass" if all(item["status"] == "pass" for item in assertions) else "fail"
            for protocol, assertions in protocol_assertions.items()
        }
        status = "pass" if strict_pass and all(value == "pass" for value in protocol_status.values()) else "fail"
        ended = _now()
        result_paths: list[Path] = []
        for protocol in PROTOCOLS:
            assertions = protocol_assertions[protocol]
            result = {
                "schema_version": 1, "run_id": run_id, "git_commit": commit,
                "mode": "replay", "protocol": protocol, "certification_level": "simulated_replay",
                "status": protocol_status[protocol], "started_at": _iso(started), "ended_at": _iso(ended),
                "assertion_counts": {"executed": len(assertions), "passed": sum(item["status"] == "pass" for item in assertions), "failed": sum(item["status"] == "fail" for item in assertions), "blocked": 0, "skipped": 0, "xfailed": 0},
                "metrics": {"valid_sample_count": 0, "model_family": "fictional", "capabilities": ["transport_policy", "parser_contract", "authorization_policy", "artifact_redaction"]},
                "assertions": assertions,
            }
            path = staging / f"{protocol}.json"
            _write(path, json.dumps(result, indent=2, sort_keys=True) + "\n")
            validate_schema(result, json.loads((ROOT / "ops/hardware_certification/schemas/protocol-result.schema.json").read_text(encoding="utf-8")))
            for assertion in result["assertions"]:
                validate_schema(assertion, json.loads((ROOT / "ops/hardware_certification/schemas/assertion-result.schema.json").read_text(encoding="utf-8")))
            result_paths.append(path)
        index = staging / "index.html"
        diagnostics = staging / "diagnostics.json"
        _write(index, _render(run_id, commit, status, counts, protocol_status))
        diagnostic_payload = {
            "schema_version": 1, "summary": "sanitized_replay_summary",
            "assertion_count": counts["tests"],
            "protocol_case_counts": protocol_cases,
        }
        validate_schema(diagnostic_payload, json.loads((ROOT / "ops/hardware_certification/schemas/diagnostics.schema.json").read_text(encoding="utf-8")))
        _write(diagnostics, json.dumps(diagnostic_payload, indent=2, sort_keys=True) + "\n")
        files = {path.name: _sha(path) for path in [junit, index, diagnostics, *result_paths]}
        manifest = {
            "schema_version": 1, "run_id": run_id, "git_commit": commit,
            "git_dirty": bool(_git("status", "--porcelain")), "mode": "replay", "protocol": "all", "status": status,
            "started_at": _iso(started), "ended_at": _iso(ended), "junit": counts,
            "duration_seconds": (ended - started).total_seconds(),
            "implementation_sha256": certification_implementation_sha256(),
            "fixture_sha256": certification_fixture_sha256(),
            "simulator_sha256": certification_simulator_sha256(),
            "sanitizer_passed": False,
            "result_references": [f"{protocol}.json" for protocol in PROTOCOLS],
            "files": files,
        }
        manifest_path = staging / "manifest.json"
        _write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        findings = scan_tree(staging)
        if not findings:
            manifest["sanitizer_passed"] = True
            _write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            findings = scan_tree(staging)
        if findings:
            raise RuntimeError("hardware certification artifact privacy scan failed")
        validate_schema(manifest, json.loads((ROOT / "ops/hardware_certification/schemas/manifest.schema.json").read_text(encoding="utf-8")))
        os.replace(staging, run_dir)
        published = True
        verify_artifact(run_dir, expected_mode="replay")
        print(run_dir / "index.html")
        return 0 if status == "pass" else 1
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        if published and run_dir.exists():
            shutil.rmtree(run_dir)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
