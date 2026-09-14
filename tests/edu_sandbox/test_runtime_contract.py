from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from ops.edu_sandbox.errors import OwnershipError, SandboxError, SecretInputError, StateError, ValidationError
from ops.edu_sandbox.executor import CommandResult
from ops.edu_sandbox.runtime import (
    BROKER_IMAGE,
    COMPOSE_FILE,
    ResourceSet,
    _lease_deadline,
    _license_deadline,
    _normalized_license,
    SandboxRuntime,
)
from ops.edu_sandbox.state import LifecycleState, Phase, load_state, save_state


def test_resource_names_are_unique_and_bounded():
    first = ResourceSet.from_id("school-one")
    second = ResourceSet.from_id("school-two")
    assert set(first.state_value().values()).isdisjoint(second.state_value().values())
    assert all(name.startswith("odin-edu-school-one") for name in first.state_value().values())


def test_network_isolation_rejects_foreign_edge_member(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    state = LifecycleState("school-one", Phase.PREPARING)
    resources = ResourceSet.from_id("school-one")

    def fake_run(args, **_kwargs):
        if args[:3] != ["docker", "network", "inspect"]:
            raise AssertionError(f"unexpected command before membership rejection: {args}")
        network = args[-1]
        if network == resources.internal_network:
            members = [resources.prepare_container, resources.prepare_proxy]
            internal = True
        else:
            members = [resources.prepare_proxy, "unrelated-edge-container"]
            internal = False
        payload = [{
            "Internal": internal,
            "Containers": {
                str(index): {"Name": name}
                for index, name in enumerate(members)
            },
        }]
        return CommandResult(tuple(args), 0, json.dumps(payload), "")

    monkeypatch.setattr(runtime, "_run", fake_run)
    with pytest.raises(SandboxError, match="only the loopback proxy"):
        runtime._assert_network_isolation(state, preparing=True)


def test_current_prepared_evidence_rejects_foreign_network_member(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        compose_project=resources.project,
        resources=resources.state_value(),
        candidate_image_id="sha256:" + "a" * 64,
        installation_id="stable-installation",
        device_public_key_sha256="b" * 64,
    )
    monkeypatch.setattr(runtime, "_assert_prepared_identity", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_try_run",
        lambda args, **_kwargs: CommandResult(tuple(args), 0, "", ""),
    )

    def fake_run(args, **_kwargs):
        network = args[-1]
        members = [] if network == resources.internal_network else ["foreign-edge-container"]
        payload = [{
            "Internal": network == resources.internal_network,
            "Containers": {
                str(index): {"Name": name}
                for index, name in enumerate(members)
            },
        }]
        return CommandResult(tuple(args), 0, json.dumps(payload), "")

    monkeypatch.setattr(runtime, "_run", fake_run)
    with pytest.raises(SandboxError, match="unexpected live member"):
        runtime._assert_prepared_current_evidence(state)


def test_normalized_license_accepts_raw_and_json_wrapper():
    assert _normalized_license(b"payload.signature\n") == b"payload.signature"
    wrapper = json.dumps({"payload": "payload", "signature": "signature"}).encode()
    assert _normalized_license(wrapper) == b"payload.signature"
    with pytest.raises(SecretInputError):
        _normalized_license(b"not-a-license")


def test_lease_deadline_requires_future_utc_at_call_site():
    value = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert _lease_deadline(value).tzinfo == timezone.utc
    with pytest.raises(ValidationError, match="UTC"):
        _lease_deadline("2030-01-01T00:00:00")
    with pytest.raises(ValidationError, match="ISO-8601"):
        _lease_deadline("tomorrow")


def test_license_deadline_preserves_timestamp_precision():
    exact = _license_deadline("2026-09-12T01:02:03+00:00")
    assert exact == datetime(2026, 9, 12, 1, 2, 3, tzinfo=timezone.utc)
    date_only = _license_deadline("2026-09-12")
    assert date_only.date().isoformat() == "2026-09-12"
    assert date_only.hour == 23 and date_only.minute == 59


def test_compose_is_exact_image_isolated_and_file_secret_only():
    source = COMPOSE_FILE.read_text(encoding="utf-8")
    compose = yaml.safe_load(source)
    assert "container_name" not in source
    assert ":latest" not in source
    assert "../../" not in source
    assert "0.0.0.0" not in source
    assert "docker.sock" not in source
    assert compose["services"]["odin"]["image"].startswith("${ODIN_EDU_CANDIDATE_IMAGE_ID")
    assert compose["services"]["publisher"]["image"] == "${ODIN_EDU_CANDIDATE_IMAGE_ID}"
    assert "ports" not in compose["services"]["odin"]
    assert compose["services"]["odin"]["networks"] == ["sandbox"]
    assert compose["services"]["proxy"]["ports"] == ["127.0.0.1::8080"]
    assert set(compose["services"]["proxy"]["networks"]) == {"sandbox", "edge"}
    assert compose["services"]["proxy"]["read_only"] is True
    assert compose["services"]["proxy"]["cap_drop"] == ["ALL"]
    assert compose["services"]["proxy"]["security_opt"] == ["no-new-privileges:true"]
    assert "volumes" not in compose["services"]["proxy"]
    assert compose["networks"]["sandbox"]["external"] is True
    assert compose["networks"]["edge"]["external"] is True
    assert compose["services"]["mosquitto"]["networks"] == ["sandbox"]
    assert compose["services"]["publisher"]["networks"] == ["sandbox"]
    assert all(
        service["labels"]["com.runsodin.edu-generation"] == "${ODIN_EDU_GENERATION}"
        or service["labels"]["com.runsodin.edu-generation"] == "${ODIN_EDU_GENERATION:?generation required}"
        for service in compose["services"].values()
    )
    assert "1883" not in compose["services"]["mosquitto"].get("ports", [])
    environment = compose["services"]["odin"]["environment"]
    assert environment["ODIN_REQUIRE_LICENSE_BINDING"] == "1"
    assert environment["ODIN_LICENSE_READ_ONLY"] == "1"
    assert environment["ENCRYPTION_KEY_FILE"].startswith("/run/odin-secrets/")
    assert environment["ODIN_DEMO_EDU_ADMIN_PASSWORD_FILE"].startswith("/run/odin-secrets/")
    assert not any(key.endswith("PASSWORD") for key in environment)
    runtime_source = Path(__file__).parents[2].joinpath("ops/edu_sandbox/runtime.py").read_text()
    assert '["docker", "network", "create", "--internal", resources.internal_network]' in runtime_source
    assert '["docker", "network", "create", resources.edge_network]' in runtime_source


def test_broker_default_is_digest_pinned():
    assert BROKER_IMAGE.startswith("eclipse-mosquitto@sha256:")
    assert len(BROKER_IMAGE.rsplit(":", 1)[1]) == 64


def test_compose_generation_advances_only_for_resetting_phase(tmp_path: Path):
    runtime = SandboxRuntime(tmp_path)
    active = LifecycleState("school-one", Phase.ACTIVE, reset_generation=2)
    resetting = LifecycleState("school-one", Phase.RESETTING, reset_generation=2)
    assert runtime._compose_env(active)["ODIN_EDU_GENERATION"] == "2"
    assert runtime._compose_env(resetting)["ODIN_EDU_GENERATION"] == "3"


def test_named_container_ownership_rejects_stale_generation(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        compose_project=resources.project,
        reset_generation=2,
    )
    monkeypatch.setattr(
        runtime,
        "_resource_labels",
        lambda *_args: {
            "com.runsodin.edu-sandbox": "school-one",
            "com.runsodin.edu-schema": "1",
            "com.runsodin.edu-generation": "1",
            "com.docker.compose.project": "odin-edu-school-one",
        },
    )
    with pytest.raises(OwnershipError, match="stale or missing generation"):
        runtime._assert_named_containers_owned(state, allow_absent=False)


def test_dockerfile_packages_only_required_sandbox_runtime_assets():
    dockerfile = (COMPOSE_FILE.parents[1] / ".." / "Dockerfile").resolve().read_text()
    assert "COPY ops/demo/demo_publisher.py /app/ops/demo/demo_publisher.py" in dockerfile
    assert "COPY ops/edu_sandbox/tcp_proxy.py /app/ops/edu_sandbox/tcp_proxy.py" in dockerfile
    assert "COPY tests/fixtures/telemetry/bambu-x1c-ams-swap.demo.jsonl" in dockerfile
    assert "COPY tests/ ./tests/" not in dockerfile


def test_expire_requires_exact_acknowledgement_and_only_changes_lease(tmp_path: Path):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        compose_project=resources.project,
        resources=resources.state_value(),
        installation_id="stable-install",
        license_sha256="a" * 64,
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
    )
    save_state(paths.state, state, paths.directory)
    with pytest.raises(ValidationError, match="confirmation"):
        runtime.expire("school-one", confirm="school-two")
    expired_lease = runtime.expire("school-one", confirm="school-one")
    assert expired_lease.phase == Phase.ACTIVE
    assert expired_lease.installation_id == "stable-install"
    assert expired_lease.license_sha256 == "a" * 64
    assert _lease_deadline(expired_lease.lease_expires_at) <= datetime.now(timezone.utc)


def test_reconcile_is_read_only_before_deadline(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        license_sha256="b" * 64,
        license_tier="education",
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        license_expires_at=(datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_state_resource_ownership", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_service_running", lambda *_args: True)
    monkeypatch.setattr(runtime, "_observe_bound_license", lambda *_args, **_kwargs: {"expired": False})
    before = paths.state.read_bytes()
    returned = runtime.reconcile("school-one")
    assert returned.phase == Phase.ACTIVE
    assert paths.state.read_bytes() == before


def test_status_observes_quiesced_expiry_without_mutating_phase(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        license_expires_at=(datetime.now(timezone.utc) + timedelta(days=10)).date().isoformat(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_state_resource_ownership", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_service_running", lambda _state, _service: False)
    monkeypatch.setattr(runtime, "_observe_bound_license", lambda *_args, **_kwargs: {"expired": False})
    monkeypatch.setattr(runtime, "_assert_network_isolation", lambda *_args, **_kwargs: {})
    observed = runtime.status("school-one")
    assert observed["phase"] == "ACTIVE"
    assert observed["observed_status"] == "EXPIRED"
    assert load_state(paths.state).phase == Phase.ACTIVE


def test_prepared_status_is_degraded_when_current_identity_or_topology_drifts(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_state_resource_ownership", lambda _state: resources)

    def reject_drift(_state):
        raise SandboxError("prepared identity or network drift")

    monkeypatch.setattr(runtime, "_assert_prepared_current_evidence", reject_drift)
    observed = runtime.status("school-one")

    assert observed["phase"] == "PREPARED"
    assert observed["observed_status"] == "DEGRADED"
    assert "prepared_current" not in observed


def test_purge_rejects_symlink_before_calling_docker(tmp_path: Path):
    class NoDocker:
        def run(self, *args, **kwargs):
            raise AssertionError("Docker must not run before host tree validation")

    runtime = SandboxRuntime(tmp_path, executor=NoDocker())
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        candidate_tag="odin-edu-candidate:school-one",
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    (paths.directory / "bad-link").symlink_to(tmp_path / "outside")
    from ops.edu_sandbox.errors import OwnershipError
    with pytest.raises(OwnershipError, match="symlink"):
        runtime.purge("school-one", confirm="school-one")
    assert load_state(paths.state).phase == Phase.PREPARED


def test_purge_rejects_unbounded_persisted_candidate_tag_before_docker(tmp_path: Path):
    class NoDocker:
        def run(self, *args, **kwargs):
            raise AssertionError("Docker must not run before candidate-tag validation")

    runtime = SandboxRuntime(tmp_path, executor=NoDocker())
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        candidate_tag="unrelated-image:latest",
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)

    with pytest.raises(OwnershipError, match="candidate tag"):
        runtime.purge("school-one", confirm="school-one")
    assert load_state(paths.state).phase == Phase.PREPARED


def test_runtime_source_contains_no_recursive_shell_delete():
    source = Path(__file__).parents[2].joinpath("ops/edu_sandbox/runtime.py").read_text()
    assert "rm -rf" not in source
    assert "docker.sock" not in source
    assert "os.chown(target, 10001, 10001)" in source
    assert "_DELETE_LICENSE_SCRIPT" in source
    assert "_CREATE_RESET_SENTINEL_SCRIPT" in source
    assert "_CHECK_RESET_SENTINEL_SCRIPT" in source
    assert "reset_sentinel_removed" in source
    assert 'tombstone.with_suffix(".html")' in source
    assert "broker_image_id" in source
    assert "_observe_bound_license" in source


def test_activation_receipt_includes_candidate_version_without_host_identity():
    source = Path(__file__).parents[2].joinpath("ops/edu_sandbox/runtime.py").read_text()
    receipt = source.split("receipt = {", 1)[1].split("}", 1)[0]
    assert '"odin_version": odin_version' in receipt
    assert "hostname" not in receipt
    assert "device_pubkey" not in receipt


def test_terminal_tombstone_prevents_sandbox_id_reuse(tmp_path: Path):
    tombstones = tmp_path / ".tombstones"
    tombstones.mkdir()
    (tombstones / "school-one.json").write_text("{}", encoding="utf-8")
    runtime = SandboxRuntime(tmp_path)
    with pytest.raises(StateError, match="terminally PURGED"):
        runtime.prepare("school-one", allow_dirty=True)


def test_repeated_purge_reverifies_absence(tmp_path: Path):
    class AbsentDocker:
        def run(self, args, **kwargs):
            missing = "inspect" in args
            return CommandResult(tuple(args), 1 if missing else 0, "", "No such object" if missing else "")

    runtime = SandboxRuntime(tmp_path, executor=AbsentDocker())
    paths = runtime._paths("school-one")
    tombstones = tmp_path / ".tombstones"
    tombstones.mkdir()
    value = {
        "schema_version": 1,
        "sandbox_id": "school-one",
        "status": "PASS",
        "candidate_tag": "odin-edu-candidate:school-one",
        "loopback_port": None,
    }
    (tombstones / "school-one.json").write_text(json.dumps(value), encoding="utf-8")
    (tombstones / "school-one.html").write_text("<!doctype html>", encoding="utf-8")
    result = runtime.purge("school-one", confirm="school-one")
    assert result["absence"]["controller_directory_absent"] is True
    assert result["absence"]["candidate_tag_absent"] is True


def test_interrupted_purge_journal_is_visible_and_resumable(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    tombstones = tmp_path / ".tombstones"
    tombstones.mkdir()
    journal = {
        "schema_version": 1,
        "sandbox_id": "school-one",
        "status": "PURGING",
        "source_commit": "deadbeef",
        "candidate_image_id": "",
        "candidate_tag": "odin-edu-candidate:school-one",
        "loopback_port": None,
        "removed": {
            "containers": [],
            "volumes": [],
            "networks": [],
            "image_tags": [],
        },
        "residue": [],
    }
    (tombstones / "school-one.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )
    observed = runtime.status("school-one")
    assert observed == {
        "sandbox_id": "school-one",
        "phase": "PURGING",
        "observed_status": "DEGRADED",
        "recovery_required": True,
    }

    absence = {
        "labelled_containers_absent": True,
        "labelled_volumes_absent": True,
        "labelled_networks_absent": True,
        "expected_named_containers_absent": True,
        "named_volumes_absent": True,
        "named_networks_absent": True,
        "candidate_tag_absent": True,
        "license_mount_absent": True,
        "heartbeat_volume_absent": True,
        "loopback_port_closed": True,
    }
    monkeypatch.setattr(runtime, "_docker_absence_evidence", lambda *_args, **_kwargs: absence)
    result = runtime.purge("school-one", confirm="school-one")
    assert result["status"] == "PASS"
    assert result["absence"]["controller_directory_absent"] is True
    assert (tombstones / "school-one.html").is_file()


def test_purge_failed_preparing_without_candidate_image_is_terminal(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.DEGRADED,
        source_commit="deadbeef",
        candidate_image_id="",
        candidate_tag="odin-edu-candidate:school-one",
        compose_project=resources.project,
        resources=resources.state_value(),
        last_transition={"from": "PREPARING", "to": "DEGRADED"},
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_resource_labels", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_labelled_resource_names", lambda *_args, **_kwargs: [])

    def absent_docker(args, **_kwargs):
        missing_image = args[1:3] in (["image", "rm"], ["image", "inspect"])
        return CommandResult(
            tuple(args),
            1 if missing_image else 0,
            "",
            "No such image" if missing_image else "",
        )

    monkeypatch.setattr(runtime, "_try_run", absent_docker)
    result = runtime.purge("school-one", confirm="school-one")
    assert result["status"] == "PASS"
    assert result["candidate_image_id"] == ""
    assert not paths.directory.exists()
    assert (tmp_path / ".tombstones" / "school-one.html").is_file()


def test_purge_removes_extra_exactly_owned_labelled_volume(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    extra = f"{resources.project}-extra-cache"
    labelled_volumes = {extra}
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        source_commit="deadbeef",
        candidate_image_id="sha256:" + "a" * 64,
        candidate_tag="odin-edu-candidate:school-one",
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)

    def labelled(kind, _sandbox_id):
        return sorted(labelled_volumes) if kind == "volume" else []

    monkeypatch.setattr(runtime, "_labelled_resource_names", labelled)

    def labels(kind, name):
        if kind == "volume" and name in labelled_volumes:
            return {
                "com.runsodin.edu-sandbox": "school-one",
                "com.runsodin.edu-schema": "1",
                "com.runsodin.edu-generation": "0",
                "com.docker.compose.project": resources.project,
            }
        return None

    monkeypatch.setattr(runtime, "_resource_labels", labels)

    def try_run(args, **_kwargs):
        missing_image = args[1:3] in (["image", "rm"], ["image", "inspect"])
        return CommandResult(
            tuple(args),
            1 if missing_image else 0,
            "",
            "No such image" if missing_image else "",
        )

    monkeypatch.setattr(runtime, "_try_run", try_run)

    def run(args, **_kwargs):
        if args[:3] == ["docker", "volume", "rm"] and args[-1] == extra:
            labelled_volumes.remove(extra)
            return CommandResult(tuple(args), 0, extra, "")
        raise AssertionError(f"unexpected Docker mutation: {args}")

    monkeypatch.setattr(runtime, "_run", run)
    result = runtime.purge("school-one", confirm="school-one")
    assert result["status"] == "PASS"
    assert result["removed"]["volumes"] == [extra]
    assert result["absence"]["labelled_volumes_absent"] is True


def test_labelled_network_inventory_requests_names_not_ids(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    captured = []

    def run(args, **_kwargs):
        captured.append(args)
        return CommandResult(tuple(args), 0, "network-two\nnetwork-one\n", "")

    monkeypatch.setattr(runtime, "_try_run", run)
    assert runtime._labelled_resource_names("network", "school-one") == [
        "network-one",
        "network-two",
    ]
    assert captured == [[
        "docker",
        "network",
        "ls",
        "--filter",
        "label=com.runsodin.edu-sandbox=school-one",
        "--format",
        "{{.Name}}",
    ]]


def test_purge_removes_stranded_project_named_helper_container(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    helper_id = "helper-container-id"
    helper_name = f"{resources.project}-helper-license-validate-deadbeef"
    stranded = {helper_id}
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        source_commit="deadbeef",
        candidate_image_id="sha256:" + "a" * 64,
        candidate_tag="odin-edu-candidate:school-one",
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_labelled_resource_names", lambda *_args, **_kwargs: [])

    def labels(kind, name):
        if kind == "container" and name in stranded:
            return {
                "com.runsodin.edu-sandbox": "school-one",
                "com.runsodin.edu-schema": "1",
                "com.runsodin.edu-generation": "0",
                "com.docker.compose.project": resources.project,
            }
        return None

    monkeypatch.setattr(runtime, "_resource_labels", labels)

    def try_run(args, **_kwargs):
        if args[:3] == ["docker", "ps", "-aq"]:
            return CommandResult(tuple(args), 0, "\n".join(sorted(stranded)), "")
        missing_image = args[1:3] in (["image", "rm"], ["image", "inspect"])
        return CommandResult(
            tuple(args),
            1 if missing_image else 0,
            "",
            "No such image" if missing_image else "",
        )

    monkeypatch.setattr(runtime, "_try_run", try_run)

    def run(args, **_kwargs):
        if args[:4] == ["docker", "inspect", "--format", "{{.Name}}"]:
            return CommandResult(tuple(args), 0, f"/{helper_name}\n", "")
        if args[:3] == ["docker", "rm", "-f"] and args[-1] == helper_id:
            stranded.remove(helper_id)
            return CommandResult(tuple(args), 0, helper_id, "")
        raise AssertionError(f"unexpected Docker mutation: {args}")

    monkeypatch.setattr(runtime, "_run", run)
    result = runtime.purge("school-one", confirm="school-one")
    assert result["status"] == "PASS"
    assert result["removed"]["containers"] == [helper_name]


def test_purge_does_not_publish_pass_before_controller_cleanup(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.PREPARED,
        source_commit="deadbeef",
        candidate_image_id="sha256:" + "a" * 64,
        candidate_tag="odin-edu-candidate:school-one",
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    blocked_file = paths.secrets / "locked" / "file"
    blocked_file.parent.mkdir(parents=True)
    blocked_file.write_text("synthetic", encoding="utf-8")

    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_resource_labels", lambda *_args, **_kwargs: None)

    def absent_docker(args, **_kwargs):
        missing = args[1:3] == ["image", "inspect"]
        return CommandResult(
            tuple(args),
            1 if missing else 0,
            "",
            "No such image" if missing else "",
        )

    monkeypatch.setattr(runtime, "_try_run", absent_docker)
    original_unlink = Path.unlink

    def fail_controller_cleanup(path: Path, *args, **kwargs):
        if path == blocked_file:
            raise PermissionError("synthetic controller cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_controller_cleanup)
    with pytest.raises(PermissionError, match="controller cleanup failure"):
        runtime.purge("school-one", confirm="school-one")

    tombstone_dir = tmp_path / ".tombstones"
    assert paths.state.is_file()
    journal = json.loads(
        (tombstone_dir / "school-one.json").read_text(encoding="utf-8")
    )
    assert journal["status"] == "PURGING"
    assert not (tombstone_dir / "school-one.html").exists()
    assert not list(tombstone_dir.glob(".*.tmp"))


def test_interrupted_prepare_recovery_removes_only_owned_partial_state(tmp_path: Path):
    class AbsentDocker:
        def run(self, args, **kwargs):
            missing = "inspect" in args
            return CommandResult(tuple(args), 1 if missing else 0, "", "No such object" if missing else "")

    runtime = SandboxRuntime(tmp_path, executor=AbsentDocker())
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
    runtime._recover_interrupted_prepare(paths, resources)
    assert not paths.directory.exists()


def test_unpublished_prepare_is_visible_and_prepare_recovery_can_restart(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    remnant = paths.directory / ".state-crash"
    remnant.write_text("partial", encoding="utf-8")
    absence = {
        "labelled_containers_absent": True,
        "labelled_volumes_absent": True,
        "labelled_networks_absent": True,
        "expected_named_containers_absent": True,
        "named_volumes_absent": True,
        "named_networks_absent": True,
        "candidate_tag_absent": True,
        "license_mount_absent": True,
        "heartbeat_volume_absent": True,
        "loopback_port_closed": True,
    }
    monkeypatch.setattr(
        runtime, "_docker_absence_evidence", lambda *_args, **_kwargs: absence
    )

    observed = runtime.status("school-one")
    assert observed["phase"] == "PREPARING"
    assert observed["observed_status"] == "DEGRADED"
    assert observed["recovery_required"] is True
    assert observed["recovery_safe"] is True

    def stop_after_recovery(*_args):
        assert not paths.directory.exists()
        raise RuntimeError("stop after unpublished recovery")

    monkeypatch.setattr(runtime, "_git", stop_after_recovery)
    with pytest.raises(RuntimeError, match="stop after unpublished recovery"):
        runtime.prepare("school-one", allow_dirty=True, recover=True)


def test_purge_unpublished_prepare_publishes_terminal_evidence(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    (paths.directory / ".state-crash").write_text("partial", encoding="utf-8")
    absence = {
        "labelled_containers_absent": True,
        "labelled_volumes_absent": True,
        "labelled_networks_absent": True,
        "expected_named_containers_absent": True,
        "named_volumes_absent": True,
        "named_networks_absent": True,
        "candidate_tag_absent": True,
        "license_mount_absent": True,
        "heartbeat_volume_absent": True,
        "loopback_port_closed": True,
    }
    monkeypatch.setattr(
        runtime, "_docker_absence_evidence", lambda *_args, **_kwargs: absence
    )

    result = runtime.purge("school-one", confirm="school-one")
    assert result["status"] == "PASS"
    assert result["candidate_image_id"] == ""
    assert not paths.directory.exists()
    assert (tmp_path / ".tombstones" / "school-one.json").is_file()
    assert (tmp_path / ".tombstones" / "school-one.html").is_file()


def test_unpublished_prepare_recovery_rejects_unexpected_controller_data(
    tmp_path: Path, monkeypatch
):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    paths.directory.mkdir(mode=0o700)
    unexpected = paths.directory / "unexpected.txt"
    unexpected.write_text("do not delete", encoding="utf-8")
    monkeypatch.setattr(
        runtime,
        "_docker_absence_evidence",
        lambda *_args, **_kwargs: pytest.fail("Docker must not be inspected"),
    )

    observed = runtime.status("school-one")
    assert observed["observed_status"] == "DEGRADED"
    assert observed["recovery_safe"] is False
    with pytest.raises(OwnershipError, match="unexpected controller data"):
        runtime.purge("school-one", confirm="school-one")
    assert unexpected.read_text(encoding="utf-8") == "do not delete"


def test_interrupted_request_recovery_removes_partial_handoff(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.REQUESTING_LICENSE,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        last_transition={"from": "PREPARED", "to": "REQUESTING_LICENSE"},
    )
    save_state(paths.state, state, paths.directory)
    paths.secrets.mkdir(mode=0o700)
    paths.public.mkdir(mode=0o700)
    (paths.secrets / "activation-request.json").write_text("partial", encoding="utf-8")
    (paths.public / "activation-request-receipt.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(runtime, "_assert_prepared_identity", lambda _state: resources)
    recovered = runtime._recover_interrupted_request(paths, state)
    assert recovered.phase == Phase.PREPARED
    assert not (paths.secrets / "activation-request.json").exists()
    assert not (paths.public / "activation-request-receipt.json").exists()
    assert load_state(paths.state).phase == Phase.PREPARED


def test_interrupted_activation_recovery_removes_staged_license(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVATING,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        license_sha256="b" * 64,
        license_tier="education",
        last_transition={"from": "PREPARED", "to": "ACTIVATING"},
    )
    save_state(paths.state, state, paths.directory)
    paths.secrets.mkdir(mode=0o700, exist_ok=True)
    (paths.secrets / "license.json").write_text("partial", encoding="utf-8")
    calls: list[object] = []
    monkeypatch.setattr(runtime, "_assert_prepared_identity", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_compose", lambda _state, *args, **_kwargs: calls.append(args))
    monkeypatch.setattr(runtime, "_remove_volume_license", lambda _state, _resources: calls.append("removed"))
    recovered = runtime._recover_interrupted_activation(paths, state)
    assert recovered.phase == Phase.PREPARED
    assert recovered.license_sha256 == ""
    assert not (paths.secrets / "license.json").exists()
    assert calls == [("down",), "removed"]
    assert load_state(paths.state).phase == Phase.PREPARED


def test_expired_status_is_degraded_when_service_restarts(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.EXPIRED,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_state_resource_ownership", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_service_running", lambda _state, service: service == "odin")
    monkeypatch.setattr(runtime, "_observe_bound_license", lambda *_args, **_kwargs: {"expired": False})
    monkeypatch.setattr(runtime, "_assert_network_isolation", lambda *_args, **_kwargs: {})
    observed = runtime.status("school-one")
    assert observed["phase"] == "EXPIRED"
    assert observed["observed_status"] == "DEGRADED"


def test_repeated_reconcile_rechecks_and_stops_expired_services(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.EXPIRED,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
    )
    save_state(paths.state, state, paths.directory)
    calls = []
    monkeypatch.setattr(runtime, "_assert_state_resource_ownership", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_observe_bound_license", lambda *_args, **_kwargs: {"expired": False})
    monkeypatch.setattr(runtime, "_compose", lambda _state, *args, **kwargs: calls.append(args))
    monkeypatch.setattr(runtime, "_service_running", lambda _state, _service: False)
    returned = runtime.reconcile("school-one")
    assert returned.phase == Phase.EXPIRED
    assert calls == [("stop", "publisher", "proxy", "odin")]


def test_reconcile_failure_persists_degraded_phase(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        lease_expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_assert_state_resource_ownership", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_observe_bound_license", lambda *_args, **_kwargs: {"expired": False})
    monkeypatch.setattr(runtime, "_service_running", lambda *_args: True)
    monkeypatch.setattr(runtime, "_compose", lambda *_args, **_kwargs: (_ for _ in ()).throw(SandboxError("stop failed")))
    with pytest.raises(SandboxError, match="stop failed"):
        runtime.reconcile("school-one")
    assert load_state(paths.state).phase == Phase.DEGRADED


def test_reconcile_uses_observed_license_expiry_not_future_state_date(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        license_sha256="b" * 64,
        license_expires_at=(datetime.now(timezone.utc) + timedelta(days=10)).isoformat(),
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(days=5)).isoformat(),
    )
    save_state(paths.state, state, paths.directory)
    running = {"odin": True, "publisher": True, "proxy": True}
    monkeypatch.setattr(runtime, "_assert_state_resource_ownership", lambda _state: resources)
    monkeypatch.setattr(runtime, "_assert_named_containers_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_observe_bound_license", lambda *_args, **_kwargs: {"expired": True})
    monkeypatch.setattr(runtime, "_service_running", lambda _state, service: running[service])

    def stop(_state, *args, **_kwargs):
        assert args == ("stop", "publisher", "proxy", "odin")
        running.update(odin=False, publisher=False, proxy=False)

    monkeypatch.setattr(runtime, "_compose", stop)
    reconciled = runtime.reconcile("school-one")
    assert reconciled.phase == Phase.EXPIRED
    assert running == {"odin": False, "publisher": False, "proxy": False}


def test_purge_absence_rejects_expected_name_even_without_owner_label(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    resources = ResourceSet.from_id("school-one")
    monkeypatch.setattr(
        runtime,
        "_try_run",
        lambda args, **_kwargs: CommandResult(
            tuple(args),
            1 if args[1:3] == ["image", "inspect"] else 0,
            "",
            "No such image" if args[1:3] == ["image", "inspect"] else "",
        ),
    )
    monkeypatch.setattr(
        runtime,
        "_resource_labels",
        lambda kind, name: {} if kind == "container" and name == resources.prepare_container else None,
    )
    with pytest.raises(SandboxError, match="residue"):
        runtime._docker_absence_evidence(
            "school-one",
            candidate_tag="odin-edu-candidate:school-one",
            loopback_port=None,
        )


def test_reset_rejects_foreign_volume_before_phase_change(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        license_sha256="b" * 64,
    )
    save_state(paths.state, state, paths.directory)
    monkeypatch.setattr(runtime, "_resource_labels", lambda _kind, _name: {"foreign": "1"})
    with pytest.raises(OwnershipError, match="foreign or missing ownership"):
        runtime.reset("school-one", confirm="school-one")
    assert load_state(paths.state).phase == Phase.ACTIVE


def test_invalid_expired_renewal_preserves_prior_license(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    paths = runtime._paths("school-one")
    resources = ResourceSet.from_id("school-one")
    prior = b"old-payload.old-signature"
    prior_digest = hashlib.sha256(prior).hexdigest()
    state = LifecycleState(
        "school-one",
        Phase.EXPIRED,
        candidate_image_id="sha256:" + "a" * 64,
        compose_project=resources.project,
        resources=resources.state_value(),
        license_sha256=prior_digest,
        license_tier="education",
        license_expires_at="2026-09-01T00:00:00Z",
    )
    save_state(paths.state, state, paths.directory)
    from ops.edu_sandbox.secrets import write_secret, bytes_stream
    write_secret(paths.secrets / "license.json", prior, paths.directory)
    restored = []
    monkeypatch.setattr(runtime, "_assert_prepared_identity", lambda _state: resources)
    monkeypatch.setattr(runtime, "_activation_request_digest", lambda _paths, _state: "c" * 64)
    monkeypatch.setattr(runtime, "_volume_license_digest", lambda _state, _resources: prior_digest)
    monkeypatch.setattr(runtime, "_validate_license", lambda *_args: (_ for _ in ()).throw(SandboxError("invalid")))
    monkeypatch.setattr(runtime, "_compose", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "_put_volume_license", lambda _state, _resources, content: restored.append(content))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with pytest.raises(SandboxError, match="invalid"):
        runtime.activate(
            "school-one",
            bytes_stream(b"new-payload.new-signature"),
            is_tty=False,
            expires_at=future,
        )
    after = load_state(paths.state)
    assert after.phase == Phase.EXPIRED
    assert after.license_sha256 == prior_digest
    assert (paths.secrets / "license.json").read_bytes() == prior
    assert restored == [prior]


def test_runtime_rejects_application_license_expiry_mismatch(tmp_path: Path, monkeypatch):
    runtime = SandboxRuntime(tmp_path)
    content = b"payload.signature"
    digest = hashlib.sha256(content).hexdigest()
    state = LifecycleState(
        "school-one",
        Phase.ACTIVE,
        loopback_port=12345,
        license_sha256=digest,
        license_expires_at="2030-01-01T00:00:00Z",
    )
    resources = ResourceSet.from_id("school-one")
    monkeypatch.setattr(runtime, "_controller_license_bytes", lambda *_args: content)
    monkeypatch.setattr(runtime, "_volume_license_digest", lambda *_args: digest)
    monkeypatch.setattr(
        runtime,
        "_validate_license",
        lambda *_args: {
            "valid": True,
            "tier": "education",
            "expires_at": "2030-01-01T00:00:00Z",
            "binding_present": True,
            "binding_matches_current": True,
            "license_sha256": digest,
        },
    )
    monkeypatch.setattr(
        runtime,
        "_http_json",
        lambda *_args: (200, {
            "valid": True,
            "tier": "education",
            "expires_at": "2030-01-02T00:00:00Z",
            "managed_externally": True,
            "binding_present": True,
            "binding_matches_current": True,
            "license_sha256": digest,
        }),
    )
    with pytest.raises(SandboxError, match="application license observation"):
        runtime._observe_bound_license(state, resources, include_application=True)


def test_identity_verification_is_strict_and_read_only():
    source = Path(__file__).parents[2].joinpath("ops/edu_sandbox/runtime.py").read_text()
    read_script = source.split('_READ_IDENTITY_SCRIPT = r"""', 1)[1].split('""".strip()', 1)[0]
    request_script = source.split('_REQUEST_LICENSE_SCRIPT = r"""', 1)[1].split('""".strip()', 1)[0]
    assert "get_installation_id" not in read_script
    assert "get_device_keypair" not in read_script
    assert "get_installation_id" not in request_script
    assert f'{{resources.data_volume}}:/data:ro' in source


def test_smoke_runner_converts_sigterm_into_cleanup_path():
    source = Path(__file__).parents[2].joinpath("ops/edu_sandbox/certify.py").read_text()
    assert "signal.SIGTERM" in source
    assert "raise KeyboardInterrupt" in source


def test_every_one_shot_docker_helper_has_ownership_labels():
    source = Path(__file__).parents[2].joinpath("ops/edu_sandbox/runtime.py").read_text()
    docker_runs = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.List) or len(node.elts) < 2:
            continue
        first = node.elts[:2]
        if all(isinstance(item, ast.Constant) for item in first) and [item.value for item in first] == ["docker", "run"]:
            docker_runs.append(node)
    assert docker_runs
    for command in docker_runs:
        literal_values = [item.value for item in command.elts if isinstance(item, ast.Constant)]
        if "--rm" not in literal_values:
            continue
        source = ast.unparse(command)
        assert literal_values.count("--label") >= 4
        assert "--name" in literal_values
        assert "_helper_container_name" in source
        assert "GENERATION_LABEL" in source
        assert "PROJECT_LABEL" in source


def test_ci_semgrep_scans_edu_sandbox_controller():
    source = Path(__file__).parents[2].joinpath("ops/release_control/structured_targets.py").read_text()
    assert '"ops/edu_sandbox/"' in source
    assert '"ops/edu_readiness/"' in source


def test_runbooks_do_not_put_license_challenge_values_in_shell_arguments():
    root = Path(__file__).parents[2]
    markdown = root.joinpath("docs/EDU_SANDBOX_RUNBOOK.md").read_text()
    html = root.joinpath("docs/EDU_SANDBOX_RUNBOOK.html").read_text()
    for source in (markdown, html):
        assert "printf '%s'" not in source
        assert "getpass.getpass" in source
        assert "input=json.dumps(request).encode()" in source


def test_sandbox_contract_gate_rejects_skips_and_xfails():
    source = Path(__file__).parents[2].joinpath("Makefile").read_text()
    target = source.split("test-edu-sandbox-contracts:", 1)[1].split("\n\n", 1)[0]
    assert "ops/demo/run_junit_gate.py" in target
    assert "--junitxml={junit}" in target
    assert "xfail_strict=true" in target
