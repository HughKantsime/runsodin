"""Exercise the SSH wrapper with stand-in host tools, without running any gates."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile

import pytest

from ops.release_control import aggregate, host_lock

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def private_home():
    # Unlike pytest's /tmp base, these ancestors meet the protected-JSON policy.
    with tempfile.TemporaryDirectory(prefix='.precheck-test-', dir=ROOT) as directory:
        yield Path(directory)


@pytest.mark.parametrize("status,browser,blocked", [
    (0, None, None), (1, "/runner/browsers", None),
    (0, None, "runner"), (0, None, "aggregate"), (17, None, "bootstrap"),
])
def test_vm_precheck_matches_runner_and_propagates_failure(tmp_path, private_home, status, browser, blocked):
    repo = tmp_path / "checkout"
    script = repo / "ops/release_control/vm_precheck.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "ops/release_control/vm_precheck.sh", script)
    lock_source = (ROOT / "ops/release_control/host_lock.py").read_text().replace(
        "'/tmp/odin-trusted-validation.lock'", repr(str(tmp_path / "host.lock")))
    (script.parent / "host_lock.py").write_text(lock_source)
    (script.parent / "bootstrap_validation_deps.sh").write_text(
        "#!/bin/sh\nset -e\ntest -n \"$ODIN_VALIDATION_LOCK_FD\"\necho bootstrap-ran > bootstrap.txt\nexit \"$TEST_BOOTSTRAP_STATUS\"\n")
    venv = repo / ".venv-contracts/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "python3.11").symlink_to(sys.executable)
    (tools / "uname").write_text("#!/bin/sh\necho Linux\n")
    (tools / "uname").chmod(0o755)
    (tools / "ps").write_text("#!/bin/sh\n" + (
        "echo /runner/bin/Runner.Worker\n" if blocked == "runner" else "exit 0\n"))
    (tools / "ps").chmod(0o755)
    make = tools / "make"
    make.write_text(f"#!{sys.executable}\n" + """import json, os, sys
from pathlib import Path
import tempfile
root = Path(os.environ['TMPDIR'])
with tempfile.TemporaryDirectory() as child:
    assert Path(child).parent == root
print(json.dumps({'tmp_mode': root.stat().st_mode & 0o777, 'argv': sys.argv[1:], 'mask': oct(os.umask(0)),
                  'env': dict(os.environ)}))
print('CV01: pass')
print('CV10: fail' if int(os.environ['TEST_STATUS']) else 'CV10: pass')
sys.exit(int(os.environ['TEST_STATUS']))
""")
    make.chmod(0o755)
    env = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(private_home),
           "RELEASE_CONTROL_RUN_ID": "fixture-run",
           "TEST_BOOTSTRAP_STATUS": "17" if blocked == "bootstrap" else "0",
           "TEST_STATUS": str(status), "DOCKER_HOST": "unix:///wrong.sock",
           "MAKEFLAGS": "-n", "PYTHONPATH": "/wrong"}
    if browser:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browser
    with (tmp_path / "host.lock").open("w") as held:
        if blocked == "aggregate":
            host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
        result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert not (private_home / '.cache/odin-precheck/fixture-run/tmp').exists()
    if blocked == "bootstrap":
        assert result.returncode == 17
        assert (repo / 'bootstrap.txt').exists()
        assert "CV01:" not in result.stdout
        return
    if blocked:
        assert not (repo / 'bootstrap.txt').exists()
        assert result.returncode != 0
        assert "refused:" in result.stderr
        assert "CV01:" not in result.stdout
        return
    assert result.returncode == status, result.stderr
    payload = json.loads(result.stdout.splitlines()[1])
    assert payload["argv"] == ["trusted-validation-gate", f"PYTHON={venv}/python"]
    assert payload["mask"] == "0o22"
    assert payload["tmp_mode"] == 0o700
    assert (repo / 'bootstrap.txt').exists()
    child = payload["env"]
    assert child["PATH"].startswith(str(venv) + ":")
    assert child["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert child["PLAYWRIGHT_BROWSERS_PATH"] == (browser or f"{private_home}/.cache/ms-playwright")
    assert child["CI"] == "true"
    for name in ("TMPDIR", "TMP", "TEMP"):
        assert child[name] == str(private_home / '.cache/odin-precheck/fixture-run/tmp')
    assert "MAKEFLAGS" not in child and "PYTHONPATH" not in child
    assert "CV01: pass" in result.stdout
    assert host_lock.LOCK_FD_ENV in child


def test_vm_precheck_make_target_invokes_wrapper():
    source = (ROOT / "Makefile").read_text()
    assert "release-vm-precheck:" in source
    assert "\tbash ops/release_control/vm_precheck.sh" in source


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / "host.lock"
    monkeypatch.setattr(host_lock, "LOCK_PATH", path)
    monkeypatch.delenv(host_lock.LOCK_FD_ENV, raising=False)
    return path


def test_precheck_refuses_active_runner_before_starting_any_gate(lock_path, monkeypatch):
    monkeypatch.setattr(host_lock, "runner_job_active", lambda: True)
    with pytest.raises(SystemExit, match="runner job is active"):
        with host_lock.validation_lock(precheck=True):
            pytest.fail("precheck must not start")
    # Refusal releases the lock for the runner aggregate.
    monkeypatch.setattr(aggregate, "run_aggregate", lambda: 42)
    assert aggregate.main() == 42


@pytest.mark.parametrize("precheck", [False, True])
def test_another_process_cannot_start_aggregate_or_precheck(lock_path, monkeypatch, precheck):
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, "0")
    monkeypatch.setattr(host_lock, "runner_job_active", lambda: False)
    with host_lock.validation_lock():
        source = (
            "from pathlib import Path; from ops.release_control import host_lock, aggregate; "
            f"host_lock.LOCK_PATH=Path({str(lock_path)!r}); "
            "aggregate.run_aggregate=lambda: print('GATES STARTED'); "
        )
        if precheck:
            source += "\nwith host_lock.validation_lock(precheck=True): print('GATES STARTED')"
        else:
            source += "aggregate.main()"
        result = subprocess.run([sys.executable, "-c", source], env=dict(os.environ),
                                capture_output=True, text=True)
    assert result.returncode != 0
    expected = ("lock is busy or unavailable" if precheck else
                "validation host lock wait timed out after 0 s")
    assert expected in result.stderr
    assert "GATES STARTED" not in result.stdout
    # No stale sentinel: closing the owner releases flock without unlinking.
    with host_lock.validation_lock():
        assert lock_path.exists()


def test_precheck_lock_is_inherited_through_make_by_aggregate(lock_path, tmp_path, monkeypatch):
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, "0")
    source = (
        "from pathlib import Path; from ops.release_control import aggregate, host_lock; "
        f"host_lock.LOCK_PATH=Path({str(lock_path)!r}); "
        "aggregate.run_aggregate=lambda: 0; raise SystemExit(aggregate.main())"
    )
    makefile = tmp_path / "Makefile"
    makefile.write_text("gate:\n\t" + shlex.join([sys.executable, "-c", source]) + "\n")
    with host_lock.validation_lock() as fd:
        result = subprocess.run(
            ["make", "-f", str(makefile), "gate"],
            env=dict(os.environ, **{host_lock.LOCK_FD_ENV: str(fd)}), pass_fds=(fd,),
            capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stderr
        # The child must not unlock the parent's shared open file description.
        with pytest.raises(SystemExit, match="validation host lock wait timed out after 0 s"):
            with host_lock.validation_lock():
                pytest.fail("parent's lock was released")


@pytest.mark.parametrize("listing,expected", [
    ("/opt/runner/bin/Runner.Worker spawnclient 1 2\n", True),
    ("Runner.Worker spawnclient 1 2\n", True),
    ("/opt/runner/bin/Runner.Listener run\n", False),
    ("bash -c echo Runner.Worker\n", False),
])
def test_runner_process_detection_is_exact(monkeypatch, listing, expected):
    monkeypatch.setattr(host_lock.subprocess, "check_output", lambda *a, **k: listing)
    assert host_lock.runner_job_active() is expected


def test_unreadable_process_list_refuses_precheck(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("unavailable")
    monkeypatch.setattr(host_lock.subprocess, "check_output", fail)
    assert host_lock.runner_job_active()


def test_inherited_lock_descriptor_cannot_bypass_different_lock_file(lock_path, tmp_path, monkeypatch):
    with (tmp_path / "unrelated").open("w") as unrelated:
        monkeypatch.setenv(host_lock.LOCK_FD_ENV, str(unrelated.fileno()))
        lock_path.touch()
        with pytest.raises(SystemExit, match="lock is busy or unavailable"):
            with host_lock.validation_lock():
                pytest.fail("unrelated fd accepted")


@pytest.fixture
def wait_clock(monkeypatch):
    class Clock:
        now = 100.0
        on_sleep = None

        def __init__(self):
            self.sleeps = []
            self.attempts = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
            if self.on_sleep is not None:
                self.on_sleep()

    clock = Clock()
    flock = host_lock.fcntl.flock

    def record_attempt(fd, operation):
        if operation & host_lock.fcntl.LOCK_EX:
            clock.attempts.append(clock.now)
        return flock(fd, operation)

    monkeypatch.setattr(host_lock.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(host_lock.time, "sleep", clock.sleep)
    monkeypatch.setattr(host_lock.fcntl, "flock", record_attempt)
    monkeypatch.setattr(host_lock, "LOCK_POLL_SECONDS", 10)
    return clock


def test_runner_aggregate_waits_for_a_precheck_instead_of_failing(
        lock_path, monkeypatch, wait_clock, capsys):
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, "25")
    monkeypatch.setattr(host_lock, "runner_job_active", lambda: False)
    with host_lock.validation_lock(precheck=True) as held:
        wait_clock.attempts.clear()

        def release_after_contention():
            assert wait_clock.attempts == [100.0]
            host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_UN)

        wait_clock.on_sleep = release_after_contention
        with host_lock.validation_lock() as fd:
            assert wait_clock.attempts == [100.0, 110.0]
            assert wait_clock.sleeps == [10]
            assert wait_clock.now == 110.0
            assert os.fstat(fd).st_ino == lock_path.stat().st_ino
            # A separate open description must be blocked by the new owner.
            with lock_path.open("r") as contender:
                with pytest.raises(BlockingIOError):
                    host_lock.fcntl.flock(
                        contender, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
    assert capsys.readouterr().out == (
        "waiting for the validation host lock "
        "(another validation or precheck holds the host lock)\n")
    # Closing the new owner also releases its lock.
    with host_lock.validation_lock():
        pass


@pytest.mark.parametrize("limit,attempts,sleeps", [
    (1, [100.0], [1]),
    (25, [100.0, 110.0, 120.0], [10, 10, 5]),
])
def test_runner_lock_positive_deadline_times_out(
        lock_path, monkeypatch, wait_clock, capsys, limit, attempts, sleeps):
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, str(limit))
    with lock_path.open("w") as held:
        host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
        wait_clock.attempts.clear()
        with pytest.raises(SystemExit, match=f"validation host lock wait timed out after {limit} s"):
            with host_lock.validation_lock():
                pytest.fail("contended lock acquired")
    assert wait_clock.attempts == attempts
    assert wait_clock.sleeps == sleeps
    assert wait_clock.now == 100.0 + limit
    assert capsys.readouterr().out.count("another validation or precheck holds the host lock") == 1


@pytest.mark.parametrize("overshoot", [0, 3])
def test_runner_never_acquires_at_or_after_deadline(
        lock_path, monkeypatch, wait_clock, overshoot):
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, "1")
    with lock_path.open("w") as held:
        host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
        wait_clock.attempts.clear()

        def release_at_deadline():
            wait_clock.now += overshoot
            held.close()

        wait_clock.on_sleep = release_at_deadline
        with pytest.raises(SystemExit, match="validation host lock wait timed out after 1 s"):
            with host_lock.validation_lock():
                pytest.fail("lock acquired after its deadline")
    assert wait_clock.attempts == [100.0]
    assert wait_clock.sleeps == [1]


@pytest.mark.parametrize("precheck,limit", [(False, "0"), (False, "-1"), (True, "3600")])
def test_zero_wait_and_precheck_never_sleep(lock_path, monkeypatch, wait_clock, precheck, limit):
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, limit)

    def never_sleep(seconds):
        pytest.fail("immediate refusal must not sleep")

    monkeypatch.setattr(host_lock.time, "sleep", never_sleep)
    with lock_path.open("w") as held:
        host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
        wait_clock.attempts.clear()
        expected = ("busy or unavailable" if precheck else
                    "validation host lock wait timed out after 0 s")
        with pytest.raises(SystemExit, match=expected):
            with host_lock.validation_lock(precheck=precheck):
                pytest.fail("contended lock acquired")
    assert wait_clock.attempts == [100.0]
    assert wait_clock.now == 100.0


def test_zero_wait_still_acquires_immediately(lock_path, monkeypatch, wait_clock):
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, "0")
    with host_lock.validation_lock():
        assert wait_clock.attempts == [100.0]
        assert wait_clock.sleeps == []
