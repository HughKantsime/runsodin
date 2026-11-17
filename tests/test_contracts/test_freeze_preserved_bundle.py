"""Freezing a release accepts its entire migration tail and refuses ledger drift."""
import pytest

from ops.database_parity import freeze_preserved_bundle as freeze


@pytest.fixture
def released(monkeypatch):
    base = ['sql:base', 'python:057-slot-feed-policy']
    tail = ['python:058-filament-catalogue', 'python:059-six-materials',
            'python:060-print-files-stored-path-index']
    monkeypatch.setattr(freeze, 'resolve_commit', lambda commit: commit)
    monkeypatch.setattr(freeze, 'verify_release', lambda *a: base[1:] + tail)
    monkeypatch.setattr(freeze, 'base_manifest', lambda era: {'ledger_ids': base, 'sources': {}})
    monkeypatch.setattr(freeze, 'selected_paths', lambda commit: [])
    monkeypatch.setattr(freeze, 'independent_ledger', lambda sources: base + tail)
    return base, tail


def test_freeze_accepts_all_three_released_migrations(released):
    base, tail = released
    manifest, overlay = freeze.build('owned', '2.3.0', 60, 57, migration=tail[-1])
    assert manifest['ledger_ids'] == sorted(base + tail)
    assert overlay == {}


@pytest.mark.parametrize('change', ['missing_base', 'missing_middle', 'extra'])
def test_freeze_refuses_independent_ledger_drift(released, monkeypatch, change):
    base, tail = released
    ledger = base + tail
    if change == 'missing_base': ledger.remove(base[0])
    elif change == 'missing_middle': ledger.remove(tail[1])
    else: ledger.append('python:061-unreleased')
    monkeypatch.setattr(freeze, 'independent_ledger', lambda sources: ledger)
    with pytest.raises(freeze.FreezeError, match='Released ledger'):
        freeze.build('owned', '2.3.0', 60, 57, migration=tail[-1])


def test_freeze_refuses_nonprefix_base(released, monkeypatch):
    base, tail = released
    monkeypatch.setattr(freeze, 'verify_release', lambda *a: [tail[0], base[1], *tail[1:]])
    with pytest.raises(freeze.FreezeError, match='registry does not extend'):
        freeze.build('owned', '2.3.0', 60, 57, migration=tail[-1])
