"""Executable acceptance cases for disposable validation-image ownership."""

from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

from ops.release_control import validation_image_cleanup as lifecycle


ROOT = Path(__file__).parents[2]
ALLOWLIST = ROOT / "ops" / "release_control" / "validation-image-cleanup-20260915.json"
OTHER_ID = "sha256:" + "f" * 64
LOCAL_TAG = "odin-candidate:local-developer-check"
PARENT_ID = "sha256:" + "1" * 64
FINAL_ID = "sha256:" + "2" * 64
FINAL_PARENT_ID = "sha256:" + "3" * 64
FRONTEND_ID = "sha256:" + "4" * 64
FRONTEND_PARENT_ID = "sha256:" + "5" * 64
UNRELATED_ID = "sha256:" + "6" * 64
PULLED_BASE_ID = "sha256:" + "7" * 64
PULLED_PARENT_ID = "sha256:" + "8" * 64
OWNER_TOKEN = "a" * 64


class DockerSimulation:
    def __init__(self, images: dict[str, str]) -> None:
        self.images = dict(images)
        self.containers: list[dict[str, str]] = []
        self.calls: list[list[str]] = []
        self.df_output = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "overlay 40000000 30000000 10000000 75% /\n"
        )
        self.fail_remove: set[str] = set()

    def __call__(
        self, args: list[str], *, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del capture
        self.calls.append(list(args))
        returncode = 0
        output = ""
        if args[:3] == ["docker", "image", "inspect"]:
            tag = args[-1]
            if tag in self.images:
                output = self.images[tag] + "\n"
            else:
                returncode = 1
                output = f"Error response from daemon: No such image: {tag}\n"
        elif args[:3] == ["docker", "image", "rm"]:
            tag = args[-1]
            if tag in self.fail_remove:
                returncode = 1
                output = "removal refused\n"
            elif tag not in self.images:
                returncode = 1
                output = f"Error response from daemon: No such image: {tag}\n"
            else:
                del self.images[tag]
                output = f"Untagged: {tag}\n"
        elif args[:3] == ["docker", "ps", "-a"]:
            output = "".join(container["id"] + "\n" for container in self.containers)
        elif args[:2] == ["docker", "inspect"]:
            container = next(item for item in self.containers if item["id"] == args[-1])
            output = f'{container["image_id"]}\t{container["configured_image"]}\n'
        elif args[:2] == ["docker", "run"]:
            output = self.df_output
        else:
            raise AssertionError(f"unexpected command: {args}")
        result = subprocess.CompletedProcess(args, returncode, output)
        if check and returncode:
            raise lifecycle.ValidationImageError(f"simulated command failure: {args[:3]}")
        return result


class HistoryDockerSimulation:
    def __init__(
        self,
        visible: list[str],
        histories: dict[str, list[str]],
    ) -> None:
        self.visible = list(visible)
        self.histories = {key: list(value) for key, value in histories.items()}
        self.metadata = {
            image_id: {
                "Id": image_id,
                "RepoTags": [],
                "RepoDigests": [],
                "Config": {"Labels": {}},
            }
            for image_id in visible
        }
        self.containers: list[dict[str, str]] = []
        self.calls: list[list[str]] = []
        self.df_output = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "overlay 40000000 30000000 10000000 75% /\n"
        )

    def __call__(
        self, args: list[str], *, capture: bool = True, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        del capture
        self.calls.append(list(args))
        returncode = 0
        output = ""
        if args == ["docker", "image", "ls", "-a", "--no-trunc", "--quiet"]:
            output = "".join(f"{image_id}\n" for image_id in self.visible)
        elif args[:3] == ["docker", "history", "--no-trunc"]:
            image_id = args[-1]
            if image_id not in self.histories:
                returncode = 1
                output = "history unavailable\n"
            else:
                output = "".join(f"{value}\n" for value in self.histories[image_id])
        elif args[:3] == ["docker", "image", "inspect"]:
            image_id = args[-1]
            metadata = self.metadata.get(image_id)
            if metadata is None:
                returncode = 1
                output = f"Error response from daemon: No such image: {image_id}\n"
            else:
                output = json.dumps([metadata]) + "\n"
        elif args[:3] == ["docker", "image", "rm"]:
            image_id = args[-1]
            if image_id not in self.metadata:
                returncode = 1
                output = f"Error response from daemon: No such image: {image_id}\n"
            else:
                del self.metadata[image_id]
                self.visible = [value for value in self.visible if value != image_id]
                output = f"Deleted: {image_id}\n"
        elif args[:3] == ["docker", "ps", "-a"]:
            output = "".join(f"{item['id']}\n" for item in self.containers)
        elif args[:2] == ["docker", "inspect"]:
            container = next(item for item in self.containers if item["id"] == args[-1])
            output = f"{container['image_id']}\t{container['configured_image']}\n"
        elif args[:2] == ["docker", "run"]:
            output = self.df_output
        else:
            raise AssertionError(f"unexpected command: {args}")
        result = subprocess.CompletedProcess(args, returncode, output)
        if check and returncode:
            raise lifecycle.ValidationImageError(f"simulated command failure: {args[:3]}")
        return result


def _policy_images() -> dict[str, str]:
    payload = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    return {target["tag"]: target["image_id"] for target in payload["targets"]}


def test_DI01_preflight_rejects_an_existing_exact_tag() -> None:
    tag = "odin-candidate:candidate-unit"
    docker = DockerSimulation({tag: OTHER_ID})
    with pytest.raises(lifecycle.ValidationImageError, match="already exists"):
        lifecycle.assert_image_absent(tag, command=docker)
    assert not any(call[:3] == ["docker", "image", "rm"] for call in docker.calls)


def test_DI02_build_iid_must_be_full_and_match_the_tag(tmp_path: Path) -> None:
    tag = "odin-dbparity:parity-unit"
    iidfile = tmp_path / "build.iid"
    iidfile.write_text(OTHER_ID + "\n", encoding="ascii")
    docker = DockerSimulation({tag: OTHER_ID})
    assert lifecycle.verify_built_image(tag, iidfile, command=docker) == OTHER_ID

    iidfile.write_text("sha256:short\n", encoding="ascii")
    with pytest.raises(lifecycle.ValidationImageError, match="identity is invalid"):
        lifecycle.verify_built_image(tag, iidfile, command=docker)

    iidfile.write_text(OTHER_ID + "\n", encoding="ascii")
    docker.images[tag] = "sha256:" + "e" * 64
    with pytest.raises(lifecycle.ValidationImageError, match="does not match"):
        lifecycle.verify_built_image(tag, iidfile, command=docker)


def test_DI03_matching_owned_tag_is_removed_without_force_and_verified_absent() -> None:
    tag = "odin-candidate:candidate-unit"
    docker = DockerSimulation({tag: OTHER_ID})
    lifecycle.remove_owned_image(tag, OTHER_ID, command=docker)
    assert ["docker", "image", "rm", tag] in docker.calls
    assert not any("--force" in call or "-f" in call for call in docker.calls)
    assert tag not in docker.images
    assert docker.calls[-1][:3] == ["docker", "image", "inspect"]


@pytest.mark.parametrize("mode", ["missing", "mismatch"])
def test_DI04_DI05_missing_or_mismatched_owned_tag_is_not_removed(mode: str) -> None:
    tag = "odin-candidate:candidate-unit"
    images = {} if mode == "missing" else {tag: "sha256:" + "e" * 64}
    docker = DockerSimulation(images)
    errors = lifecycle.cleanup_owned_image(tag, OTHER_ID, command=docker)
    assert errors
    assert not any(call[:3] == ["docker", "image", "rm"] for call in docker.calls)


def test_DI06_no_established_iid_means_no_image_command_or_deletion() -> None:
    docker = DockerSimulation({LOCAL_TAG: OTHER_ID})
    assert lifecycle.cleanup_owned_image(LOCAL_TAG, None, command=docker) == []
    assert docker.calls == []
    assert docker.images[LOCAL_TAG] == OTHER_ID


def test_DI07_remove_failure_is_a_cleanup_failure() -> None:
    tag = "odin-dbparity:parity-unit"
    docker = DockerSimulation({tag: OTHER_ID})
    docker.fail_remove.add(tag)
    errors = lifecycle.cleanup_owned_image(tag, OTHER_ID, command=docker)
    assert errors == ["simulated command failure: ['docker', 'image', 'rm']"]
    assert docker.images[tag] == OTHER_ID


def test_DI13_protection_includes_visible_histories_and_ignores_only_missing() -> None:
    docker = HistoryDockerSimulation(
        [OTHER_ID], {OTHER_ID: [OTHER_ID, "<missing>", PARENT_ID]}
    )

    snapshot = lifecycle.capture_protected_image_history(command=docker)

    assert snapshot.visible_ids == (OTHER_ID,)
    assert set(snapshot.protected_ids) == {OTHER_ID, PARENT_ID}
    assert lifecycle.protected_image_history_evidence(snapshot) == {
        "count": 2,
        "sha256": lifecycle._identity_digest({OTHER_ID, PARENT_ID}),
    }


def test_DI13_malformed_non_missing_history_entry_fails() -> None:
    docker = HistoryDockerSimulation([OTHER_ID], {OTHER_ID: [OTHER_ID, "missing"]})
    with pytest.raises(lifecycle.ValidationImageError, match="identity is invalid"):
        lifecycle.capture_protected_image_history(command=docker)


def test_DI13_post_attempt_capture_owns_every_new_visible_history_top_down() -> None:
    docker = HistoryDockerSimulation([OTHER_ID], {OTHER_ID: [OTHER_ID, PARENT_ID]})
    protected = lifecycle.capture_protected_image_history(command=docker)
    docker.visible = [
        OTHER_ID,
        FINAL_ID,
        FRONTEND_ID,
        UNRELATED_ID,
        PULLED_BASE_ID,
    ]
    docker.histories.update(
        {
            FINAL_ID: [FINAL_ID, FINAL_PARENT_ID, PULLED_BASE_ID, PULLED_PARENT_ID],
            FRONTEND_ID: [FRONTEND_ID, "<missing>", FRONTEND_PARENT_ID],
            UNRELATED_ID: [UNRELATED_ID],
            PULLED_BASE_ID: [PULLED_BASE_ID, PULLED_PARENT_ID],
        }
    )
    for image_id in (
        FINAL_ID,
        FINAL_PARENT_ID,
        FRONTEND_ID,
        FRONTEND_PARENT_ID,
        UNRELATED_ID,
        PULLED_BASE_ID,
        PULLED_PARENT_ID,
    ):
        docker.metadata[image_id] = {
            "Id": image_id,
            "RepoTags": [],
            "RepoDigests": [],
            "Config": {
                "Labels": (
                    {lifecycle.OWNER_LABEL: OWNER_TOKEN}
                    if image_id in {FINAL_ID, FRONTEND_ID}
                    else {}
                )
            },
        }
    docker.metadata[PULLED_BASE_ID]["RepoTags"] = ["python:3.11-slim"]
    docker.metadata[PULLED_BASE_ID]["RepoDigests"] = [
        "python@sha256:" + "9" * 64
    ]

    roots, owned, unowned = lifecycle.capture_owned_image_history(
        protected,
        final_image_id=FINAL_ID,
        owner_token=OWNER_TOKEN,
        command=docker,
    )

    assert set(roots) == {FINAL_ID, FRONTEND_ID}
    assert set(owned) == {FINAL_PARENT_ID, FRONTEND_ID, FRONTEND_PARENT_ID}
    assert PARENT_ID not in owned
    assert FINAL_ID not in owned
    assert UNRELATED_ID not in roots and UNRELATED_ID not in owned
    assert PULLED_BASE_ID not in roots and PULLED_BASE_ID not in owned
    assert PULLED_PARENT_ID not in owned
    assert set(unowned) == {UNRELATED_ID, PULLED_BASE_ID}
    assert owned.index(FRONTEND_ID) < owned.index(FRONTEND_PARENT_ID)
    assert ["docker", "history", "--no-trunc", "--quiet", FRONTEND_ID] in docker.calls


def test_DI13_lock_contention_rejects_before_a_second_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "lifecycle.lock"
    first = lifecycle.acquire_image_lifecycle_lock(path)
    try:
        with pytest.raises(lifecycle.ValidationImageError, match="lifecycle is active"):
            lifecycle.acquire_image_lifecycle_lock(path)
    finally:
        assert lifecycle.release_image_lifecycle_lock(first) == []
    second = lifecycle.acquire_image_lifecycle_lock(path)
    assert lifecycle.release_image_lifecycle_lock(second) == []


def test_DI15_frontend_and_final_owner_labels_are_terminal_cache_metadata() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    terminal_owner = (
        "ARG ODIN_BUILD_OWNER=unowned\n"
        "LABEL com.runsodin.validation-image-owner=${ODIN_BUILD_OWNER}"
    )
    assert dockerfile.count(terminal_owner) == 2
    assert dockerfile.count("ARG ODIN_BUILD_OWNER") == 2
    assert not dockerfile.startswith("ARG ODIN_BUILD_OWNER")
    assert "\nARG ODIN_BUILD_OWNER=unowned\n\nFROM " not in dockerfile
    assert f"RUN npm run build\n{terminal_owner}\n\n# ── Final image" in dockerfile
    assert dockerfile.endswith(terminal_owner + "\n")

    instructions = [
        line.strip()
        for line in dockerfile.splitlines()
        if line and not line.lstrip().startswith("#")
    ]
    token_positions = [
        index
        for index, value in enumerate(instructions)
        if value == "ARG ODIN_BUILD_OWNER=unowned"
    ]
    assert len(token_positions) == 2
    assert all(
        instructions[index + 1]
        == "LABEL com.runsodin.validation-image-owner=${ODIN_BUILD_OWNER}"
        for index in token_positions
    )
    # Simulate token-sensitive cache keys: only the terminal metadata pair
    # consumes the build arg, so every earlier instruction remains identical.
    for token in ("a" * 64, "b" * 64):
        rendered = [
            value.replace("${ODIN_BUILD_OWNER}", token)
            if index - 1 in token_positions
            else value
            for index, value in enumerate(instructions)
        ]
        if token.startswith("a"):
            first = rendered
        else:
            assert [
                index for index, (left, right) in enumerate(zip(first, rendered))
                if left != right
            ] == [position + 1 for position in token_positions]


def test_DI16_validation_capacity_preflight_is_common_and_fail_closed() -> None:
    docker = HistoryDockerSimulation([], {})
    docker.df_output = (
        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
        "overlay 40000000 35000000 5000000 88% /\n"
    )
    lifecycle_run = lifecycle.DisposableImageLifecycle(
        "odin-candidate:candidate-low-capacity", command=docker
    )

    with pytest.raises(lifecycle.ValidationImageError, match="below 6291456 KiB"):
        lifecycle_run.begin()

    assert lifecycle_run.lock is None
    assert lifecycle_run.capacity == {
        "probe_image_id": lifecycle.PROBE_IMAGE_ID,
        "command_argv": list(lifecycle.VALIDATION_CAPACITY_COMMAND),
        "required_free_kib": 6291456,
        "observed_free_kib": 5000000,
        "passed": False,
        "observed_at": lifecycle_run.capacity["observed_at"],
    }
    assert docker.calls == [list(lifecycle.VALIDATION_CAPACITY_COMMAND)]


def test_DI16_all_four_validation_builders_use_the_shared_lifecycle() -> None:
    sources = {
        "installer": ROOT / "ops/release_control/installer_smoke.py",
        "candidate": ROOT / "ops/release_gate/runner.py",
        "database": ROOT / "ops/database_parity/runner.py",
        "edu": ROOT / "ops/edu_sandbox/runtime.py",
    }
    for path in sources.values():
        source = path.read_text(encoding="utf-8")
        assert "DisposableImageLifecycle(" in source
        assert ".begin()" in source


def test_DI13_all_gate_runners_freeze_before_later_work() -> None:
    installer = (ROOT / "ops/release_control/installer_smoke.py").read_text()
    candidate = (ROOT / "ops/release_gate/runner.py").read_text()
    database = (ROOT / "ops/database_parity/runner.py").read_text()
    assert installer.index("image_lifecycle.freeze_ownership()") < installer.index(
        "env = os.environ.copy()"
    )
    assert candidate.index("image_lifecycle.freeze_ownership()") < candidate.index(
        'active_phase = "candidate-boot"'
    )
    assert database.index("image_lifecycle.freeze_ownership()") < database.index(
        "client_probe = _command("
    )


def test_DI14_cleanup_uses_only_fixed_ids_and_accepts_already_absent() -> None:
    docker = HistoryDockerSimulation(
        [FRONTEND_ID, FRONTEND_PARENT_ID],
        {
            FRONTEND_ID: [FRONTEND_ID, FRONTEND_PARENT_ID],
            FRONTEND_PARENT_ID: [FRONTEND_PARENT_ID],
        },
    )
    del docker.metadata[FRONTEND_PARENT_ID]

    records, errors = lifecycle.cleanup_owned_image_history(
        (FRONTEND_ID, FRONTEND_PARENT_ID), command=docker
    )

    assert errors == []
    assert [record["status"] for record in records] == ["REMOVED", "ALREADY_ABSENT"]
    assert ["docker", "image", "rm", FRONTEND_ID] in docker.calls
    assert not any(call[:3] == ["docker", "history", "--no-trunc"] for call in docker.calls)
    assert not any(call[:3] == ["docker", "image", "ls"] for call in docker.calls)


@pytest.mark.parametrize("drift", ["tag", "digest", "container"])
def test_DI14_tag_digest_or_container_drift_refuses_history_deletion(drift: str) -> None:
    docker = HistoryDockerSimulation([FRONTEND_ID], {FRONTEND_ID: [FRONTEND_ID]})
    if drift == "tag":
        docker.metadata[FRONTEND_ID]["RepoTags"] = ["shared:latest"]
    elif drift == "digest":
        docker.metadata[FRONTEND_ID]["RepoDigests"] = ["shared@sha256:" + "a" * 64]
    else:
        docker.containers.append(
            {
                "id": "a" * 64,
                "image_id": FRONTEND_ID,
                "configured_image": "shared:latest",
            }
        )

    records, errors = lifecycle.cleanup_owned_image_history(
        (FRONTEND_ID,), command=docker
    )

    assert errors
    assert records[0]["removed"] is False
    assert ["docker", "image", "rm", FRONTEND_ID] not in docker.calls


def test_DI14_failed_build_path_cleans_only_fixed_new_untagged_history() -> None:
    docker = HistoryDockerSimulation([OTHER_ID], {OTHER_ID: [OTHER_ID, PARENT_ID]})
    lifecycle_run = lifecycle.DisposableImageLifecycle(
        "odin-candidate:candidate-failed-build", command=docker
    )
    lifecycle_run.begin()
    lifecycle_run.mark_build_attempted()
    docker.visible.extend([FRONTEND_ID, FRONTEND_PARENT_ID])
    docker.histories.update(
        {
            FRONTEND_ID: [FRONTEND_ID, FRONTEND_PARENT_ID, PARENT_ID],
            FRONTEND_PARENT_ID: [FRONTEND_PARENT_ID, PARENT_ID],
        }
    )
    for image_id in (FRONTEND_ID, FRONTEND_PARENT_ID):
        docker.metadata[image_id] = {
            "Id": image_id,
            "RepoTags": [],
            "RepoDigests": [],
            "Config": {"Labels": {lifecycle.OWNER_LABEL: lifecycle_run.owner_token}},
        }

    lifecycle_run.freeze_ownership()
    errors = lifecycle_run.finalize()

    assert errors == []
    assert lifecycle_run.ownership_capture_succeeded is True
    assert lifecycle_run.owned_history_ids == (FRONTEND_ID, FRONTEND_PARENT_ID)
    assert [record["status"] for record in lifecycle_run.history_cleanup or []] == [
        "REMOVED",
        "REMOVED",
    ]
    assert OTHER_ID in docker.metadata
    assert PARENT_ID not in lifecycle_run.owned_history_ids


def test_DI14_failed_ownership_capture_issues_no_image_deletion() -> None:
    docker = HistoryDockerSimulation([], {})
    lifecycle_run = lifecycle.DisposableImageLifecycle(
        "odin-dbparity:parity-failed-capture", command=docker
    )
    lifecycle_run.begin()
    lifecycle_run.mark_build_attempted()
    docker.visible.append(FRONTEND_ID)
    docker.metadata[FRONTEND_ID] = {
        "Id": FRONTEND_ID,
        "RepoTags": [],
        "RepoDigests": [],
        "Config": {"Labels": {lifecycle.OWNER_LABEL: lifecycle_run.owner_token}},
    }
    docker.histories[FRONTEND_ID] = [FRONTEND_ID, "malformed"]

    with pytest.raises(lifecycle.ValidationImageError, match="identity is invalid"):
        lifecycle_run.freeze_ownership()
    errors = lifecycle_run.finalize()

    assert errors and "capture failed" in errors[0]
    assert lifecycle_run.ownership_capture_succeeded is False
    assert FRONTEND_ID in docker.metadata
    assert not any(call[:3] == ["docker", "image", "rm"] for call in docker.calls)


def test_DI18_finalize_tag_identity_drift_stops_frozen_history_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_run = lifecycle.DisposableImageLifecycle(
        "odin-edu-candidate:school-one"
    )
    lifecycle_run.final_image_id = FINAL_ID
    lifecycle_run.build_attempted = True
    lifecycle_run.ownership_capture_attempted = True
    lifecycle_run.ownership_capture_succeeded = True
    lifecycle_run.owned_history_ids = (FRONTEND_PARENT_ID,)
    history_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        lifecycle,
        "cleanup_owned_image",
        lambda *_args, **_kwargs: ["owned validation image identity changed"],
    )
    monkeypatch.setattr(
        lifecycle,
        "cleanup_owned_image_history",
        lambda ids, **_kwargs: history_calls.append(ids) or ([], []),
    )

    errors = lifecycle_run.finalize()

    assert errors == ["image tag: owned validation image identity changed"]
    assert history_calls == []
    assert lifecycle_run.history_cleanup == []


def test_DI18_finalize_already_absent_verified_tag_still_cleans_fixed_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_run = lifecycle.DisposableImageLifecycle(
        "odin-edu-candidate:school-one"
    )
    lifecycle_run.final_image_id = FINAL_ID
    lifecycle_run.build_attempted = True
    lifecycle_run.ownership_capture_attempted = True
    lifecycle_run.ownership_capture_succeeded = True
    lifecycle_run.owned_history_ids = (FRONTEND_PARENT_ID,)
    monkeypatch.setattr(
        lifecycle,
        "cleanup_owned_image",
        lambda *_args, **_kwargs: ["owned validation image tag is missing"],
    )
    monkeypatch.setattr(
        lifecycle,
        "cleanup_owned_image_history",
        lambda ids, **_kwargs: (
            [{"image_id": ids[0], "status": "ALREADY_ABSENT"}],
            [],
        ),
    )

    errors = lifecycle_run.finalize()

    assert errors == ["image tag: owned validation image tag is missing"]
    assert lifecycle_run.history_cleanup == [
        {"image_id": FRONTEND_PARENT_ID, "status": "ALREADY_ABSENT"}
    ]


def test_DI09_exact_allowlist_cleanup_writes_bounded_pass_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = _policy_images()
    images[LOCAL_TAG] = OTHER_ID
    docker = DockerSimulation(images)
    allowlist_bytes = ALLOWLIST.read_bytes()
    read_count = 0
    original_read_bytes = Path.read_bytes

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal read_count
        read_count += 1
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    monkeypatch.setattr(lifecycle, "_host_free_bytes", lambda: 8 * 1024**3)
    output = tmp_path / "cleanup.json"

    record = lifecycle.execute_cleanup(ALLOWLIST, output, command=docker)

    assert record["status"] == "PASS"
    assert read_count == 1
    assert record["allowlist_sha256"] == hashlib.sha256(allowlist_bytes).hexdigest()
    assert record["capacity"]["meets_minimum"] is True
    assert len(record["prechecks"]["target_identity_checks"]) == 5
    assert len(record["removals"]) == 5
    assert all(removal["absent_after"] is True for removal in record["removals"])
    assert docker.images == {LOCAL_TAG: OTHER_ID}
    assert json.loads(output.read_text(encoding="utf-8")) == record
    probe_calls = [call for call in docker.calls if call[:2] == ["docker", "run"]]
    assert probe_calls == [[
        "docker", "run", "--pull=never", "--rm", "--network", "none",
        "--read-only", "--entrypoint", "/bin/df",
        "sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534",
        "-Pk", "/",
    ]]


@pytest.mark.parametrize("mode", ["missing-tag", "id-drift", "container-conflict"])
def test_DI09_identity_or_container_drift_fails_before_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    images = _policy_images()
    first_tag, first_id = next(iter(images.items()))
    docker = DockerSimulation(images)
    if mode == "missing-tag":
        del docker.images[first_tag]
    elif mode == "id-drift":
        docker.images[first_tag] = OTHER_ID
    else:
        docker.containers.append(
            {
                "id": "a" * 64,
                "image_id": first_id,
                "configured_image": "unrelated:tag",
            }
        )
    monkeypatch.setattr(lifecycle, "_host_free_bytes", lambda: 8 * 1024**3)

    record = lifecycle.execute_cleanup(ALLOWLIST, tmp_path / "failure.json", command=docker)

    assert record["status"] == "FAIL"
    assert record["error"]
    assert not any(call[:3] == ["docker", "image", "rm"] for call in docker.calls)


def test_DI09_reused_output_rejects_before_inspection_or_deletion(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("do not replace", encoding="utf-8")
    docker = DockerSimulation(_policy_images())
    with pytest.raises(lifecycle.ValidationImageError, match="already exists"):
        lifecycle.execute_cleanup(ALLOWLIST, output, command=docker)
    assert docker.calls == []
    assert output.read_text(encoding="utf-8") == "do not replace"


def test_DI09_allowlist_is_literal_and_not_a_general_cleanup_interface(tmp_path: Path) -> None:
    payload = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    payload["targets"][0]["tag"] = "odin-candidate:candidate-gha-99999999999-1"
    altered = tmp_path / "altered.json"
    altered.write_text(json.dumps(payload), encoding="utf-8")
    docker = DockerSimulation(_policy_images())

    with pytest.raises(lifecycle.ValidationImageError, match="inventory is invalid"):
        lifecycle.execute_cleanup(altered, tmp_path / "result.json", command=docker)

    assert docker.calls == []


@pytest.mark.parametrize("mode", ["malformed-df", "bad-header", "low-docker", "low-host"])
def test_DI09_malformed_or_low_capacity_is_recorded_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    docker = DockerSimulation(_policy_images())
    if mode == "malformed-df":
        docker.df_output = "not POSIX df output\n"
    elif mode == "bad-header":
        docker.df_output = (
            "arbitrary untrusted capacity header\n"
            "overlay 40000000 30000000 10000000 75% /\n"
        )
    elif mode == "low-docker":
        docker.df_output = (
            "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
            "overlay 40000000 39999999 1 99% /\n"
        )
    monkeypatch.setattr(
        lifecycle,
        "_host_free_bytes",
        lambda: 1 if mode == "low-host" else 8 * 1024**3,
    )

    record = lifecycle.execute_cleanup(ALLOWLIST, tmp_path / f"{mode}.json", command=docker)

    assert record["status"] == "FAIL"
    assert record["error"]
    assert record["capacity"]["meets_minimum"] is False
