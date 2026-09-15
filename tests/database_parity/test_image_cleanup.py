from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ops.database_parity import runner
from ops.release_gate.policy import GatePolicyError


IMAGE_ID = "sha256:" + "a" * 64


@pytest.mark.parametrize("observed", [None, "sha256:" + "b" * 64])
def test_DI04_DI05_database_iid_tag_failure_reports_without_deletion(
    tmp_path: Path, monkeypatch, observed: str | None
) -> None:
    image = "odin-dbparity:parity-iid"
    built = False
    commands: list[list[str]] = []

    def fake_command(args, **_kwargs):
        nonlocal built
        commands.append(list(args))
        if args[:3] == ["docker", "image", "inspect"]:
            if not built or observed is None:
                return subprocess.CompletedProcess(args, 1, f"No such image: {image}\n")
            return subprocess.CompletedProcess(args, 0, observed + "\n")
        if args[:2] == ["docker", "build"]:
            Path(args[args.index("--iidfile") + 1]).write_text(
                IMAGE_ID + "\n", encoding="ascii"
            )
            built = True
            return subprocess.CompletedProcess(args, 0, "built\n")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n")
        if args[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(args, 0, "")
        return subprocess.CompletedProcess(args, 0, "")

    monkeypatch.setattr(runner, "_docker_resources", lambda _run_id: set())
    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(
        runner, "render_report", lambda _manifest, path: path.write_text("report", encoding="utf-8")
    )

    result = runner.run("parity-iid", tmp_path / "artifacts", image)

    manifest = json.loads(
        (tmp_path / "artifacts" / "parity-iid" / "manifest.json").read_text()
    )
    assert result == 1
    assert manifest["status"] == "FAIL"
    assert any(
        phase["name"] == "cleanup-image"
        and phase["status"] == "FAIL"
        and "ownership verification failed" in phase["detail"]
        for phase in manifest["phases"]
    )
    assert not any(call[:3] == ["docker", "image", "rm"] for call in commands)


def test_DI08_database_failure_cleans_resources_and_exact_owned_image(
    tmp_path: Path, monkeypatch
) -> None:
    image = "odin-dbparity:parity-unit"
    image_present = False
    owner_token = ""
    commands: list[list[str]] = []
    resources = iter(
        (
            set(),
            {"container:odin-sqlite-parity-parity-unit"},
            set(),
        )
    )
    cleaned_resources: list[set[str]] = []

    def fake_resources(_run_id: str) -> set[str]:
        return next(resources)

    def fake_cleanup(found: set[str]) -> list[str]:
        cleaned_resources.append(found)
        return []

    def fake_command(args, **kwargs):
        nonlocal image_present, owner_token
        commands.append(list(args))
        if args == ["docker", "image", "ls", "-a", "--no-trunc", "--quiet"]:
            return subprocess.CompletedProcess(
                args, 0, IMAGE_ID + "\n" if image_present else ""
            )
        check = kwargs.get("check", True)
        if args[:3] == ["docker", "image", "inspect"]:
            if image_present:
                output = (
                    IMAGE_ID + "\n"
                    if "--format" in args
                    else json.dumps([{
                        "Id": IMAGE_ID,
                        "RepoTags": [image],
                        "RepoDigests": [],
                        "Config": {"Labels": {
                            "com.runsodin.validation-image-owner": owner_token
                        }},
                    }]) + "\n"
                )
                return subprocess.CompletedProcess(args, 0, output)
            return subprocess.CompletedProcess(args, 1, f"No such image: {image}\n")
        if args[:3] == ["docker", "history", "--no-trunc"]:
            return subprocess.CompletedProcess(args, 0, IMAGE_ID + "\n")
        if args[:3] == ["docker", "image", "rm"]:
            image_present = False
            return subprocess.CompletedProcess(args, 0, f"Untagged: {image}\n")
        if args[:2] == ["docker", "build"]:
            owner_token = args[args.index("--build-arg") + 1].split("=", 1)[1]
            iidfile = Path(args[args.index("--iidfile") + 1])
            iidfile.write_text(IMAGE_ID + "\n", encoding="ascii")
            image_present = True
            return subprocess.CompletedProcess(args, 0, "built\n")
        if args[:2] == ["docker", "run"]:
            raise GatePolicyError("stop after ownership is established")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n")
        if args[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(args, 0, "")
        result = subprocess.CompletedProcess(args, 0, "")
        if check and result.returncode:
            raise GatePolicyError("unexpected failure")
        return result

    monkeypatch.setattr(runner, "_docker_resources", fake_resources)
    monkeypatch.setattr(runner, "_cleanup_resources", fake_cleanup)
    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(runner, "_assert_image_metadata", lambda _image: IMAGE_ID)
    monkeypatch.setattr(
        runner, "render_report", lambda _manifest, path: path.write_text("report", encoding="utf-8")
    )

    result = runner.run("parity-unit", tmp_path / "artifacts", image)

    assert result == 1
    assert cleaned_resources == [{"container:odin-sqlite-parity-parity-unit"}]
    assert ["docker", "image", "rm", image] in commands
    assert image_present is False


def test_database_cleanup_observation_failure_still_removes_owned_image_and_reports(
    tmp_path: Path, monkeypatch
) -> None:
    image = "odin-dbparity:parity-observation"
    image_present = False
    owner_token = ""
    commands: list[list[str]] = []
    resource_calls = 0

    def fake_resources(_run_id: str) -> set[str]:
        nonlocal resource_calls
        resource_calls += 1
        if resource_calls == 1:
            return set()
        raise OSError("simulated Docker inventory failure")

    def fake_command(args, **_kwargs):
        nonlocal image_present, owner_token
        commands.append(list(args))
        if args == ["docker", "image", "ls", "-a", "--no-trunc", "--quiet"]:
            return subprocess.CompletedProcess(
                args, 0, IMAGE_ID + "\n" if image_present else ""
            )
        if args[:3] == ["docker", "image", "inspect"]:
            if image_present:
                output = (
                    IMAGE_ID + "\n"
                    if "--format" in args
                    else json.dumps([{
                        "Id": IMAGE_ID,
                        "RepoTags": [image],
                        "RepoDigests": [],
                        "Config": {"Labels": {
                            "com.runsodin.validation-image-owner": owner_token
                        }},
                    }]) + "\n"
                )
                return subprocess.CompletedProcess(args, 0, output)
            return subprocess.CompletedProcess(args, 1, f"No such image: {image}\n")
        if args[:3] == ["docker", "history", "--no-trunc"]:
            return subprocess.CompletedProcess(args, 0, IMAGE_ID + "\n")
        if args[:3] == ["docker", "image", "rm"]:
            image_present = False
            return subprocess.CompletedProcess(args, 0, f"Untagged: {image}\n")
        if args[:2] == ["docker", "build"]:
            owner_token = args[args.index("--build-arg") + 1].split("=", 1)[1]
            Path(args[args.index("--iidfile") + 1]).write_text(
                IMAGE_ID + "\n", encoding="ascii"
            )
            image_present = True
            return subprocess.CompletedProcess(args, 0, "built\n")
        if args[:2] == ["docker", "run"]:
            raise GatePolicyError("stop after ownership is established")
        if args[:2] == ["git", "rev-parse"]:
            return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n")
        if args[:2] == ["git", "status"]:
            return subprocess.CompletedProcess(args, 0, "")
        return subprocess.CompletedProcess(args, 0, "")

    monkeypatch.setattr(runner, "_docker_resources", fake_resources)
    monkeypatch.setattr(runner, "_command", fake_command)
    monkeypatch.setattr(runner, "_assert_image_metadata", lambda _image: IMAGE_ID)
    monkeypatch.setattr(
        runner, "render_report", lambda _manifest, path: path.write_text("report", encoding="utf-8")
    )

    result = runner.run("parity-observation", tmp_path / "artifacts", image)

    assert result == 1
    assert ["docker", "image", "rm", image] in commands
    assert image_present is False
    manifest = json.loads(
        (tmp_path / "artifacts" / "parity-observation" / "manifest.json").read_text()
    )
    assert manifest["status"] == "FAIL"
    assert any(
        phase["name"] == "cleanup-recovery" and phase["status"] == "FAIL"
        for phase in manifest["phases"]
    )
