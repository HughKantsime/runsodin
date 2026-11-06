"""Mutation checks for the release's caller-owned audit boundary recognizer."""
from pathlib import Path
import pytest
import re
from tests.test_contracts.release_audit_contract import PROTOCOLS, caller_owned_audit_is_atomic

ROOT = Path(__file__).resolve().parents[2] / 'backend'


def sources_for(key):
    return {p: (ROOT / p).read_text() for p in {key[0], *PROTOCOLS[key]}}


@pytest.mark.parametrize('key', list(PROTOCOLS))
def test_live_helper_and_callers_keep_audit_in_the_mutation_transaction(key):
    assert caller_owned_audit_is_atomic(key, sources_for(key))


@pytest.mark.parametrize('key', list(PROTOCOLS))
def test_helper_cannot_commit_early(key):
    sources = sources_for(key)
    sources[key[0]] = re.sub(r'^( +)log_audit\(', r'\1db.commit()\n\1log_audit(', sources[key[0]], flags=re.M)
    assert not caller_owned_audit_is_atomic(key, sources)


@pytest.mark.parametrize('key,path', [(k,p) for k,paths in PROTOCOLS.items() for p in paths])
def test_each_caller_must_commit_the_same_session(key, path):
    sources = sources_for(key)
    sources[path] = sources[path].replace('db.commit()', 'other_session.commit()')
    assert not caller_owned_audit_is_atomic(key, sources)
