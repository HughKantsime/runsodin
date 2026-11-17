"""Catch a missing release ledger before expensive validation/publication."""
import json
import hashlib
from pathlib import Path
from ops.release_control.record_post027_ledger import registered_ids


def test_changelog_preserves_released_history_after_train_merge():
    root = Path(__file__).resolve().parents[2]
    changelog = (root / 'CHANGELOG.md').read_bytes()
    unreleased, released = changelog.split(b'## [2.3.1]', 1)
    # Byte-for-byte released history as on main after 2.3.1 (e65d4477: the approved 2.3.0 wording
    # correction is part of it); works in shallow CI checkouts too. Update at each release.
    assert hashlib.sha256(b'## [2.3.1]' + released).hexdigest() == (
        '49e443c077ef8cc57852caa52dbb374c33d1dcdf8f97a4e5729fe8d0ce68e210'
    )
    unreleased = unreleased.split(b'## [Unreleased]\n', 1)[1].split(b'\n## [', 1)[0]
    headings = [line for line in unreleased.splitlines() if line.startswith(b'### ')]
    order = [b'### Added', b'### Changed', b'### Fixed', b'### Security']
    assert headings == sorted(set(headings), key=order.index)
    bullets = [line for line in unreleased.splitlines() if line.startswith(b'- ')]
    assert len(bullets) == len(set(bullets))


def test_current_release_records_exact_registered_migrations():
    root=Path(__file__).resolve().parents[2]
    version=(root/'VERSION').read_text().strip()
    releases=json.loads((root/'backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert version in releases, 'Record the current release migration ledger before validation'
    assert releases[version]==registered_ids(root), 'Current release ledger differs from registered migrations'
