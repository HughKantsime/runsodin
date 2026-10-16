"""The cert-D capture helper compares by default and writes only with CAPTURE=1."""
import json

import pytest

from tests.test_contracts import cert_D_capture as capture


def test_compare_mode_fails_on_producer_drift_and_never_writes(tmp_path, monkeypatch):
    monkeypatch.delenv('CAPTURE', raising=False)
    fixture = tmp_path / 'wire.json'
    fixture.write_text(json.dumps({'jobs': [{'id': 1, 'status': 'pending', 'created_at': '2026-01-01'}]}))
    before = fixture.read_bytes()
    capture.publish(fixture, {'jobs': [{'id': 9, 'status': 'done', 'created_at': '2026-10-16'}]})  # values may vary
    with pytest.raises(AssertionError, match='keys differ'):
        capture.publish(fixture, {'jobs': [{'id': 1, 'status': 'pending', 'created_at': 'x', 'new_field': 1}]})
    with pytest.raises(AssertionError, match='items committed'):
        capture.publish(fixture, {'jobs': []})
    with pytest.raises(AssertionError, match='number committed, str produced'):
        capture.publish(fixture, {'jobs': [{'id': '1', 'status': 'pending', 'created_at': 'x'}]})
    assert fixture.read_bytes() == before


def test_capture_mode_writes_the_committed_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv('CAPTURE', '1')
    fixture = tmp_path / 'wire.json'
    capture.publish(fixture, {'a': 1}, key='first')
    capture.publish(fixture, [1, 2], key='second')
    assert json.loads(fixture.read_text()) == {'first': {'a': 1}, 'second': [1, 2]}


def test_missing_fixture_or_key_asks_for_an_explicit_capture(tmp_path, monkeypatch):
    monkeypatch.delenv('CAPTURE', raising=False)
    with pytest.raises(AssertionError, match='CAPTURE=1'):
        capture.publish(tmp_path / 'absent.json', {'a': 1})
    (tmp_path / 'w.json').write_text('{}')
    with pytest.raises(AssertionError, match='CAPTURE=1'):
        capture.publish(tmp_path / 'w.json', {'a': 1}, key='k')
