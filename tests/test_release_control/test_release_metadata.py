"""Catch a missing release ledger before expensive validation/publication."""
import json
import hashlib
from pathlib import Path
from ops.release_control.record_post027_ledger import registered_ids


def test_changelog_preserves_released_history_after_train_merge():
    root = Path(__file__).resolve().parents[2]
    changelog = (root / 'CHANGELOG.md').read_bytes()
    unreleased, released = changelog.split(b'## [2.4.1]', 1)
    # Pin the complete released history on release/2.4.1, including in shallow checkouts.
    assert hashlib.sha256(b'## [2.4.1]' + released).hexdigest() == (
        '09f891d18578175f410cff8c9509a4af820da2f31a7b08f3c35c0e2a271a7ddb'
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
