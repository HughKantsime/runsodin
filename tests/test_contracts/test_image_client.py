import json
from subprocess import CompletedProcess
from unittest.mock import Mock

import pytest

from ops.release_control.check_image_client import MINIMUMS, main


def test_minimums_for_both_tools():
    assert json.loads(MINIMUMS.read_text()) == {"pg_restore": "16.16", "pg_dump": "16.16"}


@pytest.mark.parametrize("version,suffix", [("16.16", " (Debian 16.16-1.pgdg13+1)"), ("17.2", "")])
def test_image_client_versions_pass(version, suffix, capsys):
    def run(command, **kwargs):
        tool = command[4]
        assert command == ["docker", "run", "--rm", "--entrypoint", tool, "candidate", "--version"]
        return CompletedProcess(command, 0, f"{tool} (PostgreSQL) {version}{suffix}\n")

    runner = Mock(side_effect=run)
    assert main(["candidate"], runner) == 0
    assert [call.args[0][4] for call in runner.call_args_list] == ["pg_restore", "pg_dump"]
    assert json.loads(capsys.readouterr().out) == {
        "image": "candidate", "pg_restore": version, "pg_dump": version, "ok": True,
    }


@pytest.mark.parametrize("code,output,reason", [
    (0, "pg_dump (PostgreSQL) 16.15", "16.15"),
    (0, "pg_dump (PostgreSQL) 16.9", "16.9"),
    (0, "unknown", "unreadable"),
    (127, "pg_dump (PostgreSQL) 16.16", "exit 127"),
])
def test_image_client_failure_exits_one(code, output, reason, capsys):
    runner = Mock(side_effect=[
        CompletedProcess([], 0, "pg_restore (PostgreSQL) 16.16"),
        CompletedProcess([], code, output),
    ])
    assert main(["candidate"], runner) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert all(text in captured.err for text in ("pg_dump", reason, "16.16"))
