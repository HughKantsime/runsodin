"""Release dates belong to CHANGELOG; legal dates follow recorded text revisions."""
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MARKER = "<!-- release-date placeholder: set by ops/bump-version.sh -->"
LEGAL = ("TERMS_OF_SERVICE.md", "PRIVACY_POLICY.md")
RECORD = "ops/release_control/legal_revisions.json"
FILES = ("CHANGELOG.md", *LEGAL, RECORD, "ops/legal-revision.mjs")
HEAD_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", re.M)
DATE_RE = re.compile(rb"^\*\*Last Updated:\*\*[^\r\n]*(?:\r?\n|$)", re.M)


def _copy(tmp_path):
    for name in FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, target)
    return tmp_path


def _snapshot(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _pending_copy(tmp_path):
    root = _copy(tmp_path)
    changelog = (root / "CHANGELOG.md").read_text()
    head = HEAD_RE.search(changelog)
    assert head, "CHANGELOG.md has a dated release block"
    if not changelog[head.end():].startswith("\n\n" + MARKER):
        changelog = changelog[:head.end()] + "\n\n" + MARKER + changelog[head.end():]
        (root / "CHANGELOG.md").write_text(changelog)
    return root, head.group(1)


def _run(root, version, date):
    return subprocess.run(["node", str(ROOT / "ops/set-release-date.mjs"), "--root", str(root), version, date],
                          capture_output=True, text=True)


def _legal(root, *args):
    return subprocess.run(["node", str(root / "ops/legal-revision.mjs"), *args],
                          capture_output=True, text=True)


def test_set_release_date_updates_only_changelog_and_removes_marker(tmp_path):
    root, version = _pending_copy(tmp_path)
    before = _snapshot(root)
    result = _run(root, version, "2026-11-03")
    assert result.returncode == 0, result.stderr
    after = _snapshot(root)
    assert {name for name in before if before[name] != after[name]} == {"CHANGELOG.md"}
    expected = HEAD_RE.sub(f"## [{version}] - 2026-11-03", before["CHANGELOG.md"].decode(), count=1)
    expected = expected.replace(MARKER + "\n", "", 1)
    assert after["CHANGELOG.md"].decode() == expected
    assert MARKER not in expected


def test_set_release_date_does_not_require_legal_pages(tmp_path):
    root, version = _pending_copy(tmp_path)
    for name in LEGAL:
        (root / name).unlink()
    result = _run(root, version, "2026-11-03")
    assert result.returncode == 0, result.stderr
    assert all(not (root / name).exists() for name in LEGAL)


def test_set_release_date_refuses_another_version_and_touches_nothing(tmp_path):
    root, _ = _pending_copy(tmp_path)
    before = _snapshot(root)
    result = _run(root, "9.9.9", "2026-11-03")
    assert result.returncode != 0 and "9.9.9" in result.stderr
    assert _snapshot(root) == before


def test_set_release_date_requires_changelog_marker(tmp_path):
    root, version = _pending_copy(tmp_path)
    path = root / "CHANGELOG.md"
    path.write_text(path.read_text().replace(MARKER + "\n", ""))
    before = _snapshot(root)
    result = _run(root, version, "2026-11-03")
    assert result.returncode == 1 and "CHANGELOG.md" in result.stderr
    assert _snapshot(root) == before


def test_repository_legal_revisions_match():
    result = _legal(ROOT, "--check")
    assert result.returncode == 0, result.stderr
    records = json.loads((ROOT / RECORD).read_text())
    for name in LEGAL:
        body = DATE_RE.sub(b"", (ROOT / name).read_bytes())
        assert records[name]["body_sha256"] == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("name", LEGAL)
@pytest.mark.parametrize("change", ["body", "date", "marker", "body_marker", "missing_line", "duplicate_line"])
def test_check_rejects_unrecorded_changes_without_writing(tmp_path, name, change):
    root = _copy(tmp_path)
    path = root / name
    data = path.read_bytes()
    if change == "body":
        data += b"\nA deliberate text change.\n"
    elif change == "date":
        data = DATE_RE.sub(b"**Last Updated:** November 16, 2026\n", data)
    elif change == "marker":
        data = DATE_RE.sub(lambda match: match[0].rstrip(b"\n") + b" " + MARKER.encode() + b"\n", data)
    elif change == "body_marker":
        data += MARKER.encode() + b"\n"
        # Even a matching body hash cannot make a release marker valid.
        records = json.loads((root / RECORD).read_text())
        records[name]["body_sha256"] = hashlib.sha256(DATE_RE.sub(b"", data)).hexdigest()
        (root / RECORD).write_text(json.dumps(records))
    elif change == "missing_line":
        data = DATE_RE.sub(b"", data)
    else:
        data += b"\n**Last Updated:** November 16, 2026\n"
    path.write_bytes(data)
    before = _snapshot(root)
    result = _legal(root, "--check")
    assert result.returncode == 1 and name in result.stderr
    assert _snapshot(root) == before


@pytest.mark.parametrize("name", LEGAL)
@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_record_updates_only_selected_date_and_record_and_is_idempotent(tmp_path, name, ending):
    root = _copy(tmp_path)
    path = root / name
    # Include non-ASCII bytes before and after the date to verify byte-based hashing.
    data = "# Révision\n\n".encode() + b"**Last Updated:** November 1, 2026" + ending
    if ending:
        data += "\nDeliberate change — reviewed.\n".encode()
    path.write_bytes(data)
    before = _snapshot(root)
    old_records = json.loads(before[RECORD])
    result = _legal(root, "--record", name, "2026-11-16")
    assert result.returncode == 0, result.stderr
    after = _snapshot(root)
    assert {key for key in before if before[key] != after[key]} == {name, RECORD}
    assert after[name] == DATE_RE.sub(b"**Last Updated:** November 16, 2026" + ending, data)
    records = json.loads(after[RECORD])
    assert records[name] == {"last_updated": "2026-11-16", "body_sha256": hashlib.sha256(DATE_RE.sub(b"", data)).hexdigest()}
    other = next(page for page in LEGAL if page != name)
    assert records[other] == old_records[other]
    assert _legal(root, "--check").returncode == 0
    assert _legal(root, "--record", name, "2026-11-16").returncode == 0
    assert _snapshot(root) == after


@pytest.mark.parametrize("args", [[], ["--check", "extra"], ["--record", "CHANGELOG.md", "2026-11-16"],
                                  ["--record", LEGAL[0], "2026-02-30"], ["--record", LEGAL[0], "2026-13-01"],
                                  ["--record", LEGAL[0], "November 16, 2026"]])
def test_invalid_arguments_write_nothing(tmp_path, args):
    root = _copy(tmp_path)
    before = _snapshot(root)
    assert _legal(root, *args).returncode == 2
    assert _snapshot(root) == before


def test_bump_version_checks_legal_revisions_before_any_write():
    script = (ROOT / "ops/bump-version.sh").read_text()
    check = 'node "$REPO_ROOT/ops/legal-revision.mjs" --check'
    assert script.index(check) < script.index('echo "$VERSION" >')
    assert 'node "$REPO_ROOT/ops/set-release-date.mjs" "$VERSION"' in script
    assert all(name not in script for name in LEGAL)
