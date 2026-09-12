"""Deploy-parity contracts for liveness, readiness, and installer provenance."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]


def test_api_container_healthchecks_use_public_readiness_without_credentials() -> None:
    compose_files = (
        ROOT / "docker-compose.yml",
        ROOT / "docker-compose.enterprise.yml",
        ROOT / "install" / "docker-compose.yml",
        ROOT / "ops" / "demo" / "docker-compose.demo.yml",
    )
    for compose_file in compose_files:
        source = compose_file.read_text(encoding="utf-8")
        assert "http://localhost:8000/health/ready" in source, compose_file
        assert '"http://localhost:8000/health"' not in source, compose_file
        health_lines = [line for line in source.splitlines() if "localhost:8000/health" in line]
        assert health_lines, compose_file
        assert all("API_KEY" not in line and "Authorization" not in line for line in health_lines)

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "CMD curl -f http://localhost:8000/health/ready || exit 1" in dockerfile

    kubernetes = (ROOT / "ops" / "demo" / "k8s" / "40-odin.yaml").read_text(
        encoding="utf-8"
    )
    readiness, liveness = kubernetes.split("readinessProbe:", 1)[1].split(
        "livenessProbe:", 1
    )
    assert "path: /health/ready" in readiness
    assert "path: /health" in liveness


def test_distributed_compose_and_shell_installer_support_guarded_candidate_image() -> None:
    compose = (ROOT / "install" / "docker-compose.yml").read_text(encoding="utf-8")
    installer = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    updater = (ROOT / "install" / "update.sh").read_text(encoding="utf-8")

    assert "${ODIN_IMAGE:-ghcr.io/hughkantsime/odin:latest}" in compose
    assert 'ODIN_IMAGE="${ODIN_IMAGE:-ghcr.io/hughkantsime/odin:latest}"' in installer
    assert "ODIN_COMPOSE_SOURCE" in installer
    assert "ODIN_UPDATE_SOURCE" in installer
    assert "ODIN_SKIP_IMAGE_PULL" in installer
    assert "docker image inspect" in installer
    candidate_guard = updater.index('if [ "${ODIN_SKIP_IMAGE_PULL}" = "1" ]; then')
    already_current_exit = updater.index(
        'elif [ "$CURRENT_VERSION" = "$LATEST_VERSION" ] && [ "$FORCE" = false ]; then'
    )
    assert candidate_guard < already_current_exit


def test_install_smoke_builds_and_verifies_the_candidate_image() -> None:
    workflow = (ROOT / ".github" / "workflows" / "install-smoke.yml").read_text(encoding="utf-8")
    assert "docker build" in workflow
    assert "ODIN_COMPOSE_SOURCE=/install/docker-compose.yml" in workflow
    assert "ODIN_UPDATE_SOURCE=/install/update.sh" in workflow
    assert "ODIN_SKIP_IMAGE_PULL=1" in workflow
    assert "/health/ready" in workflow
    assert "docker inspect odin" in workflow
    assert "docker image inspect" in workflow
    assert "--add-host=host.docker.internal:host-gateway" in workflow
    assert "ODIN_READINESS_URL=http://host.docker.internal:8000/health/ready" in workflow
    pull_request_paths = workflow.split("workflow_dispatch:", 1)[0]
    assert "- 'Dockerfile'" in pull_request_paths


def test_installers_wait_for_ready_true() -> None:
    shell = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install" / "install.ps1").read_text(encoding="utf-8")
    assert (
        'ODIN_READINESS_URL="${ODIN_READINESS_URL:-http://localhost:8000/health/ready}"'
        in shell
    )
    assert 'curl -sf "${ODIN_READINESS_URL}"' in shell
    for source in (shell, powershell):
        assert "http://localhost:8000/health/ready" in source
        assert '"ready"' in source or "ready" in source


def test_installers_fail_closed_when_readiness_is_not_confirmed() -> None:
    shell = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install" / "install.ps1").read_text(encoding="utf-8")

    assert 'die "API readiness was not confirmed on localhost:8000"' in shell
    assert 'Stop-WithError "O.D.I.N. readiness was not confirmed within ${maxAttempts}s"' in powershell


FULL_APP_PROBE = textwrap.dedent(
    r"""
    import json
    import os
    from pathlib import Path
    import sqlite3

    from core.db import engine
    from core.schema import bootstrap_database
    import core.models  # noqa: F401
    import modules.archives.models  # noqa: F401
    import modules.inventory.models  # noqa: F401
    import modules.jobs.models  # noqa: F401
    import modules.models_library.models  # noqa: F401
    import modules.notifications.models  # noqa: F401
    import modules.orders.models  # noqa: F401
    import modules.printers.models  # noqa: F401
    import modules.system.models  # noqa: F401
    import modules.vision.models  # noqa: F401

    database_url = os.environ["DATABASE_URL"]
    bootstrap_database(engine, Path("backend"))

    from core.app import create_app
    from starlette.testclient import TestClient

    security_headers = (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Strict-Transport-Security",
    )
    app = create_app()
    with TestClient(app, base_url="http://school.test") as client:
        live = client.get("/health")
        assert live.status_code == 200, live.text
        assert live.json()["status"] == "ok"

        ready = client.get("/health/ready")
        assert ready.status_code == 200, ready.text
        assert ready.json()["ready"] is True
        for header in security_headers:
            assert header in ready.headers, header

        not_public = client.get("/health/not-a-probe")
        assert not_public.status_code == 401, not_public.text

        for api_ready in ("/api/health/ready", "/api/v1/health/ready"):
            rejected = client.get(api_ready)
            assert rejected.status_code == 401, (api_ready, rejected.text)
            assert rejected.headers["Cache-Control"] == "no-store"
            assert rejected.headers["Pragma"] == "no-cache"
            for header in security_headers:
                assert header in rejected.headers, (api_ready, header)
            accepted = client.get(api_ready, headers={"X-API-Key": "synthetic-edge-key"})
            assert accepted.status_code == 200, (api_ready, accepted.text)
            assert accepted.json()["ready"] is True

        openapi_rejected = client.get("/openapi.json")
        assert openapi_rejected.status_code == 401
        assert openapi_rejected.headers["Cache-Control"] == "no-store"
        openapi_allowed = client.get(
            "/openapi.json", headers={"X-API-Key": "synthetic-edge-key"}
        )
        assert openapi_allowed.status_code == 200, openapi_allowed.text
        assert openapi_allowed.headers["Cache-Control"] == "no-store"

        allowed_origin = "https://portal.school.test"
        allowed = client.options(
            "/api/health/ready",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.headers["Access-Control-Allow-Origin"] == allowed_origin
        denied = client.options(
            "/api/health/ready",
            headers={
                "Origin": "https://untrusted.test",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "Access-Control-Allow-Origin" not in denied.headers

        bad_host = client.get("/health/ready", headers={"Host": "untrusted.test"})
        assert bad_host.status_code == 400, bad_host.text

        with sqlite3.connect(os.environ["DATABASE_PATH"]) as connection:
            connection.execute("DROP TABLE users")
        unavailable = client.get("/health/ready")
        assert unavailable.status_code == 503, unavailable.text
        assert client.get("/health").status_code == 200
    """
)


def test_real_create_app_deploy_parity(tmp_path: Path) -> None:
    database = tmp_path / "odin.db"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": os.pathsep.join((str(ROOT / "backend"), str(ROOT))),
            "DATABASE_URL": f"sqlite:///{database}",
            "DATABASE_PATH": str(database),
            "JWT_SECRET_KEY": "synthetic-deploy-parity-jwt-secret",
            "ENCRYPTION_KEY": "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
            "API_KEY": "synthetic-edge-key",
            "TRUSTED_HOSTS": "school.test",
            "CORS_ORIGINS": "https://portal.school.test",
            "COOKIE_SECURE": "true",
            "COOKIE_SAMESITE": "strict",
            "LICENSE_DIR": str(tmp_path / "license"),
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", FULL_APP_PROBE],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"full create_app deploy-parity probe failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert database.exists()
