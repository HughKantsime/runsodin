"""F16-16.4-03 "Installable web app (v1.2.0)": the release annotation is checked against git history.

Installability itself is exercised in the compiled bundle (cert-G-ui.playwright.mjs,
Page.getInstallabilityErrors == []); this pins the version the claim names.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PWA_COMMIT = "06d3df2"


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)


pytestmark = pytest.mark.skipif(shutil.which("git") is None or _git("rev-parse").returncode != 0,
                                reason="git history unavailable")


def test_pwa_feature_commit_first_ships_in_v1_2_0():
    subject = _git("show", "-s", "--format=%s", PWA_COMMIT).stdout
    assert "PWA" in subject
    assert _git("merge-base", "--is-ancestor", PWA_COMMIT, "v1.2.0").returncode == 0
    assert _git("merge-base", "--is-ancestor", PWA_COMMIT, "v1.1.0").returncode != 0


def test_v1_2_0_tree_has_manifest_link_and_service_worker():
    assert 'rel="manifest"' in _git("show", "v1.2.0:frontend/index.html").stdout
    assert _git("show", "v1.2.0:frontend/public/sw.js").returncode == 0
    assert '"display": "standalone"' in _git("show", "v1.2.0:frontend/public/manifest.json").stdout
