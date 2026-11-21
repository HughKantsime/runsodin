import runpy
import subprocess
from pathlib import Path
from unittest.mock import Mock, call

import pytest


@pytest.mark.parametrize("launcher", ["run_priced_estimate_restore", "run_account_bound_session_parity"])
@pytest.mark.parametrize("image,expected,message", [
    (None, "sha256:expected", "ODIN_PARITY_IMAGE "),
    ("candidate", None, "ODIN_PARITY_IMAGE_ID "),
    ("candidate", "sha256:other", "does not match ODIN_PARITY_IMAGE_ID"),
])
def test_launcher_rejects_unverified_runtime(monkeypatch, launcher, image, expected, message):
    for key, value in [("ODIN_PARITY_IMAGE", image), ("ODIN_PARITY_IMAGE_ID", expected)]:
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    docker = Mock(return_value=subprocess.CompletedProcess([], 0, "sha256:resolved\n"))
    monkeypatch.setattr(subprocess, "run", docker)
    no_output = Mock(side_effect=AssertionError("launcher started before verifying its image"))
    monkeypatch.setattr(Path, "mkdir", no_output)
    monkeypatch.setattr(Path, "write_text", no_output)
    with pytest.raises(SystemExit, match=message):
        runpy.run_module(f"ops.database_parity.{launcher}")
    assert [c.args[0] for c in docker.call_args_list] == (
        [("docker", "image", "inspect", "--format", "{{.Id}}", image)] if image and expected else []
    )


def test_parity_receipt_records_verified_id_and_its_architecture(monkeypatch):
    from ops.database_parity import runtime_image

    image_id = "sha256:resolved"
    monkeypatch.setenv("ODIN_PARITY_IMAGE", "candidate")
    monkeypatch.setenv("ODIN_PARITY_IMAGE_ID", image_id)
    inspect = Mock(side_effect=[image_id + "\n", "arm64\n"])
    monkeypatch.setattr(runtime_image, "run", inspect)
    assert runtime_image.runtime_image_receipt() == {
        "runtime_image": image_id, "runtime_architecture": "arm64",
    }
    assert inspect.call_args_list == [
        call("docker", "image", "inspect", "--format", "{{.Id}}", "candidate"),
        call("docker", "image", "inspect", "--format", "{{.Architecture}}", image_id),
    ]
