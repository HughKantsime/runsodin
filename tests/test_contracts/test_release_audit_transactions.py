"""Mutation checks for the release's caller-owned audit boundary recognizer."""
from pathlib import Path
import pytest
import re
from tests.test_contracts.release_audit_contract import (
    BIOMETRIC, PROTOCOLS, RECOVERY, STARTUP, caller_owned_audit_is_atomic,
)

ROOT = Path(__file__).resolve().parents[2] / 'backend'


def sources_for(key):
    return {p: (ROOT / p).read_text() for p in {key[0], *PROTOCOLS[key]}}


@pytest.mark.parametrize('key', list(PROTOCOLS))
def test_live_helper_and_callers_keep_audit_in_the_mutation_transaction(key):
    assert caller_owned_audit_is_atomic(key, sources_for(key))


@pytest.mark.parametrize('key', list(PROTOCOLS))
def test_helper_cannot_commit_early(key):
    sources = sources_for(key)
    # Biometric audit keeps the returned entry to set its timestamp.
    mutated, count = re.subn(r'^( +)(?:\w+ = )?log_audit\(', r'\1db.commit()\n\g<0>',
                             sources[key[0]], flags=re.M)
    assert count > 0
    sources[key[0]] = mutated
    assert not caller_owned_audit_is_atomic(key, sources)


@pytest.mark.parametrize('key,path', [(k,p) for k,paths in PROTOCOLS.items() for p in paths])
def test_each_caller_must_commit_the_same_session(key, path):
    sources = sources_for(key)
    before, after = 'db.commit()', 'other_session.commit()'
    if path in (RECOVERY, STARTUP):
        before, after = 'as db, db.begin():', 'as db, other_session.begin():'
    assert before in sources[path]
    sources[path] = sources[path].replace(before, after)
    assert not caller_owned_audit_is_atomic(key, sources)


@pytest.mark.parametrize('replacement', [
    '        return _audit(db, row, action, now, age)',
    '        _audit(db, row, action, now, age)\n        if age > 0:\n            return',
], ids=['return-audit', 'conditional-return'])
def test_biometric_return_before_commit(replacement):
    key = (BIOMETRIC, '_audit')
    sources = sources_for(key)
    audit = '        _audit(db, row, action, now, age)'
    assert sources[BIOMETRIC].count(audit) == 1
    sources[BIOMETRIC] = sources[BIOMETRIC].replace(audit, replacement)
    assert not caller_owned_audit_is_atomic(key, sources)


@pytest.mark.parametrize('after_commit', [
    '        return None, outcome',
    '    return (record if outcome == "pending" else None), outcome',
    '    if plan is not None:',
], ids=['send-failed', 'send-confirmed', 'observed-outcome'])
def test_moonraker_finish_requires_each_caller_commit(after_commit):
    path = 'modules/printers/moonraker_controls.py'
    key = (path, '_finish')
    sources = sources_for(key)
    assert caller_owned_audit_is_atomic(key, sources)
    boundary = f'db.commit()\n{after_commit}'
    assert sources[path].count(boundary) == 1
    sources[path] = sources[path].replace(boundary, f'db.flush()\n{after_commit}')
    assert not caller_owned_audit_is_atomic(key, sources)
