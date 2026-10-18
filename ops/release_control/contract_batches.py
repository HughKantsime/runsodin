"""Run a pytest suite as concurrent file batches and merge their JUnit results.

The contract suite grew from about 1,000 tests (1.9.24) to about 12,600 (2.0). One
serial pytest process over all of it no longer fits the gate, and per-test databases
accumulate under a single basetemp. This runner splits the test files into fixed
batches (sorted, so a batch always holds the same files), runs up to --jobs batches at
once, each in its own pytest process with its own basetemp (removed afterwards), and
merges every batch's JUnit into one file for the gate's JUnit policy. A batch that
exits non-zero, times out or writes no JUnit makes the whole run fail.

Usage:
  python3.11 -m ops.release_control.contract_batches --tests tests/test_contracts \
      --junit <junit.xml> --work <dir> [--jobs 6] [--batch-size 40] [--batch-timeout 1800] \
      [--exclude-marker live_environment --inventory <node-id list>]

With --exclude-marker, tests carrying that marker are deselected from every batch, and the
run fails unless the deselected node ids equal the committed --inventory exactly, so no test
can leave the gate without a reviewed inventory change.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parents[2]


@dataclass(frozen=True)
class Batch:
    name: str
    files: tuple[str, ...]


@dataclass
class BatchResult:
    batch: Batch
    exit_code: int | None
    timed_out: bool
    seconds: float
    junit: Path
    log: Path


def discover(tests: str) -> list[str]:
    base = Path(tests) if Path(tests).is_absolute() else ROOT / tests
    if not base.is_dir():
        raise SystemExit(f"test directory not found: {tests}")
    files = sorted(base.rglob("test_*.py"))
    return [str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path in files]


def plan(files: list[str], batch_size: int) -> list[Batch]:
    if batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")
    return [Batch(f"batch-{index:03d}", tuple(files[start:start + batch_size]))
            for index, start in enumerate(range(0, len(files), batch_size))]


def marked_node_ids(tests: str, marker: str) -> list[str]:
    """Node ids under ``tests`` that carry ``marker`` (pytest collection, no execution)."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", tests, "--collect-only", "-q", "-p", "no:cacheprovider", "-m", marker],
        cwd=ROOT, capture_output=True, text=True, timeout=600, check=False)
    if completed.returncode not in (0, 5):  # 5: nothing carries the marker
        raise SystemExit(f"collecting {marker} tests failed:\n{completed.stdout[-2000:]}{completed.stderr[-2000:]}")
    return sorted(line.strip() for line in completed.stdout.splitlines() if "::" in line)


def read_inventory(path: Path) -> list[str]:
    return sorted(line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip() and not line.lstrip().startswith("#"))


def inventory_problems(actual: list[str], inventory: list[str]) -> list[str]:
    added, removed = sorted(set(actual) - set(inventory)), sorted(set(inventory) - set(actual))
    return ([f"deselected but not in the inventory: {node}" for node in added]
            + [f"in the inventory but not deselected: {node}" for node in removed])


def run_batch(batch: Batch, work: Path, timeout: int, marker: str | None = None) -> BatchResult:
    junit = work / f"{batch.name}.xml"
    log = work / f"{batch.name}.log"
    temp = Path(tempfile.mkdtemp(prefix=f"odin-{batch.name}-"))
    command = [sys.executable, "-m", "pytest", *batch.files, "-v", "--tb=short",
               "-o", "xfail_strict=true", "-p", "no:cacheprovider",
               f"--basetemp={temp / 't'}", f"--junitxml={junit}",
               *(["-m", f"not {marker}"] if marker else [])]
    started = time.monotonic()
    timed_out = False
    exit_code: int | None = None
    try:
        with log.open("w", encoding="utf-8") as handle:
            completed = subprocess.run(command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                       timeout=timeout, check=False)
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return BatchResult(batch, exit_code, timed_out, round(time.monotonic() - started, 1), junit, log)


def merge(results: list[BatchResult], output: Path) -> list[str]:
    """Write one <testsuites> holding every batch's suites; return the problems found."""
    problems: list[str] = []
    merged = ET.Element("testsuites", name="pytest batches")
    for result in results:
        if not result.junit.is_file():
            problems.append(f"{result.batch.name}: no JUnit written")
            continue
        try:
            root = ET.parse(result.junit).getroot()
        except ET.ParseError as exc:
            problems.append(f"{result.batch.name}: invalid JUnit ({exc})")
            continue
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        for suite in suites:
            suite.set("name", f"{suite.get('name', 'pytest')}[{result.batch.name}]")
            merged.append(suite)
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(merged).write(output, encoding="utf-8", xml_declaration=True)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests", required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--batch-timeout", type=int, default=1800)
    parser.add_argument("--exclude-marker")
    parser.add_argument("--inventory", type=Path)
    args = parser.parse_args(argv)
    if bool(args.exclude_marker) != bool(args.inventory):
        raise SystemExit("--exclude-marker and --inventory go together")
    if args.jobs < 1:
        raise SystemExit("--jobs must be at least 1")
    batches = plan(discover(args.tests), args.batch_size)
    if not batches:
        print(f"no test files under {args.tests}")
        return 1
    args.work.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    inventory_findings: list[str] = []
    if args.exclude_marker:
        deselected = marked_node_ids(args.tests, args.exclude_marker)
        inventory_findings = inventory_problems(deselected, read_inventory(args.inventory))
        print(f"{args.exclude_marker}: {len(deselected)} deselected; inventory "
              f"{'matches' if not inventory_findings else 'DIFFERS'}")
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        results = list(pool.map(lambda batch: run_batch(batch, args.work, args.batch_timeout, args.exclude_marker), batches))
    problems = merge(results, args.junit) + inventory_findings
    for result in results:
        state = "timeout" if result.timed_out else f"exit={result.exit_code}"
        print(f"{result.batch.name} files={len(result.batch.files)} {state} seconds={result.seconds}")
        if result.timed_out:
            problems.append(f"{result.batch.name}: timed out after {args.batch_timeout}s")
        elif result.exit_code != 0:
            problems.append(f"{result.batch.name}: pytest exited {result.exit_code}")
    for result in results:
        print(f"===== {result.batch.name}: {' '.join(result.batch.files)}")
        if result.log.is_file():
            print(result.log.read_text(encoding="utf-8", errors="replace"), end="")
    print(f"batches={len(batches)} jobs={args.jobs} wall_seconds={round(time.monotonic() - started, 1)}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
