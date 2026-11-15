"""Recorded release images for the native PostgreSQL restore gate (no Docker)."""
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
TABLE = Path('ops/database_parity/restore_baselines.json')
LEDGERS = Path('backend/core/schema/released_post027_ledgers.json')


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('Release version must be X.Y.Z')
    return tuple(map(int, value.split('.')))


def load_baselines(root=ROOT):
    data = json.loads((root / TABLE).read_text())
    version_key(data['first_required_release'])
    rows = data['releases']
    if not isinstance(rows, list) or not rows:
        raise ValueError('The restore baseline table is empty')
    seen = set()
    for row in rows:
        version_key(row['version'])
        if row['version'] in seen:
            raise ValueError('Duplicate restore baseline version')
        seen.add(row['version'])
        if row.get('image_digest') is None and row.get('todo'):
            continue  # An explicit pending receipt parses, but cannot be used or pass coverage.
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(row.get('image_digest', ''))):
            raise ValueError('Invalid restore baseline image digest')
        if type(row.get('schema_era')) is not int or row['schema_era'] < 27:
            raise ValueError('Invalid restore baseline schema era')
    return data


def require_record(row):
    if row.get('image_digest') is None:
        raise ValueError(f"TODO: record version={row['version']}, image_digest=target_digest from its successful production receipt, and schema_era=the last released ledger era in ops/database_parity/restore_baselines.json before validating a later candidate")
    return row


def check_release_coverage(root=ROOT, candidate=None):
    data = load_baselines(root)
    rows = {row['version']: row for row in data['releases']}
    ledgers = json.loads((root / LEDGERS).read_text())['releases']
    for version, ids in ledgers.items():
        if candidate is not None and version_key(version) >= version_key(candidate):
            continue
        if version_key(version) < version_key(data['first_required_release']):
            continue
        if version not in rows:
            raise ValueError(f'Restore baseline entry missing for released version {version}')
        row = require_record(rows[version])
        era = int(ids[-1].split(':')[1].split('-')[0]) if ids else 27
        if row['schema_era'] != era:
            raise ValueError(f'Restore baseline schema era differs from the release ledger for {version}')


def select_baseline(candidate, root=ROOT):
    data = load_baselines(root)
    check_release_coverage(root, candidate)
    # The ledger is recorded before publication. Exclude the candidate itself, even
    # when already recorded, and never silently fall back past a missing release.
    versions = set(json.loads((root / LEDGERS).read_text())['releases'])
    versions.update(row['version'] for row in data['releases'])
    prior = [version for version in versions if version_key(version) < version_key(candidate)]
    if not prior:
        raise ValueError('No released version precedes this candidate')
    newest = max(prior, key=version_key)
    row = next((row for row in data['releases'] if row['version'] == newest), None)
    if row is None:
        raise ValueError(f'Restore baseline entry missing for previous release {newest}')
    return require_record(row)
