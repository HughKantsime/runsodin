"""Run a deterministic command and emit a fail-loud readiness result."""

from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import time
from pathlib import Path

try:
    from .common import utc_now, write_result
except ImportError:
    from common import utc_now, write_result


COUNT_RE = re.compile(r"(?<!\d)(\d+)\s+(passed|failed|skipped|xfailed)\b")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--gate", required=True)
    parser.add_argument("--minimum-assertions", type=int, default=1)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")

    raw_dir = args.run_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    log_path = raw_dir / f"{args.gate}.log"
    started_at = utc_now()
    started = time.perf_counter()
    process = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = process.stdout or ""
    print(output, end="")
    log_path.write_text(output, encoding="utf-8")
    counts = {"passed": 0, "failed": 0, "skipped": 0, "xfailed": 0}
    for line in output.splitlines():
        stripped = line.strip()
        # Pytest's terminal summary and Vitest's `Tests` row are the
        # authoritative totals. Ignore Vitest's separate `Test Files` row.
        is_pytest_summary = " passed" in stripped and (
            stripped.startswith("=") or re.search(r"\bpassed(?:,| in )", stripped)
        )
        is_vitest_tests = stripped.startswith("Tests ")
        if is_pytest_summary or is_vitest_tests:
            for value, label in COUNT_RE.findall(stripped):
                counts[label] += int(value)
    observed = sum(counts.values())
    executed = counts["passed"] + counts["failed"]
    findings = []
    if process.returncode:
        findings.append(f"command exited {process.returncode}")
    if observed == 0:
        findings.append("command emitted no parseable assertion counts")
    if executed < args.minimum_assertions:
        findings.append(
            f"executed {executed} checks; minimum is {args.minimum_assertions}"
        )
    if counts["skipped"] or counts["xfailed"]:
        findings.append(
            f"command reported {counts['skipped']} skipped and {counts['xfailed']} xfailed"
        )
    result = {
        "schema_version": 1,
        "run_id": args.run_id,
        "gate_id": args.gate,
        "mandatory": True,
        "status": "pass" if not findings else "fail",
        "started_at": started_at,
        "ended_at": utc_now(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "tool_versions": {"python": platform.python_version()},
        "executed_count": executed,
        "skipped_count": counts["skipped"],
        "xfailed_count": counts["xfailed"],
        "metrics": {"returncode": process.returncode},
        "findings": findings,
        "artifacts": [str(log_path.relative_to(args.run_dir))],
    }
    write_result(args.run_dir, result)
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
