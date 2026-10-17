"""The 2.0 release date is a placeholder in CHANGELOG.md, TERMS_OF_SERVICE.md and
PRIVACY_POLICY.md, marked with one comment; ops/set-release-date.mjs (run by
ops/bump-version.sh) sets all three together and removes the marker."""
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "<!-- release-date placeholder: set by ops/bump-version.sh -->"
FILES = ("CHANGELOG.md", "TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md")


def test_all_three_files_carry_the_same_placeholder_date():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    head = re.search(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4})-(\d{2})-(\d{2})\n\n" + re.escape(MARKER) + "$", changelog, re.M)
    assert head, "the newest release block carries the placeholder marker"
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
              "October", "November", "December"]
    human = f"{months[int(head.group(3)) - 1]} {int(head.group(4))}, {head.group(2)}"
    for name in ("TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md"):
        assert f"**Last Updated:** {human} {MARKER}" in (ROOT / name).read_text(), name


def _copy(tmp_path):
    for name in FILES:
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def _run(root, version, date):
    return subprocess.run(["node", str(ROOT / "ops/set-release-date.mjs"), "--root", str(root), version, date],
                          capture_output=True, text=True)


def test_set_release_date_updates_all_three_and_removes_the_marker(tmp_path):
    root = _copy(tmp_path)
    version = re.search(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md").read_text(), re.M).group(1)
    result = _run(root, version, "2026-11-03")
    assert result.returncode == 0, result.stderr
    changelog = (root / "CHANGELOG.md").read_text()
    assert f"## [{version}] - 2026-11-03\n" in changelog and MARKER not in changelog
    for name in ("TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md"):
        text = (root / name).read_text()
        assert "**Last Updated:** November 3, 2026\n" in text and MARKER not in text
    # every other line is unchanged
    for name in FILES:
        before = [l for l in (ROOT / name).read_text().splitlines() if MARKER not in l and not l.startswith(f"## [{version}]")]
        after = [l for l in (root / name).read_text().splitlines() if "2026-11-03" not in l and "November 3, 2026" not in l]
        assert before == after, name


def test_set_release_date_refuses_another_version_and_touches_nothing(tmp_path):
    root = _copy(tmp_path)
    result = _run(root, "9.9.9", "2026-11-03")
    assert result.returncode != 0 and "9.9.9" in result.stderr
    for name in FILES:
        assert (root / name).read_text() == (ROOT / name).read_text()


def test_bump_version_runs_set_release_date_and_commits_the_legal_pages():
    script = (ROOT / "ops/bump-version.sh").read_text()
    assert 'node "$REPO_ROOT/ops/set-release-date.mjs" "$VERSION"' in script
    assert "CHANGELOG.md TERMS_OF_SERVICE.md PRIVACY_POLICY.md" in script
