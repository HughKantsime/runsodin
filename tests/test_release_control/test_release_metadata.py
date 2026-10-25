"""Catch a missing release ledger before expensive validation/publication."""
import json
from pathlib import Path
from ops.release_control.record_post027_ledger import registered_ids


def test_current_release_records_exact_registered_migrations():
    root=Path(__file__).resolve().parents[2]
    version=(root/'VERSION').read_text().strip()
    releases=json.loads((root/'backend/core/schema/released_post027_ledgers.json').read_text())['releases']
    assert version in releases, 'Record the current release migration ledger before validation'
    assert releases[version]==registered_ids(root), 'Current release ledger differs from registered migrations'
