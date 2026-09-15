"""Remove three exact untagged ODIN validation images and record capacity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from .validation_image_cleanup import (
    DOCKER_MIN_FREE_KIB,
    HOST_MIN_FREE_BYTES,
    IMAGE_ID_RE,
    PROBE_IMAGE_ID,
    ROOT,
    ValidationImageError,
    _assert_fresh_output,
    _container_images,
    _docker_free_kib,
    _host_free_bytes,
)


Command = Callable[..., subprocess.CompletedProcess[str]]
RECENT_ALLOWLIST_SHA256 = "ef244e9d64f9258aad2da08ad070156474ad268bfccd7ba5f1b1e1ab2121780c"
STALE_ALLOWLIST_SHA256 = "ad98b036f035f09de1206f9ccf50b75ae9c1fd3718c010e9e5c7f131f41d2a4b"
PARENT_ALLOWLIST_SHA256 = "212991b322d63f45a52d67ebe40390c7d475a2044c70b8a7dc6f498e643c264d"
RECENT_TARGETS = [
    {
        "image_id": "sha256:1e9ff690fc9e86b66250676ff3579306e13467f627ad80d19cfa5f5a90a6bb17",
        "created": "2026-09-15T07:44:35.913111582-04:00",
        "working_dir": "/app",
        "entrypoint": ["/app/entrypoint.sh"],
    },
    {
        "image_id": "sha256:e111d2d6d7ebb364957132d2e48c3168088b6af922924d6817b6d71740a048d2",
        "created": "2026-09-15T08:00:57.38444628-04:00",
        "working_dir": "/app",
        "entrypoint": ["/app/entrypoint.sh"],
    },
    {
        "image_id": "sha256:747aa17e0ac64b92e2c2b6aea447a182598c313ed76ad762a3f03c91bf252a14",
        "created": "2026-09-15T08:28:52.181318614-04:00",
        "working_dir": "/app",
        "entrypoint": ["/app/entrypoint.sh"],
    },
]
STALE_TARGETS = [
    {
        "image_id": "sha256:058ac37d0660ecc328472fa1a76d6db0d4621830059a802502996d1c1cb03235",
        "created": "2026-05-01T10:42:32.182790497Z",
        "working_dir": "/app",
        "entrypoint": ["/app/entrypoint.sh"],
    },
    {
        "image_id": "sha256:c53984bad473885a863e54798ddcf53ff7faa7579bb0c84c8ea4aaf67e80c83c",
        "created": "2026-04-14T12:00:24.590357992Z",
        "working_dir": "/app",
        "entrypoint": ["/app/entrypoint.sh"],
    },
    {
        "image_id": "sha256:0ad83341fac9b07ead2518b55d9295d8128cbd753becabbd99f796c903799370",
        "created": "2026-09-14T18:43:58.712262651-04:00",
        "working_dir": "/build/frontend",
        "entrypoint": ["docker-entrypoint.sh"],
    },
]
PARENT_TARGETS = [
    {
        "image_id": "sha256:db924d0266552b52d34295819a57afde0425c2801296f176de66fce795e598ee",
        "created": "2026-09-15T07:44:35.219441057-04:00",
        "working_dir": "/app",
        "entrypoint": None,
    },
    {
        "image_id": "sha256:a9969c3ed0c9bb94071adf99c236c116747cd45bce593fb2150bee1b2eed9d57",
        "created": "2026-09-15T08:00:56.701376824-04:00",
        "working_dir": "/app",
        "entrypoint": None,
    },
    {
        "image_id": "sha256:9fe0e2d570717c88d1d8fb60299d34a7225ef4050839b4cfba3e9815ce4bbf0e",
        "created": "2026-09-15T08:28:51.532847992-04:00",
        "working_dir": "/app",
        "entrypoint": None,
    },
    {
        "image_id": "sha256:7d2150ade6e94ad72e9a8a060fafa7d30c19681c60485cc40360cc2446a4ed20",
        "created": "2026-09-15T09:24:28.391607115-04:00",
        "working_dir": "/build/frontend",
        "entrypoint": ["docker-entrypoint.sh"],
    },
]


def _policy(targets: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "probe_image_id": PROBE_IMAGE_ID,
        "host_min_free_bytes": HOST_MIN_FREE_BYTES,
        "docker_min_free_kib": DOCKER_MIN_FREE_KIB,
        "targets": targets,
    }


EXPECTED_TARGETS = RECENT_TARGETS
EXPECTED_POLICY = _policy(RECENT_TARGETS)
EXPECTED_POLICIES = {
    RECENT_ALLOWLIST_SHA256: EXPECTED_POLICY,
    STALE_ALLOWLIST_SHA256: _policy(STALE_TARGETS),
    PARENT_ALLOWLIST_SHA256: _policy(PARENT_TARGETS),
}


def _command(
    args: list[str], *, capture: bool = True, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValidationImageError(
            f"command could not complete: {' '.join(args[:4])}"
        ) from exc
    if check and completed.returncode:
        raise ValidationImageError(
            f"command failed ({completed.returncode}): {' '.join(args[:4])}"
        )
    return completed


def _load_policy(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationImageError("dangling-image allowlist is unreadable") from exc
    digest = hashlib.sha256(raw).hexdigest()
    expected = EXPECTED_POLICIES.get(digest)
    if expected is None or payload != expected:
        raise ValidationImageError("dangling-image allowlist is not the exact reviewed policy")
    return payload, digest


def _inspect(image_id: str, *, command: Command) -> dict[str, object] | None:
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise ValidationImageError("dangling image identity is invalid")
    completed = command(
        ["docker", "image", "inspect", image_id], capture=True, check=False
    )
    output = completed.stdout or ""
    if completed.returncode:
        if "no such image" in output.lower():
            return None
        raise ValidationImageError("dangling image inspection failed")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValidationImageError("dangling image inspection was malformed") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValidationImageError("dangling image inspection shape was invalid")
    return payload[0]


def _observe_target(target: dict[str, object], *, command: Command) -> dict[str, object]:
    image_id = str(target["image_id"])
    inspected = _inspect(image_id, command=command)
    if inspected is None:
        raise ValidationImageError("reviewed dangling image is missing")
    config = inspected.get("Config")
    if not isinstance(config, dict):
        raise ValidationImageError("dangling image configuration is invalid")
    observation = {
        "image_id": inspected.get("Id"),
        "created": inspected.get("Created"),
        "repo_tags": inspected.get("RepoTags"),
        "repo_digests": inspected.get("RepoDigests"),
        "working_dir": config.get("WorkingDir"),
        "entrypoint": config.get("Entrypoint"),
    }
    expected = {
        "image_id": target["image_id"],
        "created": target["created"],
        "repo_tags": [],
        "repo_digests": [],
        "working_dir": target["working_dir"],
        "entrypoint": target["entrypoint"],
    }
    if observation != expected:
        raise ValidationImageError("dangling image metadata drifted")
    return observation


def _remove(image_id: str, *, command: Command) -> None:
    command(["docker", "image", "rm", image_id], capture=True, check=True)
    if _inspect(image_id, command=command) is not None:
        raise ValidationImageError("dangling image remains after exact removal")


def _write_record(path: Path, record: dict[str, object]) -> None:
    required = {
        "schema_version",
        "status",
        "allowlist_sha256",
        "targets",
        "prechecks",
        "removals",
        "capacity",
        "error",
    }
    if set(record) != required or record["schema_version"] != 1:
        raise ValidationImageError("dangling cleanup record shape is invalid")
    if (
        record["status"] not in {"PASS", "FAIL"}
        or not isinstance(record["error"], str)
    ):
        raise ValidationImageError("dangling cleanup record status is invalid")
    expected_policy = EXPECTED_POLICIES.get(str(record["allowlist_sha256"]))
    if expected_policy is None or record["targets"] != expected_policy["targets"]:
        raise ValidationImageError("dangling cleanup record policy is invalid")
    prechecks = record["prechecks"]
    removals = record["removals"]
    capacity = record["capacity"]
    if not isinstance(prechecks, dict) or set(prechecks) != {
        "image_observations",
        "containers_checked",
        "container_conflicts",
    }:
        raise ValidationImageError("dangling cleanup record prechecks are invalid")
    if (
        not isinstance(prechecks["image_observations"], list)
        or not isinstance(prechecks["containers_checked"], int)
        or isinstance(prechecks["containers_checked"], bool)
        or prechecks["containers_checked"] < 0
        or not isinstance(prechecks["container_conflicts"], list)
        or not isinstance(removals, list)
    ):
        raise ValidationImageError("dangling cleanup record observations are invalid")
    expected_observation_keys = {
        "image_id",
        "created",
        "repo_tags",
        "repo_digests",
        "working_dir",
        "entrypoint",
    }
    if any(
        not isinstance(observation, dict)
        or set(observation) != expected_observation_keys
        for observation in prechecks["image_observations"]
    ):
        raise ValidationImageError("dangling cleanup record image observation is invalid")
    if any(
        not isinstance(conflict, dict)
        or set(conflict) != {"id", "image_id", "configured_image"}
        for conflict in prechecks["container_conflicts"]
    ):
        raise ValidationImageError("dangling cleanup record conflict is invalid")
    if any(
        not isinstance(removal, dict)
        or set(removal) != {"image_id", "status", "absent_after"}
        or removal["status"] != "removed"
        or removal["absent_after"] is not True
        for removal in removals
    ):
        raise ValidationImageError("dangling cleanup record removal is invalid")
    if not isinstance(capacity, dict) or set(capacity) != {
        "host_free_bytes",
        "docker_free_kib",
        "host_min_free_bytes",
        "docker_min_free_kib",
        "meets_minimum",
    }:
        raise ValidationImageError("dangling cleanup record capacity is invalid")
    for key in (
        "host_free_bytes",
        "docker_free_kib",
        "host_min_free_bytes",
        "docker_min_free_kib",
    ):
        if (
            not isinstance(capacity[key], int)
            or isinstance(capacity[key], bool)
            or capacity[key] < 0
        ):
            raise ValidationImageError("dangling cleanup record capacity value is invalid")
    if not isinstance(capacity["meets_minimum"], bool):
        raise ValidationImageError("dangling cleanup record capacity result is invalid")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def execute_cleanup(
    allowlist_path: Path, output_path: Path, *, command: Command = _command
) -> dict[str, object]:
    _assert_fresh_output(output_path)
    policy, digest = _load_policy(allowlist_path)
    targets = policy["targets"]
    if not isinstance(targets, list):
        raise ValidationImageError("dangling-image targets were not normalized")
    observations: list[dict[str, object]] = []
    removals: list[dict[str, object]] = []
    conflicts: list[dict[str, str]] = []
    containers_checked = 0
    host_free = 0
    docker_free = 0
    error = ""
    try:
        for target in targets:
            if not isinstance(target, dict):
                raise ValidationImageError("dangling-image target was not normalized")
            observations.append(_observe_target(target, command=command))
        target_ids = {str(target["image_id"]) for target in targets}
        containers = _container_images(command=command)
        containers_checked = len(containers)
        conflicts = [
            item
            for item in containers
            if item["image_id"] in target_ids or item["configured_image"] in target_ids
        ]
        if conflicts:
            raise ValidationImageError("dangling image is referenced by a container")
        for target in targets:
            image_id = str(target["image_id"])
            _remove(image_id, command=command)
            removals.append({"image_id": image_id, "status": "removed", "absent_after": True})
        host_free = _host_free_bytes()
        docker_free = _docker_free_kib(PROBE_IMAGE_ID, command=command)
        if host_free < HOST_MIN_FREE_BYTES or docker_free < DOCKER_MIN_FREE_KIB:
            raise ValidationImageError("post-cleanup capacity is below the redispatch minimum")
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    meets_minimum = (
        host_free >= HOST_MIN_FREE_BYTES and docker_free >= DOCKER_MIN_FREE_KIB
    )
    record: dict[str, object] = {
        "schema_version": 1,
        "status": "FAIL" if error else "PASS",
        "allowlist_sha256": digest,
        "targets": targets,
        "prechecks": {
            "image_observations": observations,
            "containers_checked": containers_checked,
            "container_conflicts": conflicts,
        },
        "removals": removals,
        "capacity": {
            "host_free_bytes": host_free,
            "docker_free_kib": docker_free,
            "host_min_free_bytes": HOST_MIN_FREE_BYTES,
            "docker_min_free_kib": DOCKER_MIN_FREE_KIB,
            "meets_minimum": meets_minimum,
        },
        "error": error,
    }
    if record["status"] == "PASS" and (
        len(observations) != len(targets)
        or len(removals) != len(targets)
        or conflicts
        or not meets_minimum
    ):
        raise ValidationImageError("dangling cleanup PASS record is inconsistent")
    if record["status"] == "FAIL" and not error:
        raise ValidationImageError("dangling cleanup FAIL record is inconsistent")
    _write_record(output_path, record)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowlist", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        record = execute_cleanup(args.allowlist, args.output)
    except ValidationImageError as exc:
        print(f"dangling validation image cleanup failed: {exc}")
        return 1
    print(args.output)
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
