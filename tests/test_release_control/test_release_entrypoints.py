import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.fixture(scope="module")
def bumped_repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("bump-repo")
    for directory in ("ops", "frontend/src", "frontend/public", "backend/core", "install", "design"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "ops/bump-version.sh", root / "ops/bump-version.sh")
    (root / "VERSION").write_text("1.0.0\n")
    (root / "frontend/package.json").write_text('{"name":"odin","version":"1.0.0"}\n')
    (root / "frontend/package-lock.json").write_text('{"name":"odin","version":"1.0.0","packages":{"":{"version":"1.0.0"}}}\n')
    (root / "backend/core/app.py").write_text('__version__ = "1.0.0"\n')
    (root / "docker-compose.yml").write_text('    image: ghcr.io/hughkantsime/odin:v1.0.0\n')
    (root / "install/install.sh").write_text('ODIN_VERSION="1.0.0"\n')
    (root / "install/install.ps1").write_text('$ODIN_VERSION = "1.0.0"\n')
    (root / "frontend/public/sw.js").write_text("const CACHE_NAME = 'odin-v1.0.0';\n")
    (root / "frontend/src/design-tokens.css").write_text(":root {}\n")
    (root / "design/generate.mjs").write_text("console.log('generated');\n")
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
    result = subprocess.run(["bash", "ops/bump-version.sh", "1.0.1"], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return root


def test_RE01_bump_creates_one_local_commit(bumped_repo):
    assert subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=bumped_repo, text=True).strip() == "2"
    assert (bumped_repo / "VERSION").read_text().strip() == "1.0.1"


def test_RE02_bump_creates_no_tag(bumped_repo):
    assert subprocess.check_output(["git", "tag"], cwd=bumped_repo, text=True).strip() == ""


def test_RE03_bump_performs_no_network_write():
    source = (ROOT / "ops/bump-version.sh").read_text()
    assert "git push" not in source


def test_RE04_legacy_push_flag_is_rejected():
    result = subprocess.run(["bash", str(ROOT / "ops/bump-version.sh"), "--push"], text=True, capture_output=True)
    assert result.returncode != 0 and "disabled" in result.stdout + result.stderr


def test_RE05_unknown_flag_is_rejected():
    result = subprocess.run(["bash", str(ROOT / "ops/bump-version.sh"), "--wat"], text=True, capture_output=True)
    assert result.returncode != 0 and "Unknown" in result.stdout + result.stderr


def test_RE06_make_release_fails_closed():
    result = subprocess.run(["make", "release"], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode != 0 and "reviewed promotion workflow" in result.stdout + result.stderr


def test_RE07_active_docs_do_not_instruct_legacy_release():
    docs = [ROOT / "ops/README.md", ROOT / "ops/RUNBOOK.md", ROOT / "ops/RELEASE_CHECKLIST.md"]
    source = "\n".join(path.read_text() for path in docs)
    assert "bump-version.sh 1.3.46 --push" not in source
    assert "git push origin main" not in source
    assert "make release VERSION=" not in source


def test_RE08_historical_release_plans_are_marked():
    path = ROOT / "docs/plans/release-history-notice.md"
    assert path.read_text().startswith("---\nhistorical: true\n---")
    assert "historical records only" in path.read_text()


def test_RE09_controls_have_no_publish_or_deploy_command():
    source = (ROOT / "ops/bump-version.sh").read_text() + (ROOT / ".github/workflows/trusted-validation.yml").read_text()
    for forbidden in ("git push", "git tag ", "docker push", "gh release create", "repository_dispatch"):
        assert forbidden not in source


def test_RE10_release_foundation_is_documented_in_all_formats():
    assert "release-control foundation" in (ROOT / "CHANGELOG.md").read_text().lower()
    assert "release-control foundation" in (ROOT / "ROADMAP.md").read_text().lower()
    assert (ROOT / "docs/RELEASE_CONTROL_FOUNDATION.html").is_file()
