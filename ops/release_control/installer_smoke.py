"""Run the Unix installer/update flow with uniquely owned Docker resources."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ops.release_control.validation_image_cleanup import (
    DisposableImageLifecycle,
    read_iidfile,
    verify_built_image,
)

ROOT = Path(__file__).parents[2]
LABEL = "com.runsodin.install-smoke"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,62}$")


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
        phases.append({"name": "failure", "status": "fail", "detail": str(exc)})
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
