"""Run the Unix installer/update flow with uniquely owned Docker resources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ops.edu_readiness.artifact_scan import scan_text as scan_text_for_pii
from ops.release_gate.policy import redact_text, scan_text_for_secrets
from ops.release_control.validation_image_cleanup import (
    DisposableImageLifecycle,
    read_iidfile,
    verify_built_image,
)

ROOT = Path(__file__).parents[2]
LABEL = "com.runsodin.install-smoke"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,62}$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
IPV4_RE = re.compile(r"(?<![A-Za-z0-9])(?:\d{1,3}\.){3}\d{1,3}(?![A-Za-z0-9])")
HOST_RE = re.compile(
    r"(?i)\b[A-Z0-9][A-Z0-9.-]{0,251}\.(?:local|internal|lan|home|corp|localdomain)\b"
)
SECRET_ENV_KEYS = frozenset(
    {
        "API_KEY",
        "JWT_SECRET_KEY",
        "ENCRYPTION_KEY",
        "ADMIN_PASSWORD",
        "POSTGRES_PASSWORD",
    }
)
ENV_MAX_BYTES = 65_536
LOG_MAX_LINES = 700
LOG_MAX_BYTES = 262_144
HEALTH_MAX_PROBES = 5
HEALTH_PROBE_MAX_BYTES = 4_096
HEALTH_DOCUMENT_MAX_BYTES = 32_768
HEALTH_INSPECT_MAX_BYTES = 262_144


class InstallerSmokeError(RuntimeError):
    pass


def _run(command: list[str], *, env: dict[str, str] | None = None, cwd: Path = ROOT,
         timeout: int = 900, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise InstallerSmokeError(f"command timed out: {command[0]}") from exc
    if check and result.returncode:
        raise InstallerSmokeError(f"command failed ({result.returncode}): {' '.join(command[:4])}\n{result.stdout[-1200:]}")
    return result


def _image_command(
    command: list[str], *, capture: bool = True, check: bool = True
) -> subprocess.CompletedProcess[str]:
    del capture
    return _run(command, check=check)


def validate_name(value: str) -> str:
    if not NAME_RE.fullmatch(value):
        raise InstallerSmokeError("resource name must be 3-63 lowercase letters, digits, dot, underscore, or hyphen")
    return value


def validate_test_path(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or resolved_root not in resolved.parents:
        raise InstallerSmokeError("test path must be a child of the run root")
    return resolved


def reserve_ports(count: int = 3) -> list[int]:
    sockets: list[socket.socket] = []
    try:
        for _ in range(count):
            item = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            item.bind(("127.0.0.1", 0))
            sockets.append(item)
        return [int(item.getsockname()[1]) for item in sockets]
    finally:
        for item in sockets:
            item.close()


def assert_ports_free(ports: list[int]) -> None:
    for port in ports:
        if not 1024 <= port <= 65535:
            raise InstallerSmokeError(f"port out of range: {port}")
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise InstallerSmokeError(f"port is occupied: {port}") from exc
        finally:
            probe.close()


@dataclass(frozen=True)
class Resources:
    run_id: str
    project: str
    container: str
    network: str
    volume: str

    @classmethod
    def from_run_id(cls, run_id: str) -> "Resources":
        safe = validate_name(run_id)
        return cls(safe, f"odin-smoke-{safe}", f"odin-smoke-{safe}",
                   f"odin-smoke-{safe}-net", f"odin-smoke-{safe}-data")


def _inspect(kind: str, name: str) -> dict[str, object] | None:
    command = ["docker", kind, "inspect", name]
    result = _run(command, check=False)
    if result.returncode:
        return None
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise InstallerSmokeError(f"unexpected docker inspect output for {kind}:{name}")
    return payload[0]


def assert_absent(resources: Resources, install_dir: Path, data_path: Path, image: str | None = None) -> None:
    for kind, name in (("container", resources.container), ("network", resources.network), ("volume", resources.volume)):
        if _inspect(kind, name) is not None:
            raise InstallerSmokeError(f"pre-existing exact resource: {kind}:{name}")
    for path in (install_dir, data_path):
        if path.exists():
            raise InstallerSmokeError(f"pre-existing exact path: {path}")
    if image and _inspect("image", image) is not None:
        raise InstallerSmokeError(f"pre-existing exact resource: image:{image}")


def _identity(kind: str, payload: dict[str, object]) -> str:
    if kind in {"container", "network"}:
        value = str(payload.get("Id", ""))
    else:
        value = "|".join(str(payload.get(key, "")) for key in ("Name", "CreatedAt", "Mountpoint"))
        value = hashlib.sha256(value.encode()).hexdigest()
    if not value:
        raise InstallerSmokeError(f"{kind} has no stable identity")
    return value


def record_owned(resources: Resources) -> dict[str, dict[str, str]]:
    records: dict[str, dict[str, str]] = {}
    image_built = False
    for kind, name in (("container", resources.container), ("network", resources.network), ("volume", resources.volume)):
        payload = _inspect(kind, name)
        if payload is None:
            raise InstallerSmokeError(f"expected resource is missing: {kind}:{name}")
        labels = payload.get("Config", {}).get("Labels", {}) if kind == "container" else payload.get("Labels", {})
        if not isinstance(labels, dict) or labels.get(LABEL) != resources.run_id or labels.get(f"{LABEL}.kind") != kind:
            raise InstallerSmokeError(f"resource labels do not establish ownership: {kind}:{name}")
        records[kind] = {"name": name, "identity": _identity(kind, payload)}
    return records


def discover_owned(resources: Resources) -> dict[str, dict[str, str]]:
    """Record any surviving objects only when their labels prove this run owns them."""
    records: dict[str, dict[str, str]] = {}
    for kind, name in (("container", resources.container), ("network", resources.network), ("volume", resources.volume)):
        payload = _inspect(kind, name)
        if payload is None:
            continue
        labels = payload.get("Config", {}).get("Labels", {}) if kind == "container" else payload.get("Labels", {})
        if not isinstance(labels, dict) or labels.get(LABEL) != resources.run_id or labels.get(f"{LABEL}.kind") != kind:
            raise InstallerSmokeError(f"surviving resource lacks exact ownership labels: {kind}:{name}")
        records[kind] = {"name": name, "identity": _identity(kind, payload)}
    return records


def cleanup_owned(resources: Resources, records: dict[str, dict[str, str]]) -> None:
    commands = {"container": ["docker", "rm", "-f"], "network": ["docker", "network", "rm"], "volume": ["docker", "volume", "rm"]}
    for kind in ("container", "network", "volume"):
        record = records.get(kind)
        if not record:
            continue
        payload = _inspect(kind, record["name"])
        if payload is None:
            raise InstallerSmokeError(f"cleanup target disappeared: {kind}:{record['name']}")
        labels = payload.get("Config", {}).get("Labels", {}) if kind == "container" else payload.get("Labels", {})
        if _identity(kind, payload) != record["identity"] or not isinstance(labels, dict) or labels.get(LABEL) != resources.run_id or labels.get(f"{LABEL}.kind") != kind:
            raise InstallerSmokeError(f"cleanup refused identity/label mismatch: {kind}:{record['name']}")
        _run([*commands[kind], record["name"]])


def assert_zero_residue(resources: Resources) -> None:
    for kind, ls_args in (
        ("container", ["docker", "container", "ls", "-aq"]),
        ("network", ["docker", "network", "ls", "-q"]),
        ("volume", ["docker", "volume", "ls", "-q"]),
    ):
        result = _run([*ls_args, "--filter", f"label={LABEL}={resources.run_id}"])
        if result.stdout.strip():
            raise InstallerSmokeError(f"run-owned {kind} residue remains")


def _bounded_command_output(
    args: list[str], *, maximum: int, timeout: int, reject_overflow: bool
) -> bytes:
    """Drain command output with fixed memory instead of communicate()."""
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            args,
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        raise InstallerSmokeError("bounded Docker evidence command could not start") from exc
    if process.stdout is None:
        process.kill()
        raise InstallerSmokeError("bounded Docker evidence command has no output pipe")
    retained = bytearray()
    total = 0
    reader_error: list[BaseException] = []

    def drain() -> None:
        nonlocal total
        try:
            while True:
                chunk = process.stdout.read(65_536)
                if not chunk:
                    break
                total += len(chunk)
                if len(chunk) >= maximum:
                    retained[:] = chunk[-maximum:]
                else:
                    retained.extend(chunk)
                    overflow = len(retained) - maximum
                    if overflow > 0:
                        del retained[:overflow]
        except BaseException as exc:
            reader_error.append(exc)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        reader.join(timeout=5)
        raise InstallerSmokeError("bounded Docker evidence command timed out") from exc
    reader.join(timeout=5)
    if reader.is_alive() or reader_error:
        raise InstallerSmokeError("bounded Docker evidence output could not be drained")
    if returncode:
        raise InstallerSmokeError("bounded Docker evidence command failed")
    if reject_overflow and total > maximum:
        raise InstallerSmokeError("bounded Docker evidence response is oversized")
    return bytes(retained)


def _stream_docker_logs(container: str) -> str:
    payload = _bounded_command_output(
        ["docker", "logs", "--tail", str(LOG_MAX_LINES), container],
        maximum=LOG_MAX_BYTES,
        timeout=30,
        reject_overflow=False,
    )
    return payload.decode("utf-8", errors="replace")


def _inspect_startup_state(container: str) -> dict[str, object]:
    payload = _bounded_command_output(
        ["docker", "inspect", "--format", "{{json .State}}", container],
        maximum=HEALTH_INSPECT_MAX_BYTES,
        timeout=30,
        reject_overflow=True,
    )
    try:
        state = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerSmokeError("Docker startup state is malformed") from exc
    if not isinstance(state, dict):
        raise InstallerSmokeError("Docker startup state is malformed")
    return state


def _known_env_secrets(path: Path) -> tuple[str, ...]:
    """Read only exact secret keys from a private, controller-owned installer env."""
    if not path.exists() and not path.is_symlink():
        return ()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise InstallerSmokeError("installer env metadata is unreadable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size > ENV_MAX_BYTES
    ):
        raise InstallerSmokeError("installer env metadata is unsafe")
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise InstallerSmokeError("installer env is unreadable UTF-8") from exc
    seen: set[str] = set()
    secrets_found: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise InstallerSmokeError("installer env contains a malformed line")
        key, value = line.split("=", 1)
        if not ENV_KEY_RE.fullmatch(key) or key in seen:
            raise InstallerSmokeError("installer env contains an invalid or duplicate key")
        seen.add(key)
        if key in SECRET_ENV_KEYS and value:
            secrets_found.append(value)
    return tuple(secrets_found)


def _bounded_utf8_tail(text: str, maximum: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= maximum:
        return text
    return encoded[-maximum:].decode("utf-8", errors="ignore")


def _sanitize_evidence_text(text: str, known_secrets: tuple[str, ...]) -> str:
    sanitized = redact_text(text, known_secrets)
    sanitized = EMAIL_RE.sub("[REDACTED-EMAIL]", sanitized)
    sanitized = IPV4_RE.sub("[REDACTED-IP]", sanitized)
    sanitized = HOST_RE.sub("[REDACTED-HOST]", sanitized)
    return sanitized


def _safe_evidence_bytes(
    text: str, *, known_secrets: tuple[str, ...], relative_name: str, maximum: int
) -> bytes:
    candidate = _bounded_utf8_tail(
        _sanitize_evidence_text(_bounded_utf8_tail(text, maximum), known_secrets),
        maximum,
    )
    findings = scan_text_for_secrets(candidate, known_secrets)
    findings.extend(scan_text_for_pii(candidate, relative_name))
    if findings:
        raise InstallerSmokeError(
            "retained startup evidence failed secret/PII scan: "
            + ", ".join(sorted(set(findings)))
        )
    payload = candidate.encode("utf-8")
    if len(payload) > maximum:
        raise InstallerSmokeError("retained startup evidence exceeds its byte bound")
    return payload


def _safe_failure_detail(install_dir: Path, detail: str) -> str:
    try:
        known_secrets = _known_env_secrets(install_dir / ".env")
        payload = _safe_evidence_bytes(
            detail,
            known_secrets=known_secrets,
            relative_name="manifest.json",
            maximum=1_600,
        )
        return payload.decode("utf-8")
    except Exception:
        return "installer failed; unsafe diagnostic detail omitted"


def _write_private_evidence(path: Path, payload: bytes) -> dict[str, object]:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {
        "filename": path.name,
        "byte_count": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def retain_startup_failure_evidence(
    run_root: Path, install_dir: Path, resources: Resources
) -> tuple[list[dict[str, object]], list[str]]:
    """Retain bounded diagnostics before cleanup; never weaken cleanup on failure."""
    retained: list[dict[str, object]] = []
    errors: list[str] = []
    try:
        known_secrets = _known_env_secrets(install_dir / ".env")
    except Exception as exc:
        return retained, [str(exc)]
    try:
        log_text = "\n".join(
            _stream_docker_logs(resources.container).splitlines()[-LOG_MAX_LINES:]
        )
        log_bytes = _safe_evidence_bytes(
            log_text,
            known_secrets=known_secrets,
            relative_name="startup.log",
            maximum=LOG_MAX_BYTES,
        )
        retained.append(_write_private_evidence(run_root / "startup.log", log_bytes))
    except Exception as exc:
        errors.append(f"startup.log: {exc}")
    try:
        state = _inspect_startup_state(resources.container)
        status = state.get("Status", "")
        running = state.get("Running", False)
        restarting = state.get("Restarting", False)
        oom_killed = state.get("OOMKilled", False)
        exit_code = state.get("ExitCode", 0)
        if (
            not isinstance(status, str)
            or not all(isinstance(item, bool) for item in (running, restarting, oom_killed))
            or not isinstance(exit_code, int)
            or isinstance(exit_code, bool)
        ):
            raise InstallerSmokeError("Docker startup health state types are invalid")
        health = state.get("Health")
        health_status: str | None = None
        probes: list[dict[str, object]] = []
        if health is not None:
            if not isinstance(health, dict):
                raise InstallerSmokeError("Docker startup health state is malformed")
            raw_health_status = health.get("Status", "")
            if not isinstance(raw_health_status, str):
                raise InstallerSmokeError("Docker startup health status is invalid")
            health_status = raw_health_status[:64]
            health_log = health.get("Log", [])
            if not isinstance(health_log, list):
                raise InstallerSmokeError("Docker startup health log is malformed")
            for entry in health_log[-HEALTH_MAX_PROBES:]:
                if not isinstance(entry, dict):
                    raise InstallerSmokeError("Docker startup health probe is malformed")
                probe_exit_code = entry.get("ExitCode", 0)
                probe_output = entry.get("Output", "")
                if (
                    not isinstance(probe_exit_code, int)
                    or isinstance(probe_exit_code, bool)
                    or not isinstance(probe_output, str)
                ):
                    raise InstallerSmokeError("Docker startup health probe types are invalid")
                output = _safe_evidence_bytes(
                    probe_output,
                    known_secrets=known_secrets,
                    relative_name="startup-health.json",
                    maximum=HEALTH_PROBE_MAX_BYTES,
                ).decode("utf-8")
                probes.append({"exit_code": probe_exit_code, "output": output})
        document = {
            "status": status[:64],
            "running": running,
            "restarting": restarting,
            "oom_killed": oom_killed,
            "exit_code": exit_code,
            "health_status": health_status,
            "health_probes": probes,
        }
        health_bytes = json.dumps(
            document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(health_bytes) > HEALTH_DOCUMENT_MAX_BYTES:
            raise InstallerSmokeError("Docker startup health evidence exceeds its byte bound")
        # Probe outputs were scanned individually; scan canonical bytes once more.
        _safe_evidence_bytes(
            health_bytes.decode("utf-8"),
            known_secrets=known_secrets,
            relative_name="startup-health.json",
            maximum=HEALTH_DOCUMENT_MAX_BYTES,
        )
        retained.append(
            _write_private_evidence(run_root / "startup-health.json", health_bytes)
        )
    except Exception as exc:
        errors.append(f"startup-health.json: {exc}")
    return retained, errors


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
    return f"{stamp}-{os.getpid():x}"


def run(run_id: str, artifact_root: Path) -> int:
    resources = Resources.from_run_id(run_id)
    run_root = artifact_root.resolve() / run_id
    install_dir = validate_test_path(run_root / "install", run_root)
    data_path = validate_test_path(run_root / "bind-data", run_root)
    if run_root.exists():
        raise InstallerSmokeError("installer smoke artifact directory already exists")
    run_root.mkdir(parents=True, mode=0o700)
    ports = reserve_ports()
    image = f"odin-install-smoke:{run_id}"
    records: dict[str, dict[str, str]] = {}
    image_built = False
    iid_file_path: Path | None = None
    image_lifecycle = DisposableImageLifecycle(image, command=_image_command)
    started = time.monotonic()
    phases: list[dict[str, object]] = []
    startup_evidence: list[dict[str, object]] = []
    startup_evidence_errors: list[str] = []
    error: Exception | None = None
    try:
        assert_ports_free(ports)
        assert_absent(resources, install_dir, data_path)
        image_lifecycle.begin()
        with tempfile.NamedTemporaryFile(prefix="odin-install-smoke-iid-", delete=False) as handle:
            iid_file_path = Path(handle.name)
        iid_file_path.unlink()
        image_lifecycle.mark_build_attempted()
        try:
            _run([
                "docker", "build", "--iidfile", str(iid_file_path),
                *image_lifecycle.docker_build_owner_args(),
                "--label", f"{LABEL}={run_id}", "-t", image, ".",
            ], timeout=1800)
            image_built = True
            read_iidfile(iid_file_path)
            expected_image = verify_built_image(
                image, iid_file_path, command=_image_command
            )
            image_lifecycle.establish_final_image(expected_image)
            image_lifecycle.freeze_ownership()
        except Exception:
            if not image_lifecycle.ownership_capture_attempted:
                try:
                    image_lifecycle.freeze_ownership()
                except Exception:
                    pass
            raise
        env = os.environ.copy()
        env.update({
            "ODIN_INSTALL_TEST_MODE": "1", "ODIN_TEST_ROOT": str(run_root),
            "ODIN_TEST_RUN_ID": run_id, "ODIN_COMPOSE_PROJECT": resources.project,
            "ODIN_TEST_CONTAINER_NAME": resources.container, "ODIN_CONTAINER_NAME": resources.container,
            "ODIN_TEST_NETWORK_NAME": resources.network, "ODIN_TEST_VOLUME_NAME": resources.volume,
            "ODIN_TEST_DATA_PATH": str(data_path), "ODIN_INSTALL_DIR": str(install_dir),
            "ODIN_TEST_HTTP_PORT": str(ports[0]), "ODIN_HTTP_PORT": str(ports[0]),
            "ODIN_TEST_GO2RTC_PORT": str(ports[1]), "ODIN_GO2RTC_PORT": str(ports[1]),
            "ODIN_TEST_WEBRTC_PORT": str(ports[2]), "ODIN_WEBRTC_PORT": str(ports[2]),
            "ODIN_TEST_COMPOSE_SOURCE": str(ROOT / "install/docker-compose.test.yml"),
            "ODIN_COMPOSE_SOURCE": str(ROOT / "install/docker-compose.yml"),
            "ODIN_UPDATE_SOURCE": str(ROOT / "install/update.sh"),
            "ODIN_REPO": f"file://{ROOT}", "ODIN_IMAGE": image,
            "ODIN_SKIP_IMAGE_PULL": "1", "ODIN_SKIP_PUBLIC_CHECK": "1",
        })
        _run(["bash", str(ROOT / "install/install.sh")], env=env, cwd=run_root)
        records = record_owned(resources)
        actual_image = str(_inspect("container", resources.container).get("Image", ""))  # type: ignore[union-attr]
        if actual_image != expected_image:
            raise InstallerSmokeError("running container does not use the candidate image ID")
        phases.append({"name": "install", "status": "pass"})
        _run(["bash", str(install_dir / "update.sh"), "--force"], env=env, cwd=install_dir)
        if str(_inspect("container", resources.container).get("Image", "")) != expected_image:  # type: ignore[union-attr]
            raise InstallerSmokeError("update changed candidate image identity")
        phases.append({"name": "update", "status": "pass"})
    except Exception as exc:  # retain deterministic failure evidence, then clean only owned objects
        error = exc
        phases.append(
            {
                "name": "failure",
                "status": "fail",
                "detail": _safe_failure_detail(install_dir, str(exc)),
            }
        )
        try:
            startup_evidence, startup_evidence_errors = retain_startup_failure_evidence(
                run_root, install_dir, resources
            )
        except Exception as evidence_error:
            startup_evidence_errors = [
                f"startup evidence retention failed: {evidence_error}"
            ]
        phases.append(
            {
                "name": "startup-evidence",
                "status": "pass" if not startup_evidence_errors else "fail",
                "detail": (
                    f"retained {len(startup_evidence)} bounded startup artifacts"
                    if not startup_evidence_errors
                    else "; ".join(startup_evidence_errors)
                ),
            }
        )
    finally:
        try:
            if not records:
                records = discover_owned(resources)
            if records:
                cleanup_owned(resources, records)
            assert_zero_residue(resources)
            phases.append({"name": "cleanup", "status": "pass"})
        except Exception as cleanup_error:
            error = error or cleanup_error
            phases.append({"name": "cleanup", "status": "fail", "detail": str(cleanup_error)})
        image_cleanup_errors = image_lifecycle.finalize()
        if image_cleanup_errors:
            cleanup_error = InstallerSmokeError(
                "image cleanup failed: " + "; ".join(image_cleanup_errors)
            )
            error = error or cleanup_error
            phases.append(
                {"name": "image-cleanup", "status": "fail", "detail": str(cleanup_error)}
            )
        else:
            phases.append(
                {
                    "name": "image-cleanup",
                    "status": "pass",
                    "detail": (
                        "fixed disposable image tag/history cleaned"
                        if image_built
                        else "no image build completed; fixed post-attempt history cleaned or absent"
                    ),
                }
            )
        if iid_file_path is not None:
            try:
                iid_file_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                error = error or cleanup_error
                phases.append(
                    {
                        "name": "iid-cleanup",
                        "status": "fail",
                        "detail": str(cleanup_error),
                    }
                )
        for path in (install_dir, data_path):
            if path.exists():
                shutil.rmtree(path)
    manifest = {
        "schema_version": 1, "run_id": run_id, "status": "pass" if error is None else "fail",
        "duration_seconds": round(time.monotonic() - started, 3), "resources": records,
        "ports": ports, "candidate_image": image,
        "image_lifecycle": image_lifecycle.evidence(), "phases": phases,
        "startup_evidence": startup_evidence,
        "startup_evidence_errors": startup_evidence_errors,
    }
    (run_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(run_root / "manifest.json")
    if error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=os.getenv("ODIN_INSTALL_SMOKE_RUN_ID") or _default_run_id())
    parser.add_argument("--artifact-root", type=Path,
                        default=Path(os.getenv("ODIN_INSTALL_SMOKE_ARTIFACT_ROOT", ROOT / "artifacts/install-smoke")))
    args = parser.parse_args()
    return run(args.run_id, args.artifact_root)


if __name__ == "__main__":
    raise SystemExit(main())
