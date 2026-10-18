"""Cert group H — F22-22.2-01/02/03 GHCR distribution and GitHub Actions CI/CD.

Read-only calls only: anonymous OCI registry API on ghcr.io and `gh api` GETs.
Gated by CERT_H_NETWORK=1. Never dispatches workflows or pushes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
REPO = "hughkantsime/odin"
GH_REPO = "HughKantsime/runsodin"
VERSION = (ROOT / "VERSION").read_text().strip()
WORKFLOWS = ROOT / ".github/workflows"
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json",
])

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # pragma: no cover
    _CTX = ssl.create_default_context()

pytestmark = pytest.mark.skipif(os.environ.get("CERT_H_NETWORK") != "1", reason="CERT_H_NETWORK=1 required")


def _token() -> str:
    with urllib.request.urlopen(f"https://ghcr.io/token?scope=repository:{REPO}:pull", timeout=30, context=_CTX) as r:
        return json.loads(r.read())["token"]


def _get(path: str, accept: str | None = None):
    req = urllib.request.Request(f"https://ghcr.io/v2/{REPO}/{path}", headers={
        "Authorization": f"Bearer {_token()}", **({"Accept": accept} if accept else {})})
    with urllib.request.urlopen(req, timeout=60, context=_CTX) as r:
        return r.status, dict(r.headers), r.read()


def _tags() -> list[str]:
    tags: list[str] = []
    path = "tags/list?n=1000"
    while path:
        status, headers, body = _get(path)
        tags += json.loads(body).get("tags") or []
        link = headers.get("Link") or headers.get("link") or ""
        m = re.search(r"<([^>]+)>", link)
        path = m.group(1).split(f"/v2/{REPO}/", 1)[-1] if m else ""
    return tags


def test_images_are_published_to_ghcr_hughkantsime_odin():
    tags = _tags()
    assert len(tags) > 10
    status, headers, body = _get(f"manifests/v{VERSION}", ACCEPT)
    assert status == 200
    index = json.loads(body)
    platforms = {(m["platform"]["os"], m["platform"]["architecture"]) for m in index.get("manifests", []) if "platform" in m}
    assert {("linux", "amd64"), ("linux", "arm64")} <= platforms, platforms
    (ROOT / "artifacts/application-certification/cert-H").mkdir(parents=True, exist_ok=True)
    (ROOT / "artifacts/application-certification/cert-H/ghcr-tags.json").write_text(json.dumps(sorted(tags), indent=0))


def test_images_are_version_tagged_including_the_documented_example():
    tags = set(_tags())
    semver = sorted(t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t))
    assert "v1.3.0" in tags and f"v{VERSION}" in tags
    assert len(semver) > 50
    digests = {}
    for tag in ("v1.3.0", f"v{VERSION}"):
        status, headers, _ = _get(f"manifests/{tag}", ACCEPT)
        assert status == 200
        digests[tag] = headers.get("Docker-Content-Digest") or headers.get("docker-content-digest")
    assert digests["v1.3.0"] != digests[f"v{VERSION}"]


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))
    return data


@pytest.mark.skipif(shutil.which("gh") is None, reason="gh CLI required")
@pytest.mark.parametrize("name", ["trusted-validation.yml", "publish-image.yml", "promote.yml", "promote-production.yml"])
def test_release_workflows_are_manual_dispatch_and_have_succeeded_on_github(name):
    assert list(_workflow(name)["on"]) == ["workflow_dispatch"]
    out = subprocess.run(["gh", "api", f"repos/{GH_REPO}/actions/workflows/{name}/runs?per_page=20"],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    runs = json.loads(out)["workflow_runs"]
    assert runs, name
    assert {r["event"] for r in runs} == {"workflow_dispatch"}, {r["event"] for r in runs}
    assert any(r["conclusion"] == "success" for r in runs), name


@pytest.mark.skipif(shutil.which("gh") is None, reason="gh CLI required")
def test_successful_publish_run_produced_a_ghcr_image_for_its_commit():
    out = subprocess.run(["gh", "api", f"repos/{GH_REPO}/actions/workflows/publish-image.yml/runs?per_page=20"],
                         capture_output=True, text=True, timeout=60, check=True).stdout
    shas = [r["head_sha"] for r in json.loads(out)["workflow_runs"] if r["conclusion"] == "success"]
    tags = _tags()
    assert any(any(sha in t or t.endswith(sha[:7]) or sha.startswith(t.removeprefix("sha-")) for t in tags if t.startswith(("sha-", "candidate-")))
               for sha in shas), "no sha-/candidate- tag for any successful publish run"
    publish = (WORKFLOWS / "publish-image.yml").read_text(encoding="utf-8")
    assert "ghcr.io/hughkantsime/odin" in publish
