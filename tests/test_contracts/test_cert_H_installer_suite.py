"""Cert group H — F22-22.4-05 size and result of tests/test_installer.sh.

Runs the unit half for real and counts the distinct integration checks in the script
(the integration half hard-codes container `odin`, port 8000 and a /tmp bind mount, so it
is not executed on a shared certification host; install/update behaviour is exercised in
isolation by test_cert_H_installer.py instead).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests/test_installer.sh"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _integration_checks() -> set[str]:
    src = SUITE.read_text()
    integration = src[src.index("# INTEGRATION TESTS"):]
    names = set(re.findall(r'\bpass "([^"$]+)', integration))
    # "API ready after update (slow start)" is an alternative pass of the same check
    return {n.replace(" (slow start)", "").strip() for n in names}


def test_installer_suite_unit_half_passes_and_counts():
    result = subprocess.run(["bash", str(SUITE), "--unit"], capture_output=True, text=True, timeout=300)
    out = ANSI.sub("", result.stdout)
    (ROOT / "artifacts/application-certification/cert-H/test_installer_unit.log").write_text(out)
    assert result.returncode == 0, out[-2000:]
    unit = int(re.search(r"Results: (\d+) tests", out).group(1))
    assert re.search(rf"✓ {unit} passed", out)
    integration = len(_integration_checks())
    assert (unit, integration) == (43, 17), (unit, integration)
    assert unit + integration != 59  # advertised "59 unit + integration tests" is not the current count
