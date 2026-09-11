"""Run a test command and reject any skipped or xfailed JUnit cases."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from defusedxml import ElementTree as ET
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    if not args:
        print("usage: run_junit_gate.py -- <command containing {junit}>", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="odin-edu-junit-") as directory:
        report = Path(directory) / "results.xml"
        command = [part.replace("{junit}", str(report)) for part in args]
        if not any(str(report) in part for part in command):
            print("test command must contain a {junit} report placeholder", file=sys.stderr)
            return 2

        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            return completed.returncode
        if not report.is_file():
            print("test command succeeded without writing its JUnit report", file=sys.stderr)
            return 2

        root = ET.parse(report).getroot()
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        tests = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
        skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
        failures = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
        errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
        if tests <= 0 or skipped or failures or errors:
            print(
                f"EDU test gate rejected JUnit totals: tests={tests} skipped={skipped} "
                f"failures={failures} errors={errors}",
                file=sys.stderr,
            )
            return 1
        print(f"EDU test gate accepted {tests} tests with zero skips/xfails")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
