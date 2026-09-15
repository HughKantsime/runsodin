from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from ops.release_control import dangling_validation_image_cleanup as cleanup
from ops.release_control.validation_image_cleanup import ValidationImageError


ROOT = Path(__file__).parents[2]
ALLOWLIST = ROOT / "ops" / "release_control" / "dangling-validation-image-cleanup-20260915.json"


class DockerSimulation:
    def __init__(self, targets=None) -> None:
        targets = targets or cleanup.EXPECTED_TARGETS
        self.images = {
            target["image_id"]: {
                "Id": target["image_id"],
                "Created": target["created"],
                "RepoTags": [],
                "RepoDigests": [],
                "Config": {
                    "WorkingDir": target["working_dir"],
                    "Entrypoint": target["entrypoint"],
                },
            }
            for target in targets
        }
        self.unrelated_id = "sha256:" + "9" * 64
        self.images[self.unrelated_id] = {
            "Id": self.unrelated_id,
            "Created": "2026-01-01T00:00:00Z",
            "RepoTags": ["unrelated:latest"],
            "RepoDigests": [],
            "Config": {"WorkingDir": "/", "Entrypoint": None},
        }
        self.containers: list[dict[str, str]] = []
        self.calls: list[list[str]] = []

    def __call__(self, args, *, capture=True, check=True):
        del capture
        self.calls.append(list(args))
        returncode = 0
        output = ""
        if args[:3] == ["docker", "image", "inspect"]:
            image_id = args[-1]
            if image_id in self.images:
                output = json.dumps([self.images[image_id]])
            else:
                returncode = 1
                output = f"No such image: {image_id}\n"
        elif args[:3] == ["docker", "image", "rm"]:
            del self.images[args[-1]]
            output = f"Deleted: {args[-1]}\n"
        elif args[:3] == ["docker", "ps", "-a"]:
            output = "".join(item["id"] + "\n" for item in self.containers)
        elif args[:2] == ["docker", "inspect"]:
            item = next(container for container in self.containers if container["id"] == args[-1])
            output = f'{item["image_id"]}\t{item["configured_image"]}\n'
        elif args[:2] == ["docker", "run"]:
            output = (
                "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                "overlay 40000000 30000000 10000000 75% /\n"
            )
        else:
            raise AssertionError(f"unexpected command: {args}")
        result = subprocess.CompletedProcess(args, returncode, output)
        if check and returncode:
            raise ValidationImageError("simulated command failure")
        return result


def test_DI10_exact_images_are_removed_and_unrelated_image_is_preserved(
    tmp_path: Path, monkeypatch
) -> None:
    docker = DockerSimulation()
    monkeypatch.setattr(cleanup, "_host_free_bytes", lambda: 8 * 1024**3)
    output = tmp_path / "result.json"

    record = cleanup.execute_cleanup(ALLOWLIST, output, command=docker)

    assert record["status"] == "PASS"
    assert len(record["removals"]) == 3
    assert docker.images == {docker.unrelated_id: docker.images[docker.unrelated_id]}
    remove_calls = [call for call in docker.calls if call[:3] == ["docker", "image", "rm"]]
    assert remove_calls == [
        ["docker", "image", "rm", target["image_id"]]
        for target in cleanup.EXPECTED_TARGETS
    ]
    assert all("-f" not in call and "--force" not in call for call in remove_calls)
    assert json.loads(output.read_text(encoding="utf-8")) == record


def test_DI11_stale_odin_policy_is_separately_pinned_and_preserves_unrelated_image(
    tmp_path: Path, monkeypatch
) -> None:
    docker = DockerSimulation(cleanup.STALE_TARGETS)
    monkeypatch.setattr(cleanup, "_host_free_bytes", lambda: 8 * 1024**3)
    allowlist = ROOT / "ops" / "release_control" / "stale-odin-image-cleanup-20260915.json"

    record = cleanup.execute_cleanup(allowlist, tmp_path / "stale.json", command=docker)

    assert record["status"] == "PASS"
    assert record["allowlist_sha256"] == cleanup.STALE_ALLOWLIST_SHA256
    assert [item["image_id"] for item in record["removals"]] == [
        item["image_id"] for item in cleanup.STALE_TARGETS
    ]
    assert list(docker.images) == [docker.unrelated_id]


def test_DI12_parent_policy_is_exact_and_cannot_discover_more_images(
    tmp_path: Path, monkeypatch
) -> None:
    docker = DockerSimulation(cleanup.PARENT_TARGETS)
    extra_odin_id = "sha256:" + "7" * 64
    docker.images[extra_odin_id] = {
        "Id": extra_odin_id,
        "Created": "2026-09-15T09:00:00Z",
        "RepoTags": [],
        "RepoDigests": [],
        "Config": {"WorkingDir": "/app", "Entrypoint": ["/app/entrypoint.sh"]},
    }
    monkeypatch.setattr(cleanup, "_host_free_bytes", lambda: 8 * 1024**3)
    allowlist = ROOT / "ops" / "release_control" / "odin-parent-image-cleanup-20260915.json"

    record = cleanup.execute_cleanup(allowlist, tmp_path / "parents.json", command=docker)

    assert record["status"] == "PASS"
    assert record["allowlist_sha256"] == cleanup.PARENT_ALLOWLIST_SHA256
    assert [item["image_id"] for item in record["removals"]] == [
        item["image_id"] for item in cleanup.PARENT_TARGETS
    ]
    assert set(docker.images) == {docker.unrelated_id, extra_odin_id}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Created", "2026-09-15T00:00:00Z"),
        ("RepoTags", ["claimed:latest"]),
        ("RepoDigests", ["claimed@sha256:" + "8" * 64]),
        ("WorkingDir", "/other"),
        ("Entrypoint", ["/bin/sh"]),
    ],
)
def test_DI10_any_metadata_drift_blocks_all_removal(
    tmp_path: Path, monkeypatch, field: str, value
) -> None:
    docker = DockerSimulation()
    first = docker.images[cleanup.EXPECTED_TARGETS[0]["image_id"]]
    if field in {"WorkingDir", "Entrypoint"}:
        first["Config"][field] = value
    else:
        first[field] = value
    monkeypatch.setattr(cleanup, "_host_free_bytes", lambda: 8 * 1024**3)

    record = cleanup.execute_cleanup(ALLOWLIST, tmp_path / "failure.json", command=docker)

    assert record["status"] == "FAIL"
    assert "metadata drifted" in record["error"]
    assert not any(call[:3] == ["docker", "image", "rm"] for call in docker.calls)


def test_DI10_container_reference_blocks_all_removal(tmp_path: Path, monkeypatch) -> None:
    docker = DockerSimulation()
    target_id = cleanup.EXPECTED_TARGETS[0]["image_id"]
    docker.containers.append(
        {"id": "a" * 64, "image_id": target_id, "configured_image": target_id}
    )
    monkeypatch.setattr(cleanup, "_host_free_bytes", lambda: 8 * 1024**3)

    record = cleanup.execute_cleanup(ALLOWLIST, tmp_path / "failure.json", command=docker)

    assert record["status"] == "FAIL"
    assert record["prechecks"]["container_conflicts"]
    assert not any(call[:3] == ["docker", "image", "rm"] for call in docker.calls)


def test_DI10_allowlist_change_is_rejected_before_docker_access(tmp_path: Path) -> None:
    payload = deepcopy(cleanup.EXPECTED_POLICY)
    payload["targets"][0]["created"] = "2026-09-15T00:00:00Z"
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(payload), encoding="utf-8")
    docker = DockerSimulation()

    with pytest.raises(ValidationImageError, match="exact reviewed policy"):
        cleanup.execute_cleanup(altered, tmp_path / "result.json", command=docker)

    assert docker.calls == []


def test_DI10_reused_output_is_rejected_before_docker_access(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("preserve", encoding="utf-8")
    docker = DockerSimulation()

    with pytest.raises(ValidationImageError, match="already exists"):
        cleanup.execute_cleanup(ALLOWLIST, output, command=docker)

    assert docker.calls == []
    assert output.read_text(encoding="utf-8") == "preserve"
