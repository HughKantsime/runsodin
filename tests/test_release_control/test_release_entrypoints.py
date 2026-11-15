import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def _initial_repo(root):
    for directory in ("ops/release_control", "frontend/src", "frontend/public", "backend/core", "install", "design"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "ops/bump-version.sh", root / "ops/bump-version.sh")
    shutil.copy2(ROOT / "ops/set-release-date.mjs", root / "ops/set-release-date.mjs")
    shutil.copy2(ROOT / "ops/legal-revision.mjs", root / "ops/legal-revision.mjs")
    marker = "<!-- release-date placeholder: set by ops/bump-version.sh -->"
    (root / "CHANGELOG.md").write_text(f"# Changelog\n\n## [1.0.1] - 2000-01-01\n\n{marker}\n\n### Fixed\n\n- Synthetic.\n")
    records = {}
    for legal in ("TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md"):
        (root / legal).write_text("# Synthetic\n\n**Last Updated:** January 1, 2000\n")
        records[legal] = {"last_updated": "2000-01-01", "body_sha256": hashlib.sha256(b"# Synthetic\n\n").hexdigest()}
    (root / "ops/release_control/legal_revisions.json").write_text(json.dumps(records, indent=2) + "\n")
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
    return root


@pytest.fixture(scope="module")
def bumped_repo(tmp_path_factory):
    root = _initial_repo(tmp_path_factory.mktemp("bump-repo"))
    result = subprocess.run(["bash", "ops/bump-version.sh", "1.0.1"], cwd=root, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return root


def test_RE01_bump_creates_one_local_commit(bumped_repo):
    assert subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=bumped_repo, text=True).strip() == "2"
    assert (bumped_repo / "VERSION").read_text().strip() == "1.0.1"


def test_RE01b_bump_dates_only_the_changelog_in_its_commit(bumped_repo):
    changelog = (bumped_repo / "CHANGELOG.md").read_text()
    assert "## [1.0.1] - 2000-01-01" not in changelog and "placeholder" not in changelog
    for legal in ("TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md", "ops/release_control/legal_revisions.json"):
        original = subprocess.check_output(["git", "show", f"HEAD~1:{legal}"], cwd=bumped_repo)
        assert (bumped_repo / legal).read_bytes() == original
    committed = subprocess.check_output(["git", "show", "--name-only", "--format=", "HEAD"], cwd=bumped_repo, text=True).split()
    assert {"CHANGELOG.md", "VERSION"} <= set(committed)
    assert not {"TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md", "ops/release_control/legal_revisions.json"} & set(committed)
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=bumped_repo, text=True) == ""


@pytest.mark.parametrize("legal", ["TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md"])
@pytest.mark.parametrize("change", ["body", "date", "marker"])
def test_bump_stops_before_writing_when_legal_revision_is_invalid(tmp_path, legal, change):
    root = _initial_repo(tmp_path)
    path = root / legal
    text = path.read_text()
    if change == "body":
        text += "\nUnrecorded text change.\n"
    elif change == "date":
        text = text.replace("January 1, 2000", "January 2, 2000")
    else:
        text += "\n<!-- release-date placeholder: set by ops/bump-version.sh -->\n"
    path.write_text(text)
    subprocess.run(["git", "add", legal], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "unrecorded revision"], cwd=root, check=True, capture_output=True)
    before = {name: (root / name).read_bytes() for name in subprocess.check_output(
        ["git", "ls-files"], cwd=root, text=True).splitlines()}
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root)
    result = subprocess.run(["bash", "ops/bump-version.sh", "1.0.1"], cwd=root, text=True, capture_output=True)
    assert result.returncode != 0 and legal in result.stderr
    assert all((root / name).read_bytes() == data for name, data in before.items())
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root) == head
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=root) == b""


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
    # ROADMAP.md lists product features only (internal release work was removed from it for 2.0).
    assert "release-control foundation" in (ROOT / "CHANGELOG.md").read_text().lower()
    assert (ROOT / "docs/RELEASE_CONTROL_FOUNDATION.html").is_file()
