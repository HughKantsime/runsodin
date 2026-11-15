"""The container entrypoint and the backend read ODIN_ITAR_MODE the same way.

Review 2026-11-13 finding 8: docker/entrypoint.sh dropped every leading and trailing byte
that was not a letter or digit before comparing, core/itar.py did not. `!on!` or a literal
`"1"` (quotes included, a common env-file mistake) removed the proxies and printed an ITAR
message in the entrypoint while the backend ran with ITAR mode OFF.

Now both accept exactly: on = 1 true yes on, off = unset, empty, 0 false no off, in any
letter case, with ASCII white space around them. Every other value stops the container
(entrypoint) and every ODIN process (import of `core` or `modules`) with one message.

The shell block is the real text of docker/entrypoint.sh, run by /bin/bash on this machine
(the image's bash is not run here).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import itar

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PROXY = "http://203.0.113.9:3128"

ON = ["1", "true", "TRUE", "True", "yes", "YES", "on", "On", " 1 ", "1\r", "\n1", "\t true \r\n", "\v\fON\f", "yes\n"]
OFF = ["", "0", "false", "FALSE", "no", "No", "off", "OFF", " ", "\t", " 0 ", "off\r\n", "\v\f"]
MALFORMED = ['"1"', "'1'", "!on!", "on!", "2", "-1", "01", "10", "y", "n", "t", "enabled", "disabled", "1 1", "o n",
             "1\t1", "true,", "ja", "\x1c1\x1c", "\x1d\x1e\x1ftrue", "\x85yes\x85", "\xa0on\xa0", " 1 ",
             "　1", "1\x7f", "ＯＮ", "ōn", "K"]


def _entrypoint(value):
    """Run the entrypoint's ITAR block. Returns (exit code, {variable: value}, text)."""
    text = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    block = text[text.index("# ITAR-PROXIES-BEGIN"):text.index("# ITAR-PROXIES-END")]
    script = "set -Eeuo pipefail\n" + block + '\necho "itar_on=${odin_itar_on}"\necho "http_proxy_is=${HTTP_PROXY:-removed}"\n'
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                            env={"PATH": os.environ.get("PATH", ""), "ODIN_ITAR_MODE": value, "HTTP_PROXY": PROXY})
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return result.returncode, values, result.stdout + result.stderr


@pytest.mark.parametrize("value", ON)
def test_an_on_spelling_is_on_in_both(value, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", value)
    assert itar.itar_flag_state() == "on" and itar.is_itar_mode() is True
    itar.refuse_malformed_itar_flag()
    code, values, _ = _entrypoint(value)
    assert code == 0 and values["itar_on"] == "1" and values["http_proxy_is"] == "removed"


@pytest.mark.parametrize("value", OFF)
def test_an_off_spelling_is_off_in_both(value, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", value)
    assert itar.itar_flag_state() == "off" and itar.is_itar_mode() is False
    itar.refuse_malformed_itar_flag()
    code, values, output = _entrypoint(value)
    assert code == 0 and values["itar_on"] == "0" and values["http_proxy_is"] == PROXY
    assert "ITAR" not in output


def test_unset_is_off_in_both(monkeypatch):
    monkeypatch.delenv("ODIN_ITAR_MODE", raising=False)
    assert itar.itar_flag_state() == "off" and itar.is_itar_mode() is False
    text = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    block = text[text.index("# ITAR-PROXIES-BEGIN"):text.index("# ITAR-PROXIES-END")]
    result = subprocess.run(["/bin/bash", "-c", "set -Eeuo pipefail\n" + block + '\necho "itar_on=${odin_itar_on}"'],
                            capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")})
    assert result.returncode == 0 and result.stdout == "itar_on=0\n"


@pytest.mark.parametrize("value", MALFORMED)
def test_a_malformed_value_is_refused_by_both_with_the_same_message(value, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", value)
    assert itar.itar_flag_state() == "invalid"
    assert itar.is_itar_mode() is True, "until the process stops, every guard stays closed"
    with pytest.raises(SystemExit) as refused:
        itar.refuse_malformed_itar_flag()
    assert str(refused.value) == itar.ITAR_FLAG_REFUSAL
    code, values, output = _entrypoint(value)
    assert code == 1 and "itar_on" not in values, "the container must not start"
    assert itar.ITAR_FLAG_REFUSAL in output
    assert "ignoring the proxy settings" not in output, "no ITAR message for a value the backend would not accept"


@pytest.mark.parametrize("package", ["core", "modules"])
def test_every_odin_process_stops_at_import_on_a_malformed_value(package):
    result = subprocess.run([sys.executable, "-c", f"import {package}; print('started')"], cwd=BACKEND,
                            capture_output=True, text=True,
                            env={**os.environ, "ODIN_ITAR_MODE": '"1"', "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 1 and "started" not in result.stdout
    assert result.stderr.strip() == itar.ITAR_FLAG_REFUSAL, "one plain line, no traceback"


@pytest.mark.parametrize("value", ["1", "0", ""])
def test_a_valid_value_does_not_stop_an_import(value):
    result = subprocess.run([sys.executable, "-c", "import core; print('started')"], cwd=BACKEND,
                            capture_output=True, text=True,
                            env={**os.environ, "ODIN_ITAR_MODE": value, "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0 and result.stdout.strip() == "started"


def test_the_two_lists_of_spellings_are_the_same_text():
    """The shell `case` arms and the Python sets, compared as written."""
    text = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    block = text[text.index("# ITAR-PROXIES-BEGIN"):text.index("# ITAR-PROXIES-END")]
    assert block.count("    1|true|yes|on)") == 2 and block.count('    ""|0|false|no|off) ;;') == 1
    assert itar._ENABLED_VALUES == {"1", "true", "yes", "on"}
    assert itar._DISABLED_VALUES == {"", "0", "false", "no", "off"}
    assert itar._ASCII_SPACE == " \t\n\r\v\f" and "odin_itar_space=$' \\t\\n\\r\\v\\f'" in block
    assert itar.ITAR_FLAG_REFUSAL in block
