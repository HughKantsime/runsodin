"""Build and exercise one isolated ODIN candidate image."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException, RemoteDisconnected
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

from cryptography.fernet import Fernet

from ops.release_control.validation_image_cleanup import (
    DisposableImageLifecycle,
    read_iidfile,
    verify_built_image,
)

from .policy import (
    GatePolicyError,
    assert_candidate_suite_has_no_skip_mechanisms,
    inspect_junit,
    redact_text,
    scan_text_for_secrets,
)
from .report import render_report


_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def validate_run_id(run_id: str) -> str:
    """Validate a value before it can become part of a Docker resource name."""
    if not _RUN_ID.fullmatch(run_id):
        raise GatePolicyError(
            "run ID must be 3-64 lowercase ASCII letters, digits, or hyphens"
        )
    return run_id


@dataclass(frozen=True)
class ResourceNames:
    container: str
    network: str
    volume: str

    @classmethod
    def from_run_id(cls, run_id: str) -> "ResourceNames":
        safe = validate_run_id(run_id)
        prefix = f"odin-candidate-{safe}"
        return cls(container=prefix, network=prefix, volume=f"{prefix}-data")

    @property
    def all(self) -> tuple[str, str, str]:
        return (self.container, self.network, self.volume)


@dataclass(frozen=True)
class GateSecrets:
    api_key: str
    jwt_secret: str
    encryption_key: str
    admin_password: str
    operator_password: str
    viewer_password: str

    @classmethod
    def generate(cls) -> "GateSecrets":
        def password(role: str) -> str:
            return f"Candidate-{role}-Aa1!-{secrets.token_urlsafe(18)}"

        return cls(
            api_key=secrets.token_urlsafe(36),
            jwt_secret=secrets.token_urlsafe(48),
            encryption_key=Fernet.generate_key().decode("ascii"),
            admin_password=password("Admin"),
            operator_password=password("Operator"),
            viewer_password=password("Viewer"),
        )

    @property
    def values(self) -> tuple[str, ...]:
        return (
            self.api_key,
            self.jwt_secret,
            self.encryption_key,
            self.admin_password,
            self.operator_password,
            self.viewer_password,
        )


def _command(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=Path(__file__).parents[2],
        env=env,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=False,
    )
    if check and completed.returncode != 0:
        detail = f" (exit {completed.returncode})"
        if capture and completed.stdout:
            detail += ": " + completed.stdout[-1200:]
        raise GatePolicyError(f"command failed{detail}: {' '.join(args[:4])}")
    return completed


def _git(*args: str) -> str:
    return _command(["git", *args], capture=True).stdout.strip()


def _default_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
    commit = _git("rev-parse", "--short=8", "HEAD").lower()
    entropy = secrets.token_hex(4)
    return validate_run_id(f"{timestamp}-{commit}-{entropy}")


def _validate_image_tag(image: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]{0,127}:[a-z0-9][a-z0-9._-]{0,63}", image):
        raise GatePolicyError("candidate image tag contains unsupported characters")
    return image


def _write_env_file(path: Path, values: dict[str, str]) -> None:
    if any("\n" in value or "\r" in value for value in values.values()):
        raise GatePolicyError("candidate environment values must be single-line")
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    path.chmod(0o600)


def _loopback_json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: int = 15,
) -> tuple[int, dict[str, object]]:
    """Request JSON from the dynamically mapped candidate port on literal loopback only."""
    parsed = urlparse(base_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise GatePolicyError("candidate base URL has an invalid port") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise GatePolicyError("candidate base URL must be an origin on http://127.0.0.1:<port>")
    if not re.fullmatch(r"/[A-Za-z0-9_/-]+", path):
        raise GatePolicyError("candidate request path is invalid")

    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise GatePolicyError("candidate JSON response must be an object")
        return response.status, result
    finally:
        connection.close()


def _wait_ready(base_url: str, timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            status, payload = _loopback_json_request(base_url, "/health/ready", timeout=3)
            if status == 200 and payload.get("ready") is True:
                return
            last_error = f"status={status} payload={payload}"
        except (HTTPException, OSError, RemoteDisconnected, TimeoutError, json.JSONDecodeError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise GatePolicyError(f"candidate readiness timeout: {last_error}")


def _claim_setup(base_url: str, password: str) -> None:
    try:
        status, payload = _loopback_json_request(
            base_url,
            "/api/setup/admin",
            method="POST",
            payload={
                "username": "candidate-admin@example.invalid",
                "email": "candidate-admin@example.invalid",
                "password": password,
                "role": "admin",
            },
        )
    except (HTTPException, OSError, RemoteDisconnected, TimeoutError, json.JSONDecodeError) as exc:
        raise GatePolicyError(f"setup claim request failed: {exc}") from exc
    if status != 200:
        raise GatePolicyError(f"setup claim returned HTTP {status}")
    if not payload.get("access_token"):
        raise GatePolicyError("setup claim did not return an access token")


def _phase(phases: list[dict[str, object]], name: str, started: float, detail: str) -> None:
    phases.append(
        {
            "name": name,
            "status": "PASS",
            "detail": detail,
            "duration_seconds": round(time.monotonic() - started, 3),
        }
    )


def _failure(phases: list[dict[str, object]], name: str, exc: BaseException) -> None:
    phases.append({"name": name, "status": "FAIL", "detail": str(exc)[:1200]})


def _cleanup(resources: ResourceNames) -> list[str]:
    validate_run_id(resources.container.removeprefix("odin-candidate-"))
    errors: list[str] = []
    for args in (
        ["docker", "rm", "-f", resources.container],
        ["docker", "network", "rm", resources.network],
        ["docker", "volume", "rm", resources.volume],
    ):
        completed = _command(args, capture=True, check=False)
        output = completed.stdout or ""
        absent = "No such" in output or "not found" in output.lower()
        if completed.returncode != 0 and not absent:
            errors.append(f"{' '.join(args[:3])}: {output[-500:].strip()}")
    return errors


def _assert_resources_absent(resources: ResourceNames) -> None:
    """Refuse to reuse or remove Docker resources not created by this run."""
    existing: list[str] = []
    for kind, name, command in (
        (
            "container",
            resources.container,
            ["docker", "container", "ls", "-a", "--format", "{{.Names}}"],
        ),
        ("network", resources.network, ["docker", "network", "ls", "--format", "{{.Name}}"]),
        ("volume", resources.volume, ["docker", "volume", "ls", "--format", "{{.Name}}"]),
    ):
        output = _command(command, capture=True).stdout or ""
        if name in output.splitlines():
            existing.append(f"{kind}:{name}")
    if existing:
        raise GatePolicyError("candidate Docker resources already exist: " + ", ".join(existing))


def _scan_artifacts(artifact_dir: Path, known_secrets: Iterable[str]) -> list[str]:
    findings: list[str] = []
    text_suffixes = {".json", ".xml", ".html", ".log", ".txt"}
    values = tuple(known_secrets)
    for path in sorted(artifact_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in text_suffixes:
            labels = scan_text_for_secrets(path.read_text(encoding="utf-8", errors="replace"), values)
            findings.extend(f"{path.name}: {label}" for label in labels)
    return findings


def run_gate(run_id: str, artifact_root: Path, candidate_image: str) -> int:
    repo_root = Path(__file__).parents[2]
    run_id = validate_run_id(run_id)
    candidate_image = _validate_image_tag(candidate_image)
    resources = ResourceNames.from_run_id(run_id)
    artifact_dir = artifact_root / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    (artifact_dir / "playwright").mkdir()
    secrets_for_run = GateSecrets.generate()
    phases: list[dict[str, object]] = []
    fixture_counts: dict[str, int] = {}
    candidate_image_id = ""
    running_image_id = ""
    active_phase = "preflight"
    run_error: BaseException | None = None
    env_file_path: Path | None = None
    iid_file_path: Path | None = None
    owns_resources = False
    owned_image_id: str | None = None
    image_ownership_failed = False
    image_lifecycle = DisposableImageLifecycle(candidate_image, command=_command)

    try:
        started = time.monotonic()
        if shutil.which("docker") is None:
            raise GatePolicyError("docker is required")
        assert_candidate_suite_has_no_skip_mechanisms(repo_root / "tests" / "candidate_gate")
        _command([sys.executable, "-c", "import pytest, requests, playwright, websocket"])
        _assert_resources_absent(resources)
        image_lifecycle.begin()
        owns_resources = True
        _phase(
            phases,
            "preflight",
            started,
            "Docker and host test dependencies available; no skip mechanisms, resources, or image tag",
        )

        active_phase = "candidate-build"
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(prefix="odin-candidate-iid-", delete=False) as handle:
            iid_file_path = Path(handle.name)
        iid_file_path.unlink()
        image_lifecycle.mark_build_attempted()
        try:
            _command(
                [
                    "docker", "build", "--pull", "--iidfile", str(iid_file_path),
                    *image_lifecycle.docker_build_owner_args(),
                    "-t", candidate_image, ".",
                ]
            )
            read_iidfile(iid_file_path)
            candidate_image_id = verify_built_image(
                candidate_image, iid_file_path, command=_command
            )
            owned_image_id = candidate_image_id
            image_lifecycle.establish_final_image(candidate_image_id)
            image_lifecycle.freeze_ownership()
        except BaseException:
            if not image_lifecycle.ownership_capture_attempted:
                try:
                    image_lifecycle.freeze_ownership()
                except Exception:
                    pass
            if not image_lifecycle.final_image_id:
                image_ownership_failed = True
            raise
        _phase(phases, active_phase, started, f"built {candidate_image} as {candidate_image_id}")

        active_phase = "candidate-boot"
        started = time.monotonic()
        _command(["docker", "network", "create", resources.network])
        _command(["docker", "volume", "create", resources.volume])
        with tempfile.NamedTemporaryFile(prefix="odin-candidate-env-", delete=False) as handle:
            env_file_path = Path(handle.name)
        _write_env_file(
            env_file_path,
            {
                "DATABASE_URL": "sqlite:////data/odin.db",
                "ENCRYPTION_KEY": secrets_for_run.encryption_key,
                "JWT_SECRET_KEY": secrets_for_run.jwt_secret,
                "API_KEY": secrets_for_run.api_key,
                "CORS_ORIGINS": "http://127.0.0.1",
                "TRUSTED_HOSTS": "127.0.0.1,localhost",
                "COOKIE_SECURE": "false",
                "ODIN_RELEASE_GATE": "1",
                "ODIN_RELEASE_GATE_RUN_ID": run_id,
                "ODIN_CANDIDATE_ADMIN_PASSWORD": secrets_for_run.admin_password,
                "ODIN_CANDIDATE_OPERATOR_PASSWORD": secrets_for_run.operator_password,
                "ODIN_CANDIDATE_VIEWER_PASSWORD": secrets_for_run.viewer_password,
            },
        )
        _command(
            [
                "docker", "run", "-d", "--name", resources.container,
                "--hostname", resources.container,
                "--network", resources.network,
                "--env-file", str(env_file_path),
                "-p", "127.0.0.1::8000",
                "-v", f"{resources.volume}:/data",
                candidate_image,
            ]
        )
        port_output = _command(
            ["docker", "port", resources.container, "8000/tcp"], capture=True
        ).stdout.strip()
        match = re.search(r"127\.0\.0\.1:(\d+)$", port_output)
        if not match:
            raise GatePolicyError(f"unable to resolve candidate loopback port: {port_output}")
        base_url = f"http://127.0.0.1:{match.group(1)}"
        _wait_ready(base_url)
        running_image_id = _command(
            ["docker", "inspect", "--format", "{{.Image}}", resources.container], capture=True
        ).stdout.strip()
        if running_image_id != candidate_image_id:
            raise GatePolicyError(
                f"candidate image identity mismatch: built={candidate_image_id} running={running_image_id}"
            )
        _phase(phases, active_phase, started, f"ready at loopback port {match.group(1)}; image identity matched")

        active_phase = "setup-and-seed"
        started = time.monotonic()
        _claim_setup(base_url, secrets_for_run.admin_password)
        seed_output = _command(
            [
                "docker", "exec", resources.container, "python3",
                "/app/backend/scripts/seed_release_gate.py", "--run-id", run_id,
            ],
            capture=True,
        ).stdout.strip()
        try:
            seed_payload = json.loads(seed_output.splitlines()[-1])
            fixture_counts = seed_payload["counts"]
        except (IndexError, KeyError, json.JSONDecodeError, TypeError) as exc:
            raise GatePolicyError("candidate seed did not emit a valid manifest") from exc
        _phase(phases, active_phase, started, f"seeded {sum(fixture_counts.values())} fixture rows")

        test_env = os.environ.copy()
        test_env.update(
            {
                "ADMIN_USERNAME": "candidate-admin@example.invalid",
                "ADMIN_PASSWORD": secrets_for_run.admin_password,
                "ODIN_CANDIDATE_BASE_URL": base_url,
                "ODIN_CANDIDATE_API_KEY": secrets_for_run.api_key,
                "ODIN_CANDIDATE_ADMIN_PASSWORD": secrets_for_run.admin_password,
                "ODIN_CANDIDATE_OPERATOR_PASSWORD": secrets_for_run.operator_password,
                "ODIN_CANDIDATE_VIEWER_PASSWORD": secrets_for_run.viewer_password,
                "ODIN_CANDIDATE_ARTIFACT_DIR": str(artifact_dir / "playwright"),
            }
        )

        active_phase = "candidate-api-rbac"
        started = time.monotonic()
        api_junit = artifact_dir / "api-junit.xml"
        _command(
            [
                sys.executable, "-m", "pytest", "tests/candidate_gate/test_api.py",
                "--confcutdir=tests/candidate_gate", "-v", "--tb=short",
                "-o", "xfail_strict=true", f"--junitxml={api_junit}",
            ],
            env=test_env,
        )
        api_totals = inspect_junit(api_junit)
        _phase(phases, active_phase, started, f"{api_totals['passed']} passed; zero skips/xfails")

        active_phase = "candidate-playwright"
        started = time.monotonic()
        browser_junit = artifact_dir / "playwright-junit.xml"
        _command(
            [
                sys.executable, "-m", "pytest", "tests/candidate_gate/test_ui.py",
                "--confcutdir=tests/candidate_gate", "-v", "--tb=short",
                "-o", "xfail_strict=true", f"--junitxml={browser_junit}",
            ],
            env=test_env,
        )
        browser_totals = inspect_junit(browser_junit)
        _phase(phases, active_phase, started, f"{browser_totals['passed']} passed; zero skips/xfails")
    except BaseException as exc:
        run_error = exc
        _failure(phases, active_phase, exc)
    finally:
        cleanup_errors: list[str] = []
        if env_file_path is not None:
            try:
                env_file_path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(f"environment file cleanup failed: {exc}")
        if iid_file_path is not None:
            try:
                iid_file_path.unlink(missing_ok=True)
            except OSError as exc:
                cleanup_errors.append(f"IID file cleanup failed: {exc}")
        retained_logs: dict[str, str] = {}
        if owns_resources:
            try:
                logs = _command(
                    ["docker", "logs", "--tail", "700", resources.container],
                    capture=True,
                    check=False,
                ).stdout or ""
                retained_logs["candidate.log"] = logs
                for service_log in (
                    "backend.log",
                    "mqtt_monitor.log",
                    "moonraker_monitor.log",
                    "prusalink_monitor.log",
                    "elegoo_monitor.log",
                    "vision_monitor.log",
                ):
                    service_output = _command(
                        [
                            "docker", "exec", resources.container, "tail", "-n", "1500",
                            f"/data/{service_log}",
                        ],
                        capture=True,
                        check=False,
                    )
                    if service_output.returncode == 0 and service_output.stdout:
                        retained_logs[service_log] = service_output.stdout
            except Exception as exc:
                cleanup_errors.append(f"log retention failed: {exc}")

        if owns_resources:
            try:
                cleanup_errors.extend(_cleanup(resources))
            except Exception as exc:
                cleanup_errors.append(f"resource cleanup failed: {exc}")
        if image_ownership_failed:
            cleanup_errors.append(
                "image ownership verification failed; tag deletion was not attempted"
            )
        cleanup_errors.extend(image_lifecycle.finalize())
        try:
            for log_name, log_text in retained_logs.items():
                (artifact_dir / log_name).write_text(
                    redact_text(log_text, secrets_for_run.values), encoding="utf-8"
                )
        except OSError as exc:
            cleanup_errors.append(f"retained log write failed: {exc}")
        if cleanup_errors:
            cleanup_error = GatePolicyError("candidate cleanup failed: " + "; ".join(cleanup_errors))
            if run_error is None:
                run_error = cleanup_error
            _failure(phases, "cleanup", cleanup_error)
        else:
            detail = (
                "exact disposable resources and owned image tag removed"
                if owned_image_id is not None
                else "no owned image tag created; disposable resources removed or absent"
            )
            phases.append({"name": "cleanup", "status": "PASS", "detail": detail})

        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "FAIL" if run_error else "PASS",
            "commit": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")),
            "candidate_image": candidate_image,
            "candidate_image_id": candidate_image_id,
            "running_image_id": running_image_id,
            "image_lifecycle": image_lifecycle.evidence(),
            "fixtures": fixture_counts,
            "phases": phases,
        }
        manifest_path = artifact_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        render_report(manifest, artifact_dir / "index.html")
        final_findings = _scan_artifacts(artifact_dir, secrets_for_run.values)
        if final_findings:
            run_error = GatePolicyError("retained artifacts failed secret scan: " + "; ".join(final_findings))
            manifest["status"] = "FAIL"
            manifest["phases"].append(
                {"name": "artifact-sanitization", "status": "FAIL", "detail": str(run_error)}
            )
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            render_report(manifest, artifact_dir / "index.html")

    print(f"ODIN candidate gate {'FAIL' if run_error else 'PASS'}: {artifact_dir / 'index.html'}")
    if run_error:
        print(str(run_error), file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ODIN full-stack candidate gate")
    parser.add_argument("--run-id", default=os.environ.get("ODIN_CANDIDATE_RUN_ID"))
    parser.add_argument(
        "--artifact-root",
        default=os.environ.get("ODIN_CANDIDATE_ARTIFACT_ROOT", "artifacts/candidate-gate"),
    )
    parser.add_argument("--image", default=os.environ.get("ODIN_CANDIDATE_IMAGE"))
    args = parser.parse_args(argv)
    run_id = validate_run_id(args.run_id) if args.run_id else _default_run_id()
    image = args.image or f"odin-candidate:{run_id}"
    return run_gate(run_id, Path(args.artifact_root), image)


if __name__ == "__main__":
    raise SystemExit(main())
