"""The contract gate runs its suite as concurrent file batches and merges their JUnit."""
from pathlib import Path

from ops.release_control import contract_batches
from ops.release_control.run_gate import parse_junit


def _suite(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir()
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
