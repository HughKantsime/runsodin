import subprocess
import json
import os
import sys
from pathlib import Path

import pytest

from ops.release_control import installer_smoke
from ops.release_control.installer_smoke import InstallerSmokeError, Resources

ROOT = Path(__file__).parents[2]


def test_IN01_real_isolated_installer_entrypoint_is_wired():
    source = (ROOT / "ops/release_control/local_gate.py").read_text()
    overlay = (ROOT / "install/docker-compose.test.yml").read_text()
    assert "ops.release_control.installer_smoke" in source
    assert "com.runsodin.install-smoke" in overlay


def test_IN02_updater_reuses_isolated_compose_project():
    source = (ROOT / "install/update.sh").read_text()
    assert 'compose_cmd=("${COMPOSE_CMD[@]}" -p "$ODIN_COMPOSE_PROJECT"' in source
    assert "COMPOSE_CMD=(docker-compose)" in source
    assert "docker-compose.test.yml" in source


def test_IN03_invalid_resource_name_is_rejected():
    with pytest.raises(InstallerSmokeError):
        Resources.from_run_id("BAD name")


def test_IN04_path_outside_run_root_is_rejected(tmp_path):
    with pytest.raises(InstallerSmokeError):
        installer_smoke.validate_test_path(tmp_path.parent / "outside", tmp_path / "root")


def test_IN05_occupied_port_is_rejected():
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        with pytest.raises(InstallerSmokeError):
            installer_smoke.assert_ports_free([sock.getsockname()[1]])
    finally:
        sock.close()


@pytest.mark.parametrize("kind", ["container", "network", "volume"], ids=["IN06", "IN07", "IN08"])
def test_exact_preexisting_resource_is_rejected(monkeypatch, tmp_path, kind):
    resources = Resources.from_run_id("abc-123")
    target = getattr(resources, kind)
    monkeypatch.setattr(installer_smoke, "_inspect", lambda probe_kind, name: {} if (probe_kind, name) == (kind, target) else None)
    with pytest.raises(InstallerSmokeError):
        installer_smoke.assert_absent(resources, tmp_path / "install", tmp_path / "data")


def test_IN09_preexisting_path_is_rejected(monkeypatch, tmp_path):
    resources = Resources.from_run_id("abc-123")
    monkeypatch.setattr(installer_smoke, "_inspect", lambda *_: None)
    install = tmp_path / "install"
    install.mkdir()
    with pytest.raises(InstallerSmokeError):
        installer_smoke.assert_absent(resources, install, tmp_path / "data")


def test_IN10_candidate_image_identity_comparison_is_exact():
    source = (ROOT / "ops/release_control/installer_smoke.py").read_text()
    assert "actual_image != expected_image" in source


def test_IN11_database_backed_readiness_is_mandatory():
    assert "/health/ready" in (ROOT / "install/install.sh").read_text()
    assert "ready\"[[:space:]]*:[[:space:]]*true" in (ROOT / "install/install.sh").read_text()


def test_unix_install_and_update_poll_authoritative_readiness_directly():
    for relative in ("install/install.sh", "install/update.sh"):
        source = (ROOT / relative).read_text()
        assert ".State.Health.Status" not in source
        assert "curl -sf" in source
        assert "ready\"[[:space:]]*:[[:space:]]*true" in source


def test_IN12_cleanup_refuses_identity_or_label_mismatch(monkeypatch):
    resources = Resources.from_run_id("abc-123")
    records = {"container": {"name": resources.container, "identity": "expected"}}
    payload = {"Id": "different", "Config": {"Labels": {installer_smoke.LABEL: resources.run_id, f"{installer_smoke.LABEL}.kind": "container"}}}
    monkeypatch.setattr(
        installer_smoke, "_inspect_startup_state", lambda *_: payload["State"]
    )
    with pytest.raises(InstallerSmokeError):
        installer_smoke.cleanup_owned(resources, records)


def test_IN13_zero_owned_residue_passes(monkeypatch):
    resources = Resources.from_run_id("abc-123")
    monkeypatch.setattr(installer_smoke, "_run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=""))
    installer_smoke.assert_zero_residue(resources)


def test_IN14_unix_defaults_remain_customer_compatible():
    source = (ROOT / "install/install.sh").read_text()
    assert 'ODIN_CONTAINER_NAME="${ODIN_CONTAINER_NAME:-odin}"' in source
    assert 'ODIN_HTTP_PORT="${ODIN_HTTP_PORT:-8000}"' in source
    assert 'INSTALL_DIR="${ODIN_INSTALL_DIR:-./odin}"' in source


def test_IN15_powershell_defaults_remain_customer_compatible():
    source = (ROOT / "install/install.ps1").read_text()
    assert "else { 'odin' }" in source
    assert "else { 8000 }" in source
    assert '"C:\\odin"' in source


def test_custom_http_port_controls_readiness_exposure_and_user_guidance():
    shell = (ROOT / "install/install.sh").read_text()
    powershell = (ROOT / "install/install.ps1").read_text()
    assert 'http://localhost:${ODIN_HTTP_PORT}/health/ready' in shell
    assert '</dev/tcp/$PUBLIC_IP/$ODIN_HTTP_PORT' in shell
    assert 'CORS_ORIGINS=http://${HOST_IP}:${ODIN_HTTP_PORT}' in shell
    assert '-Port $HttpPort' in powershell
    assert 'http://localhost:$HttpPort' in powershell
    assert 'localport=$HttpPort' in powershell


def test_updater_reloads_persisted_custom_identity_and_port():
    source = (ROOT / "install/update.sh").read_text()
    for key in ("ODIN_COMPOSE_PROJECT", "ODIN_CONTAINER_NAME", "ODIN_HTTP_PORT"):
        assert f"^{key}=" in source
    assert 'URL|http://${HOST_IP}:${ODIN_HTTP_PORT}' in source


def test_installer_failure_cleanup_has_initialized_image_state():
    source = (ROOT / "ops/release_control/installer_smoke.py").read_text()
    assert source.index("image_built = False") < source.index("try:\n        assert_ports_free")


def test_installer_outer_timeout_covers_uncached_build():
    inventory = (ROOT / "ops/release_control/inventory.json").read_text()
    assert '"id":"CV01"' in inventory
    assert '"timeout_seconds":2700' in inventory


def test_DI17_startup_failure_evidence_is_bounded_sanitized_and_hashed(
    monkeypatch, tmp_path
):
    resources = Resources.from_run_id("abc-123")
    run_root = tmp_path / "run"
    install = run_root / "install"
    install.mkdir(parents=True)
    secret = "installer-secret-value-123456"
    env_path = install / ".env"
    env_path.write_text(f"API_KEY={secret}\nODIN_HTTP_PORT=1234\n", encoding="utf-8")
    env_path.chmod(0o600)
    health_output = (secret + " 10.20.30.40 ") * 500
    payload = {
        "State": {
            "Status": "running",
            "Running": True,
            "Restarting": False,
            "OOMKilled": False,
            "ExitCode": 0,
            "Health": {
                "Status": "starting",
                "Log": [
                    {"ExitCode": index, "Output": health_output}
                    for index in range(9)
                ],
            },
        }
    }
    monkeypatch.setattr(
        installer_smoke, "_inspect_startup_state", lambda *_: payload["State"]
    )
    log_text = "\n".join(
        f"line-{index} {secret} user@school.edu 10.20.30.40"
        for index in range(900)
    )
    monkeypatch.setattr(installer_smoke, "_stream_docker_logs", lambda *_: log_text)
    immutable_id = "a" * 64
    monkeypatch.setattr(
        installer_smoke, "_validated_evidence_container_id", lambda *_: immutable_id
    )
    backend_text = "\n".join(
        f"backend-{index} {secret} user@school.edu 10.20.30.40 lab.internal "
        "Authorization: Bearer scanneronlytoken123456"
        for index in range(700)
    )
    monkeypatch.setattr(installer_smoke, "_stream_backend_log", lambda *_: backend_text)

    retained, errors = installer_smoke.retain_startup_failure_evidence(
        run_root, install, resources
    )

    assert errors == []
    assert {item["filename"] for item in retained} == {
        "startup.log",
        "backend.log",
        "startup-health.json",
    }
    for item in retained:
        artifact = run_root / str(item["filename"])
        content = artifact.read_bytes()
        assert len(content) == item["byte_count"]
        assert installer_smoke.hashlib.sha256(content).hexdigest() == item["sha256"]
        assert secret.encode() not in content
        assert b"user@school.edu" not in content
        assert b"10.20.30.40" not in content
        assert artifact.stat().st_mode & 0o777 == 0o600
    assert len((run_root / "startup.log").read_text().splitlines()) <= 700
    assert (run_root / "startup.log").stat().st_size <= 262_144
    assert len((run_root / "backend.log").read_text().splitlines()) <= 700
    assert (run_root / "backend.log").stat().st_size <= 262_144
    assert b"lab.internal" not in (run_root / "backend.log").read_bytes()
    assert b"scanneronlytoken123456" not in (run_root / "backend.log").read_bytes()
    health = json.loads((run_root / "startup-health.json").read_text())
    assert len(health["health_probes"]) == 5
    assert all(
        len(probe["output"].encode("utf-8")) <= 4_096
        for probe in health["health_probes"]
    )
    assert (run_root / "startup-health.json").stat().st_size <= 32_768


def test_DI17_unsafe_env_omits_evidence_without_touching_cleanup_targets(
    monkeypatch, tmp_path
):
    resources = Resources.from_run_id("abc-123")
    run_root = tmp_path / "run"
    install = run_root / "install"
    install.mkdir(parents=True)
    env_path = install / ".env"
    env_path.write_text("API_KEY=first\nAPI_KEY=second\n", encoding="utf-8")
    env_path.chmod(0o600)
    inspected = False

    def unexpected_inspect(*_args):
        nonlocal inspected
        inspected = True
        return None

    monkeypatch.setattr(installer_smoke, "_inspect_startup_state", unexpected_inspect)
    monkeypatch.setattr(
        installer_smoke, "_validated_evidence_container_id", unexpected_inspect
    )
    retained, errors = installer_smoke.retain_startup_failure_evidence(
        run_root, install, resources
    )

    assert retained == []
    assert errors == ["installer env contains an invalid or duplicate key"]
    assert inspected is False
    assert not (run_root / "startup.log").exists()


def test_BE03_owned_container_inspection_is_bounded_and_returns_immutable_id(
    monkeypatch,
):
    resources = Resources.from_run_id("abc-123")
    immutable_id = "b" * 64
    observed = {}

    def bounded(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return f"{immutable_id}\t{resources.run_id}\tcontainer\n".encode()

    monkeypatch.setattr(installer_smoke, "_bounded_command_output", bounded)

    assert installer_smoke._validated_evidence_container_id(resources) == immutable_id
    assert observed["args"][:4] == ["docker", "container", "inspect", "--format"]
    assert observed["args"][-1] == resources.container
    assert installer_smoke.LABEL in observed["args"][4]
    assert observed["kwargs"] == {
        "maximum": 512,
        "timeout": 30,
        "reject_overflow": True,
    }


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"", "inspection is malformed"),
        (b"not-an-id\tabc-123\tcontainer\n", "container ID is malformed"),
        (("c" * 64 + "\twrong-run\tcontainer\n").encode(), "ownership is not established"),
        (("c" * 64 + "\tabc-123\tvolume\n").encode(), "ownership is not established"),
        (("c" * 64 + "\tabc-123\tcontainer\textra\n").encode(), "inspection is malformed"),
        (b"\xff\xfe", "inspection is malformed"),
    ],
)
def test_BE03_owned_container_inspection_rejects_malformed_or_foreign_output(
    monkeypatch, payload, message
):
    resources = Resources.from_run_id("abc-123")
    monkeypatch.setattr(
        installer_smoke, "_bounded_command_output", lambda *_args, **_kwargs: payload
    )

    with pytest.raises(InstallerSmokeError, match=message):
        installer_smoke._validated_evidence_container_id(resources)


def test_BE01_backend_log_exec_uses_only_validated_immutable_id(monkeypatch):
    immutable_id = "d" * 64
    observed = {}

    def bounded(args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return b"backend traceback\n"

    monkeypatch.setattr(installer_smoke, "_bounded_command_output", bounded)

    assert installer_smoke._stream_backend_log(immutable_id) == "backend traceback\n"
    assert observed["args"] == [
        "docker",
        "exec",
        immutable_id,
        "tail",
        "-n",
        "700",
        "/data/backend.log",
    ]
    assert observed["kwargs"] == {
        "maximum": 262_144,
        "timeout": 30,
        "reject_overflow": True,
    }


def test_BE03_retain_path_executes_original_id_after_container_name_rebind(
    monkeypatch, tmp_path
):
    resources = Resources.from_run_id("abc-123")
    original_id = "1" * 64
    replacement_id = "2" * 64
    run_root = tmp_path / "run"
    install = run_root / "install"
    install.mkdir(parents=True)
    (install / ".env").write_text("ODIN_HTTP_PORT=1234\n", encoding="utf-8")
    (install / ".env").chmod(0o600)
    name_binding = {resources.container: original_id}
    exec_commands = []

    def bounded(args, **_kwargs):
        if args[:2] == ["docker", "logs"]:
            return b"docker startup log\n"
        if args[:4] == ["docker", "container", "inspect", "--format"]:
            inspected_id = name_binding[resources.container]
            name_binding[resources.container] = replacement_id
            return f"{inspected_id}\t{resources.run_id}\tcontainer\n".encode()
        if args[:2] == ["docker", "exec"]:
            exec_commands.append(args)
            return b"original backend traceback\n"
        raise AssertionError(args)

    monkeypatch.setattr(installer_smoke, "_bounded_command_output", bounded)
    monkeypatch.setattr(
        installer_smoke,
        "_inspect_startup_state",
        lambda *_: {
            "Status": "running",
            "Running": True,
            "Restarting": False,
            "OOMKilled": False,
            "ExitCode": 0,
        },
    )

    retained, errors = installer_smoke.retain_startup_failure_evidence(
        run_root, install, resources
    )

    assert errors == []
    assert {item["filename"] for item in retained} == {
        "startup.log",
        "backend.log",
        "startup-health.json",
    }
    assert name_binding[resources.container] == replacement_id
    assert len(exec_commands) == 1
    assert exec_commands[0][2] == original_id
    assert resources.container not in exec_commands[0]
    assert replacement_id not in exec_commands[0]


def test_BE02_backend_log_rejects_malformed_utf8_before_sanitization(monkeypatch):
    monkeypatch.setattr(
        installer_smoke,
        "_bounded_command_output",
        lambda *_args, **_kwargs: b"installer-secret-value-123456\xff\xfftail",
    )

    with pytest.raises(InstallerSmokeError, match="not valid UTF-8"):
        installer_smoke._stream_backend_log("e" * 64)


def test_BE02_backend_log_overflow_rejects_secret_at_cutoff_before_retention():
    secret = "installer-secret-value-123456"
    command = [
        sys.executable,
        "-c",
        (
            "import sys; "
            f"sys.stdout.write('x' * {installer_smoke.LOG_MAX_BYTES} + {secret!r})"
        ),
    ]

    with pytest.raises(InstallerSmokeError, match="oversized"):
        installer_smoke._bounded_command_output(
            command,
            maximum=installer_smoke.LOG_MAX_BYTES,
            timeout=10,
            reject_overflow=True,
        )


def test_BE03_backend_evidence_failure_preserves_existing_artifacts(
    monkeypatch, tmp_path
):
    resources = Resources.from_run_id("abc-123")
    run_root = tmp_path / "run"
    install = run_root / "install"
    install.mkdir(parents=True)
    (install / ".env").write_text("ODIN_HTTP_PORT=1234\n", encoding="utf-8")
    (install / ".env").chmod(0o600)
    monkeypatch.setattr(installer_smoke, "_stream_docker_logs", lambda *_: "docker log")
    monkeypatch.setattr(
        installer_smoke,
        "_inspect_startup_state",
        lambda *_: {
            "Status": "running",
            "Running": True,
            "Restarting": False,
            "OOMKilled": False,
            "ExitCode": 0,
        },
    )
    monkeypatch.setattr(
        installer_smoke, "_validated_evidence_container_id", lambda *_: "f" * 64
    )
    monkeypatch.setattr(
        installer_smoke,
        "_stream_backend_log",
        lambda *_: (_ for _ in ()).throw(InstallerSmokeError("bounded Docker evidence response is oversized")),
    )

    retained, errors = installer_smoke.retain_startup_failure_evidence(
        run_root, install, resources
    )

    assert {item["filename"] for item in retained} == {
        "startup.log",
        "startup-health.json",
    }
    assert errors == ["backend.log: bounded Docker evidence response is oversized"]
    assert not (run_root / "backend.log").exists()


def test_LA03_LA04_installer_emits_unique_test_environment(tmp_path):
    source = (ROOT / "install/install.sh").read_text()
    phase = source[
        source.index("# ── Phase 5: Generate environment") :
        source.index("# ── Phase 6: Pull image")
    ]
    install = tmp_path / "install"
    install.mkdir()
    values = {
        "TOTAL": "10",
        "INSTALL_DIR": str(install),
        "HOST_IP": "127.0.0.1",
        "TIMEZONE": "UTC",
        "ODIN_IMAGE": "odin:test",
        "ODIN_COMPOSE_PROJECT": "odin-test-school",
        "ODIN_CONTAINER_NAME": "odin-test-container",
        "ODIN_HTTP_PORT": "18000",
        "ODIN_GO2RTC_PORT": "11984",
        "ODIN_WEBRTC_PORT": "18555",
        "ODIN_INSTALL_TEST_MODE": "1",
        "ODIN_TEST_RUN_ID": "school-run",
        "ODIN_TEST_CONTAINER_NAME": "odin-test-container",
        "ODIN_TEST_NETWORK_NAME": "odin-test-network",
        "ODIN_TEST_VOLUME_NAME": "odin-test-volume",
        "ODIN_TEST_DATA_PATH": str(tmp_path / "data"),
        "ODIN_TEST_HTTP_PORT": "18000",
        "ODIN_TEST_GO2RTC_PORT": "11984",
        "ODIN_TEST_WEBRTC_PORT": "18555",
    }
    environment = os.environ.copy()
    environment.update(values)
    completed = subprocess.run(
        ["bash"],
        input="set -euo pipefail\nphase() { :; }\nok() { :; }\n" + phase,
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    emitted_lines = [
        line
        for line in (install / ".env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]
    emitted_keys = [line.split("=", 1)[0] for line in emitted_lines]
    assert len(emitted_keys) == len(set(emitted_keys))
    emitted = dict(line.split("=", 1) for line in emitted_lines)
    for key, value in values.items():
        if key.startswith("ODIN_TEST_") or key in {
            "ODIN_INSTALL_TEST_MODE",
            "ODIN_COMPOSE_PROJECT",
        }:
            assert emitted[key] == value


def test_DI17_command_capture_bounds_one_pathological_line_before_retention():
    command = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"]

    retained = installer_smoke._bounded_command_output(
        command, maximum=4096, timeout=10, reject_overflow=False
    )
    assert retained == b"x" * 4096

    with pytest.raises(InstallerSmokeError, match="oversized"):
        installer_smoke._bounded_command_output(
            command, maximum=4096, timeout=10, reject_overflow=True
        )
