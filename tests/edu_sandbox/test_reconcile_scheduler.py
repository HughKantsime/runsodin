from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_scheduler_skips_non_runtime_phases_and_continues_after_failure(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    for sandbox_id, phase in (
        ("aaa-active", "ACTIVE"),
        ("bbb-prepared", "PREPARED"),
        ("ccc-expired", "EXPIRED"),
    ):
        directory = state_root / sandbox_id
        directory.mkdir(parents=True)
        (directory / "state.json").write_text(
            json.dumps({"sandbox_id": sandbox_id, "phase": phase}),
            encoding="utf-8",
        )

    calls = tmp_path / "calls.log"
    wrapper = tmp_path / "python-wrapper.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-c\" ]; then\n"
        f"  exec {sys.executable} \"$@\"\n"
        "fi\n"
        "last=\n"
        "for argument in \"$@\"; do last=$argument; done\n"
        "printf '%s\\n' \"$last\" >> \"$ODIN_EDU_TEST_CALLS\"\n"
        "[ \"$last\" != \"aaa-active\" ]\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)

    script = Path(__file__).parents[2] / "ops/edu_sandbox/reconcile.example.sh"
    environment = os.environ.copy()
    environment.update(
        {
            "ODIN_EDU_STATE_ROOT": str(state_root),
            "ODIN_EDU_PYTHON": str(wrapper),
            "ODIN_EDU_TEST_CALLS": str(calls),
        }
    )
    completed = subprocess.run(
        [str(script)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "aaa-active",
        "ccc-expired",
    ]
