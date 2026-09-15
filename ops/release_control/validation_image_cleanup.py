"""Fail-closed lifecycle and one-time cleanup for disposable validation images."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).parents[2]
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}:[a-z0-9][a-z0-9._-]{0,63}$")
GHA_TAG_RE = re.compile(r"^odin-(?:candidate:candidate|dbparity:parity)-gha-[0-9]+-1$")
Command = Callable[..., subprocess.CompletedProcess[str]]
PROBE_IMAGE_ID = "sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534"
HOST_MIN_FREE_BYTES = 5 * 1024**3
DOCKER_MIN_FREE_KIB = 3 * 1024**2
OWNER_LABEL = "com.runsodin.validation-image-owner"
OWNER_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
LIFECYCLE_LOCK_PATH = Path(tempfile.gettempdir()) / (
    f"odin-docker-image-lifecycle-{os.geteuid()}.lock"
)
EXPECTED_GHA_TARGETS = {
    "odin-candidate:candidate-gha-34964750789-1": (
        "sha256:4829081c02409f7998f1f9dd967dd43c758ca390c7aad9ac7e1884e973051b06"
    ),
    "odin-dbparity:parity-gha-34964750789-1": (
        "sha256:4829081c02409f7998f1f9dd967dd43c758ca390c7aad9ac7e1884e973051b06"
    ),
    "odin-candidate:candidate-gha-34965989876-1": (
        "sha256:2fa9060f9b015df6a515550c483f25eae4a35c0b081a3f2961324fa3c37a1d25"
    ),
    "odin-dbparity:parity-gha-34965989876-1": (
        "sha256:2fa9060f9b015df6a515550c483f25eae4a35c0b081a3f2961324fa3c37a1d25"
    ),
    "odin-candidate:candidate-gha-34968490625-1": (
        "sha256:76fdff4ecd215ffab05a69a7e87bff3a0db220ab04768cdc7fe6dd87306b3236"
    ),
}


class ValidationImageError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProtectedImageHistory:
    visible_ids: tuple[str, ...]
    protected_ids: tuple[str, ...]
    protected_sha256: str


@dataclass
class ImageLifecycleLock:
    path: Path
    fd: int
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.released = True


def acquire_image_lifecycle_lock(path: Path = LIFECYCLE_LOCK_PATH) -> ImageLifecycleLock:
    """Acquire the host-wide ODIN image-build lock without following links."""
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValidationImageError("Docker image lifecycle lock could not be opened") from exc
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise ValidationImageError("Docker image lifecycle lock metadata is unsafe")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValidationImageError("another ODIN Docker image lifecycle is active") from exc
        return ImageLifecycleLock(path=path, fd=fd)
    except BaseException:
        os.close(fd)
        raise


def release_image_lifecycle_lock(lock: ImageLifecycleLock | None) -> list[str]:
    if lock is None:
        return []
    try:
        lock.release()
    except Exception as exc:
        return [f"Docker image lifecycle lock release failed: {exc}"]
    return []


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


def _validate_tag(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise ValidationImageError("validation image tag is invalid")
    return tag


def _validate_image_id(image_id: str) -> str:
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise ValidationImageError("validation image identity is invalid")
    return image_id


def _validate_owner_token(token: str) -> str:
    if not OWNER_TOKEN_RE.fullmatch(token):
        raise ValidationImageError("validation image owner token is invalid")
    return token


def generate_owner_token() -> str:
    return secrets.token_hex(32)


def read_iidfile(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 80:
        raise ValidationImageError("build IID file is invalid")
    return _validate_image_id(path.read_text(encoding="ascii").strip())


def inspect_image_id(tag: str, *, command: Command = _command) -> str | None:
    tag = _validate_tag(tag)
    completed = command(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        capture=True,
        check=False,
    )
    output = (completed.stdout or "").strip()
    if completed.returncode:
        if "no such image" in output.lower():
            return None
        raise ValidationImageError("validation image inspection failed")
    return _validate_image_id(output)


def assert_image_absent(tag: str, *, command: Command = _command) -> None:
    if inspect_image_id(tag, command=command) is not None:
        raise ValidationImageError("validation image tag already exists")


def verify_built_image(tag: str, iidfile: Path, *, command: Command = _command) -> str:
    image_id = read_iidfile(iidfile)
    if inspect_image_id(tag, command=command) != image_id:
        raise ValidationImageError("built image tag does not match build IID")
    return image_id


def remove_owned_image(
    tag: str, expected_image_id: str, *, command: Command = _command
) -> None:
    tag = _validate_tag(tag)
    expected_image_id = _validate_image_id(expected_image_id)
    current = inspect_image_id(tag, command=command)
    if current is None:
        raise ValidationImageError("owned validation image tag is missing")
    if current != expected_image_id:
        raise ValidationImageError("owned validation image identity changed")
    command(["docker", "image", "rm", tag], capture=True, check=True)
    if inspect_image_id(tag, command=command) is not None:
        raise ValidationImageError("owned validation image tag remains after removal")


def cleanup_owned_image(
    tag: str, expected_image_id: str | None, *, command: Command = _command
) -> list[str]:
    if expected_image_id is None:
        return []
    try:
        remove_owned_image(tag, expected_image_id, command=command)
    except Exception as exc:
        return [str(exc)]
    return []


def visible_image_ids(*, command: Command = _command) -> tuple[str, ...]:
    completed = command(
        ["docker", "image", "ls", "-a", "--no-trunc", "--quiet"],
        capture=True,
        check=True,
    )
    observed: list[str] = []
    seen: set[str] = set()
    for raw_line in (completed.stdout or "").splitlines():
        image_id = _validate_image_id(raw_line.strip())
        if image_id not in seen:
            seen.add(image_id)
            observed.append(image_id)
    return tuple(observed)


def image_history_ids(
    image_id: str, *, command: Command = _command
) -> tuple[str, ...]:
    image_id = _validate_image_id(image_id)
    completed = command(
        ["docker", "history", "--no-trunc", "--quiet", image_id],
        capture=True,
        check=True,
    )
    observed: list[str] = []
    for raw_line in (completed.stdout or "").splitlines():
        value = raw_line.strip()
        if value == "<missing>":
            continue
        observed.append(_validate_image_id(value))
    if not observed:
        raise ValidationImageError("Docker image history contains no concrete identity")
    return tuple(observed)


def _identity_digest(image_ids: set[str]) -> str:
    payload = "".join(f"{image_id}\n" for image_id in sorted(image_ids)).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def capture_protected_image_history(
    *, command: Command = _command
) -> ProtectedImageHistory:
    visible = visible_image_ids(command=command)
    protected = set(visible)
    for image_id in visible:
        protected.update(image_history_ids(image_id, command=command))
    return ProtectedImageHistory(
        visible_ids=visible,
        protected_ids=tuple(sorted(protected)),
        protected_sha256=_identity_digest(protected),
    )


def protected_image_history_evidence(
    snapshot: ProtectedImageHistory | None,
) -> dict[str, object]:
    if snapshot is None:
        return {"count": 0, "sha256": None}
    return {
        "count": len(snapshot.protected_ids),
        "sha256": snapshot.protected_sha256,
    }


def _top_down_owned_order(
    identities: set[str], histories: list[tuple[str, ...]], discovery: list[str]
) -> tuple[str, ...]:
    """Return child-before-parent order for the fixed, newly owned image graph."""
    edges: dict[str, set[str]] = {image_id: set() for image_id in identities}
    incoming: dict[str, int] = {image_id: 0 for image_id in identities}
    priority = {image_id: index for index, image_id in enumerate(discovery)}
    for history in histories:
        owned_chain = [image_id for image_id in history if image_id in identities]
        for child, parent in zip(owned_chain, owned_chain[1:]):
            if parent not in edges[child]:
                edges[child].add(parent)
                incoming[parent] += 1
    ready = sorted(
        (image_id for image_id, count in incoming.items() if count == 0),
        key=lambda image_id: (priority.get(image_id, len(priority)), image_id),
    )
    ordered: list[str] = []
    while ready:
        image_id = ready.pop(0)
        ordered.append(image_id)
        for parent in sorted(edges[image_id]):
            incoming[parent] -= 1
            if incoming[parent] == 0:
                ready.append(parent)
                ready.sort(
                    key=lambda value: (priority.get(value, len(priority)), value)
                )
    if len(ordered) != len(identities):
        raise ValidationImageError("Docker image history ownership graph is cyclic")
    return tuple(ordered)


def capture_owned_image_history(
    protected: ProtectedImageHistory,
    *,
    final_image_id: str | None,
    owner_token: str,
    command: Command = _command,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    owner_token = _validate_owner_token(owner_token)
    protected_ids = set(protected.protected_ids)
    post_visible = visible_image_ids(command=command)
    new_visible = sorted(image_id for image_id in post_visible if image_id not in protected_ids)
    metadata_by_id: dict[str, dict[str, object]] = {}
    owned_roots: list[str] = []
    unlabeled_new: list[str] = []
    for image_id in new_visible:
        metadata = _inspect_image_metadata(image_id, command=command)
        if metadata is None:
            unlabeled_new.append(image_id)
            continue
        metadata_by_id[image_id] = metadata
        config = metadata.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if isinstance(labels, dict) and labels.get(OWNER_LABEL) == owner_token:
            owned_roots.append(image_id)
        else:
            unlabeled_new.append(image_id)
    if final_image_id is not None:
        final_image_id = _validate_image_id(final_image_id)
        if final_image_id not in owned_roots:
            raise ValidationImageError(
                "verified final image lacks exact build-owner provenance"
            )
    histories: list[tuple[str, ...]] = []
    discovery: list[str] = []
    owned: set[str] = set()
    for image_id in owned_roots:
        history = image_history_ids(image_id, command=command)
        if image_id not in history:
            history = (image_id, *history)
        bounded_history: list[str] = []
        for history_id in history:
            if history_id in protected_ids:
                break
            metadata = metadata_by_id.get(history_id)
            if metadata is None:
                metadata = _inspect_image_metadata(history_id, command=command)
                if metadata is not None:
                    metadata_by_id[history_id] = metadata
            if (
                history_id != image_id
                and metadata is not None
                and (metadata["RepoTags"] or metadata["RepoDigests"])
            ):
                break
            bounded_history.append(history_id)
            if history_id not in owned:
                owned.add(history_id)
                discovery.append(history_id)
        histories.append(tuple(bounded_history))
    if final_image_id is not None:
        owned.discard(final_image_id)
    return (
        tuple(owned_roots),
        _top_down_owned_order(owned, histories, discovery),
        tuple(image_id for image_id in unlabeled_new if image_id not in owned),
    )


def _inspect_image_metadata(
    image_id: str, *, command: Command = _command
) -> dict[str, object] | None:
    image_id = _validate_image_id(image_id)
    completed = command(
        ["docker", "image", "inspect", image_id], capture=True, check=False
    )
    output = (completed.stdout or "").strip()
    if completed.returncode:
        if "no such image" in output.lower():
            return None
        raise ValidationImageError("owned image-history inspection failed")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ValidationImageError("owned image-history metadata is malformed") from exc
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise ValidationImageError("owned image-history metadata is malformed")
    metadata = payload[0]
    if metadata.get("Id") != image_id:
        raise ValidationImageError("owned image-history identity changed")
    if not isinstance(metadata.get("RepoTags"), list) or not isinstance(
        metadata.get("RepoDigests"), list
    ):
        raise ValidationImageError("owned image-history tag metadata is malformed")
    return metadata


def cleanup_owned_image_history(
    owned_ids: tuple[str, ...], *, command: Command = _command
) -> tuple[list[dict[str, object]], list[str]]:
    """Clean only the caller-frozen full IDs; never discover additional targets."""
    records: list[dict[str, object]] = []
    errors: list[str] = []
    for image_id in owned_ids:
        image_id = _validate_image_id(image_id)
        record: dict[str, object] = {
            "image_id": image_id,
            "status": "FAIL",
            "observed_image_id": None,
            "repo_tags": None,
            "repo_digests": None,
            "container_conflicts": [],
            "removed": False,
            "absent_after": False,
            "error": None,
        }
        try:
            metadata = _inspect_image_metadata(image_id, command=command)
            if metadata is None:
                record.update(
                    {"status": "ALREADY_ABSENT", "absent_after": True}
                )
                records.append(record)
                continue
            record["observed_image_id"] = metadata["Id"]
            record["repo_tags"] = metadata["RepoTags"]
            record["repo_digests"] = metadata["RepoDigests"]
            containers = _container_images(command=command)
            conflicts = [
                container["id"]
                for container in containers
                if container["image_id"] == image_id
                or container["configured_image"] == image_id
            ]
            record["container_conflicts"] = conflicts
            if metadata["RepoTags"] or metadata["RepoDigests"]:
                raise ValidationImageError("owned image-history identity became tagged")
            if conflicts:
                raise ValidationImageError("owned image-history identity has a container reference")
            command(["docker", "image", "rm", image_id], capture=True, check=True)
            record["removed"] = True
            if _inspect_image_metadata(image_id, command=command) is not None:
                raise ValidationImageError("owned image-history identity remains after removal")
            record.update({"status": "REMOVED", "absent_after": True})
        except Exception as exc:
            record["error"] = str(exc)
            errors.append(f"{image_id}: {exc}")
        records.append(record)
    return records, errors


@dataclass
class DisposableImageLifecycle:
    """One locked build attempt and its fixed, fail-closed image cleanup evidence."""

    tag: str
    command: Command = _command
    owner_token: str = field(default_factory=generate_owner_token)
    lock: ImageLifecycleLock | None = None
    protected: ProtectedImageHistory | None = None
    build_attempted: bool = False
    final_image_id: str | None = None
    ownership_capture_attempted: bool = False
    ownership_capture_succeeded: bool = False
    ownership_capture_error: str | None = None
    owned_root_ids: tuple[str, ...] = ()
    owned_history_ids: tuple[str, ...] = ()
    unowned_new_ids: tuple[str, ...] = ()
    history_cleanup: list[dict[str, object]] | None = None
    cleanup_errors: list[str] | None = None

    def begin(self) -> None:
        self.tag = _validate_tag(self.tag)
        self.owner_token = _validate_owner_token(self.owner_token)
        self.lock = acquire_image_lifecycle_lock()
        self.protected = capture_protected_image_history(command=self.command)
        assert_image_absent(self.tag, command=self.command)

    def docker_build_owner_args(self) -> list[str]:
        return ["--build-arg", f"ODIN_BUILD_OWNER={self.owner_token}"]

    def mark_build_attempted(self) -> None:
        if self.lock is None or self.protected is None:
            raise ValidationImageError("image lifecycle was not initialized before build")
        self.build_attempted = True

    def establish_final_image(self, image_id: str) -> None:
        self.final_image_id = _validate_image_id(image_id)

    def freeze_ownership(self) -> None:
        if not self.build_attempted or self.protected is None:
            raise ValidationImageError("image build ownership cannot be frozen yet")
        if self.ownership_capture_attempted:
            raise ValidationImageError("image build ownership was already frozen")
        self.ownership_capture_attempted = True
        try:
            (
                self.owned_root_ids,
                self.owned_history_ids,
                self.unowned_new_ids,
            ) = capture_owned_image_history(
                self.protected,
                final_image_id=self.final_image_id,
                owner_token=self.owner_token,
                command=self.command,
            )
            self.ownership_capture_succeeded = True
        except Exception as exc:
            self.ownership_capture_error = str(exc)
            raise

    def finalize(self) -> list[str]:
        errors: list[str] = []
        history_cleanup: list[dict[str, object]] = []
        ownership_capture_failed = self.build_attempted and not self.ownership_capture_succeeded
        try:
            if ownership_capture_failed:
                detail = self.ownership_capture_error or "ownership was not frozen immediately"
                errors.append(f"owned image-history capture failed: {detail}")
            if not ownership_capture_failed:
                errors.extend(
                    f"image tag: {message}"
                    for message in cleanup_owned_image(
                        self.tag, self.final_image_id, command=self.command
                    )
                )
                history_cleanup, history_errors = cleanup_owned_image_history(
                    self.owned_history_ids, command=self.command
                )
                errors.extend(f"image history: {message}" for message in history_errors)
        finally:
            errors.extend(release_image_lifecycle_lock(self.lock))
            self.lock = None
            self.history_cleanup = history_cleanup
            self.cleanup_errors = list(errors)
        return errors

    def evidence(self) -> dict[str, object]:
        return {
            "lock_scope": "exclusive host-user ODIN Docker image lifecycle",
            "build_attempted": self.build_attempted,
            "owner_label": OWNER_LABEL,
            "owner_token_sha256": hashlib.sha256(
                self.owner_token.encode("ascii")
            ).hexdigest(),
            "protected_history": protected_image_history_evidence(self.protected),
            "final_image_id": self.final_image_id,
            "ownership_capture_attempted": self.ownership_capture_attempted,
            "ownership_capture_succeeded": self.ownership_capture_succeeded,
            "owned_root_ids": list(self.owned_root_ids),
            "owned_history_ids": list(self.owned_history_ids),
            "unowned_new_ids": list(self.unowned_new_ids),
            "history_cleanup": list(self.history_cleanup or []),
            "cleanup_errors": list(self.cleanup_errors or []),
        }


def _load_allowlist(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationImageError("cleanup allowlist is unreadable") from exc
    required = {
        "schema_version", "probe_image_id", "host_min_free_bytes",
        "docker_min_free_kib", "targets",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or payload["schema_version"] != 1
        or isinstance(payload["schema_version"], bool)
    ):
        raise ValidationImageError("cleanup allowlist shape is invalid")
    probe = _validate_image_id(str(payload["probe_image_id"]))
    host_min = payload["host_min_free_bytes"]
    docker_min = payload["docker_min_free_kib"]
    targets = payload["targets"]
    if (
        not isinstance(host_min, int)
        or isinstance(host_min, bool)
        or host_min != HOST_MIN_FREE_BYTES
        or not isinstance(docker_min, int)
        or isinstance(docker_min, bool)
        or docker_min != DOCKER_MIN_FREE_KIB
        or probe != PROBE_IMAGE_ID
        or not isinstance(targets, list)
    ):
        raise ValidationImageError("cleanup allowlist policy is invalid")
    normalized: list[dict[str, str]] = []
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"tag", "image_id"}:
            raise ValidationImageError("cleanup target shape is invalid")
        tag = str(target["tag"])
        if not GHA_TAG_RE.fullmatch(tag):
            raise ValidationImageError("cleanup target is not an exact GHA validation tag")
        normalized.append({"tag": tag, "image_id": _validate_image_id(str(target["image_id"]))})
    target_mapping = {target["tag"]: target["image_id"] for target in normalized}
    if len(target_mapping) != len(normalized) or target_mapping != EXPECTED_GHA_TARGETS:
        raise ValidationImageError("cleanup target inventory is invalid")
    return (
        {
            "schema_version": 1,
            "probe_image_id": probe,
            "host_min_free_bytes": host_min,
            "docker_min_free_kib": docker_min,
            "targets": normalized,
        },
        hashlib.sha256(raw).hexdigest(),
    )


def _container_images(*, command: Command = _command) -> list[dict[str, str]]:
    listed = command(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"],
        capture=True,
        check=True,
    )
    containers: list[dict[str, str]] = []
    for container_id in (listed.stdout or "").splitlines():
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ValidationImageError("container inventory is malformed")
        inspected = command(
            ["docker", "inspect", "--format", "{{.Image}}\t{{.Config.Image}}", container_id],
            capture=True,
            check=True,
        )
        fields = (inspected.stdout or "").strip().split("\t")
        if len(fields) != 2 or not IMAGE_ID_RE.fullmatch(fields[0]) or not fields[1]:
            raise ValidationImageError("container image observation is malformed")
        containers.append(
            {"id": container_id, "image_id": fields[0], "configured_image": fields[1]}
        )
    return containers


def _host_free_bytes() -> int:
    try:
        stats = os.statvfs(ROOT)
    except OSError as exc:
        raise ValidationImageError("host capacity observation failed") from exc
    return stats.f_bavail * stats.f_frsize


def _docker_free_kib(probe_image_id: str, *, command: Command = _command) -> int:
    completed = command(
        [
            "docker", "run", "--pull=never", "--rm", "--network", "none",
            "--read-only", "--entrypoint", "/bin/df", probe_image_id, "-Pk", "/",
        ],
        capture=True,
        check=True,
    )
    lines = [line for line in (completed.stdout or "").splitlines() if line.strip()]
    if len(lines) != 2:
        raise ValidationImageError("Docker capacity output is malformed")
    if lines[0].split() != [
        "Filesystem",
        "1024-blocks",
        "Used",
        "Available",
        "Capacity",
        "Mounted",
        "on",
    ]:
        raise ValidationImageError("Docker capacity header is malformed")
    fields = lines[1].split()
    if (
        len(fields) != 6
        or fields[-1] != "/"
        or not all(field.isdigit() for field in fields[1:4])
        or not re.fullmatch(r"[0-9]{1,3}%", fields[4])
        or int(fields[4][:-1]) > 100
        or int(fields[2]) + int(fields[3]) > int(fields[1])
    ):
        raise ValidationImageError("Docker capacity row is malformed")
    return int(fields[3])


def _assert_fresh_output(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise ValidationImageError("cleanup output path already exists")
    path.parent.mkdir(parents=True, exist_ok=True)


def _validate_record(record: dict[str, object]) -> None:
    if set(record) != {
        "schema_version",
        "status",
        "allowlist_sha256",
        "targets",
        "prechecks",
        "removals",
        "capacity",
        "error",
    }:
        raise ValidationImageError("cleanup record shape is invalid")
    if record["schema_version"] != 1 or record["status"] not in {"PASS", "FAIL"}:
        raise ValidationImageError("cleanup record status is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(record["allowlist_sha256"])):
        raise ValidationImageError("cleanup record allowlist digest is invalid")
    targets = record["targets"]
    removals = record["removals"]
    prechecks = record["prechecks"]
    capacity = record["capacity"]
    if not isinstance(targets, list) or not isinstance(removals, list):
        raise ValidationImageError("cleanup record collections are invalid")
    for target in targets:
        if (
            not isinstance(target, dict)
            or set(target) != {"tag", "image_id"}
            or not isinstance(target["tag"], str)
            or not isinstance(target["image_id"], str)
        ):
            raise ValidationImageError("cleanup record target is invalid")
    if not isinstance(prechecks, dict) or set(prechecks) != {
        "target_identity_checks",
        "containers_checked",
        "container_conflicts",
    }:
        raise ValidationImageError("cleanup record prechecks are invalid")
    if (
        not isinstance(prechecks["target_identity_checks"], list)
        or not isinstance(prechecks["containers_checked"], int)
        or isinstance(prechecks["containers_checked"], bool)
        or prechecks["containers_checked"] < 0
        or not isinstance(prechecks["container_conflicts"], list)
    ):
        raise ValidationImageError("cleanup record precheck values are invalid")
    for check in prechecks["target_identity_checks"]:
        if (
            not isinstance(check, dict)
            or set(check)
            != {"tag", "expected_image_id", "observed_image_id", "matches"}
            or not isinstance(check["tag"], str)
            or not isinstance(check["expected_image_id"], str)
            or (
                check["observed_image_id"] is not None
                and not isinstance(check["observed_image_id"], str)
            )
            or not isinstance(check["matches"], bool)
        ):
            raise ValidationImageError("cleanup record target check is invalid")
    for conflict in prechecks["container_conflicts"]:
        if (
            not isinstance(conflict, dict)
            or set(conflict) != {"id", "image_id", "configured_image"}
            or not all(isinstance(value, str) for value in conflict.values())
        ):
            raise ValidationImageError("cleanup record container conflict is invalid")
    for removal in removals:
        if (
            not isinstance(removal, dict)
            or set(removal) != {"tag", "image_id", "status", "absent_after"}
            or not isinstance(removal["tag"], str)
            or not isinstance(removal["image_id"], str)
            or removal["status"] != "removed"
            or removal["absent_after"] is not True
        ):
            raise ValidationImageError("cleanup record removal is invalid")
    if not isinstance(capacity, dict) or set(capacity) != {
        "host_free_bytes",
        "docker_free_kib",
        "host_min_free_bytes",
        "docker_min_free_kib",
        "meets_minimum",
    }:
        raise ValidationImageError("cleanup record capacity is invalid")
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
            raise ValidationImageError("cleanup record capacity value is invalid")
    if not isinstance(capacity["meets_minimum"], bool) or not isinstance(record["error"], str):
        raise ValidationImageError("cleanup record result values are invalid")
    if record["status"] == "PASS":
        checks = prechecks["target_identity_checks"]
        if (
            record["error"]
            or not capacity["meets_minimum"]
            or len(removals) != len(targets)
            or len(checks) != len(targets)
            or any(not check["matches"] for check in checks)
            or prechecks["container_conflicts"]
        ):
            raise ValidationImageError("cleanup PASS record is internally inconsistent")
    elif not record["error"]:
        raise ValidationImageError("cleanup FAIL record has no error")


def _write_record(path: Path, record: dict[str, object]) -> None:
    _validate_record(record)
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
    policy, allowlist_digest = _load_allowlist(allowlist_path)
    targets = policy["targets"]
    if not isinstance(targets, list):
        raise ValidationImageError("cleanup targets were not normalized")
    removals: list[dict[str, str]] = []
    target_checks: list[dict[str, object]] = []
    container_conflicts: list[dict[str, str]] = []
    containers_checked = 0
    error = ""
    host_free = 0
    docker_free = 0
    try:
        for target in targets:
            if not isinstance(target, dict):
                raise ValidationImageError("cleanup target was not normalized")
            observed = inspect_image_id(target["tag"], command=command)
            matches = observed == target["image_id"]
            target_checks.append(
                {
                    "tag": target["tag"],
                    "expected_image_id": target["image_id"],
                    "observed_image_id": observed,
                    "matches": matches,
                }
            )
            if not matches:
                raise ValidationImageError("cleanup target identity drifted")
        target_ids = {target["image_id"] for target in targets}
        target_tags = {target["tag"] for target in targets}
        containers = _container_images(command=command)
        containers_checked = len(containers)
        for container in containers:
            if container["image_id"] in target_ids or container["configured_image"] in target_tags:
                container_conflicts.append(container)
        if container_conflicts:
            raise ValidationImageError("cleanup target is referenced by a container")
        for target in targets:
            remove_owned_image(target["tag"], target["image_id"], command=command)
            removals.append(
                {
                    "tag": target["tag"],
                    "image_id": target["image_id"],
                    "status": "removed",
                    "absent_after": True,
                }
            )
        host_free = _host_free_bytes()
        docker_free = _docker_free_kib(str(policy["probe_image_id"]), command=command)
        if (
            host_free < int(policy["host_min_free_bytes"])
            or docker_free < int(policy["docker_min_free_kib"])
        ):
            raise ValidationImageError("post-cleanup capacity is below the redispatch minimum")
    except Exception as exc:
        error = str(exc) or type(exc).__name__
    meets_minimum = (
        host_free >= int(policy["host_min_free_bytes"])
        and docker_free >= int(policy["docker_min_free_kib"])
    )
    record: dict[str, object] = {
        "schema_version": 1,
        "status": "FAIL" if error else "PASS",
        "allowlist_sha256": allowlist_digest,
        "targets": targets,
        "prechecks": {
            "target_identity_checks": target_checks,
            "containers_checked": containers_checked,
            "container_conflicts": container_conflicts,
        },
        "removals": removals,
        "capacity": {
            "host_free_bytes": host_free,
            "docker_free_kib": docker_free,
            "host_min_free_bytes": int(policy["host_min_free_bytes"]),
            "docker_min_free_kib": int(policy["docker_min_free_kib"]),
            "meets_minimum": meets_minimum,
        },
        "error": error,
    }
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
        print(f"validation image cleanup failed: {exc}")
        return 1
    print(args.output)
    return 0 if record["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
