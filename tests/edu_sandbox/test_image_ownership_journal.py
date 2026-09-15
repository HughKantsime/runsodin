"""DI18-DI20 contracts for bounded EDU validation-image ownership."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ops.edu_sandbox import runtime as runtime_module
from ops.edu_sandbox.runtime import SandboxRuntime, _journal_digest, _load_image_journal
from ops.edu_sandbox.runtime import ResourceSet
from ops.edu_sandbox.state import LifecycleState, Phase, save_state
from ops.edu_sandbox.executor import CommandResult
from ops.release_control.validation_image_cleanup import (
    DisposableImageLifecycle,
    OWNER_LABEL as IMAGE_OWNER_LABEL,
    ProtectedImageHistory,
)
from ops.release_control import validation_image_cleanup as image_lifecycle_module


IMAGE_ID = "sha256:" + "a" * 64
ROOT_ID = "sha256:" + "b" * 64
HISTORY_ID = "sha256:" + "c" * 64
PROTECTED_ID = "sha256:" + "d" * 64


def _history_record(image_id: str, status: str) -> dict[str, object]:
    if status == "REMOVED":
        return {
            "image_id": image_id,
            "status": status,
            "observed_image_id": image_id,
            "repo_tags": [],
            "repo_digests": [],
            "container_conflicts": [],
            "removed": True,
            "absent_after": True,
            "error": None,
        }
    return {
        "image_id": image_id,
        "status": "ALREADY_ABSENT",
        "observed_image_id": None,
        "repo_tags": None,
        "repo_digests": None,
        "container_conflicts": [],
        "removed": False,
        "absent_after": True,
        "error": None,
    }


def _frozen_lifecycle(runtime: SandboxRuntime, tag: str) -> DisposableImageLifecycle:
    lifecycle = DisposableImageLifecycle(tag, command=runtime._image_command)
    lifecycle.capacity = {
        "probe_image_id": runtime_module.PROBE_IMAGE_ID,
        "command_argv": list(runtime_module.VALIDATION_CAPACITY_COMMAND),
        "required_free_kib": runtime_module.VALIDATION_DOCKER_MIN_FREE_KIB,
        "observed_free_kib": 7_000_000,
        "passed": True,
        "observed_at": "2026-09-15T00:00:00Z",
    }
    lifecycle.protected = ProtectedImageHistory(
        visible_ids=(PROTECTED_ID,),
        protected_ids=(PROTECTED_ID,),
        protected_sha256=runtime_module.hashlib.sha256(
            f"{PROTECTED_ID}\n".encode("ascii")
        ).hexdigest(),
    )
    lifecycle.build_attempted = True
    lifecycle.final_image_id = IMAGE_ID
    lifecycle.ownership_capture_attempted = True
    lifecycle.ownership_capture_succeeded = True
    lifecycle.owned_root_ids = (ROOT_ID,)
    lifecycle.owned_history_ids = (HISTORY_ID,)
    lifecycle.unowned_new_ids = ()
    return lifecycle


def test_DI19_private_pending_and_succeeded_journals_define_one_durable_lease(
    tmp_path: Path,
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    lifecycle = _frozen_lifecycle(runtime, "odin-edu-candidate:school-one")
    runtime._create_private_iid(paths)

    pending = runtime._pending_image_journal(lifecycle, paths)
    assert pending["capture_status"] == "pending"
    assert pending["owner_token"] == lifecycle.owner_token
    assert paths.image_journal.stat().st_mode & 0o777 == 0o600
    assert paths.image_iid.stat().st_mode & 0o777 == 0o600

    succeeded = runtime._succeeded_image_journal(lifecycle, paths, pending)
    persisted = _load_image_journal(paths.image_journal)
    assert succeeded == persisted
    assert persisted["capture_status"] == "succeeded"
    assert persisted["owner_token"] is None
    assert persisted["final_image_id"] == IMAGE_ID
    assert persisted["owned_history_ids"] == [HISTORY_ID]
    assert lifecycle.owner_token not in paths.image_journal.read_text(encoding="utf-8")


def test_DI18_null_final_identity_never_authorizes_tag_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    lifecycle = _frozen_lifecycle(runtime, "odin-edu-candidate:school-one")
    pending = runtime._pending_image_journal(lifecycle, paths)
    lifecycle.final_image_id = None
    succeeded = runtime._succeeded_image_journal(lifecycle, paths, pending)
    calls: list[object] = []

    monkeypatch.setattr(
        runtime_module, "acquire_image_lifecycle_lock", lambda: object()
    )
    monkeypatch.setattr(
        runtime_module,
        "release_image_lifecycle_lock",
        lambda lock: calls.append(("release", lock)) or [],
    )
    monkeypatch.setattr(
        runtime_module,
        "inspect_image_id",
        lambda *_args, **_kwargs: pytest.fail("tag inspection is unauthorized"),
    )
    monkeypatch.setattr(
        runtime_module,
        "remove_owned_image",
        lambda *_args, **_kwargs: pytest.fail("tag deletion is unauthorized"),
    )

    def clean_history(ids, **_kwargs):
        calls.append(("history", ids))
        return ([_history_record(ids[0], "ALREADY_ABSENT")], [])

    monkeypatch.setattr(runtime_module, "cleanup_owned_image_history", clean_history)
    monkeypatch.setattr(
        runtime_module, "verify_owned_image_history_absent", lambda *_a, **_k: []
    )
    journal, errors = runtime._cleanup_fixed_image_journal(paths, succeeded)

    assert errors == []
    assert journal["cleanup"]["tag"] == "unauthorized"
    assert ("history", (HISTORY_ID,)) in calls


def test_DI20_fixed_cleanup_resumes_from_progress_without_rediscovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    lifecycle = _frozen_lifecycle(runtime, "odin-edu-candidate:school-one")
    pending = runtime._pending_image_journal(lifecycle, paths)
    succeeded = runtime._succeeded_image_journal(lifecycle, paths, pending)
    lease_digest = _journal_digest(paths.image_journal)
    calls: list[object] = []

    monkeypatch.setattr(
        runtime_module, "acquire_image_lifecycle_lock", lambda: object()
    )
    monkeypatch.setattr(
        runtime_module, "release_image_lifecycle_lock", lambda _lock: []
    )
    monkeypatch.setattr(
        runtime_module,
        "inspect_image_id",
        lambda tag, **_kwargs: calls.append(("inspect-tag", tag)) or IMAGE_ID,
    )
    monkeypatch.setattr(
        runtime_module,
        "remove_owned_image",
        lambda tag, image_id, **_kwargs: calls.append(("remove-tag", tag, image_id)),
    )

    def clean_history(ids, **_kwargs):
        calls.append(("history", ids))
        return ([_history_record(ids[0], "REMOVED")], [])

    monkeypatch.setattr(runtime_module, "cleanup_owned_image_history", clean_history)
    monkeypatch.setattr(
        runtime_module, "verify_owned_image_history_absent", lambda *_a, **_k: []
    )
    first, errors = runtime._cleanup_fixed_image_journal(paths, succeeded)
    assert errors == []
    assert _journal_digest(paths.image_journal) == lease_digest
    assert first["cleanup"]["tag"] == "removed"
    assert calls == [
        ("inspect-tag", "odin-edu-candidate:school-one"),
        ("remove-tag", "odin-edu-candidate:school-one", IMAGE_ID),
        ("history", (HISTORY_ID,)),
    ]

    calls.clear()
    resumed = _load_image_journal(paths.image_journal)
    second, errors = runtime._cleanup_fixed_image_journal(paths, resumed)
    assert errors == []
    assert second["cleanup"] == first["cleanup"]
    assert calls == []
    assert not any(
        word in json.dumps(second) for word in ("docker image ls", "docker history")
    )


def test_DI20_tag_identity_drift_refuses_tag_and_history_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    lifecycle = _frozen_lifecycle(runtime, "odin-edu-candidate:school-one")
    pending = runtime._pending_image_journal(lifecycle, paths)
    succeeded = runtime._succeeded_image_journal(lifecycle, paths, pending)
    calls: list[object] = []

    monkeypatch.setattr(
        runtime_module, "acquire_image_lifecycle_lock", lambda: object()
    )
    monkeypatch.setattr(
        runtime_module, "release_image_lifecycle_lock", lambda _lock: []
    )
    monkeypatch.setattr(
        runtime_module,
        "inspect_image_id",
        lambda *_args, **_kwargs: "sha256:" + "e" * 64,
    )
    monkeypatch.setattr(
        runtime_module,
        "remove_owned_image",
        lambda *_args, **_kwargs: calls.append("tag-delete"),
    )
    monkeypatch.setattr(
        runtime_module,
        "cleanup_owned_image_history",
        lambda *_args, **_kwargs: calls.append("history-delete"),
    )
    monkeypatch.setattr(
        runtime_module, "verify_owned_image_history_absent", lambda *_a, **_k: []
    )

    journal, errors = runtime._cleanup_fixed_image_journal(paths, succeeded)

    assert errors == ["image tag identity changed; deletion refused"]
    assert journal["cleanup"]["tag"] == "refused"
    assert calls == []


def test_DI19_pending_recovery_captures_once_then_rolls_back_fixed_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARING,
        candidate_tag="odin-edu-candidate:school-one",
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    lifecycle = _frozen_lifecycle(runtime, state.candidate_tag)
    lifecycle.final_image_id = None
    lifecycle.ownership_capture_attempted = False
    lifecycle.ownership_capture_succeeded = False
    lifecycle.owned_root_ids = ()
    lifecycle.owned_history_ids = ()
    pending = runtime._pending_image_journal(lifecycle, paths)
    paths.image_iid.write_text(IMAGE_ID + "\n", encoding="ascii")
    paths.image_iid.chmod(0o600)
    present = True
    calls: list[list[str]] = []

    def image_command(args, **_kwargs):
        nonlocal present
        calls.append(list(args))
        if args == ["docker", "image", "ls", "-a", "--no-trunc", "--quiet"]:
            return subprocess.CompletedProcess(args, 0, IMAGE_ID + "\n" if present else "")
        if args[:3] == ["docker", "image", "inspect"]:
            if not present:
                return subprocess.CompletedProcess(args, 1, "No such image\n")
            if "--format" in args:
                return subprocess.CompletedProcess(args, 0, IMAGE_ID + "\n")
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": IMAGE_ID,
                            "RepoTags": [state.candidate_tag],
                            "RepoDigests": [],
                            "Config": {
                                "Labels": {
                                    IMAGE_OWNER_LABEL: pending["owner_token"]
                                }
                            },
                        }
                    ]
                )
                + "\n",
            )
        if args[:3] == ["docker", "history", "--no-trunc"]:
            return subprocess.CompletedProcess(args, 0, IMAGE_ID + "\n")
        if args[:3] == ["docker", "image", "rm"]:
            present = False
            return subprocess.CompletedProcess(args, 0, "Untagged\n")
        raise AssertionError(f"unexpected image command: {args}")

    monkeypatch.setattr(runtime, "_image_command", image_command)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_cleanup_failed_prepare_resources", lambda *_a: [])
    monkeypatch.setattr(runtime, "_docker_absence_evidence", lambda *_a, **_k: {})

    runtime._recover_interrupted_prepare(paths, resources)

    assert present is False
    assert not paths.directory.exists()
    assert calls.count(["docker", "image", "ls", "-a", "--no-trunc", "--quiet"]) == 1
    assert ["docker", "image", "rm", state.candidate_tag] in calls


def test_DI20_purge_recovers_after_state_unlink_before_journal_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    lifecycle = _frozen_lifecycle(runtime, "odin-edu-candidate:school-one")
    pending = runtime._pending_image_journal(lifecycle, paths)
    succeeded = runtime._succeeded_image_journal(lifecycle, paths, pending)
    succeeded["cleanup"] = {
        "tag": "removed",
        "history": [_history_record(HISTORY_ID, "REMOVED")],
    }
    runtime_module._private_json(paths.image_journal, succeeded)
    digest = _journal_digest(paths.image_journal)
    tombstone_dir = tmp_path / ".tombstones"
    tombstone_dir.mkdir(mode=0o700)
    tombstone = {
        "schema_version": 1,
        "sandbox_id": "school-one",
        "status": "PURGING",
        "source_commit": "a" * 40,
        "purged_at": "",
        "candidate_image_id": IMAGE_ID,
        "candidate_tag": "odin-edu-candidate:school-one",
        "loopback_port": None,
        "installation_id_sha256": "",
        "device_public_key_sha256": "",
        "license_sha256": "",
        "removed": {
            "containers": [],
            "volumes": [],
            "networks": [],
            "image_tags": ["odin-edu-candidate:school-one"],
        },
        "image_ownership_cleanup": succeeded["cleanup"],
        "image_ownership_journal_sha256": digest,
        "residue": [],
        "absence": {},
    }
    runtime_module._public_json(tombstone_dir / "school-one.json", tombstone)
    monkeypatch.setattr(
        runtime_module, "verify_owned_image_history_absent", lambda *_a, **_k: []
    )
    monkeypatch.setattr(runtime, "_docker_absence_evidence", lambda *_a, **_k: {})

    result = runtime.purge("school-one", confirm="school-one")

    assert result["status"] == "PASS"
    assert not paths.directory.exists()
    assert not paths.image_journal.exists()
    assert (tombstone_dir / "school-one.html").is_file()


def test_DI20_legacy_present_tag_refuses_before_any_resource_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    class PresentLegacyImage:
        def run(self, args, **_kwargs):
            calls.append(list(args))
            if args[:3] == ["docker", "image", "inspect"]:
                return CommandResult(tuple(args), 0, IMAGE_ID + "\n", "")
            raise AssertionError(f"mutation reached before legacy preflight: {args}")

    runtime = SandboxRuntime(tmp_path, executor=PresentLegacyImage())
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        candidate_tag="odin-edu-candidate:school-one",
        candidate_image_id=IMAGE_ID,
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(
        runtime,
        "_assert_named_containers_owned",
        lambda *_a, **_k: pytest.fail("resource validation must follow legacy image preflight"),
    )

    with pytest.raises(runtime_module.OwnershipError, match="no bounded deletion authority"):
        runtime.purge("school-one", confirm="school-one")

    assert calls == [
        ["docker", "image", "inspect", "--format", "{{.Id}}", state.candidate_tag]
    ]
    assert runtime_module.load_state(paths.state).phase == Phase.PREPARED


def test_DI20_malformed_or_unverified_cleanup_progress_cannot_skip_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    lifecycle = _frozen_lifecycle(runtime, "odin-edu-candidate:school-one")
    pending = runtime._pending_image_journal(lifecycle, paths)
    succeeded = runtime._succeeded_image_journal(lifecycle, paths, pending)
    succeeded["cleanup"] = {
        "tag": "removed",
        "history": [{"image_id": HISTORY_ID, "status": "REMOVED"}],
    }
    runtime_module._private_json(paths.image_journal, succeeded)
    with pytest.raises(runtime_module.OwnershipError, match="cleanup record"):
        _load_image_journal(paths.image_journal)

    succeeded["cleanup"] = {
        "tag": "removed",
        "history": [_history_record(HISTORY_ID, "REMOVED")],
    }
    runtime_module._private_json(paths.image_journal, succeeded)
    persisted = _load_image_journal(paths.image_journal)
    monkeypatch.setattr(
        runtime_module, "acquire_image_lifecycle_lock", lambda: object()
    )
    monkeypatch.setattr(
        runtime_module, "release_image_lifecycle_lock", lambda _lock: []
    )
    monkeypatch.setattr(
        runtime_module,
        "verify_owned_image_history_absent",
        lambda ids, **_kwargs: [f"{ids[0]}: owned image-history identity remains"],
    )

    _journal, errors = runtime._cleanup_fixed_image_journal(paths, persisted)
    assert errors == [f"{HISTORY_ID}: owned image-history identity remains"]


def test_DI18_pending_recovery_freezes_history_when_iid_authority_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARING,
        candidate_tag="odin-edu-candidate:school-one",
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    lifecycle = _frozen_lifecycle(runtime, state.candidate_tag)
    lifecycle.final_image_id = None
    lifecycle.ownership_capture_attempted = False
    lifecycle.ownership_capture_succeeded = False
    lifecycle.owned_root_ids = ()
    lifecycle.owned_history_ids = ()
    pending = runtime._pending_image_journal(lifecycle, paths)
    paths.image_iid.write_text("invalid-iid\n", encoding="ascii")
    paths.image_iid.chmod(0o600)
    tag_deletions: list[object] = []
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_a, **_k: None)
    monkeypatch.setattr(runtime, "_cleanup_failed_prepare_resources", lambda *_a: [])
    monkeypatch.setattr(
        image_lifecycle_module,
        "capture_owned_image_history",
        lambda *_a, **_k: ((ROOT_ID,), (HISTORY_ID,), ()),
    )
    monkeypatch.setattr(
        image_lifecycle_module,
        "cleanup_owned_image",
        lambda _tag, expected, **_k: (
            tag_deletions.append("tag") or [] if expected is not None else []
        ),
    )
    monkeypatch.setattr(
        image_lifecycle_module,
        "cleanup_owned_image_history",
        lambda ids, **_k: ([_history_record(ids[0], "REMOVED")], []),
    )

    with pytest.raises(runtime_module.OwnershipError, match="history was cleaned"):
        runtime._recover_interrupted_prepare(paths, resources)

    journal = _load_image_journal(paths.image_journal)
    assert journal["capture_status"] == "succeeded"
    assert journal["final_image_id"] is None
    assert journal["owned_history_ids"] == [HISTORY_ID]
    assert journal["cleanup"]["history"][0]["status"] == "REMOVED"
    assert tag_deletions == []
    assert paths.directory.is_dir()
