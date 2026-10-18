"""Test doubles must not outlive the test that installed them.

A fixture that rebinds ``core.db_utils.get_db`` while a backend module is
imported for the first time hands that module the double permanently: the
module's ``from core.db_utils import get_db`` keeps it after monkeypatch
restores ``core.db_utils``. Later tests then talk to a deleted database.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _run_in_one_process(*files):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(_ROOT / "backend"), str(_ROOT)]))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly", *files],
        cwd=_ROOT, env=env, capture_output=True, text=True, timeout=600,
    )


def test_camera_discovery_then_job_state_machine_in_one_process():
    result = _run_in_one_process(
        "tests/test_contracts/test_camera_discovery_persistence_behavior.py",
        "tests/test_contracts/test_cert_D_job_state_machine.py",
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_leaked_test_double_fails_the_test_that_leaked_it(tmp_path):
    """The contracts conftest reports a backend module left holding a test-defined callable."""
    leak = tmp_path / "test_leak_probe.py"
    leak.write_text(
        "def test_leaks():\n"
        "    import core.db_utils as du\n"
        "    def double(*a, **k):\n"
        "        raise AssertionError('double')\n"
        "    du._leak_probe_double = double\n"
    )
    probe = _ROOT / "tests" / "test_contracts" / "_test_leak_probe_tmp.py"
    probe.write_text(leak.read_text())
    try:
        result = _run_in_one_process(str(probe.relative_to(_ROOT)))
    finally:
        probe.unlink()
        import core.db_utils as du
        du.__dict__.pop("_leak_probe_double", None)
    output = result.stdout + result.stderr
    assert result.returncode != 0, output[-3000:]
    assert "core.db_utils._leak_probe_double" in output, output[-3000:]


def test_module_first_imported_under_a_monkeypatch_gets_the_real_binding_back():
    """The conftest rebinds an importer that captured a monkeypatched double to the
    value the patched target held before, so the next test sees the real function."""
    probe = _ROOT / "tests" / "test_contracts" / "_test_import_window_probe_tmp.py"
    probe.write_text(
        "import sys\n"
        "def test_a_imports_inside_the_window(monkeypatch):\n"
        "    import core.db_utils as du\n"
        "    assert 'modules.notifications.job_events' not in sys.modules\n"
        "    def double(*a, **k):\n"
        "        raise AssertionError('double')\n"
        "    monkeypatch.setattr(du, 'get_db', double)\n"
        "    from modules.notifications import job_events as w\n"
        "    assert w.get_db is double\n"
        "def test_b_sees_the_real_function():\n"
        "    import core.db_utils as du; import modules.notifications.job_events as w\n"
        "    assert w.get_db is du.get_db\n"
    )
    try:
        result = _run_in_one_process(str(probe.relative_to(_ROOT)))
    finally:
        probe.unlink()
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]


def test_target_patched_twice_rebinds_the_importer_to_the_pre_test_value():
    probe = _ROOT / "tests" / "test_contracts" / "_test_double_window_probe_tmp.py"
    probe.write_text(
        "def test_a_patches_twice_then_imports(monkeypatch):\n"
        "    import core.db_utils as du\n"
        "    monkeypatch.setattr(du, 'get_db', lambda: 'first')\n"
        "    monkeypatch.setattr(du, 'get_db', lambda: 'second')\n"
        "    from modules.notifications import job_events\n"
        "    assert job_events.get_db() == 'second'\n"
        "def test_b_sees_the_real_function():\n"
        "    import core.db_utils as du; import modules.notifications.job_events as w\n"
        "    assert w.get_db is du.get_db\n"
    )
    try:
        result = _run_in_one_process(str(probe.relative_to(_ROOT)))
    finally:
        probe.unlink()
    assert result.returncode == 0, (result.stdout + result.stderr)[-3000:]
