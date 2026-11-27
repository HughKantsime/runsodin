"""Run a pytest suite as concurrent file batches and merge their JUnit results.

One serial pytest process over the contract suite no longer fits the gate, and per-test
databases accumulate under a single basetemp. This runner splits the test files into fixed
batches (sorted, so a batch always holds the same files), runs up to --jobs batches at
once, each in its own pytest process (staying in the runner's process group) with its own
basetemp (removed afterwards), watches each batch for silence stalls, batch backstop timeout
and suite deadline, prints per-batch timing lines as batches finish (also appended to
progress.log in --work), writes timings.json in --work, and merges every finished batch's
JUnit into one file for the gate's JUnit policy. A batch that exits non-zero, stalls, times
out, never starts or writes no JUnit makes the whole run fail.

Usage:
  python3.11 -m ops.release_control.contract_batches --tests tests/test_contracts backend/tests \
      --junit <junit.xml> --work <dir> [--jobs N] [--batch-size N] [--batch-timeout S] \
      [--stall-seconds S] [--suite-timeout S] [--faulthandler-timeout S] \
      [--exclude-marker live_environment --inventory <node-id list>]

Defaults: 6 jobs, 40 files a batch, batch timeout 3600 s, stall 900 s, faulthandler 600 s, no
suite deadline. The release gate passes its own values (structured_targets.py).

With --exclude-marker, tests carrying that marker are deselected from every batch, and the
run fails unless the deselected node ids equal the committed --inventory exactly, so no test
can leave the gate without a reviewed inventory change.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .batch_supervision import junit_test_count, kill_process_tree, last_log_line, supervise

ROOT = Path(__file__).parents[2]
DEFAULT_BATCH_TIMEOUT = 3600
DEFAULT_STALL_SECONDS = 900
DEFAULT_FAULTHANDLER_TIMEOUT = 600
_PRINT_LOCK = threading.Lock()


@dataclass(frozen=True)
class Batch:
    name: str
    files: tuple[str, ...]


@dataclass
class BatchResult:
    batch: Batch
    exit_code: int | None
    state: str  # pass, fail, timeout, stalled, not_started
    seconds: float
    start_offset: float | None
    tests: int
    junit: Path
    log: Path
    last_line: str = ""
    killed_by_suite_timeout: bool = False
    @property
    def timed_out(self) -> bool: return self.state == "timeout"


def discover(tests: str | list[str]) -> list[str]:
    files: set[Path] = set()
    for directory in [tests] if isinstance(tests, str) else tests:
        base = Path(directory) if Path(directory).is_absolute() else ROOT / directory
        if not base.is_dir():
            raise SystemExit(f"test directory not found: {directory}")
        files.update(base.rglob("test_*.py"))
    return sorted(str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path in files)


# Certification families share capture artifacts: a family's wire-fixture file
# reads what its sibling capture files wrote earlier in the same pytest run.
_FAMILY = re.compile(r"^test_cert_([A-Z])_(?:([A-Z])_)?")


def _family(path: str) -> str | None:
    match = _FAMILY.match(Path(path).name)
    return None if match is None else "cert_" + "_".join(part for part in match.groups() if part)


def plan(files: list[str], batch_size: int) -> list[Batch]:
    """Sorted slices of at most batch_size files; a certification family is never split.

    A family larger than batch_size becomes one batch of its own.
    """
    if batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")
    units: list[list[str]] = []
    for path in files:
        family = _family(path)
        if family is not None and units and _family(units[-1][-1]) == family:
            units[-1].append(path)
        else:
            units.append([path])
    batches: list[list[str]] = []
    for unit in units:
        if batches and len(batches[-1]) + len(unit) <= batch_size:
            batches[-1].extend(unit)
        else:
            batches.append(list(unit))
    return [Batch(f"batch-{index:03d}", tuple(batch)) for index, batch in enumerate(batches)]


def marked_node_ids(tests: str | list[str], marker: str, timeout: float = 600) -> list[str]:
    """Node ids under ``tests`` that carry ``marker`` (pytest collection, no execution)."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *([tests] if isinstance(tests, str) else tests),
         "--collect-only", "-q", "-p", "no:cacheprovider", "-m", marker],
        cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
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


def batch_command(
    batch: Batch, junit: Path, basetemp: Path,
    marker: str | None = None, faulthandler_timeout: int = DEFAULT_FAULTHANDLER_TIMEOUT,
) -> list[str]:
    return [sys.executable, "-m", "pytest", *batch.files, "-v", "--tb=short",
            "-o", "xfail_strict=true", "-o", f"faulthandler_timeout={faulthandler_timeout}",
            "-p", "no:cacheprovider", f"--basetemp={basetemp}", f"--junitxml={junit}",
            *(["-m", f"not {marker}"] if marker else [])]


def _batch_line(result: BatchResult) -> str:
    status = f"exit={result.exit_code}" if result.state in ("pass", "fail") else result.state
    return f"{result.batch.name} files={len(result.batch.files)} {status} seconds={result.seconds:.1f} tests={result.tests}"


def _record_progress(work: Path, line: str, *, echo: bool = True) -> None:
    with _PRINT_LOCK:
        if echo:
            print(line, flush=True)
        with (work / "progress.log").open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


def run_batch(
    batch: Batch, work: Path, timeout: int, marker: str | None = None, *,
    stall_seconds: int = DEFAULT_STALL_SECONDS, faulthandler_timeout: int = DEFAULT_FAULTHANDLER_TIMEOUT,
    suite_deadline: float | None = None, suite_started: float | None = None,
) -> BatchResult:
    junit, log = work / f"{batch.name}.xml", work / f"{batch.name}.log"
    junit.unlink(missing_ok=True)
    log.unlink(missing_ok=True)
    now = time.monotonic()
    if suite_deadline is not None and now >= suite_deadline:
        return BatchResult(batch, None, "not_started", 0.0, None, 0, junit, log)
    start_offset = round(now - suite_started, 3) if suite_started is not None else 0.0
    temp = Path(tempfile.mkdtemp(prefix=f"odin-{batch.name}-"))
    command = batch_command(batch, junit, temp / "t", marker, faulthandler_timeout)
    process: subprocess.Popen[object] | None = None
    try:
        with log.open("wb") as handle:
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=handle, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            exit_code, state, killed_by_suite = supervise(
                process, log, batch_timeout=timeout,
                stall_seconds=stall_seconds, suite_deadline=suite_deadline,
            )
    finally:
        if process is not None and process.poll() is None:
            kill_process_tree(process)
        shutil.rmtree(temp, ignore_errors=True)
    result = BatchResult(
        batch, exit_code, state, max(0.001, round(time.monotonic() - now, 3)), start_offset,
        junit_test_count(junit), junit, log,
        last_line=last_log_line(log) if state in ("timeout", "stalled") else "",
        killed_by_suite_timeout=killed_by_suite,
    )
    _record_progress(work, _batch_line(result))
    return result


def merge(results: list[BatchResult], output: Path) -> list[str]:
    """Write one <testsuites> holding every finished batch's suites; return the problems found."""
    problems: list[str] = []
    merged = ET.Element("testsuites", name="pytest batches")
    for result in results:
        if result.state == "not_started":
            continue
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


def _write_timings(
    work: Path, results: list[BatchResult], *, jobs: int, wall_seconds: float,
    batch_timeout: int, stall_seconds: int, suite_timeout: int | None,
) -> None:
    payload = {
        "jobs": jobs, "wall_seconds": wall_seconds,
        "limits": {"batch_timeout_seconds": batch_timeout, "stall_seconds": stall_seconds, "suite_timeout_seconds": suite_timeout},
        "batches": [
            {"name": r.batch.name, "files": len(r.batch.files), "seconds": r.seconds,
             "start_offset": r.start_offset, "state": r.state, "tests": r.tests,
             **({"last_line": r.last_line} if r.last_line else {}),
             **({"suite_timeout": True} if r.killed_by_suite_timeout else {})}
            for r in results
        ],
    }
    (work / "timings.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests", nargs="+", required=True)
    parser.add_argument("--junit", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=40)
    parser.add_argument("--batch-timeout", type=int, default=DEFAULT_BATCH_TIMEOUT)
    parser.add_argument("--stall-seconds", type=int, default=DEFAULT_STALL_SECONDS)
    parser.add_argument("--suite-timeout", type=int, default=None)
    parser.add_argument("--faulthandler-timeout", type=int, default=DEFAULT_FAULTHANDLER_TIMEOUT)
    parser.add_argument("--exclude-marker")
    parser.add_argument("--inventory", type=Path)
    args = parser.parse_args(argv)
    if bool(args.exclude_marker) != bool(args.inventory):
        raise SystemExit("--exclude-marker and --inventory go together")
    if args.jobs < 1 or args.batch_timeout < 1 or args.stall_seconds < 1 or args.faulthandler_timeout < 1:
        raise SystemExit("jobs and timeout options must be at least 1")
    if args.suite_timeout is not None and args.suite_timeout < 1:
        raise SystemExit("--suite-timeout must be at least 1")
    batches = plan(discover(args.tests), args.batch_size)
    if not batches:
        print(f"no test files under {args.tests}"); return 1
    args.work.mkdir(parents=True, exist_ok=True)
    (args.work / "timings.json").unlink(missing_ok=True)
    (args.work / "progress.log").unlink(missing_ok=True)
    for batch in batches:
        (args.work / f"{batch.name}.xml").unlink(missing_ok=True)
        (args.work / f"{batch.name}.log").unlink(missing_ok=True)
    _record_progress(
        args.work,
        f"start batches={len(batches)} jobs={args.jobs} batch_timeout={args.batch_timeout} "
        f"stall_seconds={args.stall_seconds} suite_timeout={args.suite_timeout}",
        echo=False,
    )
    started = time.monotonic()
    suite_deadline = None if args.suite_timeout is None else started + args.suite_timeout
    inventory_findings: list[str] = []
    collection_timed_out = False
    if args.exclude_marker:
        collect_timeout: float = 600.0
        if suite_deadline is not None:
            collect_timeout = max(0.001, min(600.0, suite_deadline - time.monotonic()))
        try:
            deselected = marked_node_ids(args.tests, args.exclude_marker, timeout=collect_timeout)
        except subprocess.TimeoutExpired as exc:
            collection_timed_out = True
            seconds = max(1, round(exc.timeout)) if isinstance(exc.timeout, (int, float)) else max(1, round(collect_timeout))
            inventory_findings = [f"collecting {args.exclude_marker} tests timed out after {seconds} s"]
        else:
            inventory_findings = inventory_problems(deselected, read_inventory(args.inventory))
            print(f"{args.exclude_marker}: {len(deselected)} deselected; inventory "
                  f"{'matches' if not inventory_findings else 'DIFFERS'}", flush=True)
    if collection_timed_out:
        results = [
            BatchResult(batch, None, "not_started", 0.0, None, 0,
                        args.work / f"{batch.name}.xml", args.work / f"{batch.name}.log")
            for batch in batches
        ]
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(
                lambda batch: run_batch(
                    batch, args.work, args.batch_timeout, args.exclude_marker,
                    stall_seconds=args.stall_seconds, faulthandler_timeout=args.faulthandler_timeout,
                    suite_deadline=suite_deadline, suite_started=started,
                ), batches,
            ))
    problems = merge(results, args.junit) + inventory_findings
    for result in results:
        print(_batch_line(result))
        if result.state == "not_started":
            reason = "marker collection timeout" if collection_timed_out else f"suite timeout {args.suite_timeout} s"
            problems.append(f"{result.batch.name}: not started ({reason})")
        elif result.killed_by_suite_timeout:
            problems.append(f"{result.batch.name}: running at suite timeout ({args.suite_timeout} s); last line: {result.last_line}")
        elif result.state == "stalled":
            problems.append(f"{result.batch.name}: stalled, no output for {args.stall_seconds} s; last line: {result.last_line}")
        elif result.state == "timeout":
            problems.append(f"{result.batch.name}: timed out after {args.batch_timeout}s; last line: {result.last_line}")
        elif result.exit_code != 0:
            problems.append(f"{result.batch.name}: pytest exited {result.exit_code}")
    wall_seconds = round(time.monotonic() - started, 3)
    _write_timings(args.work, results, jobs=args.jobs, wall_seconds=wall_seconds,
                   batch_timeout=args.batch_timeout, stall_seconds=args.stall_seconds, suite_timeout=args.suite_timeout)
    for result in results:
        print(f"===== {result.batch.name}: {' '.join(result.batch.files)}")
        if result.log.is_file():
            print(result.log.read_text(encoding="utf-8", errors="replace"), end="")
    print(f"batches={len(batches)} jobs={args.jobs} wall_seconds={round(wall_seconds, 1)}")
    for problem in problems:
        print(f"PROBLEM {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
