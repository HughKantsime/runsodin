"""Run the 025/026 qualified-source contract tests against the preserved029 bundle.

These tests assert that the sources which build the historical restore stage are the
qualified ones. Those sources now run only from core/schema/preserved029/bundle, so the
unchanged assertions execute with the bundle as the backend root (48f215e precedent).
Every selected test must pass; a skip or deselection fails this test.
"""
import os
from pathlib import Path
import re
import subprocess
import sys

from core.schema.preserved029.integrity import check_package

ROOT = Path(__file__).resolve().parents[2]
TARGETS = (
    ("tests/test_contracts/test_pre026_restore_contract.py", 16),
    ("tests/test_contracts/test_historical026_profile_extension.py", 4),
    ("tests/test_contracts/test_spool_purchase_registration.py::test_canonical_purchase_exact_union_and_shared_truncate_target", 1),
    ("tests/test_contracts/test_postgres_canonical_executables.py::test_exact_source_registry_has_expected_union_counts", 1),
)


def test_qualified_source_contract_tests_pass_against_preserved029_bundle(tmp_path):
    bundle, _ = check_package()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTHON", "ODIN_", "DATABASE_"))}
    env.update(PYTHONPATH=os.pathsep.join([str(bundle), str(ROOT)]), PYTHONDONTWRITEBYTECODE="1",
               JWT_SECRET_KEY="test-key-for-contract-tests-only", COOKIE_SECURE="false")
    # --noconftest: the contract conftest registers live-only providers and inserts backend/.
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--noconftest", "-rs",
               "--rootdir", str(ROOT), "--basetemp", str(tmp_path / "t"), *(str(ROOT / target) for target, _ in TARGETS)]
    result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=600)
    tail = result.stdout[-4000:] + result.stderr[-2000:]
    assert result.returncode == 0, tail
    expected = sum(count for _, count in TARGETS)
    summary = result.stdout.strip().splitlines()[-1]
    assert re.fullmatch(rf"{expected} passed in [0-9.]+s(?: \([0-9:]+\))?", summary), tail
    check_package()  # the run neither wrote bytecode into nor otherwise changed the bundle
