from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ops.release_control import mutation_workflow as mutation


def _run(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, check=check)


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.integration
def test_RG01_disposable_registry_preserves_digest_and_rollback(tmp_path: Path):
    if not shutil.which("docker"):
        pytest.fail("docker is required for the release registry contract")
    suffix = uuid.uuid4().hex[:10]
    registry_name = f"odin-registry-contract-{suffix}"
    registry_volume = f"odin-registry-contract-volume-{suffix}"
    builder_name = f"odin-registry-contract-builder-{suffix}"
    port = _port()
    repository = f"localhost:{port}/odin"
    context = tmp_path / "context"; context.mkdir()
    (context / "Dockerfile").write_text(
        "FROM python:3.12-alpine@sha256:b64631e04e4920160c50fbe8d8df828f7f35f06f425cb44aa09bca53e708a35a\n"
        "ARG VERSION\nENV VERSION=$VERSION\n"
        "RUN printf '%s\\n' 'import json,os' 'from http.server import BaseHTTPRequestHandler,HTTPServer' "
        "'class H(BaseHTTPRequestHandler):' ' def do_GET(self):' "
        "'  body=json.dumps({\"status\":\"ok\",\"version\":os.environ[\"VERSION\"]}).encode()' "
        "'  self.send_response(200); self.send_header(\"Content-Type\",\"application/json\"); self.end_headers(); self.wfile.write(body)' "
        "' def log_message(self,*args): pass' 'HTTPServer((\"0.0.0.0\",8000),H).serve_forever()' > /server.py\n"
        "HEALTHCHECK --interval=1s --timeout=2s --retries=30 CMD wget -q -O- http://127.0.0.1:8000/health || exit 1\n"
        "CMD [\"python\",\"/server.py\"]\n",
        encoding="utf-8",
    )
    buildkit_config = tmp_path / "buildkitd.toml"
    buildkit_config.write_text(
        f'[registry."localhost:{port}"]\n  http = true\n  insecure = true\n',
        encoding="utf-8",
    )
    containers: list[str] = []
    try:
        _run("docker", "volume", "create", registry_volume)
        _run(
            "docker", "buildx", "create", "--name", builder_name, "--driver", "docker-container",
            "--driver-opt", "network=host", "--buildkitd-config", str(buildkit_config), "--bootstrap",
        )
        _run("docker", "run", "-d", "--name", registry_name, "-p", f"127.0.0.1:{port}:5000", "-v", f"{registry_volume}:/var/lib/registry", "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373")
        containers.append(registry_name)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _run("curl", "-fsS", f"http://127.0.0.1:{port}/v2/", check=False).returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("disposable registry did not become ready")

        def build(version: str, tag: str) -> str:
            metadata = tmp_path / f"{tag}.json"
            _run(
                "docker", "buildx", "build", "--builder", builder_name, "--push", "--platform", "linux/amd64,linux/arm64",
                "--provenance=false", "--build-arg", f"VERSION={version}", "--tag", f"{repository}:{tag}",
                "--metadata-file", str(metadata), str(context),
            )
            digest = json.loads(metadata.read_text())["containerimage.digest"]
            assert mutation.DIGEST_RE.fullmatch(digest)
            return digest

        prior_digest = build("1.9.12", "latest")
        target_digest = build("1.9.13", "staging")
        manifest = mutation.inspect_manifest(f"{repository}@{target_digest}")
        assert manifest and manifest["digest"] == target_digest
        platforms = {
            (item["platform"]["os"], item["platform"]["architecture"]): item["digest"]
            for item in manifest["manifests"]
        }
        assert set(platforms) == {("linux", "amd64"), ("linux", "arm64")}

        canonical_repository = mutation.IMAGE_REPOSITORY

        def local_registry_runner(command, **kwargs):
            translated = [
                item.replace(canonical_repository, repository) if isinstance(item, str) else item
                for item in command
            ]
            return subprocess.run(translated, **kwargs)

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        sha = "a" * 40
        staging_tag = "staging"
        tools = {"python": "integration", "docker": "integration", "buildx": "integration"}

        def publication_failure_state(writes: list[dict]) -> dict:
            return {
                "schema_version": 1, "kind": "publication", "status": "failed", "phase": "tag_written",
                "repository": mutation.REPOSITORY, "repository_id": 77, "run_id": 999, "run_attempt": 1,
                "workflow_sha": "9" * 40, "actor_login": mutation.OWNER_LOGIN,
                "candidate_sha": sha, "candidate_ref": f"release-candidate/{sha}",
                "validation_run_id": 100, "promotion_run_id": 200, "evidence_sha256": "e" * 64,
                "version": "1.9.13", "image_repository": canonical_repository,
                "staging_tag": staging_tag, "sha_tag": f"sha-{sha}", "version_tag": "v1.9.13",
                "target_digest": target_digest, "platform_manifests": [], "tag_writes": writes,
                "tool_versions": tools, "started_at": now, "updated_at": now,
                "error": {"code": "PUBLICATION_INCOMPLETE", "detail": "injected after registry write"},
                "cleanup": {"registry_logout": True, "auth_removed": True},
            }

        def production_failure_state(writes: list[dict], phase: str = "tag_written") -> dict:
            return {
                "schema_version": 1, "kind": "production", "status": "failed", "phase": phase,
                "repository": mutation.REPOSITORY, "repository_id": 77, "run_id": 999, "run_attempt": 1,
                "workflow_sha": "9" * 40, "actor_login": mutation.OWNER_LOGIN,
                "candidate_sha": sha, "candidate_ref": f"release-candidate/{sha}",
                "validation_run_id": 100, "promotion_run_id": 200, "publication_run_id": 300,
                "publication_receipt_sha256": "f" * 64, "evidence_sha256": "e" * 64,
                "version": "1.9.13", "image_repository": canonical_repository,
                "target_digest": target_digest, "prior_latest_digest": prior_digest,
                "rollback_tag": "rollback-999-1", "rollback_command": "restore exact digest",
                "observed_latest_digest": None, "tag_writes": writes,
                "public_health": {"url": "https://odin.subsystem.app/health", "tls_valid": True, "status": None, "version": None, "ready_observation": "not_checked"},
                "tool_versions": tools, "started_at": now, "updated_at": now,
                "error": {"code": "PRODUCTION_INCOMPLETE", "detail": "injected after registry write"},
                "cleanup": {"registry_logout": True, "auth_removed": True},
            }

        def assert_truthful_failure(state: dict, boundary: str) -> dict:
            captured = mutation.capture_failure(
                state, runner=local_registry_runner,
                error_code="INJECTED_AFTER_COPY", error_detail=f"injected failure after {boundary}",
            )
            assert captured["status"] == "failed"
            assert captured["error"] == {
                "code": "INJECTED_AFTER_COPY", "detail": f"injected failure after {boundary}",
            }
            assert captured["tag_writes"][-1]["status"] == "written"
            receipt_dir = tmp_path / f"failure-{boundary}"
            mutation.render_receipt(captured, receipt_dir)
            mutation.validate_receipt(json.loads((receipt_dir / "receipt.json").read_text()))
            return captured

        # The staging image was written by the real multi-platform build above. Reconcile an
        # injected failure immediately after that write through the exact inspection path.
        staging_failure = publication_failure_state([
            {"tag": staging_tag, "before": None, "after": target_digest, "status": "attempted"},
        ])
        assert_truthful_failure(staging_failure, "staging")

        for architecture in ("amd64", "arm64"):
            name = f"odin-registry-platform-{architecture}-{suffix}"
            containers.append(name)
            _run(
                "docker", "run", "-d", "--platform", f"linux/{architecture}", "--name", name,
                f"{repository}@{target_digest}",
            )
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                status = _run(
                    "docker", "inspect", "--format", "{{.State.Health.Status}}", name, check=False,
                ).stdout.strip()
                if status == "healthy":
                    break
                if status == "unhealthy":
                    pytest.fail(f"{architecture} exact manifest became unhealthy")
                time.sleep(1)
            else:
                pytest.fail(f"{architecture} exact manifest did not become healthy")
            observed = _run(
                "docker", "exec", name, "python", "-c",
                "import json,urllib.request;print(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'))['version'])",
            ).stdout.strip()
            assert observed == "1.9.13"

        sha_tag = "sha-" + "a" * 40
        assert mutation.attach_tag(repository=repository, target_tag=sha_tag, source_digest=target_digest)["status"] == "written"
        assert_truthful_failure(publication_failure_state([
            {"tag": staging_tag, "before": None, "after": target_digest, "status": "written"},
            {"tag": sha_tag, "before": None, "after": target_digest, "status": "attempted"},
        ]), "sha")
        assert mutation.attach_tag(repository=repository, target_tag=sha_tag, source_digest=target_digest)["status"] == "noop"
        assert mutation.attach_tag(repository=repository, target_tag="v1.9.13", source_digest=target_digest)["status"] == "written"
        assert_truthful_failure(publication_failure_state([
            {"tag": staging_tag, "before": None, "after": target_digest, "status": "written"},
            {"tag": sha_tag, "before": None, "after": target_digest, "status": "written"},
            {"tag": "v1.9.13", "before": None, "after": target_digest, "status": "attempted"},
        ]), "version")
        with pytest.raises(mutation.MutationError, match="TAG_CONFLICT"):
            mutation.attach_tag(repository=repository, target_tag="v1.9.13", source_digest=prior_digest)

        rollback_write = mutation.attach_tag(
            repository=repository, target_tag="rollback-999-1", source_digest=prior_digest,
        )
        assert_truthful_failure(production_failure_state([
            {"tag": "rollback-999-1", "before": None, "after": prior_digest, "status": "attempted"},
        ]), "rollback")
        latest_write = mutation.replace_latest(
            repository=repository, target_digest=target_digest, expected_prior=prior_digest,
        )
        writes = [rollback_write, latest_write]
        assert [item["tag"] for item in writes] == ["rollback-999-1", "latest"]
        latest_failure = assert_truthful_failure(production_failure_state([
            rollback_write,
            {"tag": "latest", "before": prior_digest, "after": target_digest, "status": "attempted"},
        ], phase="registry_verified"), "latest")
        assert latest_failure["observed_latest_digest"] == target_digest
        assert mutation.inspect_manifest(f"{repository}:latest")["digest"] == target_digest
        assert mutation.inspect_manifest(f"{repository}:rollback-999-1")["digest"] == prior_digest
        restored = mutation.replace_latest(
            repository=repository, target_digest=prior_digest, expected_prior=target_digest,
        )
        assert restored["status"] == "written"
        assert mutation.inspect_manifest(f"{repository}:latest")["digest"] == prior_digest
    finally:
        for name in reversed(containers):
            _run("docker", "rm", "-f", "-v", name, check=False)
        if "target_digest" in locals():
            _run("docker", "image", "rm", f"{repository}@{target_digest}", check=False)
        for tag in ("staging", "latest", "sha-" + "a" * 40, "v1.9.13", "rollback-999-1"):
            _run("docker", "image", "rm", f"{repository}:{tag}", check=False)
        _run("docker", "buildx", "rm", "-f", builder_name, check=False)
        _run("docker", "volume", "rm", "-f", registry_volume, check=False)
