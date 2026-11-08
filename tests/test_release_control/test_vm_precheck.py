"""Exercise the SSH wrapper with stand-in host tools, without running any gates."""
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys

import pytest

from ops.release_control import aggregate, host_lock

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("status,browser,blocked", [
    (0, None, None), (1, "/runner/browsers", None),
    (0, None, "runner"), (0, None, "aggregate"),
])
def test_vm_precheck_matches_runner_and_propagates_failure(tmp_path, status, browser, blocked):
    repo = tmp_path / "checkout"
    script = repo / "ops/release_control/vm_precheck.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "ops/release_control/vm_precheck.sh", script)
    lock_source = (ROOT / "ops/release_control/host_lock.py").read_text().replace(
        "'/tmp/odin-trusted-validation.lock'", repr(str(tmp_path / "host.lock")))
    (script.parent / "host_lock.py").write_text(lock_source)
    venv = repo / ".venv-contracts/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uname").write_text("#!/bin/sh\necho Linux\n")
    (tools / "uname").chmod(0o755)
    (tools / "ps").write_text("#!/bin/sh\n" + (
        "echo /runner/bin/Runner.Worker\n" if blocked == "runner" else "exit 0\n"))
    (tools / "ps").chmod(0o755)
    make = tools / "make"
    make.write_text(f"#!{sys.executable}\n" + """import json, os, sys
print(json.dumps({'argv': sys.argv[1:], 'mask': oct(os.umask(0)),
                  'env': dict(os.environ)}))
print('CV01: pass')
print('CV10: fail' if int(os.environ['TEST_STATUS']) else 'CV10: pass')
sys.exit(int(os.environ['TEST_STATUS']))
""")
    make.chmod(0o755)
    env = {"PATH": f"{tools}:/usr/bin:/bin", "HOME": str(tmp_path),
           "TEST_STATUS": str(status), "DOCKER_HOST": "unix:///wrong.sock",
           "MAKEFLAGS": "-n", "PYTHONPATH": "/wrong"}
    if browser:
        env["PLAYWRIGHT_BROWSERS_PATH"] = browser
    with (tmp_path / "host.lock").open("w") as held:
        if blocked == "aggregate":
            host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
        result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    if blocked:
        assert result.returncode != 0
        assert "refused:" in result.stderr
        assert "CV01:" not in result.stdout
        return
    assert result.returncode == status, result.stderr
    payload = json.loads(result.stdout.splitlines()[1])
    assert payload["argv"] == ["trusted-validation-gate", f"PYTHON={venv}/python"]
    assert payload["mask"] == "0o22"
    child = payload["env"]
    assert child["PATH"].startswith(str(venv) + ":")
    assert child["DOCKER_HOST"] == "unix:///var/run/docker.sock"
    assert child["PLAYWRIGHT_BROWSERS_PATH"] == (browser or f"{tmp_path}/.cache/ms-playwright")
    assert child["CI"] == "true"
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
        result = subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)
    assert result.returncode != 0
    assert "lock is busy or unavailable" in result.stderr
    assert "GATES STARTED" not in result.stdout
    # No stale sentinel: closing the owner releases flock without unlinking.
    with host_lock.validation_lock():
        assert lock_path.exists()


def test_precheck_lock_is_inherited_through_make_by_aggregate(lock_path, tmp_path):
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
        with pytest.raises(SystemExit, match="lock is busy"):
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


def test_runner_aggregate_waits_for_a_precheck_instead_of_failing(tmp_path, monkeypatch):
    import threading
    lock = tmp_path / "host.lock"
    monkeypatch.setattr(host_lock, "LOCK_PATH", lock)
    monkeypatch.setattr(host_lock, "LOCK_POLL_SECONDS", 0.05)
    monkeypatch.delenv(host_lock.LOCK_FD_ENV, raising=False)
    held = lock.open("w")
    host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
    threading.Timer(0.3, held.close).start()
    with host_lock.validation_lock() as fd:
        assert fd is not None


def test_runner_lock_wait_times_out_and_precheck_never_waits(tmp_path, monkeypatch):
    lock = tmp_path / "host.lock"
    monkeypatch.setattr(host_lock, "LOCK_PATH", lock)
    monkeypatch.setattr(host_lock, "LOCK_POLL_SECONDS", 0.05)
    monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, "0")
    monkeypatch.delenv(host_lock.LOCK_FD_ENV, raising=False)
    with lock.open("w") as held:
        host_lock.fcntl.flock(held, host_lock.fcntl.LOCK_EX | host_lock.fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="busy or unavailable"):
            with host_lock.validation_lock():
                pass
        monkeypatch.setenv(host_lock.LOCK_WAIT_ENV, "3600")
        with pytest.raises(SystemExit, match="busy or unavailable"):
            with host_lock.validation_lock(precheck=True):
                pass
