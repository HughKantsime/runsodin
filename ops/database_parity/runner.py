"""Build and verify one exact ODIN image on SQLite and PostgreSQL 16."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from ops.release_gate.policy import (
    GatePolicyError,
    inspect_junit,
    redact_text,
    scan_text_for_secrets,
)
from ops.release_control.validation_image_cleanup import (
    DisposableImageLifecycle,
    read_iidfile,
    verify_built_image,
)

from .report import render_report


ROOT = Path(__file__).parents[2]
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


def _command(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
    capture: bool = True,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise GatePolicyError(
            f"command timed out after {timeout}s: {' '.join(command[:4])}"
        ) from exc
    if check and result.returncode:
        raise GatePolicyError(
            f"command failed ({result.returncode}): {' '.join(command[:4])}\n"
            + (result.stdout or "")[-1600:]
        )
    return result


def _default_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower()
    commit = _command(["git", "rev-parse", "--short=8", "HEAD"]).stdout.strip()
    return f"{stamp}-{commit}-{secrets.token_hex(3)}"


def _phase(
    phases: list[dict[str, object]], name: str, started: float, detail: str
) -> None:
    phases.append(
        {
            "name": name,
            "status": "PASS",
            "duration_seconds": round(time.monotonic() - started, 3),
            "detail": detail,
        }
    )


def _docker_resources(run_id: str) -> set[str]:
    expected = {
        f"container:odin-pg-restore-{run_id}",
        f"container:odin-pg-api-{run_id}",
        f"container:odin-pg-worker-{run_id}",
        f"network:odin-pg-restore-net-{run_id}",
        f"volume:odin-pg-restore-data-{run_id}",
        f"volume:odin-pg-app-data-{run_id}",
        f"container:odin-sqlite-parity-{run_id}",
        f"volume:odin-sqlite-parity-data-{run_id}",
    }
    resources: set[str] = set()
    for kind, command in (
        ("container", ["docker", "container", "ls", "-a", "--format", "{{.Names}}"]),
        ("network", ["docker", "network", "ls", "--format", "{{.Name}}"]),
        ("volume", ["docker", "volume", "ls", "--format", "{{.Name}}"]),
    ):
        for name in _command(command).stdout.splitlines():
            resource = f"{kind}:{name}"
            if resource in expected:
                resources.add(resource)
    return resources


def _cleanup_resources(resources: set[str]) -> list[str]:
    errors: list[str] = []
    commands = {
        "container": ["docker", "rm", "-f"],
        "network": ["docker", "network", "rm"],
        "volume": ["docker", "volume", "rm"],
    }
    for kind in ("container", "network", "volume"):
        for resource in sorted(resources):
            resource_kind, name = resource.split(":", 1)
            if resource_kind != kind:
                continue
            try:
                result = subprocess.run(
                    [*commands[kind], name],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                    timeout=60,
                )
            except subprocess.TimeoutExpired:
                errors.append(f"{resource}: cleanup timed out after 60s")
                continue
            if result.returncode:
                errors.append(f"{resource}: {(result.stdout or '')[-300:]}")
    return errors


def _sanitize_and_assert_artifacts(
    artifact_dir: Path, known_secrets: list[str] | tuple[str, ...]
) -> None:
    text_suffixes = {".log", ".json", ".xml", ".html", ".txt"}
    for path in artifact_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in text_suffixes:
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        sanitized = redact_text(content, known_secrets)
        if sanitized != content:
            path.write_text(sanitized, encoding="utf-8")
        findings = scan_text_for_secrets(sanitized, known_secrets)
        if findings:
            raise GatePolicyError(
                f"retained database parity artifact contains secret material: "
                f"{path.name}: {', '.join(findings)}"
            )


def _assert_image_metadata(image: str) -> str:
    payload = json.loads(_command(["docker", "image", "inspect", image]).stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise GatePolicyError("candidate image inspect returned an unexpected payload")
    image_id = str(payload[0].get("Id", ""))
    environment = payload[0].get("Config", {}).get("Env", [])
    forbidden = ("ENCRYPTION_KEY=", "JWT_SECRET_KEY=", "API_KEY=", "DATABASE_PASSWORD=")
    if any(str(item).startswith(forbidden) for item in environment):
        raise GatePolicyError("candidate image metadata declares application credentials")
    database_values = [str(item) for item in environment if str(item).startswith("DATABASE_URL=")]
    if any(re.search(r"://[^/@:]+:[^/@]+@", value) for value in database_values):
        raise GatePolicyError("candidate image metadata contains a credential-bearing database URL")
    if not image_id.startswith("sha256:"):
        raise GatePolicyError("candidate image has no content ID")
    return image_id


def _generate_parity_secrets() -> dict[str, str]:
    return {
        "POSTGRES_ADMIN_PASSWORD": secrets.token_hex(24),
        "POSTGRES_APP_PASSWORD": secrets.token_hex(24),
        "POSTGRES_MAINTENANCE_PASSWORD": secrets.token_hex(24),
        "API_KEY": secrets.token_hex(32),
        "JWT_SECRET_KEY": secrets.token_hex(48),
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(
            "ascii"
        ),
        "ODIN_CANDIDATE_ADMIN_PASSWORD": (
            "Candidate-Admin-Aa1-" + secrets.token_hex(12)
        ),
        "ODIN_CANDIDATE_OPERATOR_PASSWORD": (
            "Candidate-Operator-Aa1-" + secrets.token_hex(12)
        ),
        "ODIN_CANDIDATE_VIEWER_PASSWORD": (
            "Candidate-Viewer-Aa1-" + secrets.token_hex(12)
        ),
    }


def _write_secret_bundle(values: dict[str, str]) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=".odin-db-parity-secrets.", dir=ROOT)
    path = Path(name)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)
    return path


def _extract_topology_evidence(output: str, expected_image_id: str) -> dict[str, str]:
    prefix = "database-parity-topology: "
    lines = [line for line in output.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise GatePolicyError("PostgreSQL drill did not emit exactly one topology record")
    try:
        evidence = json.loads(lines[0][len(prefix) :])
    except json.JSONDecodeError as exc:
        raise GatePolicyError("PostgreSQL topology evidence is invalid JSON") from exc
    required = {"api", "worker", "postgres_image_id", "postgres_digest"}
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise GatePolicyError("PostgreSQL topology evidence has an invalid shape")
    if evidence["api"] != expected_image_id or evidence["worker"] != expected_image_id:
        raise GatePolicyError("PostgreSQL ODIN role image identity mismatch")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(evidence["postgres_image_id"])):
        raise GatePolicyError("PostgreSQL image ID evidence is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(evidence["postgres_digest"])):
        raise GatePolicyError("PostgreSQL image digest evidence is invalid")
    return {key: str(value) for key, value in evidence.items()}


def _extract_database_evidence(output: str) -> dict[str, object]:
    prefix = "database-parity-evidence: "
    lines = [line for line in output.splitlines() if line.startswith(prefix)]
    if len(lines) != 1:
        raise GatePolicyError("database drill did not emit exactly one evidence record")
    try:
        evidence = json.loads(lines[0][len(prefix) :])
    except json.JSONDecodeError as exc:
        raise GatePolicyError("database drill evidence is invalid JSON") from exc
    required = {
        "dialect",
        "backup_size_bytes",
        "backup_duration_seconds",
        "validation_duration_seconds",
        "restore_duration_seconds",
        "table_count",
        "toc_entries",
        "toc_fingerprint",
        "schema_fingerprint",
        "relationship_graph_fingerprint",
    }
    if not isinstance(evidence, dict) or not required.issubset(evidence):
        raise GatePolicyError("database drill evidence is missing required measurements")
    for key in ("schema_fingerprint", "relationship_graph_fingerprint"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(evidence[key])):
            raise GatePolicyError(f"database drill has invalid {key}")
    toc = evidence["toc_fingerprint"]
    if toc is not None and not re.fullmatch(r"[0-9a-f]{64}", str(toc)):
        raise GatePolicyError("database drill has invalid toc_fingerprint")
    for key in (
        "backup_size_bytes",
        "backup_duration_seconds",
        "validation_duration_seconds",
        "restore_duration_seconds",
        "table_count",
        "toc_entries",
    ):
        if not isinstance(evidence[key], (int, float)) or evidence[key] < 0:
            raise GatePolicyError(f"database drill has invalid {key}")
    if evidence["backup_size_bytes"] <= 0 or evidence["table_count"] <= 0:
        raise GatePolicyError("database drill evidence contains empty backup measurements")
    return evidence


def _wait_for_container_ready(container: str) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        ready = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "curl",
                "-fsS",
                "http://localhost:8000/health/ready",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ready.returncode == 0:
            return
        state = _command(
            ["docker", "inspect", "--format", "{{.State.Running}}", container]
        ).stdout.strip()
        if state != "true":
            logs = _command(["docker", "logs", "--tail", "80", container]).stdout
            raise GatePolicyError(
                "exact-image SQLite container exited before readiness\n" + logs[-4000:]
            )
        time.sleep(0.25)
    raise GatePolicyError("exact-image SQLite container did not become ready")


def _run_sqlite_exact_image(
    image: str,
    image_id: str,
    run_id: str,
    parity_secrets: dict[str, str],
) -> tuple[str, dict[str, object]]:
    container = f"odin-sqlite-parity-{run_id}"
    volume = f"odin-sqlite-parity-data-{run_id}"
    descriptor, secret_name = tempfile.mkstemp(prefix=".odin-db-parity.", dir=ROOT)
    secret_file = Path(secret_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key in (
                "ODIN_CANDIDATE_ADMIN_PASSWORD",
                "ODIN_CANDIDATE_OPERATOR_PASSWORD",
                "ODIN_CANDIDATE_VIEWER_PASSWORD",
            ):
                handle.write(f"{key}={parity_secrets[key]}\n")
            handle.flush()
            os.fsync(handle.fileno())
        secret_file.chmod(0o600)
        _command(["docker", "volume", "create", volume])
        _command(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "-e",
                "DATABASE_URL=sqlite:////data/odin.db",
                "-e",
                "CORS_ORIGINS=http://school.test",
                "-e",
                "TRUSTED_HOSTS=school.test,localhost",
                "-e",
                "COOKIE_SECURE=false",
                "-e",
                "ODIN_EXPECTED_DATABASE_DIALECT=sqlite",
                "--mount",
                f"type=volume,src={volume},dst=/data",
                "--mount",
                f"type=bind,src={secret_file},dst=/run/secrets/runtime_probe,readonly",
                "--mount",
                (
                    "type=bind,src="
                    f"{ROOT / 'ops/database_parity/runtime_probe.py'},"
                    "dst=/workspace/runtime_probe.py,readonly"
                ),
                "--mount",
                (
                    "type=bind,src="
                    f"{ROOT / 'ops/database_parity/sqlite_restore_drill.py'},"
                    "dst=/workspace/sqlite_restore_drill.py,readonly"
                ),
                image,
            ]
        )
        _wait_for_container_ready(container)

        running_image = _command(
            ["docker", "inspect", "--format", "{{.Image}}", container]
        ).stdout.strip()
        if running_image != image_id:
            raise GatePolicyError(
                f"SQLite candidate image mismatch: built={image_id} running={running_image}"
            )
        probe = _command(
            [
                "docker",
                "exec",
                container,
                "/bin/bash",
                "-c",
                (
                    "set -a; source /run/secrets/runtime_probe; set +a; "
                    "read -r JWT_SECRET_KEY < /data/.jwt_secret; "
                    "read -r ENCRYPTION_KEY < /data/.encryption_key; "
                    "export JWT_SECRET_KEY ENCRYPTION_KEY; "
                    "cd /app/backend; "
                    "export PYTHONPATH=/app/backend; "
                    "exec python3 /workspace/runtime_probe.py"
                ),
            ]
        )
        if "sqlite-runtime: PASS" not in probe.stdout:
            raise GatePolicyError("exact-image SQLite runtime probe did not pass")
        staged = _command(
            [
                "docker",
                "exec",
                container,
                "/bin/bash",
                "-c",
                (
                    "cd /app/backend; export PYTHONPATH=/app/backend; "
                    "exec python3 /workspace/sqlite_restore_drill.py stage"
                ),
            ]
        )
        evidence = _extract_database_evidence(staged.stdout)
        restore_started = time.monotonic()
        _command(["docker", "restart", container])
        _wait_for_container_ready(container)
        evidence["restore_duration_seconds"] = round(
            time.monotonic() - restore_started, 3
        )
        restarted_image = _command(
            ["docker", "inspect", "--format", "{{.Image}}", container]
        ).stdout.strip()
        if restarted_image != image_id:
            raise GatePolicyError("restarted SQLite container changed candidate image")
        verified = _command(
            [
                "docker",
                "exec",
                "-e",
                (
                    "ODIN_EXPECTED_GRAPH_FINGERPRINT="
                    + str(evidence["relationship_graph_fingerprint"])
                ),
                container,
                "/bin/bash",
                "-c",
                (
                    "cd /app/backend; export PYTHONPATH=/app/backend; "
                    "python3 /workspace/sqlite_restore_drill.py verify; "
                    "set -a; source /run/secrets/runtime_probe; set +a; "
                    "read -r JWT_SECRET_KEY < /data/.jwt_secret; "
                    "read -r ENCRYPTION_KEY < /data/.encryption_key; "
                    "export JWT_SECRET_KEY ENCRYPTION_KEY ODIN_PARITY_MODE=verify-restored; "
                    "exec python3 /workspace/runtime_probe.py"
                ),
            ]
        )
        if "sqlite-restored-runtime: PASS" not in verified.stdout:
            raise GatePolicyError("restored SQLite runtime probe did not pass")
        return probe.stdout + staged.stdout + verified.stdout, evidence
    finally:
        secret_file.unlink(missing_ok=True)
        _cleanup_resources(
            {f"container:{container}", f"volume:{volume}"}
        )


def run(run_id: str, artifact_root: Path, image: str) -> int:
    if not RUN_ID.fullmatch(run_id):
        raise GatePolicyError("invalid database parity run ID")
    artifact_dir = artifact_root / run_id
    artifact_dir.mkdir(parents=True, exist_ok=False)
    phases: list[dict[str, object]] = []
    database_evidence: list[dict[str, object]] = []
    topology_evidence: list[dict[str, object]] = []
    parity_secrets = _generate_parity_secrets()
    known_secrets = list(parity_secrets.values())
    secret_bundle = _write_secret_bundle(parity_secrets)
    error: BaseException | None = None
    image_id = ""
    owned_image_id: str | None = None
    iid_file_path: Path | None = None
    image_ownership_failed = False
    image_lifecycle = DisposableImageLifecycle(image, command=_command)
    active_phase = "preflight"
    preexisting = _docker_resources(run_id)
    try:
        started = time.monotonic()
        _command([sys.executable, "-c", "import pytest, sqlalchemy"])
        _command(
            [
                sys.executable,
                "-m",
                "ops.database_parity.sqlite_compatibility_inventory",
            ]
        )
        if preexisting:
            raise GatePolicyError(
                "pre-existing database parity containers: " + ", ".join(sorted(preexisting))
            )
        image_lifecycle.begin()
        _phase(
            phases,
            active_phase,
            started,
            "dependencies available; SQLite inventory current; isolated namespace and image tag clean",
        )

        active_phase = "candidate-build"
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(prefix="odin-dbparity-iid-", delete=False) as handle:
            iid_file_path = Path(handle.name)
        iid_file_path.unlink()
        image_lifecycle.mark_build_attempted()
        try:
            build = _command(
                [
                    "docker", "build", "--pull", "--iidfile", str(iid_file_path),
                    *image_lifecycle.docker_build_owner_args(),
                    "-t", image, ".",
                ]
            )
            (artifact_dir / "build.log").write_text(
                redact_text(build.stdout, known_secrets), encoding="utf-8"
            )
            read_iidfile(iid_file_path)
            image_id = verify_built_image(image, iid_file_path, command=_command)
            owned_image_id = image_id
            image_lifecycle.establish_final_image(image_id)
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
        if _assert_image_metadata(image) != image_id:
            raise GatePolicyError("candidate image metadata identity changed after build")
        client_probe = _command(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "/bin/bash",
                image,
                "-c",
                "python3 -c 'import psycopg; print(psycopg.__version__)' && pg_restore --version",
            ]
        )
        (artifact_dir / "image-database-tools.log").write_text(
            redact_text(client_probe.stdout, known_secrets), encoding="utf-8"
        )
        if "3.3.5" not in client_probe.stdout or "pg_restore (PostgreSQL) 16." not in client_probe.stdout:
            raise GatePolicyError("candidate image has unexpected PostgreSQL driver/client versions")
        _phase(
            phases,
            active_phase,
            started,
            f"built exact image {image_id}; psycopg 3.3.5, PostgreSQL 16 client, and metadata credential scan passed",
        )

        active_phase = "sqlite-exact-image"
        started = time.monotonic()
        sqlite_runtime, sqlite_evidence = _run_sqlite_exact_image(
            image, image_id, run_id, parity_secrets
        )
        sqlite_evidence["attempt"] = 1
        database_evidence.append(sqlite_evidence)
        (artifact_dir / "sqlite-runtime.log").write_text(
            redact_text(sqlite_runtime, known_secrets), encoding="utf-8"
        )
        _phase(
            phases,
            active_phase,
            started,
            (
                "booted the exact image through its entrypoint on a fresh volume; "
                "readiness, setup, personas, RBAC, CRUD, WebSocket, and background "
                "database writes and relational backup/restore/restart passed"
            ),
        )

        active_phase = "host-contracts"
        started = time.monotonic()
        junit = artifact_dir / "sqlite-junit.xml"
        sqlite = _command(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/database_parity/test_legacy_upgrade_fixtures.py",
                "tests/test_contracts/test_database_bootstrap_foundation.py",
                "tests/test_contracts/test_schema_consistency.py",
                "tests/backup_restore/test_backup_service.py",
                "tests/backup_restore/test_postgres_backup_service.py",
                "-q",
                "-o",
                "xfail_strict=true",
                f"--junitxml={junit}",
            ]
        )
        (artifact_dir / "host-contracts.log").write_text(
            redact_text(sqlite.stdout, known_secrets), encoding="utf-8"
        )
        totals = inspect_junit(junit)
        _phase(
            phases,
            active_phase,
            started,
            f"{totals['passed']} source and provider assertions; zero skips/xfails",
        )

        drill_env = os.environ.copy()
        drill_env["ODIN_PARITY_IMAGE"] = image_id
        drill_env["ODIN_PARITY_IMAGE_ID"] = image_id
        drill_env["ODIN_PARITY_SECRET_INPUT_FILE"] = str(secret_bundle)
        drill_env["ODIN_PARITY_RESOURCE_SUFFIX"] = run_id
        for attempt in (1, 2):
            active_phase = f"postgres-exact-image-{attempt}"
            started = time.monotonic()
            drill = _command(["./ops/database_parity/run_restore_drill.sh"], env=drill_env)
            (artifact_dir / f"postgres-{attempt}.log").write_text(
                redact_text(drill.stdout, known_secrets), encoding="utf-8"
            )
            required = (
                "postgresql-runtime: PASS",
                "postgres-legacy-upgrades: PASS fixtures=5",
                "postgres-offline-restore: PASS",
            )
            if not all(marker in drill.stdout for marker in required):
                raise GatePolicyError("PostgreSQL drill did not emit all required evidence")
            postgres_evidence = _extract_database_evidence(drill.stdout)
            postgres_evidence["attempt"] = attempt
            database_evidence.append(postgres_evidence)
            topology = _extract_topology_evidence(drill.stdout, image_id)
            topology["attempt"] = attempt
            topology_evidence.append(topology)
            _phase(
                phases,
                active_phase,
                started,
                "API/RBAC/WebSocket/background writes, five legacy upgrades, malformed refusal, least-privilege roles, and restore recovery passed",
            )

        active_phase = "cleanup"
        started = time.monotonic()
        remaining = _docker_resources(run_id) - preexisting
        if remaining:
            raise GatePolicyError(
                "database parity containers leaked: " + ", ".join(sorted(remaining))
            )
        _phase(phases, active_phase, started, "all disposable database resources removed")
    except BaseException as exc:
        error = exc
        phases.append(
            {
                "name": active_phase,
                "status": "FAIL",
                "duration_seconds": 0,
                "detail": redact_text(str(exc)[-1600:], known_secrets),
            }
        )
    finally:
        resource_cleanup_errors: list[str] = []
        try:
            secret_bundle.unlink(missing_ok=True)
        except OSError as exc:
            resource_cleanup_errors.append(f"secret file cleanup failed: {exc}")
        if iid_file_path is not None:
            try:
                iid_file_path.unlink(missing_ok=True)
            except OSError as exc:
                resource_cleanup_errors.append(f"IID file cleanup failed: {exc}")
        try:
            leaked = _docker_resources(run_id) - preexisting
            if leaked:
                resource_cleanup_errors.extend(_cleanup_resources(leaked))
                still_present = _docker_resources(run_id) - preexisting
                resource_cleanup_errors.extend(sorted(still_present))
                if not resource_cleanup_errors:
                    phases.append(
                        {
                            "name": "cleanup-recovery",
                            "status": "PASS",
                            "duration_seconds": 0,
                            "detail": "removed resources left by an interrupted drill",
                        }
                    )
        except Exception as exc:
            resource_cleanup_errors.append(f"resource cleanup observation failed: {exc}")
        if resource_cleanup_errors:
            cleanup_failure = GatePolicyError(
                "database parity cleanup recovery failed: "
                + "; ".join(resource_cleanup_errors)
            )
            error = cleanup_failure
            phases.append(
                {
                    "name": "cleanup-recovery",
                    "status": "FAIL",
                    "duration_seconds": 0,
                    "detail": str(cleanup_failure),
                }
            )
        image_cleanup_errors: list[str] = []
        if image_ownership_failed:
            image_cleanup_errors.append(
                "image ownership verification failed; tag deletion was not attempted"
            )
        image_cleanup_errors.extend(image_lifecycle.finalize())
        if image_cleanup_errors:
            cleanup_failure = GatePolicyError(
                "database parity image cleanup failed: "
                + "; ".join(image_cleanup_errors)
            )
            error = cleanup_failure
            phases.append(
                {
                    "name": "cleanup-image",
                    "status": "FAIL",
                    "duration_seconds": 0,
                    "detail": str(cleanup_failure),
                }
            )
        else:
            phases.append(
                {
                    "name": "cleanup-image",
                    "status": "PASS",
                    "duration_seconds": 0,
                    "detail": (
                        "exact owned image tag removed"
                        if owned_image_id is not None
                        else "no owned image tag established or removed"
                    ),
                }
            )
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "status": "FAIL" if error else "PASS",
            "commit": _command(["git", "rev-parse", "HEAD"]).stdout.strip(),
            "dirty": bool(_command(["git", "status", "--porcelain"]).stdout.strip()),
            "candidate_image": image,
            "candidate_image_id": image_id,
            "image_lifecycle": image_lifecycle.evidence(),
            "phases": phases,
            "database_evidence": database_evidence,
            "topology_evidence": topology_evidence,
            "runtime_posture": {
                "network": "disposable isolated Docker resources; no host port published",
                "authentication": (
                    "SQLite API_KEY intentionally unset to exercise session/JWT enforcement; "
                    "PostgreSQL uses an ephemeral API key plus session/JWT enforcement"
                ),
                "classified_warnings": [
                    "TestClient duplicates go2rtc initialization inside the running SQLite container",
                    "passlib probes a removed bcrypt version metadata attribute before using bcrypt successfully",
                ],
            },
        }
        (artifact_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        render_report(manifest, artifact_dir / "index.html")
        try:
            _sanitize_and_assert_artifacts(artifact_dir, known_secrets)
        except BaseException as exc:
            error = exc
            manifest["status"] = "FAIL"
            phases.append(
                {
                    "name": "artifact-sanitization",
                    "status": "FAIL",
                    "duration_seconds": 0,
                    "detail": str(exc),
                }
            )
            (artifact_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            render_report(manifest, artifact_dir / "index.html")

    print(f"ODIN database parity {'FAIL' if error else 'PASS'}: {artifact_dir / 'index.html'}")
    if error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default=os.getenv("ODIN_DATABASE_PARITY_RUN_ID"))
    parser.add_argument(
        "--artifact-root",
        default=os.getenv("ODIN_DATABASE_PARITY_ARTIFACT_ROOT", "artifacts/database-parity"),
    )
    parser.add_argument("--image", default=os.getenv("ODIN_PARITY_IMAGE"))
    args = parser.parse_args()
    run_id = args.run_id or _default_run_id()
    image = args.image or f"odin-dbparity:{run_id}"
    return run(run_id, Path(args.artifact_root), image)


if __name__ == "__main__":
    raise SystemExit(main())
