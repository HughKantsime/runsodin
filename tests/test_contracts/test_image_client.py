import json
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import Mock

import pytest

from ops.release_control.check_image_client import MINIMUMS, check_image_client, main


def test_minimums_for_both_tools():
    assert json.loads(MINIMUMS.read_text()) == {"pg_restore": "16.16", "pg_dump": "16.16"}


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
def test_image_build_checks_installed_package_against_release_minimums(tool, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile").read_text()
    assert "apt-get install -y --no-install-recommends postgresql-client-16" in dockerfile
    assert MINIMUMS.name not in dockerfile

    minimums = json.loads(MINIMUMS.read_text())
    minimums[tool] = "16.17"
    policy = Mock()
    policy.read_text.return_value = json.dumps(minimums)
    monkeypatch.setattr("ops.release_control.check_image_client.MINIMUMS", policy)
    results = [CompletedProcess([], 0, f"{name} (PostgreSQL) 16.16") for name in ("pg_restore", "pg_dump")]
    with pytest.raises(ValueError, match=rf"Found {tool} 16\.16\. Version 16\.17"):
        check_image_client("candidate", Mock(side_effect=results))


@pytest.mark.parametrize("version,suffix", [("16.16", " (Debian 16.16-1.pgdg13+1)"), ("17.2", "")])
def test_image_client_versions_pass(version, suffix, capsys):
    def run(command, **kwargs):
        tool = command[command.index("--entrypoint") + 1]
        assert command == [
            "docker", "run", "--rm", "--pull=never", "--network", "none",
            "--entrypoint", tool, "candidate", "--version",
        ]
        return CompletedProcess(command, 0, f"{tool} (PostgreSQL) {version}{suffix}\n")

    runner = Mock(side_effect=run)
    assert main(["candidate"], runner) == 0
    assert [call.args[0][-3] for call in runner.call_args_list] == ["pg_restore", "pg_dump"]
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


@pytest.mark.parametrize("failure,reason", [
    (OSError("docker unavailable"), "could not run"),
    (TimeoutExpired(["docker"], 60), "could not run"),
    (CompletedProcess([], 127, "pg_restore (PostgreSQL) 16.16"), "exit 127"),
    (CompletedProcess([], 0, "pg_restore (PostgreSQL) 16.15"), "16.15"),
], ids=["os-error", "timeout", "nonzero", "too-old"])
def test_pg_restore_failure_rejects_image(failure, reason, capsys):
    results = [failure, CompletedProcess([], 0, "pg_dump (PostgreSQL) 16.16")]
    with pytest.raises(ValueError) as exc:
        check_image_client("candidate", Mock(side_effect=results))
    assert all(text in str(exc.value) for text in ("pg_restore", reason, "16.16"))
    assert capsys.readouterr().out == ""

    assert main(["candidate"], Mock(side_effect=results)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.strip() == str(exc.value)
