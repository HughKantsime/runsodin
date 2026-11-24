import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from modules.models_library import plate_cache
from tests.test_contracts.test_print_objects_parser import synthetic_members, package
from tests.test_contracts.test_api_token_delegation_behavior import review_db  # noqa: F401


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setenv('PLATE_CACHE_ROOT', str(tmp_path / 'cache'))
    content = package(synthetic_members())
    path = tmp_path / 'stored.3mf'; path.write_bytes(content)
    return SimpleNamespace(stored_path=str(path), file_hash=hashlib.sha256(content).hexdigest(), blob_state='present')


def test_objects_backfill_and_facts_unchanged(stored):
    plate_cache.facts_for_row(stored)
    entry = plate_cache.entry_dir(stored.file_hash)
    facts = (entry / 'facts.json').read_bytes()
    result = plate_cache.objects_for_row(stored)
    assert result == plate_cache.read_objects(stored.file_hash)
    assert (entry / 'facts.json').read_bytes() == facts
    assert result['version'] == plate_cache.OBJECTS_VERSION
    assert result['file_hash'] == stored.file_hash
    assert (entry / 'objects.json').stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('corruption', ['json', 'version', 'bool_id', 'duplicate', 'bounds', 'name', 'extra'])
def test_corrupt_objects_rebuilt(stored, corruption):
    expected = plate_cache.objects_for_row(stored)
    damaged = json.loads(json.dumps(expected))
    obj = damaged['plates'][0]['objects'][0]
    if corruption == 'version': damaged['version'] = True
    if corruption == 'bool_id': obj['identify_id'] = True
    if corruption == 'duplicate': damaged['plates'][0]['objects'].append(obj.copy())
    if corruption == 'bounds': obj['bounds']['x'] = 2
    if corruption == 'name': obj['name'] = '\nprivate'
    if corruption == 'extra': damaged['extra'] = 1
    (plate_cache.entry_dir(stored.file_hash) / 'objects.json').write_text('broken' if corruption == 'json' else json.dumps(damaged))
    assert plate_cache.read_objects(stored.file_hash) is None
    assert plate_cache.objects_for_row(stored) == expected


def test_unavailable_marker_overrides_cache_and_hash_mismatch_refuses(stored):
    assert plate_cache.objects_for_row(stored)
    plate_cache.mark_unavailable(stored.file_hash, 'bad_zip')
    assert plate_cache.objects_for_row(stored) is None
    assert plate_cache.read_objects(stored.file_hash) is None
    stored.file_hash = 'b' * 64
    assert plate_cache.objects_for_row(stored) is None


def test_missing_payload_has_no_object_list(stored):
    os.unlink(stored.stored_path)
    assert plate_cache.objects_for_row(stored) is None


def test_sweep_removes_objects_with_entry(stored, review_db):
    plate_cache.objects_for_row(stored)
    entry = plate_cache.entry_dir(stored.file_hash)
    os.utime(entry, (0, 0))
    assert plate_cache.sweep(review_db) == 1
    assert not entry.exists()
