"""Serve the compiled UI, run the axe matrix, and emit a readiness result."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

try:
    from .common import utc_now, write_result
    from .preview_server import PreviewError, local_address, owned_preview
except ImportError:
    from common import utc_now, write_result
    from preview_server import PreviewError, local_address, owned_preview


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    raw = args.run_dir / "raw" / "accessibility.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    started = time.perf_counter()
    base_url = os.environ.get("EDU_FRONTEND_URL", "http://127.0.0.1:4173")
    local_address(base_url)
    env = dict(os.environ, EDU_ACCESSIBILITY_OUTPUT=str(raw), EDU_FRONTEND_URL=base_url)
    build = subprocess.run(["npm", "--prefix", "frontend", "run", "build"], check=False)
    process = build
    preview_finding = None
    if build.returncode == 0:
        try:
            with owned_preview(base_url):
                process = subprocess.run(
                    ["node", "tests/accessibility/accessibility_audit.mjs"],
                    env=env,
                    check=False,
                )
        except PreviewError as exc:
            preview_finding = f"preview: {exc}"
    data = json.loads(raw.read_text(encoding="utf-8")) if raw.exists() else {"results": []}
    failed = [item for item in data["results"] if not item.get("pass")]
    matrix = [item for item in data["results"] if item.get("id", "").startswith("A")]
    keyboard = [item for item in data["results"] if item.get("id", "").startswith("K")]
    expected_keyboard = {f"K{index:02d}" for index in range(1, 7)}
    observed_keyboard = {item.get("id") for item in keyboard}
    complete = len(matrix) == 93 and observed_keyboard == expected_keyboard
    impact_counts = {impact: 0 for impact in ("critical", "serious", "moderate", "minor", "unknown")}
    nonblocking_rules: dict[str, set[str]] = {"moderate": set(), "minor": set(), "unknown": set()}
    for item in matrix:
        for violation in item.get("axeFindings", []):
            impact = violation.get("impact")
            normalized = impact if impact in impact_counts else "unknown"
            impact_counts[normalized] += 1
            if normalized in nonblocking_rules:
                nonblocking_rules[normalized].add(str(violation.get("id", "unknown")))
    findings = []
    for item in failed:
        summary = ", ".join(violation["id"] for violation in item.get("serious", []))
        findings.append(
            f"{item['id']} {item['persona']} {item['theme']} {item['viewport']}: "
            f"{summary or 'assertion, request, or console failure'}"
        )
    if preview_finding:
        findings.append(preview_finding)
    result = {
        "schema_version": 1,
        "run_id": args.run_id,
        "gate_id": "accessibility",
        "mandatory": True,
        "status": "pass" if process.returncode == 0 and complete and not failed and not preview_finding else "fail",
        "started_at": started_at,
        "ended_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "tool_versions": data.get("tool", {}),
        "executed_count": len(data["results"]),
        "skipped_count": 0,
        "xfailed_count": 0,
        "metrics": {
            "matrix_cases": len(matrix),
            "keyboard_cases": len(keyboard),
            "failed_cases": len(failed),
            "expected_cases_complete": complete,
            "axe_findings_by_impact": impact_counts,
            "nonblocking_axe_rule_ids": {
                impact: sorted(rule_ids) for impact, rule_ids in nonblocking_rules.items()
            },
        },
        "findings": findings,
        "artifacts": [str(raw.relative_to(args.run_dir))],
    }
    write_result(args.run_dir, result)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
