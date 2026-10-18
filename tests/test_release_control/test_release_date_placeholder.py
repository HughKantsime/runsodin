"""The release date is a placeholder in CHANGELOG.md, TERMS_OF_SERVICE.md and
PRIVACY_POLICY.md, marked with one comment; ops/set-release-date.mjs (run by
ops/bump-version.sh) sets all three together and removes the marker. Before a bump the
repo carries the marker; after it (the release commit) none of the files does and all
three show the newest release's date."""
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARKER = "<!-- release-date placeholder: set by ops/bump-version.sh -->"
FILES = ("CHANGELOG.md", "TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md")
LEGAL = ("TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
HEAD_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4})-(\d{2})-(\d{2})$", re.M)


def _human(year, month, day):
    return f"{MONTHS[int(month) - 1]} {int(day)}, {year}"


def test_all_three_files_carry_the_same_release_date():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    head = HEAD_RE.search(changelog)
    assert head, "CHANGELOG.md has a dated release block"
    human = _human(head.group(2), head.group(3), head.group(4))
    pending = changelog[head.end():].startswith("\n\n" + MARKER)
    for name in LEGAL:
        text = (ROOT / name).read_text()
        expected = f"**Last Updated:** {human} {MARKER}" if pending else f"**Last Updated:** {human}\n"
        assert expected in text, name
        if not pending:
            assert MARKER not in text, name
    if not pending:
        assert MARKER not in changelog


def _pending_copy(tmp_path):
    """Copy the three files and put them in the pre-bump state: the newest release block and
    both Last Updated lines carry the placeholder marker."""
    for name in FILES:
        shutil.copy(ROOT / name, tmp_path / name)
    changelog = (tmp_path / "CHANGELOG.md").read_text()
    head = HEAD_RE.search(changelog)
    if not changelog[head.end():].startswith("\n\n" + MARKER):
        changelog = changelog[: head.end()] + "\n\n" + MARKER + changelog[head.end():]
        (tmp_path / "CHANGELOG.md").write_text(changelog)
    for name in LEGAL:
        text = (tmp_path / name).read_text()
        if MARKER not in text:
            text = re.sub(r"^(\*\*Last Updated:\*\* [A-Za-z]+ \d+, \d{4})$", r"\1 " + MARKER, text, count=1, flags=re.M)
            assert MARKER in text, name
            (tmp_path / name).write_text(text)
    return tmp_path, head.group(1)


def _run(root, version, date):
    return subprocess.run(["node", str(ROOT / "ops/set-release-date.mjs"), "--root", str(root), version, date],
                          capture_output=True, text=True)


def test_set_release_date_updates_all_three_and_removes_the_marker(tmp_path):
    root, version = _pending_copy(tmp_path)
    before = {name: (root / name).read_text() for name in FILES}
    result = _run(root, version, "2026-11-03")
    assert result.returncode == 0, result.stderr
    changelog = (root / "CHANGELOG.md").read_text()
    assert f"## [{version}] - 2026-11-03\n" in changelog and MARKER not in changelog
    for name in LEGAL:
        text = (root / name).read_text()
        assert "**Last Updated:** November 3, 2026\n" in text and MARKER not in text
    # every other line is unchanged
    for name in FILES:
        kept = [l for l in before[name].splitlines() if MARKER not in l and not l.startswith(f"## [{version}]")]
        after = [l for l in (root / name).read_text().splitlines() if "2026-11-03" not in l and "November 3, 2026" not in l]
        assert kept == after, name


def test_set_release_date_refuses_another_version_and_touches_nothing(tmp_path):
    root, _ = _pending_copy(tmp_path)
    before = {name: (root / name).read_text() for name in FILES}
    result = _run(root, "9.9.9", "2026-11-03")
    assert result.returncode != 0 and "9.9.9" in result.stderr
    for name in FILES:
        assert (root / name).read_text() == before[name]


def test_bump_version_runs_set_release_date_and_commits_the_legal_pages():
    script = (ROOT / "ops/bump-version.sh").read_text()
    assert 'node "$REPO_ROOT/ops/set-release-date.mjs" "$VERSION"' in script
    assert "CHANGELOG.md TERMS_OF_SERVICE.md PRIVACY_POLICY.md" in script
