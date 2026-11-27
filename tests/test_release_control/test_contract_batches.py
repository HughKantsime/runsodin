"""The contract gate runs its suite as concurrent file batches and merges their JUnit."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ops.release_control import batch_supervision, contract_batches
from ops.release_control.run_gate import parse_junit


def _suite(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir()
    # Pin pytest's rootdir to the suite. Without a config file pytest uses the common
    # ancestor of the cwd (the checkout) and the suite, which is $HOME when TMPDIR is
    # under $HOME (the VM precheck), and node ids would then carry a .cache/... prefix.
    (directory / "pytest.ini").write_text("[pytest]\nmarkers =\n    live_environment: needs a live stack\n")
    for name, body in files.items():
        (directory / name).write_text(body)
    return directory


def test_batches_are_fixed_sorted_slices():
    files = [f"tests/test_{name}.py" for name in "dbca"]
    batches = contract_batches.plan(sorted(files), 3)
    assert [batch.files for batch in batches] == [
        ("tests/test_a.py", "tests/test_b.py", "tests/test_c.py"), ("tests/test_d.py",)]
    assert [batch.name for batch in batches] == ["batch-000", "batch-001"]


def test_every_outcome_reaches_the_merged_junit_and_a_failure_fails_the_run(tmp_path):
    suite = _suite(tmp_path / "suite", {
        "test_pass.py": "def test_ok():\n    assert True\n",
        "test_fail.py": "def test_broken():\n    assert False\n",
        "test_skip.py": "import pytest\n\ndef test_skipped():\n    pytest.skip('environment missing')\n",
    })
    junit = tmp_path / "out" / "junit.xml"
    code = contract_batches.main(["--tests", str(suite), "--junit", str(junit), "--work", str(tmp_path / "work"),
                                  "--jobs", "2", "--batch-size", "1"])
    assert code == 1
    counts = parse_junit(junit)
    assert (counts.tests, counts.passed, counts.failures, counts.skipped) == (3, 1, 1, 1)


def test_a_clean_suite_passes(tmp_path):
    suite = _suite(tmp_path / "suite", {f"test_{index}.py": "def test_ok():\n    assert True\n" for index in range(4)})
    junit = tmp_path / "junit.xml"
    assert contract_batches.main(["--tests", str(suite), "--junit", str(junit), "--work", str(tmp_path / "work"),
                                  "--jobs", "3", "--batch-size", "2"]) == 0
    assert parse_junit(junit).passed == 4


def test_multiple_roots_run_once_and_a_failure_in_either_root_fails(tmp_path):
    first = _suite(tmp_path / "contracts", {"test_contract.py": "def test_ok():\n    assert True\n"})
    second = _suite(tmp_path / "backend", {"test_backend.py": "def test_broken():\n    assert False\n"})
    roots = [str(second), str(first), str(second)]
    assert contract_batches.discover(roots) == sorted([str(first / "test_contract.py"), str(second / "test_backend.py")])
    junit = tmp_path / "junit.xml"
    assert contract_batches.main(["--tests", *roots, "--junit", str(junit), "--work", str(tmp_path / "work"),
                                  "--jobs", "2", "--batch-size", "1"]) == 1
    counts = parse_junit(junit)
    assert (counts.tests, counts.passed, counts.failures, counts.skipped) == (2, 1, 1, 0)


def test_marker_inventory_covers_every_root(tmp_path, capsys):
    first = _suite(tmp_path / "contracts", {"test_contract.py": _MARKED["test_live.py"]})
    second = _suite(tmp_path / "backend", {"test_backend.py": _MARKED["test_live.py"],
                                          "test_plain.py": _MARKED["test_plain.py"]})
    roots = [str(first), str(second)]
    marked = contract_batches.marked_node_ids(roots, "live_environment")
    assert len(marked) == 2
    assert {Path(node.split('::')[0]).name for node in marked} == {'test_contract.py', 'test_backend.py'}
    inventory = tmp_path / "inventory.txt"
    inventory.write_text(next(node for node in marked if 'test_contract.py::' in node) + '\n')
    junit = tmp_path / "junit.xml"
    assert contract_batches.main(["--tests", *roots, "--junit", str(junit), "--work", str(tmp_path / "work"),
                                  "--exclude-marker", "live_environment", "--inventory", str(inventory)]) == 1
    assert "deselected but not in the inventory:" in capsys.readouterr().out
    counts = parse_junit(junit)
    assert (counts.tests, counts.passed, counts.skipped) == (1, 1, 0)


def test_a_batch_that_times_out_fails_the_run(tmp_path, capsys):
    suite = _suite(tmp_path / "suite", {"test_hang.py": "import time\n\ndef test_hang():\n    time.sleep(30)\n"})
    code = contract_batches.main(["--tests", str(suite), "--junit", str(tmp_path / "junit.xml"),
                                  "--work", str(tmp_path / "work"), "--batch-timeout", "2"])
    assert code == 1
    assert "PROBLEM batch-000: timed out after 2s" in capsys.readouterr().out


def test_an_empty_suite_is_a_failure(tmp_path):
    suite = _suite(tmp_path / "suite", {})
    assert contract_batches.main(["--tests", str(suite), "--junit", str(tmp_path / "junit.xml"),
                                  "--work", str(tmp_path / "work")]) == 1


_MARKED = {
    "test_plain.py": "def test_ok():\n    assert True\n",
    "test_live.py": "import pytest\n\n@pytest.mark.live_environment\ndef test_needs_stack():\n    pytest.skip('no stack')\n",
}


def test_marked_tests_are_deselected_when_the_inventory_matches(tmp_path):
    suite = _suite(tmp_path / "suite", _MARKED)
    inventory = tmp_path / "inventory.txt"
    inventory.write_text("# reviewed\ntest_live.py::test_needs_stack\n")  # ids relative to pytest's rootdir
    junit = tmp_path / "junit.xml"
    code = contract_batches.main(["--tests", str(suite), "--junit", str(junit), "--work", str(tmp_path / "work"),
                                  "--exclude-marker", "live_environment", "--inventory", str(inventory)])
    assert code == 0
    counts = parse_junit(junit)
    assert (counts.tests, counts.passed, counts.skipped) == (1, 1, 0)


def test_a_deselection_missing_from_the_inventory_fails_the_run(tmp_path, capsys):
    suite = _suite(tmp_path / "suite", _MARKED)
    inventory = tmp_path / "inventory.txt"
    inventory.write_text("# nothing reviewed\n")
    code = contract_batches.main(["--tests", str(suite), "--junit", str(tmp_path / "junit.xml"),
                                  "--work", str(tmp_path / "work"),
                                  "--exclude-marker", "live_environment", "--inventory", str(inventory)])
    assert code == 1
    assert "deselected but not in the inventory" in capsys.readouterr().out


def test_an_inventory_entry_no_longer_marked_fails_the_run(tmp_path, capsys):
    suite = _suite(tmp_path / "suite", _MARKED)
    inventory = tmp_path / "inventory.txt"
    inventory.write_text("test_live.py::test_needs_stack\ntest_plain.py::test_ok\n")
    code = contract_batches.main(["--tests", str(suite), "--junit", str(tmp_path / "junit.xml"),
                                  "--work", str(tmp_path / "work"),
                                  "--exclude-marker", "live_environment", "--inventory", str(inventory)])
    assert code == 1
    assert "in the inventory but not deselected" in capsys.readouterr().out


def test_the_committed_live_inventory_matches_the_marked_contract_tests():
    from pathlib import Path
    root = Path(contract_batches.ROOT)
    inventory = contract_batches.read_inventory(root / "tests/test_contracts/live_environment_inventory.txt")
    assert contract_batches.marked_node_ids(["tests/test_contracts", "backend/tests"], "live_environment") == inventory


def test_certification_families_stay_in_one_batch_in_sorted_order():
    files = sorted(f"tests/{name}.py" for name in (
        "test_a", "test_cert_F_B_groups_orgs", "test_cert_F_B_rbac_roles", "test_cert_F_B_sessions",
        "test_cert_F_B_wire_fixtures", "test_cert_G_capture", "test_cert_G_wire_fixtures", "test_z"))
    batches = contract_batches.plan(files, 2)
    family = [batch.files for batch in batches if any("cert_F_B" in name for name in batch.files)]
    assert family == [tuple(name for name in files if "cert_F_B" in name)]
    assert ("tests/test_cert_G_capture.py", "tests/test_cert_G_wire_fixtures.py") in [batch.files for batch in batches]
    assert [name for batch in batches for name in batch.files] == files


def _is_zombie(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file():
        try:
            text = stat_path.read_text(encoding="utf-8", errors="replace")
            after_comm = text.rsplit(")", 1)[-1].split()
            return bool(after_comm) and after_comm[0] == "Z"
        except OSError:
            return True
    try:
        completed = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if completed.returncode != 0:
        return False
    state = completed.stdout.strip()
    return not state or state.startswith("Z")


def _pid_is_gone(pid: int, wait_seconds: float = 1.5) -> bool:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        if _is_zombie(pid):
            return True
        time.sleep(0.05)
    return False


def _wait_until(predicate, *, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _atomic_write_snippet(path: Path, expr: str) -> str:
    tmp = path.with_suffix(path.suffix + ".tmp")
    return (
        f"Path({str(tmp)!r}).write_text({expr}); "
        f"Path({str(tmp)!r}).replace(Path({str(path)!r}))"
    )


def test_stalled_batch_is_killed_with_children_while_sibling_merges(tmp_path, capsys, monkeypatch):
    pid_file = tmp_path / "child.pid"
    suite = _suite(tmp_path / "suite", {
        "test_a_quiet_pass.py": "import time\n\ndef test_quiet_ok():\n    time.sleep(0.2)\n    assert True\n",
        "test_b_stalls.py": (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n\n"
            "def test_before_stall():\n"
            "    assert True\n\n"
            "def test_hangs_silently():\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"    {_atomic_write_snippet(pid_file, 'str(child.pid)')}\n"
            "    time.sleep(60)\n"
        ),
    })
    real_supervise = contract_batches.supervise

    def synced_supervise(process, log, **kwargs):
        if log.name == "batch-001.log":
            _wait_until(lambda: process.poll() is not None or (
                pid_file.is_file()
                and log.is_file()
                and "test_b_stalls.py::test_hangs_silently" in log.read_text(encoding="utf-8", errors="replace")
            ))
        return real_supervise(process, log, **kwargs)

    monkeypatch.setattr(contract_batches, "supervise", synced_supervise)
    junit = tmp_path / "junit.xml"
    work = tmp_path / "work"
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(junit), "--work", str(work),
        "--jobs", "2", "--batch-size", "1", "--stall-seconds", "2", "--batch-timeout", "30",
    ])
    assert code == 1
    out = capsys.readouterr().out
    assert "PROBLEM batch-001: stalled, no output for 2 s; last line:" in out
    assert "test_b_stalls.py::test_hangs_silently" in out
    child_pid = int(pid_file.read_text().strip())
    assert _pid_is_gone(child_pid), f"child process {child_pid} survived stall kill"
    counts = parse_junit(junit)
    assert (counts.tests, counts.passed) == (1, 1)
    timings = json.loads((work / "timings.json").read_text())
    by_name = {row["name"]: row for row in timings["batches"]}
    assert by_name["batch-000"]["state"] == "pass"
    assert by_name["batch-001"]["state"] == "stalled"
    assert "test_b_stalls.py::test_hangs_silently" in by_name["batch-001"]["last_line"]


def test_slow_batch_that_keeps_printing_lines_is_not_stalled_and_log_is_unbuffered(tmp_path):
    work = tmp_path / "work"
    log_path = work / "batch-000.log"
    suite = _suite(tmp_path / "suite", {
        "test_steady.py": (
            "import time\n"
            "from pathlib import Path\n\n"
            "def test_step_0():\n    time.sleep(0.55)\n"
            "def test_step_1():\n    time.sleep(0.55)\n"
            "def test_step_2():\n"
            f"    text = Path({str(log_path)!r}).read_text()\n"
            "    assert 'test_steady.py::test_step_0 PASSED' in text\n"
            "    assert 'test_steady.py::test_step_1 PASSED' in text\n"
            "    time.sleep(0.55)\n"
            "def test_step_3():\n    time.sleep(0.55)\n"
            "def test_step_4():\n    time.sleep(0.55)\n"
        ),
    })
    junit = tmp_path / "junit.xml"
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(junit), "--work", str(work),
        "--jobs", "1", "--batch-size", "1", "--stall-seconds", "2", "--batch-timeout", "30",
    ])
    assert code == 0
    assert parse_junit(junit).passed == 5
    timings = json.loads((work / "timings.json").read_text())
    assert timings["batches"][0]["state"] == "pass"
    assert timings["batches"][0]["seconds"] > 2.0


def test_faulthandler_timeout_is_in_command_and_dumps_stack_to_log(tmp_path):
    cmd = contract_batches.batch_command(
        contract_batches.Batch("batch-000", ("tests/test_example.py",)),
        tmp_path / "batch-000.xml", tmp_path / "t",
    )
    pairs = list(zip(cmd, cmd[1:]))
    assert ("-o", "faulthandler_timeout=600") in pairs

    suite = _suite(tmp_path / "suite", {
        "test_slow_stack.py": "import time\n\ndef test_dumps_stack():\n    time.sleep(1.6)\n    assert True\n",
    })
    work = tmp_path / "work"
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(tmp_path / "junit.xml"), "--work", str(work),
        "--jobs", "1", "--batch-size", "1", "--faulthandler-timeout", "1",
        "--stall-seconds", "10", "--batch-timeout", "30",
    ])
    assert code == 0
    log_text = (work / "batch-000.log").read_text()
    assert "Timeout (" in log_text
    assert "Thread " in log_text
    assert "test_dumps_stack" in log_text


def test_suite_deadline_merges_finished_names_running_and_unstarted(tmp_path, capsys, monkeypatch):
    never_ran = tmp_path / "never_ran.marker"
    pid_file = tmp_path / "suite_child.pid"
    suite = _suite(tmp_path / "suite", {
        "test_0_fast.py": "def test_quick():\n    assert True\n",
        "test_1_slow.py": (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n\n"
            "def test_overruns_suite():\n"
            "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"    {_atomic_write_snippet(pid_file, 'str(child.pid)')}\n"
            "    time.sleep(60)\n"
        ),
        "test_2_never.py": (
            "from pathlib import Path\n\n"
            f"def test_should_not_start():\n    Path({str(never_ran)!r}).write_text('ran')\n"
        ),
    })
    real_supervise = contract_batches.supervise
    real_run_batch = contract_batches.run_batch
    forced_deadline: list[float | None] = [None]

    def synced_supervise(process, log, **kwargs):
        if log.name == "batch-001.log":
            _wait_until(lambda: process.poll() is not None or (
                pid_file.is_file()
                and log.is_file()
                and "test_1_slow.py::test_overruns_suite" in log.read_text(encoding="utf-8", errors="replace")
            ))
            forced_deadline[0] = time.monotonic() + 0.1
            kwargs["suite_deadline"] = forced_deadline[0]
        return real_supervise(process, log, **kwargs)

    def synced_run_batch(batch, *args, **kwargs):
        if forced_deadline[0] is not None:
            kwargs["suite_deadline"] = forced_deadline[0]
        return real_run_batch(batch, *args, **kwargs)

    monkeypatch.setattr(contract_batches, "supervise", synced_supervise)
    monkeypatch.setattr(contract_batches, "run_batch", synced_run_batch)
    junit = tmp_path / "junit.xml"
    work = tmp_path / "work"
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(junit), "--work", str(work),
        "--jobs", "1", "--batch-size", "1", "--suite-timeout", "60",
        "--stall-seconds", "30", "--batch-timeout", "30",
    ])
    assert code == 1
    assert not never_ran.exists()
    assert _pid_is_gone(int(pid_file.read_text().strip()))
    counts = parse_junit(junit)
    assert (counts.tests, counts.passed) == (1, 1)
    out = capsys.readouterr().out
    assert "PROBLEM batch-001: running at suite timeout (60 s); last line:" in out
    assert "test_1_slow.py::test_overruns_suite" in out
    assert "PROBLEM batch-002: not started (suite timeout 60 s)" in out
    timings = json.loads((work / "timings.json").read_text())
    by_name = {row["name"]: row for row in timings["batches"]}
    assert by_name["batch-000"]["state"] == "pass"
    assert by_name["batch-001"]["state"] == "timeout"
    assert by_name["batch-001"]["suite_timeout"] is True
    assert "test_1_slow.py::test_overruns_suite" in by_name["batch-001"]["last_line"]
    assert by_name["batch-002"]["state"] == "not_started"
    assert by_name["batch-002"]["seconds"] == 0.0
    assert by_name["batch-002"]["start_offset"] is None


def test_per_batch_line_appears_when_batch_ends_not_only_at_end(tmp_path, capsys):
    work = tmp_path / "work"
    progress_log = work / "progress.log"
    suite = _suite(tmp_path / "suite", {
        "test_0_slow.py": (
            "import time\nfrom pathlib import Path\n\n"
            "def test_slow():\n"
            f"    progress = Path({str(progress_log)!r})\n"
            "    deadline = time.monotonic() + 15.0\n"
            "    while time.monotonic() < deadline:\n"
            "        if progress.is_file() and 'batch-001 files=1 exit=0 ' in progress.read_text(encoding='utf-8'):\n"
            "            break\n"
            "        time.sleep(0.05)\n"
        ),
        "test_1_fast.py": "def test_fast():\n    assert True\n",
    })
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(tmp_path / "junit.xml"),
        "--work", str(work), "--jobs", "2", "--batch-size", "1",
    ])
    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    progress_lines = [line for line in lines if line.startswith(("batch-000 files=", "batch-001 files="))]
    # First two lines are live completions (fast batch-001 ends before slow batch-000);
    # next two are the final table in sorted batch order.
    assert len(progress_lines) == 4
    assert progress_lines[0].startswith("batch-001 files=1 exit=0 ") and progress_lines[0].endswith(" tests=1")
    assert progress_lines[1].startswith("batch-000 files=1 exit=0 ") and progress_lines[1].endswith(" tests=1")
    assert progress_lines[2].startswith("batch-000 files=1 exit=0 ")
    assert progress_lines[3].startswith("batch-001 files=1 exit=0 ")


def test_timings_json_on_clean_run(tmp_path):
    suite = _suite(tmp_path / "suite", {
        f"test_{i}.py": "def test_ok():\n    assert True\n" for i in range(4)
    })
    work = tmp_path / "work"
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(tmp_path / "junit.xml"), "--work", str(work),
        "--jobs", "2", "--batch-size", "2", "--batch-timeout", "120",
        "--stall-seconds", "60", "--suite-timeout", "300",
    ])
    assert code == 0
    timings = json.loads((work / "timings.json").read_text())
    assert timings["jobs"] == 2
    assert timings["wall_seconds"] > 0
    assert timings["limits"] == {
        "batch_timeout_seconds": 120,
        "stall_seconds": 60,
        "suite_timeout_seconds": 300,
    }
    assert len(timings["batches"]) == 2
    for row in timings["batches"]:
        assert row["state"] == "pass"
        assert row["files"] == 2
        assert row["tests"] == 2
        assert row["seconds"] > 0
        assert row["start_offset"] >= 0


def test_batch_stays_in_runner_process_group(tmp_path):
    pgid_file = tmp_path / "batch.pgid"
    suite = _suite(tmp_path / "suite", {
        "test_pgid.py": (
            "import os\nfrom pathlib import Path\n\n"
            "def test_record_pgid():\n"
            f"    {_atomic_write_snippet(pgid_file, 'str(os.getpgid(0))')}\n"
        ),
    })
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(tmp_path / "junit.xml"),
        "--work", str(tmp_path / "work"), "--jobs", "1", "--batch-size", "1",
    ])
    assert code == 0
    assert int(pgid_file.read_text().strip()) == os.getpgid(0)


def test_stalled_batch_kills_grandchild_process(tmp_path, monkeypatch):
    child_pid_file = tmp_path / "child.pid"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    child_code = (
        "import subprocess, sys, time; from pathlib import Path; "
        "gc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"{_atomic_write_snippet(grandchild_pid_file, 'str(gc.pid)')}; "
        "time.sleep(60)"
    )
    suite = _suite(tmp_path / "suite", {
        "test_grandchild_stall.py": (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n\n"
            "def test_spawns_grandchild_and_stalls():\n"
            f"    child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
            f"    {_atomic_write_snippet(child_pid_file, 'str(child.pid)')}\n"
            "    time.sleep(60)\n"
        ),
    })
    real_supervise = contract_batches.supervise

    def synced_supervise(process, log, **kwargs):
        _wait_until(lambda: process.poll() is not None or (
            child_pid_file.is_file()
            and grandchild_pid_file.is_file()
            and log.is_file()
            and "test_spawns_grandchild_and_stalls" in log.read_text(encoding="utf-8", errors="replace")
        ))
        return real_supervise(process, log, **kwargs)

    monkeypatch.setattr(contract_batches, "supervise", synced_supervise)
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(tmp_path / "junit.xml"),
        "--work", str(tmp_path / "work"), "--jobs", "1", "--batch-size", "1",
        "--stall-seconds", "1", "--batch-timeout", "30",
    ])
    assert code == 1
    child_pid = int(child_pid_file.read_text().strip())
    grandchild_pid = int(grandchild_pid_file.read_text().strip())
    assert _pid_is_gone(child_pid), f"child process {child_pid} survived stall kill"
    assert _pid_is_gone(grandchild_pid), f"grandchild process {grandchild_pid} survived stall kill"


def test_ps_descendant_parser_and_kill_error_tolerance(monkeypatch):
    ps_output = " 100 1\n 200 100\n 300 200\n 400 1\n bad line\n 201 100\n"
    monkeypatch.setattr(
        batch_supervision.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=ps_output, stderr=""),
    )
    assert batch_supervision._descendants_via_ps(100) == [200, 201, 300]

    def fail_kill(pid, sig):
        raise PermissionError("not permitted") if pid == 200 else ProcessLookupError("gone")

    monkeypatch.setattr(batch_supervision.os, "kill", fail_kill)
    batch_supervision._send_signal(200, 9)
    batch_supervision._send_signal(300, 9)


def test_stale_batch_xml_timings_and_progress_are_not_reused_when_batch_stalls(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "batch-000.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuite name="pytest" tests="2"><testcase name="test_stale_1"/><testcase name="test_stale_2"/></testsuite>\n',
        encoding="utf-8",
    )
    (work / "timings.json").write_text('{"stale": true}\n', encoding="utf-8")
    (work / "progress.log").write_text("stale-progress-line\n", encoding="utf-8")

    started_marker = tmp_path / "started.marker"
    suite = _suite(tmp_path / "suite", {
        "test_stall.py": (
            "import time\nfrom pathlib import Path\n\n"
            "def test_hangs():\n"
            f"    {_atomic_write_snippet(started_marker, repr('started'))}\n"
            "    time.sleep(60)\n"
        ),
    })
    real_supervise = contract_batches.supervise

    def synced_supervise(process, log, **kwargs):
        _wait_until(lambda: process.poll() is not None or started_marker.is_file())
        return real_supervise(process, log, **kwargs)

    monkeypatch.setattr(contract_batches, "supervise", synced_supervise)
    junit = tmp_path / "junit.xml"
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(junit), "--work", str(work),
        "--jobs", "1", "--batch-size", "1", "--stall-seconds", "1", "--batch-timeout", "30",
    ])
    assert code == 1
    assert parse_junit(junit).tests == 0
    timings = json.loads((work / "timings.json").read_text())
    assert "stale" not in timings
    assert timings["batches"][0]["state"] == "stalled"
    assert timings["batches"][0]["tests"] == 0
    progress_text = (work / "progress.log").read_text()
    assert "stale-progress-line" not in progress_text
    assert "batch-000 files=1 stalled " in progress_text
    assert " tests=0" in progress_text


def test_progress_log_records_start_and_first_batch_while_later_batch_runs(tmp_path):
    work = tmp_path / "work"
    progress_log = work / "progress.log"
    seen_file = tmp_path / "seen.txt"
    suite = _suite(tmp_path / "suite", {
        "test_0_first.py": "def test_first():\n    assert True\n",
        "test_1_second.py": (
            "import time\nfrom pathlib import Path\n\n"
            "def test_observes_live_progress():\n"
            f"    progress = Path({str(progress_log)!r})\n"
            "    deadline = time.monotonic() + 15.0\n"
            "    text = ''\n"
            "    while time.monotonic() < deadline:\n"
            "        if progress.is_file():\n"
            "            text = progress.read_text(encoding='utf-8')\n"
            "            if 'batch-000 files=1 exit=0 ' in text:\n"
            "                break\n"
            "        time.sleep(0.05)\n"
            f"    Path({str(seen_file)!r}).write_text(text, encoding='utf-8')\n"
            "    assert 'batch-000 files=1 exit=0 ' in text\n"
            "    assert 'batch-001 ' not in text\n"
        ),
    })
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(tmp_path / "junit.xml"), "--work", str(work),
        "--jobs", "2", "--batch-size", "1", "--batch-timeout", "120",
        "--stall-seconds", "60", "--suite-timeout", "300",
    ])
    assert code == 0
    seen_during_run = seen_file.read_text(encoding="utf-8")
    first_line = seen_during_run.splitlines()[0]
    assert first_line.startswith("start ")
    assert "batches=2" in first_line
    assert "jobs=2" in first_line
    assert "batch_timeout=120" in first_line
    assert "stall_seconds=60" in first_line
    assert "suite_timeout=300" in first_line
    assert "batch-000 files=1 exit=0 " in seen_during_run
    final_progress = progress_log.read_text(encoding="utf-8")
    assert "batch-001 files=1 exit=0 " in final_progress


def test_marker_collection_timeout_respects_suite_deadline_and_finishes_cleanly(tmp_path, capsys, monkeypatch):
    ran_marker = tmp_path / "batch_ran.marker"
    suite = _suite(tmp_path / "suite", {
        "test_a.py": f"from pathlib import Path\n\ndef test_a():\n    Path({str(ran_marker)!r}).write_text('ran')\n",
        "test_b.py": "def test_b():\n    assert True\n",
    })
    inventory = tmp_path / "inventory.txt"
    inventory.write_text("# empty\n", encoding="utf-8")
    seen_timeouts: list[float] = []

    def slow_collect(tests, marker, timeout=600):
        seen_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(["pytest"], timeout)

    monkeypatch.setattr(contract_batches, "marked_node_ids", slow_collect)
    junit = tmp_path / "junit.xml"
    work = tmp_path / "work"
    code = contract_batches.main([
        "--tests", str(suite), "--junit", str(junit), "--work", str(work),
        "--jobs", "2", "--batch-size", "1", "--suite-timeout", "12",
        "--exclude-marker", "live_environment", "--inventory", str(inventory),
    ])
    assert code == 1
    assert not ran_marker.exists()
    assert len(seen_timeouts) == 1
    assert 0 < seen_timeouts[0] <= 12
    out = capsys.readouterr().out
    assert "PROBLEM collecting live_environment tests timed out after 12 s" in out
    assert parse_junit(junit).tests == 0
    timings = json.loads((work / "timings.json").read_text())
    assert [row["state"] for row in timings["batches"]] == ["not_started", "not_started"]
