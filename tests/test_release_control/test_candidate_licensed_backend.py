"""Candidate licensing owns only its temporary process and restores Community."""
import io
from types import SimpleNamespace

import pytest

from ops.release_gate.license_fixture import candidate_pro_backend


@pytest.mark.parametrize('fail', [False, True])
def test_candidate_backend_restores_community_after_success_or_test_failure(monkeypatch, fail):
    import requests
    import subprocess
    state = {'tier': 'community'}
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[-2:] == ['pid', 'backend']:
            return SimpleNamespace(stdout='42\n')
        if '/proc/42/environ' in argv[-1]:
            return SimpleNamespace(stdout='{"ODIN_BOOT_ID":"synthetic-boot"}')
        if argv[-2:] == ['stop', 'backend']:
            state['tier'] = None
        elif argv[-2:] == ['start', 'backend']:
            state['tier'] = 'community'
        elif 'os.kill' in argv[-1]:
            state['tier'] = None

    class Process:
        returncode = 0

        def __init__(self, argv, **kwargs):
            calls.append(argv)
            self.stdin = io.StringIO()
            state['tier'] = 'pro'

        def wait(self, timeout):
            assert state['tier'] is None

    class Response:
        status_code = 200

        def json(self):
            return {'tier': state['tier']}

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(subprocess, 'Popen', Process)
    monkeypatch.setattr(requests, 'get', lambda *args, **kwargs: Response())
    # Two consecutive invocations mirror desktop and mobile parameterizations.
    for _ in range(2):
        try:
            with candidate_pro_backend('odin-candidate-fixture-run', 'http://owned.invalid'):
                assert state['tier'] == 'pro'
                if fail:
                    raise ValueError('synthetic browser failure')
        except ValueError as exc:
            assert fail and str(exc) == 'synthetic browser failure'
        assert state['tier'] == 'community'
        assert calls[-1][-2:] == ['start', 'backend']
    assert sum(call[-2:] == ['stop', 'backend'] for call in calls) == 2
    assert sum('os.kill' in call[-1] for call in calls) == 2
    assert 'ODIN_RELEASE_GATE_RUN_ID' in calls[0][-1]
    launched = next(call for call in calls if '--user' in call)
    assert launched[launched.index('--user') + 1] == 'odin'
    assert 'ODIN_BOOT_ID=synthetic-boot' in launched


def test_candidate_backend_refuses_unowned_container():
    with pytest.raises(ValueError, match='owned candidate'):
        with candidate_pro_backend('production', 'http://unused.invalid'):
            pytest.fail('unowned container accepted')
