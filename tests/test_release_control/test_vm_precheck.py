"""Exercise the SSH wrapper with stand-in host tools, without running any gates."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("status,browser", [(0, None), (1, "/runner/browsers")])
def test_vm_precheck_matches_runner_and_propagates_failure(tmp_path, status, browser):
    repo = tmp_path / "checkout"
    script = repo / "ops/release_control/vm_precheck.sh"
    script.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "ops/release_control/vm_precheck.sh", script)
    venv = repo / ".venv-contracts/bin"
    venv.mkdir(parents=True)
    (venv / "python").symlink_to(sys.executable)
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "uname").write_text("#!/bin/sh\necho Linux\n")
    (tools / "uname").chmod(0o755)
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
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
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


def test_vm_precheck_make_target_invokes_wrapper():
    source = (ROOT / "Makefile").read_text()
    assert "release-vm-precheck:" in source
    assert "\tbash ops/release_control/vm_precheck.sh" in source
