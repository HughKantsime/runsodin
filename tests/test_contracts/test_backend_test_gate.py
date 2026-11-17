"""Backend-local tests must reach both release batch entry points, including new files."""
from pathlib import Path
import shlex

import pytest

from ops.release_control import contract_batches, structured_targets


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('future_file', [False, True])
@pytest.mark.parametrize('entrypoint', ['structured', 'shell'])
def test_every_backend_test_reaches_the_gate(monkeypatch, entrypoint, future_file):
    expected = {str(path.relative_to(ROOT)) for path in (ROOT / 'backend/tests').rglob('test_*.py')}
    assert expected
    if future_file:
        # Simulate a sixth file without changing either entry point's configuration.
        future = ROOT / 'backend/tests/test_future_backend_feature.py'
        expected.add(str(future.relative_to(ROOT)))
        original = Path.rglob

        def rglob(path, pattern):
            yield from original(path, pattern)
            if path == ROOT / 'backend/tests' and pattern == 'test_*.py':
                yield future

        monkeypatch.setattr(Path, 'rglob', rglob)

    if entrypoint == 'structured':
        calls = []
        interpreter = ROOT / '.venv-contracts/bin/python'
        is_file = Path.is_file
        monkeypatch.setattr(Path, 'is_file', lambda path: path == interpreter or is_file(path))
        monkeypatch.setattr(structured_targets, '_directory', lambda _: ROOT / 'unused-gate-output')
        monkeypatch.setattr(structured_targets, 'render', lambda *args: None)

        def execute(**kwargs):
            calls.append(kwargs)
            return {'status': 'pass'}

        monkeypatch.setattr(structured_targets, 'execute', execute)
        assert structured_targets.contracts() == 0
        command = calls[0]['command']
        assert command[:3] == [str(interpreter), '-m', 'ops.release_control.contract_batches']
        start = command.index('--tests') + 1
        end = next(i for i in range(start, len(command)) if command[i].startswith('--'))
        roots = command[start:end]
        assert 'tests/test_contracts' in roots
    else:
        script = (ROOT / 'ops/run_contract_batches.sh').read_text()
        command = shlex.split(next(line for line in script.splitlines() if line.startswith('find ')))
        roots = command[1:command.index('-name')]
        assert command[command.index('-name') + 1] == 'test_*.py'
        assert {'tests/test_contracts', 'tests/test_telemetry', 'tests/test_release_control'} <= set(roots)

    files = contract_batches.discover(roots)
    planned = [path for batch in contract_batches.plan(files, 10) for path in batch.files]
    assert expected <= set(planned)
    assert len(planned) == len(set(planned))
